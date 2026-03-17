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
    """Get the authenticated user's rolling discipline score (0–100)."""
    tenant_id = request.state.tenant_id

    async with rls_session(tenant_id) as session:
        result = await session.execute(
            text("""
                SELECT score, components, trend, last_updated
                FROM discipline_scores
                WHERE tenant_id = :tenant_id
                ORDER BY last_updated DESC
                LIMIT 1
            """),
            {"tenant_id": tenant_id},
        )
        row = result.mappings().first()

    if not row:
        return {
            "success": True,
            "data": {
                "score": 100.0,
                "components": {},
                "trend": "STABLE",
                "updated_at": datetime.now(timezone.utc).isoformat(),
            },
        }

    logger.info("discipline_score_retrieved", tenant_id=tenant_id, score=row["score"])

    return {
        "success": True,
        "data": {
            "score": float(row["score"]),
            "components": row["components"] if isinstance(row["components"], dict) else {},
            "trend": row["trend"],
            "updated_at": row["last_updated"].isoformat() if row["last_updated"] else None,
        },
    }


@router.get("/circuit-breaker")
async def get_circuit_breaker(request: Request):
    """Get the authenticated user's circuit breaker state."""
    tenant_id = request.state.tenant_id

    async with rls_session(tenant_id) as session:
        result = await session.execute(
            text("""
                SELECT is_halted, halted_at, halt_reason, reset_at,
                       daily_loss_inr, daily_loss_limit_inr
                FROM circuit_breaker_state
                WHERE tenant_id = :tenant_id
            """),
            {"tenant_id": tenant_id},
        )
        row = result.mappings().first()

    if not row:
        return {
            "success": True,
            "data": {
                "status": "ACTIVE",
                "reason": None,
                "halted_at": None,
                "resume_at": None,
                "daily_loss": 0.0,
                "daily_loss_limit": 0.0,
                "consecutive_losses": 0,
                "max_consecutive_losses": 5,
            },
        }

    return {
        "success": True,
        "data": {
            "status": "HALTED" if row["is_halted"] else "ACTIVE",
            "reason": row["halt_reason"],
            "halted_at": row["halted_at"].isoformat() if row["halted_at"] else None,
            "resume_at": row["reset_at"].isoformat() if row["reset_at"] else None,
            "daily_loss": float(row["daily_loss_inr"]),
            "daily_loss_limit": float(row["daily_loss_limit_inr"]),
            "consecutive_losses": 0,
            "max_consecutive_losses": 5,
        },
    }


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
