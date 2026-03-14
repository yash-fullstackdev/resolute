"""
User router — current user profile and dashboard stats.

GET /api/v1/user/me          → Current user profile from JWT + DB
GET /api/v1/dashboard/stats  → Overview stats for dashboard
"""

import uuid
from datetime import datetime, timezone

import structlog
from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse
from sqlalchemy import text

from ..db import rls_session

logger = structlog.get_logger(service="dashboard_api", module="user")

router = APIRouter(prefix="/api/v1", tags=["user"])


@router.get("/user/me")
async def get_current_user(request: Request):
    tenant_id = request.state.tenant_id
    email = getattr(request.state, "email", "")
    tier = getattr(request.state, "tier", "STARTER")

    try:
        async with rls_session(tenant_id) as session:
            result = await session.execute(
                text("SELECT name, is_active, email_verified, created_at FROM tenants WHERE id = :tid"),
                {"tid": tenant_id},
            )
            tenant = result.fetchone()
    except Exception:
        tenant = None

    return {
        "id": tenant_id,
        "tenant_id": tenant_id,
        "email": email,
        "full_name": tenant.name if tenant else "",
        "capital_tier": tier,
        "is_active": tenant.is_active if tenant else True,
        "is_verified": tenant.email_verified if tenant else True,
        "broker_connected": False,
        "created_at": tenant.created_at.isoformat() if tenant else "",
        "updated_at": "",
    }


@router.put("/user/profile")
async def update_profile(request: Request):
    """Update user profile (full_name)."""
    from pydantic import BaseModel, Field

    class ProfileUpdate(BaseModel):
        full_name: str = Field(min_length=1, max_length=200)

    body_bytes = await request.body()
    body = ProfileUpdate.model_validate_json(body_bytes)
    tenant_id = request.state.tenant_id

    try:
        async with rls_session(tenant_id) as session:
            await session.execute(
                text("UPDATE tenants SET name = :name, updated_at = NOW() WHERE id = :tid"),
                {"name": body.full_name, "tid": tenant_id},
            )
    except Exception as exc:
        logger.warning("profile_update_failed", tenant_id=tenant_id, error=str(exc))

    return {"success": True, "data": {"message": "Profile updated"}}


@router.get("/dashboard/stats")
async def dashboard_stats(request: Request):
    tenant_id = request.state.tenant_id

    stats = {
        "total_pnl": 0.0,
        "today_pnl": 0.0,
        "open_positions": 0,
        "total_trades": 0,
        "win_rate": 0.0,
        "discipline_score": 100,
        "active_strategies": 0,
        "circuit_breaker": "ACTIVE",
    }

    try:
        async with rls_session(tenant_id) as session:
            pos_result = await session.execute(
                text("SELECT COUNT(*) FROM positions WHERE status = 'OPEN'")
            )
            stats["open_positions"] = pos_result.scalar() or 0

            trades_result = await session.execute(
                text("SELECT COUNT(*) FROM orders")
            )
            stats["total_trades"] = trades_result.scalar() or 0
    except Exception as exc:
        logger.warning("dashboard_stats_partial_failure", error=str(exc))

    return {"success": True, "data": stats}
