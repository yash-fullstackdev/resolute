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

logger = structlog.get_logger(service="user_worker_pool", module="worker")

# IST offsets for market hours checks
NSE_OPEN_UTC = _time(3, 45)    # 09:15 IST
NSE_CLOSE_UTC = _time(10, 0)   # 15:30 IST
MCX_OPEN_UTC = _time(3, 30)    # 09:00 IST
MCX_CLOSE_UTC = _time(18, 0)   # 23:30 IST


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

    async def run(self) -> None:
        """Main event loop.

        1. Subscribe to chain.nse.* and chain.mcx.* subjects
        2. Subscribe to fills.{tenant_id}.*
        3. Subscribe to discipline.override.request.{tenant_id}.*
        4. Process messages until stopped
        """
        self._running = True
        self._log.info("worker_starting", strategies=[s.name for s in self.strategies])

        # Subscribe to chain updates
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

        self._log.info("worker_started")

        # Keep running until stopped
        while self._running:
            await asyncio.sleep(1)

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

        # (c) Evaluate each strategy
        for strategy in self.strategies:
            # Capital tier enforcement
            if not is_strategy_allowed(strategy.category, self._capital_tier):
                continue

            # Check if strategy's segment matches
            chain_segment = "MCX" if segment == "mcx" else "NSE_INDEX"
            if chain_segment not in strategy.allowed_segments:
                continue

            # Instrument filter — only run if user subscribed this underlying
            strategy_instruments = self.config.get_strategy_instruments(strategy.name)
            if strategy_instruments and chain.underlying not in strategy_instruments:
                continue

            strategy_config = self.config.get_strategy_config(strategy.name)

            try:
                signal = strategy.evaluate(
                    chain,
                    regime,
                    self.portfolio.open_positions,
                    strategy_config,
                )
            except Exception as exc:
                self._log.error(
                    "strategy_evaluate_error",
                    strategy=strategy.name,
                    error=str(exc),
                )
                continue

            if signal is None:
                continue

            # (d) Route signal based on type
            if signal.signal_type == "DIRECT":
                # Informational price signal — no options chain, no order placed
                await self._publish_direct_signal(signal)
            else:
                await self._process_signal(signal, strategy, chain)

        # (e) Check exits for open positions
        await self._check_exits(chain)

    async def _publish_direct_signal(self, signal: Signal) -> None:
        """Publish a DIRECT (no-options) signal to NATS — informational only, no order."""
        await self._nats.publish(
            f"signals.{self.tenant_id}.{signal.strategy_name}.{signal.underlying}",
            {
                "signal_type": "DIRECT",
                "signal": signal.strategy_name,
                "underlying": signal.underlying,
                "direction": signal.direction,
                "entry_price": signal.entry_price,
                "stop_loss_price": signal.stop_loss_price,
                "target_price": signal.target_price,
                "stop_loss_pct": signal.stop_loss_pct,
                "target_pct": signal.target_pct,
                "confidence": signal.confidence,
                "metadata": signal.metadata,
            },
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
