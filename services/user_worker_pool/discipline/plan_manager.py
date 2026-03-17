"""
PlanManager -- manages the daily trading plan lifecycle.

Plan states:
  DRAFT    -> user can modify strategy selection and parameters
  LOCKED   -> market is open, no modifications allowed
  EXPIRED  -> plan expired at end of day, archived

Lock time: configurable per user, default 09:10 IST.
Unlock: only possible after 15:35 IST (NSE close) or 23:30 IST (MCX close).
"""

from __future__ import annotations

import hashlib
import json
import uuid
from dataclasses import dataclass, field, asdict
from datetime import date, datetime, timezone

import structlog

logger = structlog.get_logger(service="user_worker_pool", module="plan_manager")


@dataclass
class TradingPlan:
    """A user's daily trading plan."""
    user_id: str
    date: date
    enabled_strategies: list[str]     # e.g. ["long_call", "pcr_contrarian"]
    active_underlyings: list[str]     # e.g. ["NIFTY", "BANKNIFTY"]
    max_trades_per_day: int           # Hard cap on number of trades
    daily_loss_limit_inr: float       # Circuit-breaker threshold
    daily_profit_target_inr: float    # Optional halt on target hit
    notes: str                        # User's pre-market thesis (free text)


@dataclass
class LockedPlan(TradingPlan):
    """Immutable locked plan with tamper-detection hash."""
    locked_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    plan_hash: str = ""
    status: str = "LOCKED"  # "LOCKED" | "EXPIRED" | "HALTED"


class PlanManager:
    """Manages daily trading plan lifecycle for a single user."""

    def __init__(self, db=None, nats=None) -> None:
        self._db = db
        self._nats = nats
        self._active_plans: dict[str, LockedPlan] = {}  # user_id -> LockedPlan

    def lock_plan(self, user_id: str, plan: TradingPlan) -> LockedPlan:
        """Lock the plan at market open time.

        Persists to DB, publishes NATS event, returns immutable LockedPlan.
        """
        # Compute plan hash for tamper detection
        plan_data = {
            "user_id": plan.user_id,
            "date": plan.date.isoformat(),
            "enabled_strategies": sorted(plan.enabled_strategies),
            "active_underlyings": sorted(plan.active_underlyings),
            "max_trades_per_day": plan.max_trades_per_day,
            "daily_loss_limit_inr": plan.daily_loss_limit_inr,
            "daily_profit_target_inr": plan.daily_profit_target_inr,
            "notes": plan.notes,
        }
        plan_json = json.dumps(plan_data, sort_keys=True)
        plan_hash = hashlib.sha256(plan_json.encode()).hexdigest()

        locked = LockedPlan(
            user_id=plan.user_id,
            date=plan.date,
            enabled_strategies=list(plan.enabled_strategies),
            active_underlyings=list(plan.active_underlyings),
            max_trades_per_day=plan.max_trades_per_day,
            daily_loss_limit_inr=plan.daily_loss_limit_inr,
            daily_profit_target_inr=plan.daily_profit_target_inr,
            notes=plan.notes,
            locked_at=datetime.now(timezone.utc),
            plan_hash=plan_hash,
            status="LOCKED",
        )

        self._active_plans[user_id] = locked

        logger.info(
            "plan_locked",
            tenant_id=user_id,
            strategies=locked.enabled_strategies,
            underlyings=locked.active_underlyings,
            plan_hash=plan_hash[:12],
        )

        return locked

    async def persist_plan(self, locked_plan: LockedPlan) -> None:
        """Persist the locked plan to the database."""
        if self._db is None:
            return

        try:
            await self._db.execute(
                """
                INSERT INTO trading_plans
                    (tenant_id, plan_date, enabled_strategies, active_underlyings,
                     max_trades_per_day, daily_loss_limit_inr, daily_profit_target_inr,
                     notes, locked_at, plan_hash, status)
                VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11)
                ON CONFLICT (tenant_id, plan_date) DO UPDATE SET
                    enabled_strategies = EXCLUDED.enabled_strategies,
                    active_underlyings = EXCLUDED.active_underlyings,
                    locked_at = EXCLUDED.locked_at,
                    plan_hash = EXCLUDED.plan_hash,
                    status = EXCLUDED.status
                """,
                locked_plan.user_id,
                locked_plan.date,
                locked_plan.enabled_strategies,
                locked_plan.active_underlyings,
                locked_plan.max_trades_per_day,
                locked_plan.daily_loss_limit_inr,
                locked_plan.daily_profit_target_inr,
                locked_plan.notes,
                locked_plan.locked_at,
                locked_plan.plan_hash,
                locked_plan.status,
                tenant_id=locked_plan.user_id,
            )
        except Exception as exc:
            logger.error("plan_persist_failed", tenant_id=locked_plan.user_id, error=str(exc))

    async def publish_lock_event(self, locked_plan: LockedPlan) -> None:
        """Publish plan lock event to NATS."""
        if self._nats is None:
            return
        await self._nats.publish(
            f"discipline.plan.lock.{locked_plan.user_id}",
            {
                "user_id": locked_plan.user_id,
                "plan_hash": locked_plan.plan_hash,
                "locked_at": locked_plan.locked_at.isoformat(),
                "strategies": locked_plan.enabled_strategies,
            },
        )

    def validate_order_against_plan(
        self,
        order,  # Order dataclass
        locked_plan: LockedPlan,
    ) -> tuple[bool, str]:
        """Validate an order against the locked plan.

        Checks:
        1. Strategy is in the locked plan's enabled strategies
        2. Underlying is in the locked plan's active underlyings
        3. Order has a stop_loss_price set (non-null, non-zero)
        4. Order has a target_price set (non-null, non-zero)
        5. Order has a time_stop set and it is in the future
        """
        # Check 1: Strategy in plan
        if order.strategy_name not in locked_plan.enabled_strategies:
            return False, (
                f"Strategy '{order.strategy_name}' is not in today's locked plan. "
                f"Allowed: {locked_plan.enabled_strategies}"
            )

        # Check 2: Underlying in plan
        if order.underlying not in locked_plan.active_underlyings:
            return False, (
                f"Underlying '{order.underlying}' is not in today's locked plan. "
                f"Allowed: {locked_plan.active_underlyings}"
            )

        # Check 3: Stop loss set
        if not order.stop_loss_price or order.stop_loss_price <= 0:
            return False, (
                "Order rejected: stop_loss_price must be set and > 0"
            )

        # Check 4: Target price set
        if not order.target_price or order.target_price <= 0:
            return False, (
                "Order rejected: target_price must be set and > 0"
            )

        # Check 5: Time stop set and in the future
        now = datetime.now(timezone.utc)
        if not order.time_stop:
            return False, (
                "Order rejected: time_stop must be set"
            )
        if order.time_stop <= now:
            return False, (
                f"Order rejected: time_stop ({order.time_stop.isoformat()}) "
                f"must be in the future"
            )

        logger.debug(
            "order_validated_against_plan",
            tenant_id=order.tenant_id,
            strategy=order.strategy_name,
            underlying=order.underlying,
        )
        return True, ""

    def get_active_plan(self, user_id: str) -> LockedPlan | None:
        """Return the current active locked plan for a user, or None."""
        plan = self._active_plans.get(user_id)
        if plan is None:
            return None
        # Check if plan is still for today
        if plan.date != date.today():
            # Plan expired
            plan.status = "EXPIRED"
            return None
        if plan.status == "EXPIRED":
            return None
        return plan

    async def load_plan_from_db(self, user_id: str) -> LockedPlan | None:
        """Load today's plan from the database for a user."""
        if self._db is None:
            return None

        try:
            row = await self._db.fetchrow(
                """
                SELECT tenant_id, plan_date, enabled_strategies, active_underlyings,
                       max_trades_per_day, daily_loss_limit_inr, daily_profit_target_inr,
                       notes, locked_at, plan_hash, status
                FROM trading_plans
                WHERE tenant_id = $1 AND plan_date = CURRENT_DATE AND status = 'LOCKED'
                """,
                user_id,
                tenant_id=user_id,
            )
            if row is None:
                return None

            locked = LockedPlan(
                user_id=str(row["tenant_id"]),
                date=row["plan_date"],
                enabled_strategies=list(row["enabled_strategies"]),
                active_underlyings=list(row["active_underlyings"]),
                max_trades_per_day=row["max_trades_per_day"],
                daily_loss_limit_inr=float(row["daily_loss_limit_inr"]),
                daily_profit_target_inr=float(row["daily_profit_target_inr"]),
                notes=row["notes"] or "",
                locked_at=row["locked_at"],
                plan_hash=row["plan_hash"],
                status=row["status"],
            )
            self._active_plans[user_id] = locked
            return locked

        except Exception as exc:
            logger.error("plan_load_failed", tenant_id=user_id, error=str(exc))
            return None

    def expire_plan(self, user_id: str) -> None:
        """Mark the active plan as expired."""
        plan = self._active_plans.get(user_id)
        if plan:
            plan.status = "EXPIRED"
            logger.info("plan_expired", tenant_id=user_id)
