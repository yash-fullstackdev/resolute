"""
StopLossManager -- enforces all stop-loss, time-stop, and profit-target rules.

Called every tick cycle for each open position.
Handles BUYING strategies (premium loss %) and provides the foundation for
SELLING/HYBRID stop rules.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, time as _time, timezone

from ..strategies.base import Position

import structlog

logger = structlog.get_logger(service="user_worker_pool", module="stop_loss")


@dataclass
class StopResult:
    """Result of a stop-loss check."""
    should_exit: bool
    reason: str  # "STOP_HIT" | "TIME_STOP" | "TARGET_HIT" | "NONE"
    current_value: float = 0.0
    loss_pct: float = 0.0
    gain_pct: float = 0.0


class StopLossManager:
    """Enforce all stop-loss and time-stop rules from the strategy spec."""

    # BUYING strategy stop rules (premium loss based)
    BUYING_STOP_RULES: dict[str, dict] = {
        "long_call":         {"premium_loss_pct": 38, "time_stop": "weekly_wednesday_3pm"},
        "long_put":          {"premium_loss_pct": 38, "time_stop": "weekly_wednesday_3pm"},
        "bull_call_spread":  {"premium_loss_pct": 45, "time_stop": "profit_target_80pct"},
        "bear_put_spread":   {"premium_loss_pct": 45, "time_stop": "profit_target_80pct"},
        "long_straddle":     {"premium_loss_pct": 30, "time_stop": "event_day_1430"},
        "long_strangle":     {"premium_loss_pct": 40, "time_stop": "event_day_1400"},
        "pcr_contrarian":    {"premium_loss_pct": 35, "time_stop": "weekly_thursday_1000"},
        "event_directional": {"premium_loss_pct": 100, "time_stop": "event_day_1430"},
        "mcx_gold_silver":   {"premium_loss_pct": 40, "time_stop": "mcx_5d_before_expiry"},
        "mcx_crude_put":     {"premium_loss_pct": 35, "time_stop": "mcx_7d_before_expiry"},
        # ── S1: Brahmaastra ─────────────────────────────────────────────────
        # SL = wick-based (low of rejection candle for CE, high for PE)
        # or 50% of candle range on high-volatility days.
        # Partial profit book at 1:1 RR (50% of position).
        # Hard kill-switch at 10:30 IST regardless of P&L.
        "brahmaastra": {
            "sl_type": "wick_based",
            "premium_loss_pct": 50,          # backstop if wick SL not available
            "time_stop": "1030_kill_switch",
            "partial_book_pct": 50,          # book 50% at 1:1
            "partial_book_rr": 1.0,
            "sl_to_cost_at_rr": 1.0,         # move SL to entry after partial book
            "candle_50pct_rule": True,        # use 50% of candle if candle is huge
        },
        # ── S2: 5 EMA Mean Reversion ────────────────────────────────────────
        # SL = high of alert candle (PE) or low of alert candle (CE).
        # 3-loss circuit breaker halts strategy for rest of day.
        # No time-based stop — trade is managed via 1:3 RR target.
        "ema5_mean_reversion": {
            "sl_type": "alert_candle_hl",
            "premium_loss_pct": 60,          # backstop if alert candle SL not set
            "time_stop": "daily_3_loss_circuit",
            "rr_target": 3.0,
        },
        # ── S3: Parent-Child Momentum ───────────────────────────────────────
        # SL = structural swing high (PE) / swing low (CE) on 5m chart.
        # Monitor underlying price, not option premium, for SL trigger.
        # Hard exit at 15:15 IST.
        "parent_child_momentum": {
            "sl_type": "structural_swing",
            "premium_loss_pct": 70,          # backstop if structural SL unavailable
            "time_stop": "1515_hard_exit",
            "profit_target_pct": 25,         # 25% appreciation on option premium
            "underlying_target_pct": 0.75,   # OR 0.75% move in underlying
            "monitor_underlying": True,       # check underlying price, not option LTP
        },
    }

    HYBRID_STOP_RULES: dict[str, dict] = {
        "iron_butterfly_long": {"premium_loss_pct": 35, "time_stop": "event_day_1430"},
        "diagonal_spread":     {"premium_loss_pct": 40, "time_stop": "5dte_far_expiry"},
        "ratio_back_spread":   {"premium_loss_pct": 50, "time_stop": "3dte"},
    }

    SELLING_STOP_RULES: dict[str, dict] = {
        "short_straddle":     {"credit_loss_multiple": 1.5, "time_stop": "3dte", "profit_target_pct": 50},
        "short_strangle":     {"credit_loss_multiple": 2.0, "time_stop": "5dte", "profit_target_pct": 50},
        "credit_spread_call": {"credit_loss_multiple": 2.0, "time_stop": "3dte", "profit_target_pct": 65},
        "credit_spread_put":  {"credit_loss_multiple": 2.0, "time_stop": "3dte", "profit_target_pct": 65},
        "iron_condor":        {"breach_trigger": True,      "time_stop": "5dte", "profit_target_pct": 50},
        "jade_lizard":        {"credit_loss_multiple": 2.0, "time_stop": "3dte", "profit_target_pct": 50},
        "covered_call":       {"futures_loss_pct": 3.0,     "time_stop": "5dte_roll", "profit_target_pct": 80},
    }

    @property
    def STOP_RULES(self) -> dict[str, dict]:
        return {**self.BUYING_STOP_RULES, **self.HYBRID_STOP_RULES, **self.SELLING_STOP_RULES}

    def check_stop(self, position: Position, current_chain,
                   underlying_price: float = 0.0) -> StopResult:
        """Check if position should be stopped out.

        For BUYING strategies: checks premium_loss_pct against current value.
        For new sl_type variants (wick_based, alert_candle_hl, structural_swing):
          the actual SL price is stored in position.metadata['sl_price'] at entry
          time; here we compare the underlying_price (or option LTP as backstop).
        """
        rules = self.STOP_RULES.get(position.strategy_name)
        if rules is None:
            return StopResult(should_exit=False, reason="NONE")

        # Calculate current position value from chain
        current_value = self._calculate_current_value(position, current_chain)
        if current_value < 0:
            return StopResult(should_exit=False, reason="NONE")

        entry_cost = position.entry_cost_inr
        if entry_cost <= 0:
            return StopResult(should_exit=False, reason="NONE")

        sl_type = rules.get("sl_type")

        # ── Structural / wick-based / alert-candle SL ─────────────────────
        # These strategies store the exact SL price in metadata at signal time.
        # We check underlying_price for "structural_swing" (S3) and option LTP
        # for "wick_based" (S1) and "alert_candle_hl" (S2).
        if sl_type in ("wick_based", "alert_candle_hl", "structural_swing"):
            sl_price = position.metadata.get("sl_price", 0.0)
            direction = position.metadata.get("direction", "")  # "BULLISH" | "BEARISH"

            if sl_price > 0:
                # For structural_swing, monitor underlying price directly
                monitor_price = (
                    underlying_price
                    if (rules.get("monitor_underlying") and underlying_price > 0)
                    else current_value
                )
                breached = (
                    (direction == "BEARISH" and monitor_price >= sl_price) or
                    (direction == "BULLISH" and monitor_price <= sl_price)
                )
                if breached:
                    loss_pct = (entry_cost - current_value) / entry_cost * 100
                    logger.info(
                        "structured_sl_triggered",
                        position_id=position.position_id,
                        strategy=position.strategy_name,
                        sl_type=sl_type,
                        sl_price=sl_price,
                        monitor_price=round(monitor_price, 2),
                        direction=direction,
                    )
                    return StopResult(
                        should_exit=True,
                        reason="STOP_HIT",
                        current_value=current_value,
                        loss_pct=loss_pct,
                    )

            # Fall through to premium_loss_pct backstop below

        # ── BUYING strategies: premium loss % backstop ────────────────────
        premium_loss_pct = rules.get("premium_loss_pct")
        if premium_loss_pct is not None:
            loss_pct = (entry_cost - current_value) / entry_cost * 100
            if loss_pct >= premium_loss_pct:
                logger.info(
                    "stop_loss_triggered",
                    position_id=position.position_id,
                    strategy=position.strategy_name,
                    loss_pct=round(loss_pct, 2),
                    threshold=premium_loss_pct,
                )
                return StopResult(
                    should_exit=True,
                    reason="STOP_HIT",
                    current_value=current_value,
                    loss_pct=loss_pct,
                )

        # ── SELLING strategies: credit loss multiple ──────────────────────
        credit_loss_multiple = rules.get("credit_loss_multiple")
        if credit_loss_multiple is not None:
            loss = current_value - entry_cost
            if entry_cost > 0 and loss / entry_cost >= credit_loss_multiple:
                logger.info(
                    "credit_stop_triggered",
                    position_id=position.position_id,
                    strategy=position.strategy_name,
                    loss_multiple=round(loss / entry_cost, 2),
                    threshold=credit_loss_multiple,
                )
                return StopResult(
                    should_exit=True,
                    reason="STOP_HIT",
                    current_value=current_value,
                    loss_pct=loss / entry_cost * 100 if entry_cost > 0 else 0,
                )

        return StopResult(
            should_exit=False,
            reason="NONE",
            current_value=current_value,
        )

    def check_time_stop(self, position: Position, now: datetime | None = None) -> bool:
        """Check if the position's time stop has been reached.

        In addition to the absolute time_stop datetime on the position, this also
        handles intraday kill-switch rules for Brahmaastra (10:30 IST) and
        Parent-Child Momentum (15:15 IST).
        """
        now = now or datetime.now(timezone.utc)

        # Absolute time_stop from position (set at entry)
        if position.time_stop and now >= position.time_stop:
            logger.info(
                "time_stop_triggered",
                position_id=position.position_id,
                strategy=position.strategy_name,
                time_stop=position.time_stop.isoformat(),
            )
            return True

        # Intraday kill-switch rules keyed by strategy
        rules = self.STOP_RULES.get(position.strategy_name, {})
        time_stop_rule = rules.get("time_stop", "")
        now_ist = now.astimezone(timezone(timedelta(hours=5, minutes=30)))

        if time_stop_rule == "1030_kill_switch":
            cutoff = now_ist.replace(hour=10, minute=30, second=0, microsecond=0)
            if now_ist >= cutoff:
                logger.info("kill_switch_triggered", position_id=position.position_id,
                            strategy=position.strategy_name, rule="1030_kill_switch")
                return True

        elif time_stop_rule == "1515_hard_exit":
            cutoff = now_ist.replace(hour=15, minute=15, second=0, microsecond=0)
            if now_ist >= cutoff:
                logger.info("kill_switch_triggered", position_id=position.position_id,
                            strategy=position.strategy_name, rule="1515_hard_exit")
                return True

        return False

    def check_profit_target(self, position: Position, current_chain,
                            underlying_price: float = 0.0) -> bool:
        """Check if profit target has been reached.

        For BUYING: premium gained > target %.
        For S2 (ema5_mean_reversion): uses 1:3 RR target stored in metadata.
        For S3 (parent_child_momentum): 25% premium OR 0.75% underlying move.
        For SELLING: position value decayed below target threshold.
        """
        rules = self.STOP_RULES.get(position.strategy_name)
        if rules is None:
            return False

        current_value = self._calculate_current_value(position, current_chain)
        if current_value < 0:
            return False

        entry_cost = position.entry_cost_inr
        if entry_cost <= 0:
            return False

        gain_pct = (current_value - entry_cost) / entry_cost * 100

        # ── S3: check underlying move target too ─────────────────────────
        underlying_target_pct = rules.get("underlying_target_pct")
        if underlying_target_pct and underlying_price > 0:
            entry_underlying = position.metadata.get("entry_underlying", 0.0)
            direction = position.metadata.get("direction", "")
            if entry_underlying > 0:
                move_pct = abs(underlying_price - entry_underlying) / entry_underlying * 100
                direction_ok = (
                    (direction == "BULLISH" and underlying_price > entry_underlying) or
                    (direction == "BEARISH" and underlying_price < entry_underlying)
                )
                if direction_ok and move_pct >= underlying_target_pct:
                    logger.info(
                        "underlying_target_hit",
                        position_id=position.position_id,
                        strategy=position.strategy_name,
                        move_pct=round(move_pct, 3),
                        threshold=underlying_target_pct,
                    )
                    return True

        # ── Standard profit_target_pct ────────────────────────────────────
        profit_target_pct = rules.get("profit_target_pct")
        if profit_target_pct is None:
            if position.target_price > 0 and current_value >= position.target_price:
                logger.info(
                    "profit_target_hit",
                    position_id=position.position_id,
                    current_value=current_value,
                    target=position.target_price,
                )
                return True
            return False

        if gain_pct >= profit_target_pct:
            logger.info(
                "profit_target_hit",
                position_id=position.position_id,
                strategy=position.strategy_name,
                gain_pct=round(gain_pct, 2),
                threshold=profit_target_pct,
            )
            return True

        return False

    def _calculate_current_value(self, position: Position, current_chain) -> float:
        """Calculate current mark-to-market value of all position legs."""
        if not current_chain.strikes:
            return -1.0

        total_value = 0.0
        for leg in position.legs:
            strike_data = None
            for s in current_chain.strikes:
                if abs(s.strike - leg.strike) < 0.01:
                    strike_data = s
                    break

            if strike_data is None:
                return -1.0

            if leg.option_type == "CE":
                current_premium = strike_data.call_ltp
            else:
                current_premium = strike_data.put_ltp

            if leg.action == "BUY":
                total_value += current_premium * leg.lots
            else:
                # For sold legs, the value to close is the current premium
                total_value -= current_premium * leg.lots

        return total_value
