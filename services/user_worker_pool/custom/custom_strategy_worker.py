"""
CustomStrategyWorker — executes user-defined AI-built strategies within
a UserWorker.

Runs alongside built-in strategies using the same event loop and discipline
rules.
"""

from __future__ import annotations

from datetime import datetime, date, timedelta
from typing import Any

import structlog

from ..capital_tier import CapitalTier, StrategyCategory, is_strategy_allowed, CATEGORY_MIN_TIER
from ..strategies.base import Signal, Leg, Position
from .indicator_engine import IndicatorEngine
from .condition_evaluator import ConditionEvaluator
from .models import CustomStrategyDefinition, SpreadConfig

log = structlog.get_logger(__name__)


class CustomStrategyWorker:
    """Evaluates and executes a user's custom AI-built strategies.

    Runs inside the per-user ``UserWorker`` alongside the built-in strategy
    roster.
    """

    def __init__(
        self,
        custom_strategies: list[CustomStrategyDefinition],
        indicator_engine: IndicatorEngine,
        condition_evaluator: ConditionEvaluator,
    ) -> None:
        self.strategies = custom_strategies
        self.indicator_engine = indicator_engine
        self.evaluator = condition_evaluator

    # ------------------------------------------------------------------
    # Main evaluation loop
    # ------------------------------------------------------------------

    async def evaluate_all(
        self,
        chain: Any,                   # OptionsChainSnapshot
        regime: Any,                  # MarketRegime
        open_positions: list[Position],
        config: Any,                  # UserStrategyConfig
    ) -> list[Signal]:
        """Iterate all active custom strategies, compute indicators,
        evaluate conditions, and emit signals.

        Capital tier enforcement: strategies whose category exceeds the
        user's tier are skipped.

        Margin check: for SELLING strategies, the signal is only generated
        if sufficient margin is available.
        """
        signals: list[Signal] = []
        user_tier = self._resolve_user_tier(config)

        for strategy in self.strategies:
            if strategy.status != "ACTIVE":
                continue

            # Capital tier gate
            strat_category = self._to_strategy_category(strategy.category)
            if strat_category and not is_strategy_allowed(strat_category, user_tier):
                log.debug(
                    "strategy_tier_gated",
                    strategy=strategy.name,
                    category=strategy.category,
                    user_tier=user_tier.value,
                )
                continue

            for symbol in strategy.target_symbols:
                try:
                    signal = await self._evaluate_symbol(
                        strategy, symbol, chain, regime, open_positions, config,
                    )
                    if signal is not None:
                        signals.append(signal)
                except Exception:
                    log.exception(
                        "custom_strategy_eval_error",
                        strategy=strategy.name,
                        symbol=symbol,
                    )

        # Check exit conditions for existing positions from custom strategies
        exit_signals = await self._evaluate_exits(chain, open_positions, config)
        signals.extend(exit_signals)

        return signals

    # ------------------------------------------------------------------
    # Per-symbol evaluation
    # ------------------------------------------------------------------

    async def _evaluate_symbol(
        self,
        strategy: CustomStrategyDefinition,
        symbol: str,
        chain: Any,
        regime: Any,
        open_positions: list[Position],
        config: Any,
    ) -> Signal | None:
        """Evaluate a single strategy on a single symbol."""

        # Check max positions per symbol
        existing = sum(
            1 for p in open_positions
            if p.strategy_name == strategy.name
            and p.underlying == symbol
            and p.status == "OPEN"
        )
        if existing >= strategy.max_positions_per_symbol:
            return None

        # Compute all required indicators
        indicator_results = self.indicator_engine.compute_batch(
            [symbol], strategy.indicators,
        )
        symbol_results = indicator_results.get(symbol, {})

        # Get underlying price
        underlying_price = self._get_underlying_price(chain)

        # Evaluate entry conditions (OR of AND groups)
        if not self.evaluator.evaluate_entry(
            strategy.entry_conditions,
            symbol_results,
            underlying_price,
        ):
            return None

        log.info(
            "custom_strategy_entry_triggered",
            strategy=strategy.name,
            symbol=symbol,
            price=underlying_price,
        )

        # Margin check for selling strategies
        if strategy.category == "SELLING":
            margin_available = self._get_available_margin(config)
            margin_required = self._estimate_margin(chain, strategy)
            if margin_required > margin_available:
                log.warning(
                    "insufficient_margin_for_custom_sell",
                    strategy=strategy.name,
                    required=margin_required,
                    available=margin_available,
                )
                return None

        return self._build_signal(strategy, symbol, chain, regime)

    # ------------------------------------------------------------------
    # Exit evaluation
    # ------------------------------------------------------------------

    async def _evaluate_exits(
        self,
        chain: Any,
        open_positions: list[Position],
        config: Any,
    ) -> list[Signal]:
        """Check exit conditions for open positions owned by custom strategies."""
        exit_signals: list[Signal] = []

        custom_strat_names = {s.name for s in self.strategies}

        for position in open_positions:
            if position.status != "OPEN":
                continue
            if position.strategy_name not in custom_strat_names:
                continue

            strategy = next(
                (s for s in self.strategies if s.name == position.strategy_name),
                None,
            )
            if strategy is None or not strategy.exit_conditions:
                continue

            symbol = position.underlying
            indicator_results = self.indicator_engine.compute_batch(
                [symbol], strategy.indicators,
            )
            symbol_results = indicator_results.get(symbol, {})
            underlying_price = self._get_underlying_price(chain)

            if self.evaluator.evaluate_exit(
                strategy.exit_conditions, symbol_results, underlying_price,
            ):
                log.info(
                    "custom_strategy_exit_triggered",
                    strategy=strategy.name,
                    symbol=symbol,
                    position_id=position.position_id,
                )
                # Build an exit signal (reverse the legs)
                exit_signal = self._build_exit_signal(strategy, position, chain)
                if exit_signal is not None:
                    exit_signals.append(exit_signal)

        return exit_signals

    # ------------------------------------------------------------------
    # Signal construction
    # ------------------------------------------------------------------

    def _build_signal(
        self,
        strategy: CustomStrategyDefinition,
        symbol: str,
        chain: Any,
        regime: Any,
    ) -> Signal | None:
        """Convert custom strategy definition + current market state into a
        Signal.

        Selects strike based on ``strategy.strike_selection`` or
        ``strategy.delta_target``.  Sets stop_loss, target, time_stop from
        the strategy definition.
        """
        underlying_price = self._get_underlying_price(chain)
        expiry = self._select_expiry(chain, strategy.dte_min, strategy.dte_max)
        if expiry is None:
            log.warning("no_valid_expiry", strategy=strategy.name, symbol=symbol)
            return None

        # Build legs based on option_action
        legs = self._build_legs(strategy, chain, expiry, underlying_price)
        if not legs:
            log.warning("no_legs_built", strategy=strategy.name, symbol=symbol)
            return None

        # Calculate entry price (sum of premiums, buy = debit, sell = credit)
        entry_price = sum(
            leg.premium * (1 if leg.action == "BUY" else -1)
            for leg in legs
        )

        # Stop loss and target (based on premium percentages)
        stop_loss_price = entry_price * (1.0 - strategy.stop_loss_pct / 100.0) if entry_price > 0 else 0.0
        target_price = entry_price * (1.0 + strategy.profit_target_pct / 100.0) if entry_price > 0 else 0.0
        max_loss = abs(entry_price * strategy.stop_loss_pct / 100.0)

        # Time stop
        time_stop = self._compute_time_stop(strategy)

        # Direction
        direction = self._infer_direction(strategy.option_action)

        # Segment
        segment = strategy.target_segments[0] if strategy.target_segments else "NSE_INDEX"

        return Signal(
            strategy_name=strategy.name,
            underlying=symbol,
            segment=segment,
            direction=direction,
            legs=legs,
            entry_price=abs(entry_price),
            stop_loss_pct=strategy.stop_loss_pct,
            stop_loss_price=stop_loss_price,
            target_pct=strategy.profit_target_pct,
            target_price=target_price,
            time_stop=time_stop,
            max_loss_inr=max_loss,
            expiry=expiry,
            confidence=0.7,
            metadata={
                "custom_strategy_id": strategy.id,
                "option_action": strategy.option_action,
                "strike_selection": strategy.strike_selection,
            },
        )

    def _build_exit_signal(
        self,
        strategy: CustomStrategyDefinition,
        position: Position,
        chain: Any,
    ) -> Signal | None:
        """Build an exit signal by reversing the position legs."""
        reversed_legs = []
        for leg in position.legs:
            reversed_legs.append(Leg(
                option_type=leg.option_type,
                strike=leg.strike,
                expiry=leg.expiry,
                action="SELL" if leg.action == "BUY" else "BUY",
                lots=leg.lots,
                premium=leg.premium,
            ))

        return Signal(
            strategy_name=strategy.name,
            underlying=position.underlying,
            segment=position.segment,
            direction="NEUTRAL",
            legs=reversed_legs,
            entry_price=position.current_value_inr,
            stop_loss_pct=0.0,
            stop_loss_price=0.0,
            target_pct=0.0,
            target_price=0.0,
            time_stop=datetime.utcnow(),
            max_loss_inr=0.0,
            expiry=position.legs[0].expiry if position.legs else date.today(),
            confidence=1.0,
            metadata={
                "custom_strategy_id": strategy.id,
                "exit_position_id": position.position_id,
                "exit_reason": "condition_triggered",
            },
        )

    # ------------------------------------------------------------------
    # Leg building helpers
    # ------------------------------------------------------------------

    def _build_legs(
        self,
        strategy: CustomStrategyDefinition,
        chain: Any,
        expiry: date,
        underlying_price: float,
    ) -> list[Leg]:
        """Build option legs based on strategy's option_action and spread_config."""

        # Multi-leg spread
        if strategy.spread_config and strategy.spread_config.legs:
            return self._build_spread_legs(strategy, chain, expiry, underlying_price)

        # Single-leg strategies
        action_map = {
            "BUY_CALL": ("BUY", "CE"),
            "BUY_PUT": ("BUY", "PE"),
            "SELL_CALL": ("SELL", "CE"),
            "SELL_PUT": ("SELL", "PE"),
        }

        if strategy.option_action in action_map:
            action, opt_type = action_map[strategy.option_action]
            strike = self._select_strike(
                chain, opt_type, underlying_price, strategy.strike_selection,
                strategy.delta_target,
            )
            if strike is None:
                return []
            premium = self._get_premium(chain, strike, opt_type)
            return [Leg(
                option_type=opt_type,
                strike=strike,
                expiry=expiry,
                action=action,
                lots=1,
                premium=premium,
            )]

        # STRADDLE: buy/sell both ATM CE and PE
        if strategy.option_action in ("STRADDLE", "STRANGLE"):
            atm = self._find_atm_strike(chain, underlying_price)
            if atm is None:
                return []
            offset = 0 if strategy.option_action == "STRADDLE" else 1
            ce_strike = atm + offset * self._get_strike_step(chain)
            pe_strike = atm - offset * self._get_strike_step(chain)
            action = "SELL" if strategy.category == "SELLING" else "BUY"
            return [
                Leg(option_type="CE", strike=ce_strike, expiry=expiry, action=action, lots=1,
                    premium=self._get_premium(chain, ce_strike, "CE")),
                Leg(option_type="PE", strike=pe_strike, expiry=expiry, action=action, lots=1,
                    premium=self._get_premium(chain, pe_strike, "PE")),
            ]

        # SPREAD: use spread_config
        if strategy.option_action == "SPREAD":
            return self._build_spread_legs(strategy, chain, expiry, underlying_price)

        return []

    def _build_spread_legs(
        self,
        strategy: CustomStrategyDefinition,
        chain: Any,
        expiry: date,
        underlying_price: float,
    ) -> list[Leg]:
        """Build legs from SpreadConfig leg templates."""
        if not strategy.spread_config:
            return []

        atm = self._find_atm_strike(chain, underlying_price)
        if atm is None:
            return []

        step = self._get_strike_step(chain)
        legs: list[Leg] = []

        for tmpl in strategy.spread_config.legs:
            strike = atm + tmpl.strike_offset * step
            premium = self._get_premium(chain, strike, tmpl.option_type)
            legs.append(Leg(
                option_type=tmpl.option_type,
                strike=strike,
                expiry=expiry,
                action=tmpl.action,
                lots=tmpl.quantity_ratio,
                premium=premium,
            ))

        return legs

    # ------------------------------------------------------------------
    # Strike selection
    # ------------------------------------------------------------------

    def _select_strike(
        self,
        chain: Any,
        option_type: str,
        underlying_price: float,
        selection: str,
        delta_target: float | None,
    ) -> float | None:
        """Select a strike based on the strategy's strike_selection rule."""
        strikes = self._get_sorted_strikes(chain)
        if not strikes:
            return None

        atm = self._find_atm_strike(chain, underlying_price)
        if atm is None:
            return None

        step = self._get_strike_step(chain)

        if selection == "ATM":
            return atm

        # Parse OTM/ITM selections like "1_OTM", "2_OTM", "1_ITM"
        if "_OTM" in selection or "_ITM" in selection:
            parts = selection.split("_")
            try:
                steps = int(parts[0])
            except (ValueError, IndexError):
                steps = 1

            is_otm = "OTM" in selection
            if option_type == "CE":
                offset = steps if is_otm else -steps
            else:
                offset = -steps if is_otm else steps

            target = atm + offset * step
            # Clamp to available strikes
            if target in strikes:
                return target
            closest = min(strikes, key=lambda s: abs(s - target))
            return closest

        if selection == "DELTA_BASED" and delta_target is not None:
            # Approximate: for CE delta ~0.5 is ATM, higher strike = lower delta
            # For PE delta ~-0.5 is ATM, lower strike = more negative delta
            # Use a simple heuristic: each step away from ATM reduces delta by ~0.05
            steps_from_atm = int((0.5 - abs(delta_target)) / 0.05)
            if option_type == "CE":
                target = atm + steps_from_atm * step
            else:
                target = atm - steps_from_atm * step
            if target in strikes:
                return target
            closest = min(strikes, key=lambda s: abs(s - target))
            return closest

        return atm

    # ------------------------------------------------------------------
    # Chain access helpers (handle both object and dict chains)
    # ------------------------------------------------------------------

    @staticmethod
    def _get_underlying_price(chain: Any) -> float:
        if isinstance(chain, dict):
            return chain.get("underlying_price", 0.0)
        return getattr(chain, "underlying_price", 0.0)

    @staticmethod
    def _get_sorted_strikes(chain: Any) -> list[float]:
        if isinstance(chain, dict):
            strikes = chain.get("strikes", [])
        else:
            strikes = getattr(chain, "strikes", [])
        result = []
        for s in strikes:
            if isinstance(s, dict):
                result.append(s.get("strike", 0.0))
            else:
                result.append(getattr(s, "strike", 0.0))
        return sorted(result)

    def _find_atm_strike(self, chain: Any, underlying_price: float) -> float | None:
        strikes = self._get_sorted_strikes(chain)
        if not strikes:
            return None
        return min(strikes, key=lambda s: abs(s - underlying_price))

    def _get_strike_step(self, chain: Any) -> float:
        strikes = self._get_sorted_strikes(chain)
        if len(strikes) < 2:
            return 50.0  # Default for NIFTY
        return strikes[1] - strikes[0]

    @staticmethod
    def _get_premium(chain: Any, strike: float, option_type: str) -> float:
        """Get the premium for a specific strike and option type from the chain."""
        if isinstance(chain, dict):
            strikes = chain.get("strikes", [])
        else:
            strikes = getattr(chain, "strikes", [])

        for s in strikes:
            s_val = s.get("strike", 0) if isinstance(s, dict) else getattr(s, "strike", 0)
            if abs(s_val - strike) < 0.01:
                if option_type == "CE":
                    key = "call_ltp" if isinstance(s, dict) else "call_ltp"
                    return s.get(key, 0.0) if isinstance(s, dict) else getattr(s, key, 0.0)
                else:
                    key = "put_ltp" if isinstance(s, dict) else "put_ltp"
                    return s.get(key, 0.0) if isinstance(s, dict) else getattr(s, key, 0.0)
        return 0.0

    @staticmethod
    def _select_expiry(chain: Any, dte_min: int, dte_max: int) -> date | None:
        """Select the best expiry within the DTE range."""
        if isinstance(chain, dict):
            expiry_val = chain.get("expiry")
        else:
            expiry_val = getattr(chain, "expiry", None)

        if expiry_val is None:
            return None

        if isinstance(expiry_val, date):
            today = date.today()
            dte = (expiry_val - today).days
            if dte_min <= dte <= dte_max:
                return expiry_val
            # If outside range, still return it (best available)
            return expiry_val

        return None

    # ------------------------------------------------------------------
    # Time stop
    # ------------------------------------------------------------------

    @staticmethod
    def _compute_time_stop(strategy: CustomStrategyDefinition) -> datetime:
        """Compute the time stop datetime from strategy rules."""
        now = datetime.utcnow()

        if strategy.time_stop_rule == "eod":
            return now.replace(hour=9, minute=45, second=0, microsecond=0)  # 15:15 IST

        if strategy.time_stop_rule.startswith("fixed_dte_"):
            try:
                dte = int(strategy.time_stop_rule.split("_")[-1])
                return now + timedelta(days=dte)
            except (ValueError, IndexError):
                pass

        if strategy.time_stop_rule == "custom_time" and strategy.time_stop_value:
            try:
                parts = strategy.time_stop_value.split(":")
                return now.replace(
                    hour=int(parts[0]),
                    minute=int(parts[1]),
                    second=0,
                    microsecond=0,
                )
            except (ValueError, IndexError):
                pass

        # Default: end of day
        return now.replace(hour=9, minute=45, second=0, microsecond=0)

    # ------------------------------------------------------------------
    # Direction inference
    # ------------------------------------------------------------------

    @staticmethod
    def _infer_direction(option_action: str) -> str:
        bullish = {"BUY_CALL", "SELL_PUT"}
        bearish = {"BUY_PUT", "SELL_CALL"}
        if option_action in bullish:
            return "BULLISH"
        if option_action in bearish:
            return "BEARISH"
        return "NEUTRAL"

    # ------------------------------------------------------------------
    # Tier / margin helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _resolve_user_tier(config: Any) -> CapitalTier:
        if isinstance(config, dict):
            tier_str = config.get("capital_tier", "STARTER")
        else:
            tier_str = getattr(config, "capital_tier", "STARTER")

        try:
            return CapitalTier(tier_str)
        except ValueError:
            return CapitalTier.STARTER

    @staticmethod
    def _to_strategy_category(category: str) -> StrategyCategory | None:
        try:
            return StrategyCategory(category)
        except ValueError:
            return None

    @staticmethod
    def _get_available_margin(config: Any) -> float:
        if isinstance(config, dict):
            return config.get("available_margin", 0.0)
        return getattr(config, "available_margin", 0.0)

    @staticmethod
    def _estimate_margin(chain: Any, strategy: CustomStrategyDefinition) -> float:
        """Rough margin estimate for sell positions.

        Uses a simple heuristic: underlying_price * lot_size * margin_pct.
        A real implementation would call the broker margin API.
        """
        price = chain.get("underlying_price", 0.0) if isinstance(chain, dict) else getattr(chain, "underlying_price", 0.0)
        lot_size = chain.get("lot_size", 50) if isinstance(chain, dict) else getattr(chain, "lot_size", 50)
        # Approximate SPAN margin as 15% of notional for options
        return price * lot_size * 0.15
