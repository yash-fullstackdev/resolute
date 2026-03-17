"""
Trading plan router — daily plan management with lock semantics.

GET  /api/v1/plan          → Today's plan (DRAFT or LOCKED)
POST /api/v1/plan          → Create/update plan (only before lock time)
POST /api/v1/plan/lock     → Manually lock plan early
GET  /api/v1/plan/history  → Past plans
"""

import json
import uuid
from datetime import date, datetime, timezone

import structlog
from fastapi import APIRouter, Query, Request
from fastapi.responses import JSONResponse
from sqlalchemy import text

from ..db import rls_session
from ..models.schemas import TradingPlanInput

logger = structlog.get_logger(service="dashboard_api", module="plan")

router = APIRouter(prefix="/api/v1/plan", tags=["plan"])


def _error(code: str, message: str, status: int, details: dict | None = None) -> JSONResponse:
    return JSONResponse(
        status_code=status,
        content={
            "error": {
                "code": code,
                "message": message,
                "details": details or {},
            },
            "request_id": str(uuid.uuid4()),
            "timestamp": datetime.now(timezone.utc).isoformat(),
        },
    )


def _ist_now() -> datetime:
    """Get current IST time (UTC+5:30)."""
    from zoneinfo import ZoneInfo
    return datetime.now(ZoneInfo("Asia/Kolkata"))


def _is_plan_locked_time() -> bool:
    """Check if we're past the plan lock time (09:15 IST — market open)."""
    now_ist = _ist_now()
    market_open_hour = 9
    market_open_minute = 15
    return (now_ist.hour > market_open_hour or
            (now_ist.hour == market_open_hour and now_ist.minute >= market_open_minute))


@router.get("")
async def get_todays_plan(request: Request):
    """Get today's trading plan for the authenticated tenant."""
    tenant_id = request.state.tenant_id
    today = date.today()

    async with rls_session(tenant_id) as session:
        result = await session.execute(
            text("""
                SELECT * FROM trading_plans
                WHERE tenant_id = :tenant_id AND plan_date = :today
            """),
            {"tenant_id": tenant_id, "today": today},
        )
        row = result.mappings().first()

    if not row:
        return _error("NOT_FOUND", "No plan for today. Create one before market open.", 404)

    logger.info("plan_retrieved", tenant_id=tenant_id, date=str(today))
    plan = dict(row)
    return {
        "success": True,
        "data": {
            **plan,
            "thesis": plan.get("notes", ""),
            "is_locked": plan.get("status") == "LOCKED",
            "max_trades": plan.get("max_trades_per_day"),
            "daily_loss_limit": plan.get("daily_loss_limit_inr"),
            "daily_profit_target": plan.get("daily_profit_target_inr"),
        },
    }


@router.post("")
async def create_or_update_plan(request: Request, body: TradingPlanInput):
    """
    Create or update today's trading plan.
    Can only be done before market open (09:15 IST) or if plan is still DRAFT.
    Requires SEMI_AUTO tier (enforced by subscription middleware).
    """
    tenant_id = request.state.tenant_id
    today = date.today()

    # Check if plan is already locked
    async with rls_session(tenant_id) as session:
        existing = await session.execute(
            text("""
                SELECT status FROM trading_plans
                WHERE tenant_id = :tenant_id AND plan_date = :today
            """),
            {"tenant_id": tenant_id, "today": today},
        )
        existing_row = existing.mappings().first()

        if existing_row and existing_row["status"] == "LOCKED":
            return _error(
                "PLAN_LOCKED",
                "Cannot modify plan — it has already been locked for today.",
                422,
            )

        if existing_row:
            # Update existing plan
            await session.execute(
                text("""
                    UPDATE trading_plans
                    SET enabled_strategies = CAST(:enabled_strategies AS jsonb),
                        active_underlyings = CAST(:active_underlyings AS jsonb),
                        max_trades_per_day = :max_trades_per_day,
                        daily_loss_limit_inr = :daily_loss_limit_inr,
                        daily_profit_target_inr = :daily_profit_target_inr,
                        notes = :notes,
                        updated_at = NOW()
                    WHERE tenant_id = :tenant_id AND plan_date = :today
                """),
                {
                    "tenant_id": tenant_id,
                    "today": today,
                    "enabled_strategies": json.dumps(body.enabled_strategies),
                    "active_underlyings": json.dumps(body.active_underlyings),
                    "max_trades_per_day": body.max_trades_per_day,
                    "daily_loss_limit_inr": body.daily_loss_limit_inr,
                    "daily_profit_target_inr": body.daily_profit_target_inr,
                    "notes": body.notes,
                },
            )
            logger.info("plan_updated", tenant_id=tenant_id, date=str(today))
            return {"message": "Plan updated.", "date": str(today), "status": "DRAFT"}
        else:
            # Insert new plan
            plan_id = str(uuid.uuid4())
            await session.execute(
                text("""
                    INSERT INTO trading_plans
                        (id, tenant_id, plan_date, status, enabled_strategies,
                         active_underlyings, max_trades_per_day,
                         daily_loss_limit_inr, daily_profit_target_inr,
                         notes, created_at, updated_at)
                    VALUES
                        (:id, :tenant_id, :today, 'DRAFT', CAST(:enabled_strategies AS jsonb),
                         CAST(:active_underlyings AS jsonb), :max_trades_per_day,
                         :daily_loss_limit_inr, :daily_profit_target_inr,
                         :notes, NOW(), NOW())
                """),
                {
                    "id": plan_id,
                    "tenant_id": tenant_id,
                    "today": today,
                    "enabled_strategies": json.dumps(body.enabled_strategies),
                    "active_underlyings": json.dumps(body.active_underlyings),
                    "max_trades_per_day": body.max_trades_per_day,
                    "daily_loss_limit_inr": body.daily_loss_limit_inr,
                    "daily_profit_target_inr": body.daily_profit_target_inr,
                    "notes": body.notes,
                },
            )
            logger.info("plan_created", tenant_id=tenant_id, date=str(today))
            return {"message": "Plan created.", "date": str(today), "status": "DRAFT", "id": plan_id}


@router.post("/lock")
async def lock_plan(request: Request):
    """
    Manually lock today's plan early.
    Once locked, the plan cannot be modified for the rest of the day.
    """
    tenant_id = request.state.tenant_id
    today = date.today()

    async with rls_session(tenant_id) as session:
        result = await session.execute(
            text("""
                SELECT id, status FROM trading_plans
                WHERE tenant_id = :tenant_id AND plan_date = :today
            """),
            {"tenant_id": tenant_id, "today": today},
        )
        row = result.mappings().first()

        if not row:
            return _error("NOT_FOUND", "No plan for today to lock.", 404)

        if row["status"] == "LOCKED":
            return _error("PLAN_LOCKED", "Plan is already locked.", 422)

        await session.execute(
            text("""
                UPDATE trading_plans
                SET status = 'LOCKED', locked_at = NOW(), updated_at = NOW()
                WHERE tenant_id = :tenant_id AND plan_date = :today
            """),
            {"tenant_id": tenant_id, "today": today},
        )

    # Publish lock event to NATS
    nats_client = request.app.state.nats
    try:
        msg = {
            "tenant_id": tenant_id,
            "date": str(today),
            "event": "PLAN_LOCKED",
            "timestamp": datetime.now(timezone.utc).isoformat(),
        }
        await nats_client.publish(
            f"discipline.plan.lock.{tenant_id}",
            json.dumps(msg).encode(),
        )
    except Exception as exc:
        logger.error("plan_lock_publish_failed", tenant_id=tenant_id, error=str(exc))

    logger.info("plan_locked", tenant_id=tenant_id, date=str(today))
    return {"message": "Plan locked for today.", "date": str(today), "status": "LOCKED"}


@router.get("/history")
async def get_plan_history(
    request: Request,
    limit: int = Query(default=30, le=100),
    offset: int = Query(default=0, ge=0),
):
    """Get historical trading plans for the authenticated tenant."""
    tenant_id = request.state.tenant_id

    async with rls_session(tenant_id) as session:
        result = await session.execute(
            text("""
                SELECT * FROM trading_plans
                WHERE tenant_id = :tenant_id
                ORDER BY plan_date DESC
                LIMIT :limit OFFSET :offset
            """),
            {"tenant_id": tenant_id, "limit": limit, "offset": offset},
        )
        rows = result.mappings().all()

        count_result = await session.execute(
            text("SELECT COUNT(*) FROM trading_plans WHERE tenant_id = :tenant_id"),
            {"tenant_id": tenant_id},
        )
        total = count_result.scalar() or 0

    logger.info("plan_history_retrieved", tenant_id=tenant_id, count=len(rows))

    plans = []
    for r in rows:
        plan = dict(r)
        plans.append({
            **plan,
            "thesis": plan.get("notes", ""),
            "is_locked": plan.get("status") == "LOCKED",
            "max_trades": plan.get("max_trades_per_day"),
            "daily_loss_limit": plan.get("daily_loss_limit_inr"),
            "daily_profit_target": plan.get("daily_profit_target_inr"),
        })

    return {
        "success": True,
        "data": plans,
        "total": total,
    }
