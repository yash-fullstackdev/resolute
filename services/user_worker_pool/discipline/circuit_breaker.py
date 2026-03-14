"""
CircuitBreaker -- enforces daily maximum loss limit.

Once triggered, NO new orders are accepted for the rest of the trading day.
There is NO override path for the circuit breaker -- this is by design.

Also supports optional daily profit target halt (user configurable).
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone

import structlog

logger = structlog.get_logger(service="user_worker_pool", module="circuit_breaker")


@dataclass
class CircuitBreakerState:
    """Current state of a user's circuit breaker."""
    user_id: str
    status: str = "ACTIVE"          # "ACTIVE" | "HALTED"
    halted_at: datetime | None = None
    halt_reason: str | None = None  # "MAX_LOSS_HIT" | "PROFIT_TARGET_HIT" | "MAX_TRADES_HIT"
    pnl_at_halt_inr: float | None = None
    trades_today: int = 0
    max_trades_reached: bool = False


class CircuitBreaker:
    """Absolute circuit breaker -- no override path.

    Once triggered, all order processing halts for the user until next
    trading day reset.
    """

    def __init__(self, nats=None) -> None:
        self._nats = nats
        self._states: dict[str, CircuitBreakerState] = {}

    def get_state(self, user_id: str) -> CircuitBreakerState:
        """Return current circuit breaker state for a user."""
        if user_id not in self._states:
            self._states[user_id] = CircuitBreakerState(user_id=user_id)
        return self._states[user_id]

    @property
    def is_halted(self) -> bool:
        """Check if any tracked user is halted (convenience for single-user context)."""
        return any(s.status == "HALTED" for s in self._states.values())

    def is_user_halted(self, user_id: str) -> bool:
        """Check if a specific user is halted."""
        state = self._states.get(user_id)
        return state is not None and state.status == "HALTED"

    async def check_and_update(
        self,
        user_id: str,
        realised_pnl_today_inr: float,
        unrealised_pnl_today_inr: float,
        locked_plan,  # LockedPlan
    ) -> CircuitBreakerState:
        """Check P&L and trade count against limits.

        Called after every fill confirmation.
        If total P&L breaches -daily_loss_limit_inr:
          - Set state to HALTED
          - Publish discipline.circuit_breaker.halt.{user_id}
          - Log the halt event

        There is NO override path. This is absolute.
        """
        state = self.get_state(user_id)

        # Already halted -- stay halted
        if state.status == "HALTED":
            return state

        total_pnl = realised_pnl_today_inr + unrealised_pnl_today_inr

        # -- Check daily loss limit (absolute, no override)
        if locked_plan and total_pnl <= -abs(locked_plan.daily_loss_limit_inr):
            state.status = "HALTED"
            state.halted_at = datetime.now(timezone.utc)
            state.halt_reason = "MAX_LOSS_HIT"
            state.pnl_at_halt_inr = total_pnl

            logger.warning(
                "circuit_breaker_halted",
                tenant_id=user_id,
                reason="MAX_LOSS_HIT",
                total_pnl=round(total_pnl, 2),
                limit=-abs(locked_plan.daily_loss_limit_inr),
            )

            await self._publish_halt(user_id, state)
            return state

        # -- Check daily profit target (optional)
        if (
            locked_plan
            and locked_plan.daily_profit_target_inr > 0
            and total_pnl >= locked_plan.daily_profit_target_inr
        ):
            state.status = "HALTED"
            state.halted_at = datetime.now(timezone.utc)
            state.halt_reason = "PROFIT_TARGET_HIT"
            state.pnl_at_halt_inr = total_pnl

            logger.info(
                "circuit_breaker_profit_target",
                tenant_id=user_id,
                reason="PROFIT_TARGET_HIT",
                total_pnl=round(total_pnl, 2),
                target=locked_plan.daily_profit_target_inr,
            )

            await self._publish_halt(user_id, state)
            return state

        # -- Check max trades per day
        if locked_plan and state.trades_today >= locked_plan.max_trades_per_day:
            state.max_trades_reached = True
            state.status = "HALTED"
            state.halted_at = datetime.now(timezone.utc)
            state.halt_reason = "MAX_TRADES_HIT"
            state.pnl_at_halt_inr = total_pnl

            logger.info(
                "circuit_breaker_max_trades",
                tenant_id=user_id,
                trades_today=state.trades_today,
                max_trades=locked_plan.max_trades_per_day,
            )

            await self._publish_halt(user_id, state)
            return state

        return state

    def increment_trade_count(self, user_id: str) -> None:
        """Increment the daily trade counter."""
        state = self.get_state(user_id)
        state.trades_today += 1

    def reset(self, user_id: str) -> None:
        """Reset circuit breaker at start of trading day (09:00 IST).

        Resets HALTED -> ACTIVE.
        """
        self._states[user_id] = CircuitBreakerState(user_id=user_id)
        logger.info("circuit_breaker_reset", tenant_id=user_id)

    async def _publish_halt(self, user_id: str, state: CircuitBreakerState) -> None:
        """Publish halt event to NATS."""
        if self._nats is None:
            return
        try:
            await self._nats.publish(
                f"discipline.circuit_breaker.halt.{user_id}",
                {
                    "user_id": user_id,
                    "status": state.status,
                    "halt_reason": state.halt_reason,
                    "halted_at": state.halted_at.isoformat() if state.halted_at else None,
                    "pnl_at_halt_inr": state.pnl_at_halt_inr,
                    "trades_today": state.trades_today,
                },
            )
        except Exception as exc:
            logger.error("circuit_breaker_publish_failed", error=str(exc))
