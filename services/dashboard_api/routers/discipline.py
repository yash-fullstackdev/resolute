"""
Discipline router — score, circuit breaker, overrides.

GET  /api/v1/discipline/score             → Rolling discipline score (0–100)
GET  /api/v1/discipline/circuit-breaker   → Circuit breaker state
GET  /api/v1/discipline/overrides         → Override history
POST /api/v1/discipline/override          → Request override (60s cooldown)
POST /api/v1/discipline/override/{id}/confirm → Confirm after cooldown
"""

import json
import uuid
from datetime import datetime, timedelta, timezone

import structlog
from fastapi import APIRouter, Query, Request
from fastapi.responses import JSONResponse
from sqlalchemy import text

from ..db import rls_session
from ..models.schemas import OverrideRequestInput

logger = structlog.get_logger(service="dashboard_api", module="discipline")

router = APIRouter(prefix="/api/v1/discipline", tags=["discipline"])

# Cooldown period in seconds before an override can be confirmed
OVERRIDE_COOLDOWN_SECONDS = 60


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


@router.get("/score")
async def get_discipline_score(request: Request):
    """Get the authenticated user's rolling discipline score (0–100).

    Computes from trade_journal if available, otherwise returns default perfect score.
    """
    tenant_id = request.state.tenant_id

    default_response = {
        "success": True,
        "data": {
            "score": 100.0,
            "components": {
                "plan_adherence": 100.0,
                "stop_loss_respected": 100.0,
                "time_stop_respected": 100.0,
                "override_penalty": 0.0,
            },
            "trend": "STABLE",
            "updated_at": datetime.now(timezone.utc).isoformat(),
        },
    }

    try:
        async with rls_session(tenant_id) as session:
            # Use trade_journal to compute rolling 30-day discipline score
            result = await session.execute(
                text("""
                    SELECT
                        COALESCE(AVG(discipline_score), 100.0) AS score,
                        COUNT(*) AS total_trades,
                        COUNT(*) FILTER (WHERE discipline_score >= 75) AS disciplined,
                        COUNT(*) FILTER (WHERE discipline_score < 50) AS undisciplined,
                        MAX(entry_time) AS last_trade
                    FROM trade_journal
                    WHERE tenant_id = :tenant_id
                      AND trade_date >= CURRENT_DATE - INTERVAL '30 days'
                """),
                {"tenant_id": tenant_id},
            )
            row = result.mappings().first()

        if not row or row["total_trades"] == 0:
            return default_response

        total = row["total_trades"]
        score = float(row["score"])
        disciplined = row["disciplined"]
        undisciplined = row["undisciplined"]

        # Determine trend from recent vs older scores
        trend = "STABLE"
        if total >= 5:
            if disciplined / total >= 0.7:
                trend = "IMPROVING"
            elif undisciplined / total >= 0.4:
                trend = "DECLINING"

        logger.info("discipline_score_retrieved", tenant_id=tenant_id, score=score)

        return {
            "success": True,
            "data": {
                "score": round(score, 1),
                "components": {
                    "total_trades": total,
                    "disciplined_trades": disciplined,
                    "undisciplined_trades": undisciplined,
                },
                "trend": trend,
                "updated_at": row["last_trade"].isoformat() if row["last_trade"] else datetime.now(timezone.utc).isoformat(),
            },
        }
    except Exception as exc:
        logger.warning("discipline_score_query_failed", tenant_id=tenant_id, error=str(exc))
        return default_response


@router.get("/circuit-breaker")
async def get_circuit_breaker(request: Request):
    """Get the authenticated user's circuit breaker state.

    Derives state from circuit_breaker_events log and trading_plans limits.
    """
    tenant_id = request.state.tenant_id

    default_response = {
        "success": True,
        "data": {
            "status": "ACTIVE",
            "reason": None,
            "halted_at": None,
            "resume_at": None,
            "daily_loss": 0.0,
            "daily_loss_limit": 5000.0,
            "consecutive_losses": 0,
            "max_consecutive_losses": 5,
        },
    }

    try:
        async with rls_session(tenant_id) as session:
            # Check most recent circuit breaker event today
            event_result = await session.execute(
                text("""
                    SELECT event_type, trigger_reason, pnl_at_event_inr,
                           trades_at_event, event_time
                    FROM circuit_breaker_events
                    WHERE tenant_id = :tenant_id
                      AND event_time >= CURRENT_DATE
                    ORDER BY event_time DESC
                    LIMIT 1
                """),
                {"tenant_id": tenant_id},
            )
            event_row = event_result.mappings().first()

            # Get daily loss limit from trading plan
            plan_result = await session.execute(
                text("""
                    SELECT daily_loss_limit_inr
                    FROM trading_plans
                    WHERE tenant_id = :tenant_id
                    ORDER BY created_at DESC
                    LIMIT 1
                """),
                {"tenant_id": tenant_id},
            )
            plan_row = plan_result.mappings().first()

        daily_loss_limit = float(plan_row["daily_loss_limit_inr"]) if plan_row else 5000.0

        if not event_row:
            default_response["data"]["daily_loss_limit"] = daily_loss_limit
            return default_response

        is_halted = event_row["event_type"] in ("HALT", "MAX_LOSS_HIT", "MAX_TRADES_HIT", "PROFIT_TARGET_HIT")

        return {
            "success": True,
            "data": {
                "status": "HALTED" if is_halted else "ACTIVE",
                "reason": event_row["trigger_reason"],
                "halted_at": event_row["event_time"].isoformat() if is_halted and event_row["event_time"] else None,
                "resume_at": None,
                "daily_loss": abs(float(event_row["pnl_at_event_inr"] or 0)),
                "daily_loss_limit": daily_loss_limit,
                "consecutive_losses": 0,
                "max_consecutive_losses": 5,
            },
        }
    except Exception as exc:
        logger.warning("circuit_breaker_query_failed", tenant_id=tenant_id, error=str(exc))
        return default_response


@router.get("/overrides")
async def list_overrides(
    request: Request,
    limit: int = Query(default=50, le=200),
    offset: int = Query(default=0, ge=0),
):
    """Get override history for the authenticated user."""
    tenant_id = request.state.tenant_id

    async with rls_session(tenant_id) as session:
        result = await session.execute(
            text("""
                SELECT * FROM override_audit_log
                WHERE tenant_id = :tenant_id
                ORDER BY created_at DESC
                LIMIT :limit OFFSET :offset
            """),
            {"tenant_id": tenant_id, "limit": limit, "offset": offset},
        )
        rows = result.mappings().all()

        count_result = await session.execute(
            text("SELECT COUNT(*) FROM override_audit_log WHERE tenant_id = :tenant_id"),
            {"tenant_id": tenant_id},
        )
        total = count_result.scalar() or 0

    return {
        "success": True,
        "data": [dict(r) for r in rows],
    }


@router.post("/override")
async def request_override(request: Request, body: OverrideRequestInput):
    """
    Request a discipline override (starts a 60-second cooldown).
    The user must confirm the override after the cooldown expires.
    Requires SEMI_AUTO tier (enforced by subscription middleware).
    """
    tenant_id = request.state.tenant_id

    # Verify position belongs to tenant
    async with rls_session(tenant_id) as session:
        pos_result = await session.execute(
            text("""
                SELECT id FROM positions
                WHERE id = :position_id AND tenant_id = :tenant_id AND status = 'OPEN'
            """),
            {"position_id": body.position_id, "tenant_id": tenant_id},
        )
        if not pos_result.first():
            return _error("NOT_FOUND", f"Open position {body.position_id} not found.", 404)

        # Check for existing pending override on this position
        existing = await session.execute(
            text("""
                SELECT id FROM override_audit_log
                WHERE tenant_id = :tenant_id
                  AND position_id = :position_id
                  AND status IN ('PENDING_COOLDOWN', 'AWAITING_CONFIRM')
            """),
            {"tenant_id": tenant_id, "position_id": body.position_id},
        )
        if existing.first():
            return _error(
                "DISCIPLINE_REJECTED",
                "An override is already pending for this position.",
                422,
            )

        # Create override record
        override_id = str(uuid.uuid4())
        cooldown_expires = datetime.now(timezone.utc) + timedelta(seconds=OVERRIDE_COOLDOWN_SECONDS)

        await session.execute(
            text("""
                INSERT INTO override_audit_log
                    (id, tenant_id, position_id, override_type, proposed_value,
                     reason, status, cooldown_expires_at, created_at)
                VALUES
                    (:id, :tenant_id, :position_id, :override_type, :proposed_value,
                     :reason, 'PENDING_COOLDOWN', :cooldown_expires_at, NOW())
            """),
            {
                "id": override_id,
                "tenant_id": tenant_id,
                "position_id": body.position_id,
                "override_type": body.override_type,
                "proposed_value": body.proposed_value,
                "reason": body.reason,
                "cooldown_expires_at": cooldown_expires,
            },
        )

    # Publish override request to NATS
    nats_client = request.app.state.nats
    try:
        msg = {
            "override_id": override_id,
            "tenant_id": tenant_id,
            "position_id": body.position_id,
            "override_type": body.override_type,
            "event": "OVERRIDE_REQUESTED",
            "timestamp": datetime.now(timezone.utc).isoformat(),
        }
        await nats_client.publish(
            f"discipline.override.request.{tenant_id}.{body.position_id}",
            json.dumps(msg).encode(),
        )
    except Exception as exc:
        logger.error("override_request_publish_failed", tenant_id=tenant_id, error=str(exc))

    logger.info(
        "override_requested",
        tenant_id=tenant_id,
        override_id=override_id,
        position_id=body.position_id,
    )

    return {
        "id": override_id,
        "status": "PENDING_COOLDOWN",
        "cooldown_expires_at": cooldown_expires.isoformat(),
        "message": f"Override requested. Please confirm after {OVERRIDE_COOLDOWN_SECONDS}s cooldown.",
    }


@router.post("/override/{override_id}/confirm")
async def confirm_override(request: Request, override_id: str):
    """
    Confirm an override after the cooldown period has elapsed.
    """
    tenant_id = request.state.tenant_id

    async with rls_session(tenant_id) as session:
        result = await session.execute(
            text("""
                SELECT id, status, cooldown_expires_at, position_id, override_type
                FROM override_audit_log
                WHERE id = :override_id AND tenant_id = :tenant_id
            """),
            {"override_id": override_id, "tenant_id": tenant_id},
        )
        row = result.mappings().first()

        if not row:
            return _error("NOT_FOUND", f"Override {override_id} not found.", 404)

        if row["status"] not in ("PENDING_COOLDOWN", "AWAITING_CONFIRM"):
            return _error(
                "DISCIPLINE_REJECTED",
                f"Override is in '{row['status']}' state and cannot be confirmed.",
                422,
            )

        # Check cooldown
        cooldown_expires = row["cooldown_expires_at"]
        if cooldown_expires and datetime.now(timezone.utc) < cooldown_expires.replace(tzinfo=timezone.utc):
            remaining = (cooldown_expires.replace(tzinfo=timezone.utc) - datetime.now(timezone.utc)).seconds
            return _error(
                "DISCIPLINE_REJECTED",
                f"Cooldown not yet expired. {remaining}s remaining.",
                422,
                details={"remaining_seconds": remaining},
            )

        # Confirm the override
        await session.execute(
            text("""
                UPDATE override_audit_log
                SET status = 'CONFIRMED', confirmed_at = NOW()
                WHERE id = :override_id AND tenant_id = :tenant_id
            """),
            {"override_id": override_id, "tenant_id": tenant_id},
        )

    # Publish confirmation to NATS
    nats_client = request.app.state.nats
    try:
        msg = {
            "override_id": override_id,
            "tenant_id": tenant_id,
            "position_id": row["position_id"],
            "override_type": row["override_type"],
            "event": "OVERRIDE_CONFIRMED",
            "timestamp": datetime.now(timezone.utc).isoformat(),
        }
        await nats_client.publish(
            f"discipline.override.approved.{tenant_id}.{row['position_id']}",
            json.dumps(msg).encode(),
        )
    except Exception as exc:
        logger.error("override_confirm_publish_failed", tenant_id=tenant_id, error=str(exc))

    logger.info(
        "override_confirmed",
        tenant_id=tenant_id,
        override_id=override_id,
    )

    return {
        "id": override_id,
        "status": "CONFIRMED",
        "message": "Override confirmed and applied.",
    }
