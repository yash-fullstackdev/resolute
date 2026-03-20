"""
UserWorker -- per-user isolated execution context.

Subscribes to shared chain.* subjects.
Evaluates user's enabled strategies with user's config.
Enforces discipline rules inline (no separate service needed).
Publishes validated orders to orders.new.validated.{tenant_id}.
"""

from __future__ import annotations

import asyncio
import json
import time
import uuid
from dataclasses import dataclass
from datetime import datetime, time as _time, timezone

from nats.aio.msg import Msg
import structlog

from ..capital_tier import CapitalTier, is_strategy_allowed, get_capital_tier
from ..config.user_config import UserStrategyConfig
from ..strategies.base import (
    BaseStrategy,
    Signal,
    Position,
    Order,
    FillConfirmation,
    Leg,
)
from ..discipline.plan_manager import PlanManager, LockedPlan
from ..discipline.circuit_breaker import CircuitBreaker
from ..discipline.override_guard import OverrideGuard
from ..discipline.journal import TradeJournal
from ..risk.stop_loss import StopLossManager
from ..risk.position_sizer import PositionSizer
from ..risk.event_calendar import EventCalendar
from ..portfolio.manager import PortfolioManager
from ..bias.evaluator import BiasEvaluator
from ..config.user_config import InstanceConfig
from ..candles.store import CandleStore

logger = structlog.get_logger(service="user_worker_pool", module="worker")

# IST offsets for market hours checks
NSE_OPEN_UTC = _time(3, 45)    # 09:15 IST
NSE_CLOSE_UTC = _time(10, 0)   # 15:30 IST
MCX_OPEN_UTC = _time(3, 30)    # 09:00 IST
MCX_CLOSE_UTC = _time(18, 0)   # 23:30 IST

# Session time ranges (IST as UTC offsets)
SESSION_RANGES = {
    "morning": (_time(3, 50), _time(6, 0)),    # 09:20–11:30 IST
    "afternoon": (_time(7, 30), _time(9, 0)),  # 13:00–14:30 IST
    "all": (_time(3, 50), _time(9, 45)),       # 09:20–15:15 IST
}


@dataclass
class StrategyInstance:
    """Runtime wrapper for a single strategy instance."""
    instance_id: str
    instance_name: str
    strategy: BaseStrategy
    config: dict                           # merged strategy params
    instruments: list[str]
    bias_evaluator: BiasEvaluator | None
    session: str
    mode: str                              # "live" | "paper"
    max_daily_loss_pts: float | None
    daily_pnl: float = 0.0
    daily_paused: bool = False
    _last_reset_date: str = ""
    # Runtime stats
    last_evaluated_ts: float = 0.0         # unix timestamp of last evaluation
    total_evaluations: int = 0
    signals_today_buy: int = 0
    signals_today_sell: int = 0
    last_signal_ts: float = 0.0
    last_bias: str = ""

    def check_daily_limit(self) -> bool:
        """Returns True if this instance should be paused."""
        today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        if self._last_reset_date != today:
            self.daily_pnl = 0.0
            self.daily_paused = False
            self.signals_today_buy = 0
            self.signals_today_sell = 0
            self._last_reset_date = today
        if self.max_daily_loss_pts is None:
            return False
        if self.daily_pnl <= -self.max_daily_loss_pts:
            self.daily_paused = True
            return True
        return False

    def record_pnl(self, pnl_points: float) -> None:
        self.daily_pnl += pnl_points

    def to_status(self) -> dict:
        """Return runtime status for this instance."""
        now = time.time()
        last_eval_ago = now - self.last_evaluated_ts if self.last_evaluated_ts > 0 else -1
        is_running = self.last_evaluated_ts > 0 and last_eval_ago < 120

        # Determine reason if not running
        reason = None
        if not is_running:
            now_utc = datetime.now(timezone.utc).time()
            from services.user_worker_pool.pool.worker import NSE_OPEN_UTC, NSE_CLOSE_UTC, SESSION_RANGES
            if now_utc < NSE_OPEN_UTC or now_utc > NSE_CLOSE_UTC:
                reason = "Market closed (NSE: 9:15 AM – 3:30 PM IST)"
            elif not self.is_in_session(now_utc):
                session_label = {"morning": "9:20–11:30", "afternoon": "13:00–14:30"}.get(self.session, self.session)
                reason = f"Outside session window ({session_label} IST)"
            elif self.daily_paused:
                reason = f"Daily loss limit hit ({self.daily_pnl:.1f} pts, limit: {self.max_daily_loss_pts} pts)"
            elif self.last_evaluated_ts == 0:
                reason = "Waiting for first tick data"
            else:
                reason = "No recent ticks — feed may be disconnected"

        return {
            "instance_id": self.instance_id,
            "instance_name": self.instance_name,
            "strategy_name": self.strategy.name,
            "mode": self.mode,
            "session": self.session,
            "instruments": self.instruments,
            "running": is_running,
            "reason": reason,
            "last_evaluated_ago_s": round(last_eval_ago, 1) if last_eval_ago >= 0 else None,
            "total_evaluations": self.total_evaluations,
            "signals_today": self.signals_today_buy + self.signals_today_sell,
            "signals_buy": self.signals_today_buy,
            "signals_sell": self.signals_today_sell,
            "daily_pnl": round(self.daily_pnl, 2),
            "daily_paused": self.daily_paused,
            "max_daily_loss_pts": self.max_daily_loss_pts,
            "last_bias": self.last_bias or None,
            "bias_active": self.bias_evaluator is not None and self.bias_evaluator.is_active,
        }

    def is_in_session(self, current_time_utc: _time) -> bool:
        """Check if current UTC time falls within this instance's session."""
        session_range = SESSION_RANGES.get(self.session, SESSION_RANGES["all"])
        return session_range[0] <= current_time_utc <= session_range[1]


@dataclass
class DisciplineContext:
    """All discipline state for one user, embedded in their UserWorker."""
    tenant_id: str
    plan_manager: PlanManager
    circuit_breaker: CircuitBreaker
    override_guard: OverrideGuard
    journal: TradeJournal


class UserWorker:
    """Per-user isolated execution context.

    Subscribes to shared chain.* subjects.
    Evaluates user's enabled strategies.
    Enforces discipline inline.
    Publishes validated orders.
    """

    def __init__(
        self,
        tenant_id: str,
        config: UserStrategyConfig,
        strategies: list[BaseStrategy],
        discipline: DisciplineContext,
        nats,                          # WorkerPoolNATSClient
        db,                            # AsyncDB
        portfolio: PortfolioManager,
        event_calendar: EventCalendar,
        stop_loss_manager: StopLossManager,
        position_sizer: PositionSizer,
        capital_tier: CapitalTier,
    ) -> None:
        self.tenant_id = tenant_id
        self.config = config
        self.strategies = strategies
        self.discipline = discipline
        self._nats = nats
        self._db = db
        self.portfolio = portfolio
        self._event_calendar = event_calendar
        self._stop_loss_manager = stop_loss_manager
        self._position_sizer = position_sizer
        self._capital_tier = capital_tier
        self._subs: list = []
        self._running = False
        self._log = logger.bind(tenant_id=tenant_id)
        self._candle_store = CandleStore()
        self._latest_prices: dict[str, float] = {}
        self._last_eval_1m_period: dict[str, int] = {}  # symbol → last evaluated 1m period

        # Build per-instance wrappers from DB config
        self._instances: list[StrategyInstance] = []
        strategy_map = {s.name: s for s in strategies}
        for inst_cfg in config.instances:
            if inst_cfg.mode == "disabled":
                continue
            strat_cls = strategy_map.get(inst_cfg.strategy_name)
            if strat_cls is None:
                continue

            # Build bias evaluator if configured
            bias_eval = None
            if inst_cfg.bias_config and inst_cfg.bias_config.get("mode") == "bias_filtered":
                bias_eval = BiasEvaluator(inst_cfg.bias_config)
                if not bias_eval.is_active:
                    bias_eval = None

            # Merge instance params with strategy defaults
            merged_config = config.get_instance_config(inst_cfg.instance_id)

            self._instances.append(StrategyInstance(
                instance_id=inst_cfg.instance_id,
                instance_name=inst_cfg.instance_name,
                strategy=strat_cls,
                config=merged_config,
                instruments=inst_cfg.instruments,
                bias_evaluator=bias_eval,
                session=inst_cfg.session,
                mode=inst_cfg.mode,
                max_daily_loss_pts=inst_cfg.max_daily_loss_pts,
            ))

        self._log.info(
            "instances_loaded",
            total=len(self._instances),
            live=[i.instance_name for i in self._instances if i.mode == "live"],
            paper=[i.instance_name for i in self._instances if i.mode == "paper"],
        )

    async def run(self) -> None:
        """Main event loop.

        1. Subscribe to chain.nse.* and chain.mcx.* subjects
        2. Subscribe to fills.{tenant_id}.*
        3. Subscribe to discipline.override.request.{tenant_id}.*
        4. Process messages until stopped
        """
        self._running = True
        self._log.info("worker_starting", strategies=[s.name for s in self.strategies])

        # Warmup candle history from Dhan API for all instruments across instances
        all_instruments = set()
        for inst in self._instances:
            all_instruments.update(inst.instruments)
        if all_instruments:
            instruments_list = sorted(all_instruments)
            self._log.info("candle_warmup_starting", instruments=instruments_list)
            await self._candle_store.warmup(instruments_list)
            # Set up fallback poller for these symbols
            self._candle_store.set_poll_symbols(instruments_list)

        # Subscribe to tick updates (for real-time candle building)
        sub_ticks = await self._nats.subscribe("ticks.>", cb=self._on_tick_message)
        self._subs.append(sub_ticks)

        # Subscribe to chain updates (for options chain data)
        sub_nse = await self._nats.subscribe("chain.nse.>", cb=self._on_chain_message)
        sub_mcx = await self._nats.subscribe("chain.mcx.>", cb=self._on_chain_message)
        self._subs.extend([sub_nse, sub_mcx])

        # Subscribe to fill confirmations
        sub_fills = await self._nats.subscribe(
            f"fills.{self.tenant_id}.*",
            cb=self._on_fill_message,
        )
        self._subs.append(sub_fills)

        # Subscribe to override requests
        sub_overrides = await self._nats.subscribe(
            f"discipline.override.request.{self.tenant_id}.*",
            cb=self._on_override_message,
        )
        self._subs.append(sub_overrides)

        self._log.info("worker_started", instances=len(self._instances))

        # Start fallback REST poller as background task
        poller_task = asyncio.create_task(self._candle_store.run_fallback_poller())

        # 1m evaluation timer — checks every 1 second for fast signal generation
        try:
            while self._running:
                await asyncio.sleep(1)
                try:
                    await self._check_1m_evaluation()
                except Exception as exc:
                    self._log.error("eval_timer_error", error=str(exc))
        finally:
            poller_task.cancel()

    def get_instance_statuses(self) -> list[dict]:
        """Get runtime status for all instances — used by status API."""
        statuses = []
        for inst in self._instances:
            s = inst.to_status()
            # Add candle store info
            for sym in inst.instruments:
                bars_5m = self._candle_store.get_bar_count(sym, "5m")
                bars_1m = self._candle_store.get_bar_count(sym, "1m")
                warmed = self._candle_store.is_warmed_up(sym)
                stale = self._candle_store.is_tick_stale(sym)
                s.setdefault("candle_status", {})[sym] = {
                    "bars_5m": bars_5m,
                    "bars_1m": bars_1m,
                    "warmed_up": warmed,
                    "tick_stale": stale,
                }
            statuses.append(s)
        return statuses

    async def stop(self) -> None:
        """Gracefully stop the worker."""
        self._running = False

        for sub in self._subs:
            try:
                await self._nats.unsubscribe(sub)
            except Exception:
                pass
        self._subs.clear()

        self._log.info("worker_stopped")

    # ------------------------------------------------------------------
    # Chain update handler
    # ------------------------------------------------------------------

    async def _on_tick_message(self, msg: Msg) -> None:
        """Handle incoming tick messages — update candle store only (cheap)."""
        try:
            data = json.loads(msg.data.decode())
            symbol = data.get("symbol", "")
            price = data.get("last_price", 0.0)
            volume = float(data.get("volume", 0))

            if not symbol or price <= 0:
                return

            # Parse timestamp — Go time.Time serializes as ISO string
            ts_raw = data.get("timestamp")
            if isinstance(ts_raw, str):
                try:
                    from datetime import datetime as _dt
                    tick_ts = _dt.fromisoformat(ts_raw.replace("Z", "+00:00")).timestamp()
                except Exception:
                    tick_ts = time.time()
            elif isinstance(ts_raw, (int, float)):
                tick_ts = float(ts_raw)
            else:
                tick_ts = time.time()

            # Normalize symbol names: NIFTY → NIFTY_50, BANKNIFTY → BANK_NIFTY
            normalized = _normalize_symbol(symbol)

            self._candle_store.on_tick(normalized, float(price), volume, tick_ts)
            self._latest_prices[normalized] = float(price)

            # Also store under original name for backward compat
            if normalized != symbol:
                self._candle_store.on_tick(symbol, float(price), volume, tick_ts)
                self._latest_prices[symbol] = float(price)
        except Exception:
            pass

    async def _check_1m_evaluation(self) -> None:
        """Timer-driven: check if any symbol has a new 1m candle and evaluate."""
        now_ts = time.time()
        current_1m = int(now_ts // 60) * 60

        for symbol, price in list(self._latest_prices.items()):
            last_eval = self._last_eval_1m_period.get(symbol, 0)
            if current_1m > last_eval:
                self._last_eval_1m_period[symbol] = current_1m
                await self._evaluate_on_candle_close(symbol, price, {})

    async def _evaluate_on_candle_close(self, symbol: str, price: float, tick_data: dict) -> None:
        """Evaluate all strategy instances when a 5m candle closes for a symbol."""
        now = datetime.now(timezone.utc)
        current_time_utc = now.time()

        # Market hours check
        if current_time_utc < NSE_OPEN_UTC or current_time_utc > NSE_CLOSE_UTC:
            return

        # Circuit breaker
        if self.discipline.circuit_breaker.is_user_halted(self.tenant_id):
            return

        # Build a lightweight chain-like object with candle data
        candles_5m = self._candle_store.get_candles(symbol, "5m")
        candles_1m = self._candle_store.get_candles(symbol, "1m")

        if not candles_5m or "close" not in candles_5m or len(candles_5m["close"]) < 15:
            return

        # Create chain snapshot from tick data + candles
        chain_data = {
            "underlying": symbol,
            "underlying_price": price,
            "atm_iv": tick_data.get("atm_iv", 0.15),
            "segment": "nse",
            "strikes": tick_data.get("strikes", []),
            "candles_1m": candles_1m,
            "candles_5m": candles_5m,
        }
        chain = _ChainSnapshot(chain_data)

        regime = self._classify_regime(chain_data)

        for inst in self._instances:
            strategy = inst.strategy

            if not is_strategy_allowed(strategy.category, self._capital_tier):
                continue
            if not _symbol_matches(symbol, inst.instruments):
                continue
            if not inst.is_in_session(current_time_utc):
                continue
            if inst.check_daily_limit():
                continue

            # Track evaluation
            inst.last_evaluated_ts = time.time()
            inst.total_evaluations += 1

            # Bias gate
            if inst.bias_evaluator:
                try:
                    bias_direction = inst.bias_evaluator.get_current_bias(candles_5m, candles_1m)
                    inst.last_bias = bias_direction or ""
                    if bias_direction is None:
                        continue
                except Exception:
                    continue
            else:
                bias_direction = None

            try:
                signal = strategy.evaluate(chain, regime, self.portfolio.open_positions, inst.config)
            except Exception as exc:
                self._log.error("strategy_evaluate_error", instance=inst.instance_name, error=str(exc))
                continue

            if signal is None:
                continue

            if bias_direction is not None:
                sig_dir = "BUY" if signal.direction in ("BULLISH", "BUY") else "SELL"
                if sig_dir != bias_direction:
                    continue

            # Track signal
            sig_dir = "BUY" if signal.direction in ("BULLISH", "BUY") else "SELL"
            if sig_dir == "BUY":
                inst.signals_today_buy += 1
            else:
                inst.signals_today_sell += 1
            inst.last_signal_ts = time.time()

            # Tag signal
            signal.metadata["instance_id"] = inst.instance_id
            signal.metadata["instance_name"] = inst.instance_name
            signal.metadata["trading_mode"] = inst.mode
            if bias_direction:
                signal.metadata["bias_direction"] = bias_direction

            # Options overlay
            options_info = _compute_options_overlay(chain, signal)
            if options_info:
                signal.metadata["options"] = options_info

            if inst.mode == "paper":
                await self._publish_direct_signal(signal)
            elif inst.mode == "live":
                if signal.signal_type == "DIRECT":
                    await self._publish_direct_signal(signal)
                else:
                    await self._process_signal(signal, strategy, chain)

            self._log.info(
                "signal_generated",
                instance=inst.instance_name,
                strategy=strategy.name,
                symbol=symbol,
                direction=signal.direction,
                mode=inst.mode,
            )

    async def _on_chain_message(self, msg: Msg) -> None:
        """Handle incoming chain snapshot messages."""
        try:
            data = json.loads(msg.data.decode())
            await self.on_chain_update(data)
        except json.JSONDecodeError:
            self._log.warning("chain_decode_error", subject=msg.subject)
        except Exception as exc:
            self._log.error("chain_handler_error", error=str(exc))

    async def on_chain_update(self, chain_data: dict) -> None:
        """Process a chain snapshot update.

        a. Check circuit breaker -- if HALTED, skip evaluation
        b. Check market hours
        c. For each enabled strategy: evaluate()
        d. If signal: validate against plan, size, build order, publish
        e. For each open position: check exits
        """
        # Convert dict to a simple namespace for strategy access
        chain = _ChainSnapshot(chain_data)

        # Inject candles from CandleStore (real market data, not mock)
        underlying = _normalize_symbol(chain.underlying)
        chain.underlying = underlying  # normalize for instance matching
        candles_5m = self._candle_store.get_candles(underlying, "5m")
        candles_1m = self._candle_store.get_candles(underlying, "1m")
        if candles_5m:
            chain.candles_5m = candles_5m
        if candles_1m:
            chain.candles_1m = candles_1m

        # (a) Circuit breaker check -- absolute, no override
        if self.discipline.circuit_breaker.is_user_halted(self.tenant_id):
            return

        # (b) Market hours check
        now = datetime.now(timezone.utc)
        current_time = now.time()
        segment = chain_data.get("segment", "nse")

        if segment == "mcx":
            if current_time < MCX_OPEN_UTC or current_time > MCX_CLOSE_UTC:
                return
        else:
            if current_time < NSE_OPEN_UTC or current_time > NSE_CLOSE_UTC:
                return

        # Get regime from chain data
        regime = self._classify_regime(chain_data)
        current_time = now.time()

        # (c) Evaluate each strategy INSTANCE
        for inst in self._instances:
            strategy = inst.strategy

            # Capital tier enforcement
            if not is_strategy_allowed(strategy.category, self._capital_tier):
                continue

            # Segment filter
            chain_segment = "MCX" if segment == "mcx" else "NSE_INDEX"
            if chain_segment not in strategy.allowed_segments:
                continue

            # Instrument filter — instance-level (not strategy-level)
            if not _symbol_matches(chain.underlying, inst.instruments):
                continue

            # Session filter
            if not inst.is_in_session(current_time):
                continue

            # Daily loss limit
            if inst.check_daily_limit():
                continue

            # Bias gate — evaluate bias BEFORE strategy
            if inst.bias_evaluator:
                try:
                    candle_5m = getattr(chain, "candles_5m", None)
                    candle_1m = getattr(chain, "candles_1m", None)
                    bias_direction = inst.bias_evaluator.get_current_bias(candle_5m, candle_1m)
                    if bias_direction is None:
                        continue  # no clear bias → skip
                except Exception:
                    continue  # bias error → skip (fail safe)
            else:
                bias_direction = None

            # Evaluate strategy with instance-specific config
            try:
                signal = strategy.evaluate(
                    chain,
                    regime,
                    self.portfolio.open_positions,
                    inst.config,
                )
            except Exception as exc:
                self._log.error(
                    "strategy_evaluate_error",
                    instance=inst.instance_name,
                    strategy=strategy.name,
                    error=str(exc),
                )
                continue

            if signal is None:
                continue

            # Bias alignment — signal direction must match bias
            if bias_direction is not None:
                sig_dir = "BUY" if signal.direction in ("BULLISH", "BUY") else "SELL"
                if sig_dir != bias_direction:
                    self._log.debug(
                        "signal_bias_mismatch",
                        instance=inst.instance_name,
                        signal=sig_dir,
                        bias=bias_direction,
                    )
                    continue

            # Tag signal with instance info
            signal.metadata["instance_id"] = inst.instance_id
            signal.metadata["instance_name"] = inst.instance_name
            signal.metadata["trading_mode"] = inst.mode
            if bias_direction:
                signal.metadata["bias_direction"] = bias_direction

            # Compute options overlay if chain has strikes
            options_info = _compute_options_overlay(chain, signal)
            if options_info:
                signal.metadata["options"] = options_info

            # (d) Route based on mode
            if inst.mode == "paper":
                await self._publish_direct_signal(signal)
            elif inst.mode == "live":
                if signal.signal_type == "DIRECT":
                    await self._publish_direct_signal(signal)
                else:
                    await self._process_signal(signal, strategy, chain)

        # (e) Check exits for open positions
        await self._check_exits(chain)

    async def _publish_direct_signal(self, signal: Signal) -> None:
        """Publish signal to NATS with index-level + options overlay data."""
        sig_dir = "BUY" if signal.direction in ("BULLISH", "BUY") else "SELL"
        options = signal.metadata.get("options")

        # Index-level risk:reward
        idx_risk = idx_reward = 0.0
        if signal.entry_price and signal.stop_loss_price:
            if sig_dir == "BUY":
                idx_risk = round(signal.entry_price - signal.stop_loss_price, 2)
                idx_reward = round((signal.target_price or 0) - signal.entry_price, 2)
            else:
                idx_risk = round(signal.stop_loss_price - signal.entry_price, 2)
                idx_reward = round(signal.entry_price - (signal.target_price or 0), 2)

        payload = {
            "signal_type": "DIRECT",
            "signal": signal.strategy_name,
            "underlying": signal.underlying,
            "direction": sig_dir,
            "entry_price": signal.entry_price,
            "stop_loss_price": signal.stop_loss_price,
            "target_price": signal.target_price,
            "index_risk_pts": idx_risk,
            "index_reward_pts": idx_reward,
            "index_rr": f"1:{round(idx_reward / idx_risk, 1)}" if idx_risk > 0 else "N/A",
            "confidence": signal.confidence,
            "metadata": signal.metadata,
        }

        # Attach options overlay if available
        if options:
            payload["options"] = options
            payload["has_options_chain"] = True
        else:
            payload["has_options_chain"] = False

        await self._nats.publish(
            f"signals.{self.tenant_id}.{signal.strategy_name}.{signal.underlying}",
            payload,
        )
        self._log.info(
            "direct_signal_published",
            strategy=signal.strategy_name,
            underlying=signal.underlying,
            direction=signal.direction,
            entry=signal.entry_price,
            stop=signal.stop_loss_price,
            target=signal.target_price,
        )

    async def _process_signal(
        self,
        signal: Signal,
        strategy: BaseStrategy,
        chain: _ChainSnapshot,
    ) -> None:
        """Validate signal against plan, size position, publish order."""

        # Check portfolio limits
        can_trade, reason = self._position_sizer.check_portfolio_limits(
            signal,
            self.portfolio.open_positions,
            self.portfolio.portfolio_value_inr,
            self._capital_tier,
        )
        if not can_trade:
            self._log.info(
                "signal_blocked_portfolio",
                strategy=signal.strategy_name,
                reason=reason,
            )
            return

        # Calculate lots
        lots = self._position_sizer.calculate_lots(
            signal,
            self.portfolio.portfolio_value_inr,
            self.portfolio.open_positions,
            self._capital_tier,
        )
        if lots <= 0:
            self._log.info(
                "signal_blocked_sizing",
                strategy=signal.strategy_name,
            )
            return

        # Build order
        order = Order(
            order_id=str(uuid.uuid4()),
            tenant_id=self.tenant_id,
            strategy_name=signal.strategy_name,
            underlying=signal.underlying,
            segment=signal.segment,
            legs=signal.legs,
            stop_loss_price=signal.stop_loss_price,
            target_price=signal.target_price,
            time_stop=signal.time_stop,
            lots=lots,
            order_type="NEW",
        )

        # Validate against locked plan
        locked_plan = self.discipline.plan_manager.get_active_plan(self.tenant_id)
        if locked_plan is not None:
            is_valid, rejection_reason = (
                self.discipline.plan_manager.validate_order_against_plan(
                    order, locked_plan
                )
            )
            if not is_valid:
                self._log.info(
                    "order_rejected_by_plan",
                    strategy=signal.strategy_name,
                    reason=rejection_reason,
                )
                return

        # Validate order completeness
        if not self._validate_order_completeness(order):
            return

        # Margin check for SELLING/HYBRID strategies
        if strategy.requires_margin:
            sufficient, margin_reason = self._position_sizer.check_margin_for_selling(
                signal, chain, self.tenant_id
            )
            if not sufficient:
                self._log.info(
                    "signal_blocked_margin",
                    strategy=signal.strategy_name,
                    reason=margin_reason,
                )
                return

        # Publish validated order
        await self._publish_order(order, signal)

    def _validate_order_completeness(self, order: Order) -> bool:
        """Validate that order has all required fields (stop_loss, target, time_stop)."""
        now = datetime.now(timezone.utc)

        if not order.stop_loss_price or order.stop_loss_price < 0:
            # Allow stop_loss_price = 0 for event_directional (full premium at risk)
            if order.strategy_name != "event_directional":
                self._log.warning(
                    "order_missing_stop_loss",
                    order_id=order.order_id,
                    strategy=order.strategy_name,
                )
                return False

        if not order.target_price or order.target_price <= 0:
            self._log.warning(
                "order_missing_target",
                order_id=order.order_id,
                strategy=order.strategy_name,
            )
            return False

        if not order.time_stop or order.time_stop <= now:
            self._log.warning(
                "order_invalid_time_stop",
                order_id=order.order_id,
                strategy=order.strategy_name,
            )
            return False

        return True

    async def _publish_order(self, order: Order, signal: Signal) -> None:
        """Publish validated order to NATS and create position."""
        subject = f"orders.new.validated.{self.tenant_id}"

        order_data = {
            "order_id": order.order_id,
            "tenant_id": order.tenant_id,
            "strategy_name": order.strategy_name,
            "underlying": order.underlying,
            "segment": order.segment,
            "legs": [
                {
                    "option_type": leg.option_type,
                    "strike": leg.strike,
                    "expiry": leg.expiry.isoformat(),
                    "action": leg.action,
                    "lots": leg.lots,
                    "premium": leg.premium,
                }
                for leg in order.legs
            ],
            "stop_loss_price": order.stop_loss_price,
            "target_price": order.target_price,
            "time_stop": order.time_stop.isoformat(),
            "lots": order.lots,
            "order_type": order.order_type,
        }

        await self._nats.publish(subject, order_data)

        # Track the new position
        position = Position(
            position_id=order.order_id,
            tenant_id=self.tenant_id,
            strategy_name=order.strategy_name,
            underlying=order.underlying,
            segment=order.segment,
            legs=order.legs,
            entry_time=datetime.now(timezone.utc),
            entry_cost_inr=signal.entry_price * order.lots,
            current_value_inr=signal.entry_price * order.lots,
            stop_loss_price=order.stop_loss_price,
            target_price=order.target_price,
            time_stop=order.time_stop,
            lots=order.lots,
        )
        self.portfolio.add_position(position)

        # Increment trade count for circuit breaker
        self.discipline.circuit_breaker.increment_trade_count(self.tenant_id)

        # Publish signal event
        await self._nats.publish(
            f"signals.{self.tenant_id}.{order.strategy_name}.{order.underlying}",
            {
                "signal": order.strategy_name,
                "underlying": order.underlying,
                "direction": signal.direction,
                "confidence": signal.confidence,
                "order_id": order.order_id,
            },
        )

        self._log.info(
            "order_published",
            order_id=order.order_id,
            strategy=order.strategy_name,
            underlying=order.underlying,
            lots=order.lots,
            entry_price=signal.entry_price,
        )

    # ------------------------------------------------------------------
    # Exit checking
    # ------------------------------------------------------------------

    async def _check_exits(self, chain: _ChainSnapshot) -> None:
        """Check all open positions for exit conditions."""
        # Update unrealised P&L
        self.portfolio.update_unrealised_pnl(chain)

        # Check circuit breaker after P&L update
        locked_plan = self.discipline.plan_manager.get_active_plan(self.tenant_id)
        if locked_plan:
            await self.discipline.circuit_breaker.check_and_update(
                self.tenant_id,
                self.portfolio.realised_pnl_today,
                self.portfolio.unrealised_pnl_today,
                locked_plan,
            )

        for position in list(self.portfolio.open_positions):
            if position.underlying != chain.underlying:
                continue

            # Check stop loss
            stop_result = self._stop_loss_manager.check_stop(position, chain)
            if stop_result.should_exit:
                await self._exit_position(position, stop_result.reason, chain)
                continue

            # Check time stop
            if self._stop_loss_manager.check_time_stop(position):
                await self._exit_position(position, "TIME_STOP", chain)
                continue

            # Check profit target
            if self._stop_loss_manager.check_profit_target(position, chain):
                await self._exit_position(position, "TARGET_HIT", chain)
                continue

            # Strategy-level exit check
            strategy_config = self.config.get_strategy_config(position.strategy_name)
            for strategy in self.strategies:
                if strategy.name == position.strategy_name:
                    try:
                        if strategy.should_exit(position, chain, strategy_config):
                            await self._exit_position(
                                position, "STRATEGY_EXIT", chain
                            )
                    except Exception as exc:
                        self._log.error(
                            "strategy_exit_check_error",
                            strategy=strategy.name,
                            position_id=position.position_id,
                            error=str(exc),
                        )
                    break

    async def _exit_position(
        self,
        position: Position,
        exit_reason: str,
        chain: _ChainSnapshot,
    ) -> None:
        """Exit an open position and write journal entry."""
        # Calculate exit value
        exit_value = position.current_value_inr

        # Close the position
        closed = self.portfolio.close_position(
            position.position_id,
            exit_value_inr=exit_value,
            exit_reason=exit_reason,
        )

        if closed is None:
            return

        # Publish exit order
        exit_order_data = {
            "order_id": str(uuid.uuid4()),
            "tenant_id": self.tenant_id,
            "strategy_name": position.strategy_name,
            "underlying": position.underlying,
            "position_id": position.position_id,
            "order_type": "EXIT",
            "exit_reason": exit_reason,
            "lots": position.lots,
        }
        await self._nats.publish(
            f"orders.new.validated.{self.tenant_id}",
            exit_order_data,
        )

        # Publish exit signal
        await self._nats.publish(
            f"signals.{self.tenant_id}.exit.{position.position_id}",
            {
                "position_id": position.position_id,
                "exit_reason": exit_reason,
                "pnl_inr": closed.pnl_inr,
            },
        )

        # Write journal entry
        override_requests = (
            self.discipline.override_guard.get_pending_requests_for_position(
                position.position_id
            )
        )
        locked_plan = self.discipline.plan_manager.get_active_plan(self.tenant_id)

        entry = self.discipline.journal.write_entry(
            closed, locked_plan, override_requests
        )

        # Persist journal entry async
        asyncio.create_task(self.discipline.journal.persist_entry(entry))
        asyncio.create_task(self.discipline.journal.publish_entry(entry))

        # Persist position
        asyncio.create_task(self.portfolio.persist_position(closed))

        self._log.info(
            "position_exited",
            position_id=position.position_id,
            strategy=position.strategy_name,
            exit_reason=exit_reason,
            pnl=round(closed.pnl_inr, 2),
            discipline_score=entry.discipline_score,
        )

    # ------------------------------------------------------------------
    # Fill handler
    # ------------------------------------------------------------------

    async def _on_fill_message(self, msg: Msg) -> None:
        """Handle fill confirmations from order_router."""
        try:
            data = json.loads(msg.data.decode())
            fill = FillConfirmation(
                order_id=data["order_id"],
                tenant_id=data["tenant_id"],
                position_id=data["position_id"],
                fill_type=data["fill_type"],
                fill_price=data["fill_price"],
                filled_at=datetime.fromisoformat(data["filled_at"]),
                pnl_inr=data.get("pnl_inr", 0.0),
            )
            await self.on_fill(fill)
        except Exception as exc:
            self._log.error("fill_handler_error", error=str(exc))

    async def on_fill(self, fill: FillConfirmation) -> None:
        """Update position state and check circuit breaker."""
        position = self.portfolio.on_fill(fill)
        if position is None:
            return

        # If this is a close fill, write journal entry
        if fill.fill_type in {"CLOSE", "STOP_HIT", "TIME_STOP", "TARGET_HIT"}:
            override_requests = (
                self.discipline.override_guard.get_pending_requests_for_position(
                    fill.position_id
                )
            )
            locked_plan = self.discipline.plan_manager.get_active_plan(self.tenant_id)

            entry = self.discipline.journal.write_entry(
                position, locked_plan, override_requests
            )
            asyncio.create_task(self.discipline.journal.persist_entry(entry))
            asyncio.create_task(self.discipline.journal.publish_entry(entry))

        # Check circuit breaker after every fill
        locked_plan = self.discipline.plan_manager.get_active_plan(self.tenant_id)
        if locked_plan:
            await self.discipline.circuit_breaker.check_and_update(
                self.tenant_id,
                self.portfolio.realised_pnl_today,
                self.portfolio.unrealised_pnl_today,
                locked_plan,
            )

    # ------------------------------------------------------------------
    # Override handler
    # ------------------------------------------------------------------

    async def _on_override_message(self, msg: Msg) -> None:
        """Handle override request messages."""
        try:
            data = json.loads(msg.data.decode())
            action = data.get("action", "request")

            if action == "request":
                result = self.discipline.override_guard.request_override(
                    user_id=self.tenant_id,
                    position_id=data["position_id"],
                    override_type=data["override_type"],
                    proposed_value=data["proposed_value"],
                    reason=data["reason"],
                    original_value=data.get("original_value", 0.0),
                )
                if isinstance(result, tuple):
                    # Rejected
                    await self._nats.publish(
                        f"discipline.override.rejected.{self.tenant_id}.{data['position_id']}",
                        {"reason": result[1]},
                    )
                else:
                    # Accepted, pending cooldown
                    await self._nats.publish(
                        f"discipline.override.request.{self.tenant_id}.{data['position_id']}",
                        {
                            "request_id": result.id,
                            "cooldown_expires_at": result.cooldown_expires_at.isoformat(),
                            "status": result.status,
                        },
                    )

            elif action == "confirm":
                approved, message = self.discipline.override_guard.confirm_override(
                    override_request_id=data["request_id"],
                    user_id=self.tenant_id,
                )
                subject_suffix = "approved" if approved else "rejected"
                await self._nats.publish(
                    f"discipline.override.{subject_suffix}.{self.tenant_id}.{data.get('position_id', '')}",
                    {"approved": approved, "message": message},
                )

                if approved:
                    # Apply the override to the position
                    await self._apply_override(data)

        except Exception as exc:
            self._log.error("override_handler_error", error=str(exc))

    async def _apply_override(self, data: dict) -> None:
        """Apply a confirmed override to a position."""
        position_id = data.get("position_id")
        override_type = data.get("override_type")
        proposed_value = data.get("proposed_value")

        position = self.portfolio._positions.get(position_id)
        if position is None or position.status != "OPEN":
            return

        if override_type == "STOP_LOSS_MOVE":
            position.stop_loss_price = proposed_value
            position.stop_loss_moved = True
            position.override_count += 1
        elif override_type == "TIME_STOP_EXTEND":
            position.time_stop = datetime.fromisoformat(str(proposed_value))
            position.time_stop_extended = True
            position.override_count += 1
        elif override_type == "EARLY_EXIT":
            position.override_count += 1

        self._log.warning(
            "override_applied",
            position_id=position_id,
            override_type=override_type,
            proposed_value=proposed_value,
        )

    # ------------------------------------------------------------------
    # Regime classification
    # ------------------------------------------------------------------

    def _classify_regime(self, chain_data: dict):
        """Simple regime classification from chain data."""
        from ..strategies.base import BaseStrategy  # avoid circular

        class _Regime:
            def __init__(self, value: str):
                self.value = value

        underlying = chain_data.get("underlying", "")
        iv_rank = chain_data.get("iv_rank", 50)
        atm_iv = chain_data.get("atm_iv", 0.15)

        # Check for upcoming events
        days_to_event, event = self._event_calendar.get_nearest_event(underlying)
        if days_to_event <= 2:
            return _Regime("PRE_EVENT")

        # MCX segment
        segment = chain_data.get("segment", "nse")
        if segment == "mcx":
            return _Regime("COMMODITY_MACRO")

        # VIX-based classification
        vix_proxy = atm_iv * 100 if atm_iv < 1 else atm_iv

        # Trend detection (simplified: use pcr as proxy)
        pcr = chain_data.get("pcr_oi", 1.0)
        if pcr < 0.8:
            trend = "BULL"
        elif pcr > 1.2:
            trend = "BEAR"
        else:
            trend = "SIDEWAYS"

        if vix_proxy >= 20:
            if trend == "BULL":
                return _Regime("BULL_HIGH_VOL")
            elif trend == "BEAR":
                return _Regime("BEAR_HIGH_VOL")
            return _Regime("SIDEWAYS_HIGH_VOL")
        else:
            if trend == "BULL":
                return _Regime("BULL_LOW_VOL")
            elif trend == "BEAR":
                return _Regime("BEAR_LOW_VOL")
            return _Regime("SIDEWAYS_LOW_VOL")


# Bidirectional symbol aliases — maps between different naming conventions
_SYMBOL_ALIASES = {
    "NIFTY": "NIFTY_50",
    "NIFTY_50": "NIFTY",
    "BANKNIFTY": "BANK_NIFTY",
    "BANK_NIFTY": "BANKNIFTY",
}


def _normalize_symbol(symbol: str) -> str:
    """Normalize symbol — strip NSE: prefix from equities."""
    if symbol.startswith("NSE:"):
        return symbol[4:]
    return symbol


def _symbol_matches(tick_symbol: str, instance_instruments: list[str]) -> bool:
    """Check if a tick's symbol matches any of the instance's instruments.

    Handles naming mismatches: NIFTY vs NIFTY_50, BANKNIFTY vs BANK_NIFTY.
    """
    if not instance_instruments:
        return True  # empty = all instruments
    if tick_symbol in instance_instruments:
        return True
    # Check alias
    alias = _SYMBOL_ALIASES.get(tick_symbol)
    if alias and alias in instance_instruments:
        return True
    return False


def _compute_options_overlay(chain, signal: Signal) -> dict | None:
    """Compute ATM option suggestion from an index-level signal.

    Returns dict with option details, or None if no options chain available.
    Strategy logic runs on INDEX PRICE only — this is a post-signal overlay.
    """
    if not chain.strikes:
        return None

    spot = chain.underlying_price
    if spot <= 0:
        return None

    sig_dir = "BUY" if signal.direction in ("BULLISH", "BUY") else "SELL"
    option_type = "CE" if sig_dir == "BUY" else "PE"

    # Find ATM strike (closest to spot)
    atm_strike = None
    min_diff = float("inf")
    for s in chain.strikes:
        diff = abs(s.strike - spot)
        if diff < min_diff:
            min_diff = diff
            atm_strike = s

    if atm_strike is None:
        return None

    # Get option LTP and delta
    if option_type == "CE":
        option_ltp = atm_strike.call_ltp
        delta = atm_strike.call_delta if atm_strike.call_delta else 0.5  # default ATM delta
        option_iv = atm_strike.call_iv
    else:
        option_ltp = atm_strike.put_ltp
        delta = abs(atm_strike.put_delta) if atm_strike.put_delta else 0.5
        option_iv = atm_strike.put_iv

    if option_ltp <= 0:
        return None

    # Compute option SL/TP from index SL/TP using delta approximation
    # Delta tells us: for every 1 point move in index, option moves ~delta points
    index_entry = signal.entry_price if signal.entry_price else spot
    index_sl = signal.stop_loss_price if signal.stop_loss_price else 0
    index_tp = signal.target_price if signal.target_price else 0

    # For index signals, entry/SL/TP are index prices
    # For DIRECT signals from our technical strategies, these are index prices
    if index_sl > 0 and index_entry > 0:
        if sig_dir == "BUY":
            sl_move = index_entry - index_sl   # positive: how much index drops to hit SL
            tp_move = index_tp - index_entry   # positive: how much index rises to hit TP
        else:
            sl_move = index_sl - index_entry   # positive: how much index rises to hit SL
            tp_move = index_entry - index_tp   # positive: how much index drops to hit TP

        option_sl_move = sl_move * delta
        option_tp_move = tp_move * delta

        option_sl = round(max(option_ltp - option_sl_move, 0.05), 2)
        option_tp = round(option_ltp + option_tp_move, 2)
    else:
        option_sl = round(option_ltp * 0.7, 2)   # fallback: 30% SL
        option_tp = round(option_ltp * 1.5, 2)   # fallback: 50% target

    # Risk:Reward on options
    option_risk = round(option_ltp - option_sl, 2)
    option_reward = round(option_tp - option_ltp, 2)
    option_rr = f"1:{round(option_reward / option_risk, 1)}" if option_risk > 0 else "N/A"

    return {
        "strike": atm_strike.strike,
        "option_type": option_type,
        "ltp": round(option_ltp, 2),
        "sl": option_sl,
        "tp": option_tp,
        "delta": round(delta, 3),
        "iv": round(option_iv * 100, 1) if option_iv else None,
        "risk": option_risk,
        "reward": option_reward,
        "rr": option_rr,
        "expiry": chain.expiry.isoformat() if chain.expiry else None,
    }


class _ChainSnapshot:
    """Lightweight wrapper around chain dict data for strategy consumption."""

    def __init__(self, data: dict) -> None:
        self._data = data
        self.underlying = data.get("underlying", "")
        self.underlying_price = data.get("underlying_price", 0.0)
        self.atm_iv = data.get("atm_iv", 0.0)
        self.iv_rank = data.get("iv_rank", 0.0)
        self.iv_percentile = data.get("iv_percentile", 0.0)
        self.pcr_oi = data.get("pcr_oi", 0.0)
        self.pcr_volume = data.get("pcr_volume", 0.0)

        # Parse expiry
        from datetime import date as _date
        expiry_raw = data.get("expiry")
        if isinstance(expiry_raw, str):
            self.expiry = _date.fromisoformat(expiry_raw)
        elif isinstance(expiry_raw, _date):
            self.expiry = expiry_raw
        else:
            self.expiry = _date.today()

        # Parse strikes
        self.strikes = []
        for s in data.get("strikes", []):
            self.strikes.append(_StrikeData(s))

        # Candle data injected by signal_engine CandleAggregator
        # Format: {open: [...], high: [...], low: [...], close: [...], volume: [...], timestamp: [...]}
        self.candles_1m: dict = data.get("candles_1m", {})
        self.candles_5m: dict = data.get("candles_5m", {})


class _StrikeData:
    """Lightweight wrapper around strike dict for strategy consumption."""

    def __init__(self, data: dict) -> None:
        self.strike = data.get("strike", 0.0)
        self.call_ltp = data.get("call_ltp", 0.0)
        self.call_iv = data.get("call_iv", 0.0)
        self.call_delta = data.get("call_delta", 0.0)
        self.call_oi = data.get("call_oi", 0)
        self.call_volume = data.get("call_volume", 0)
        self.put_ltp = data.get("put_ltp", 0.0)
        self.put_iv = data.get("put_iv", 0.0)
        self.put_delta = data.get("put_delta", 0.0)
        self.put_oi = data.get("put_oi", 0)
        self.put_volume = data.get("put_volume", 0)
