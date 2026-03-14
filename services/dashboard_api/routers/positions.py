"""
Positions router — open/closed position management.

GET  /api/v1/positions         → List positions for this tenant
GET  /api/v1/positions/{id}    → Single position (must belong to tenant)
DELETE /api/v1/positions/{id}  → Manual close → publish exit signal to NATS (FULL_AUTO)
"""

import uuid
from datetime import datetime, timezone

import structlog
from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse
from sqlalchemy import text

from ..db import rls_session

logger = structlog.get_logger(service="dashboard_api", module="positions")

router = APIRouter(prefix="/api/v1/positions", tags=["positions"])


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


@router.get("")
async def list_positions(
    request: Request,
    status: str | None = None,
    underlying: str | None = None,
    strategy_name: str | None = None,
    limit: int = 50,
    offset: int = 0,
):
    """List all positions for the authenticated tenant, with optional filters."""
    tenant_id = request.state.tenant_id

    async with rls_session(tenant_id) as session:
        # Build dynamic query
        query = "SELECT * FROM positions WHERE tenant_id = :tenant_id"
        params: dict = {"tenant_id": tenant_id}

        if status:
            query += " AND status = :status"
            params["status"] = status

        if underlying:
            query += " AND underlying = :underlying"
            params["underlying"] = underlying

        if strategy_name:
            query += " AND strategy = :strategy_name"
            params["strategy_name"] = strategy_name

        query += " ORDER BY entry_time DESC LIMIT :limit OFFSET :offset"
        params["limit"] = min(limit, 200)
        params["offset"] = offset

        result = await session.execute(text(query), params)
        rows = result.mappings().all()

    def _map_position(r: dict) -> dict:
        legs = r.get("legs") or []
        return {
            "id": str(r["id"]),
            "tenant_id": str(r["tenant_id"]),
            "strategy_name": r.get("strategy", ""),
            "underlying": r.get("underlying", ""),
            "direction": r.get("segment", ""),
            "status": r.get("status", "OPEN"),
            "legs": legs if isinstance(legs, list) else [],
            "entry_time": r["entry_time"].isoformat() if r.get("entry_time") else "",
            "exit_time": r["exit_time"].isoformat() if r.get("exit_time") else None,
            "total_pnl": r.get("realised_pnl_inr") or 0.0,
            "total_pnl_pct": 0.0,
            "unrealized_pnl": 0.0 if r.get("status") == "OPEN" else (r.get("realised_pnl_inr") or 0.0),
            "realized_pnl": r.get("realised_pnl_inr") or 0.0,
            "stop_loss": r.get("stop_loss_price"),
            "target": r.get("target_price"),
            "capital_deployed": r.get("entry_cost_inr") or 0.0,
            "max_drawdown": 0.0,
            "created_at": r["entry_time"].isoformat() if r.get("entry_time") else "",
            "updated_at": r["exit_time"].isoformat() if r.get("exit_time") else "",
        }

    logger.info("positions_listed", tenant_id=tenant_id, count=len(rows))

    return {
        "success": True,
        "data": [_map_position(dict(r)) for r in rows],
    }


@router.get("/{position_id}")
async def get_position(request: Request, position_id: str):
    """Get a single position by ID (must belong to the authenticated tenant)."""
    tenant_id = request.state.tenant_id

    async with rls_session(tenant_id) as session:
        result = await session.execute(
            text("SELECT * FROM positions WHERE id = :id AND tenant_id = :tenant_id"),
            {"id": position_id, "tenant_id": tenant_id},
        )
        row = result.mappings().first()

    if not row:
        return _error(
            "NOT_FOUND",
            f"Position {position_id} not found.",
            404,
        )

    logger.info("position_retrieved", tenant_id=tenant_id, position_id=position_id)
    return dict(row)


@router.delete("/{position_id}")
async def close_position(request: Request, position_id: str):
    """
    Manually close a position by publishing an exit signal to NATS.
    Requires FULL_AUTO tier (enforced by subscription middleware).
    """
    tenant_id = request.state.tenant_id

    # Verify position exists and belongs to tenant
    async with rls_session(tenant_id) as session:
        result = await session.execute(
            text(
                "SELECT * FROM positions WHERE id = :id AND tenant_id = :tenant_id AND status = 'OPEN'"
            ),
            {"id": position_id, "tenant_id": tenant_id},
        )
        row = result.mappings().first()

    if not row:
        return _error(
            "NOT_FOUND",
            f"Open position {position_id} not found.",
            404,
        )

    # Publish exit signal to NATS
    nats_client = request.app.state.nats
    subject = f"signals.{tenant_id}.exit.{position_id}"
    exit_signal = {
        "id": str(uuid.uuid4()),
        "tenant_id": tenant_id,
        "position_id": position_id,
        "direction": "EXIT",
        "reason": "MANUAL_CLOSE",
        "timestamp": datetime.now(timezone.utc).isoformat(),
    }

    import json
    try:
        await nats_client.publish(subject, json.dumps(exit_signal).encode())
        logger.info(
            "exit_signal_published",
            tenant_id=tenant_id,
            position_id=position_id,
            subject=subject,
        )
    except Exception as exc:
        logger.error(
            "exit_signal_publish_failed",
            tenant_id=tenant_id,
            position_id=position_id,
            error=str(exc),
        )
        return _error(
            "SERVICE_UNAVAILABLE",
            "Failed to publish exit signal. Please try again.",
            503,
        )

    return {
        "message": f"Exit signal published for position {position_id}.",
        "position_id": position_id,
        "signal_id": exit_signal["id"],
    }
