"""
User subscription management router.

GET  /subscription       — Current plan, status, expiry
POST /subscription/upgrade — Upgrade tier
GET  /subscription/tiers — Available tiers and features
"""

from datetime import datetime, timezone

import structlog
from fastapi import APIRouter, Depends, Request
from fastapi.responses import JSONResponse
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from ..core.jwt import validate_access_token
from ..core.subscription import Tier, can_upgrade, get_all_tiers
from ..db import get_session
from ..models.schemas import (
    ErrorDetail,
    ErrorResponse,
    SubscriptionResponse,
    SubscriptionUpgradeRequest,
    SubscriptionUpgradeResponse,
    TierInfo,
    TiersResponse,
)

logger = structlog.get_logger(service="auth_service")
router = APIRouter(prefix="/subscription", tags=["subscription"])


def _error_response(status_code: int, code: str, message: str, details: dict | None = None):
    body = ErrorResponse(
        error=ErrorDetail(code=code, message=message, details=details or {}),
        timestamp=datetime.now(timezone.utc),
    )
    return JSONResponse(status_code=status_code, content=body.model_dump(mode="json"))


async def _get_tenant_from_token(request: Request) -> dict | None:
    auth_header = request.headers.get("Authorization", "")
    if not auth_header.startswith("Bearer "):
        return None
    token = auth_header[7:]
    redis = request.app.state.redis
    try:
        claims = await validate_access_token(redis, token)
        return claims
    except Exception:
        return None


# ── GET /subscription ────────────────────────────────────────────────────────


@router.get("", response_model=SubscriptionResponse)
async def get_subscription(
    request: Request,
    session: AsyncSession = Depends(get_session),
):
    claims = await _get_tenant_from_token(request)
    if not claims:
        return _error_response(401, "UNAUTHORIZED", "Missing or invalid access token.")

    tenant_id = claims["sub"]

    result = await session.execute(
        text(
            """
            SELECT subscription_tier, subscription_status, trial_ends_at, subscription_ends_at
            FROM tenants WHERE id = :tid
            """
        ),
        {"tid": tenant_id},
    )
    row = result.fetchone()

    if not row:
        return _error_response(404, "NOT_FOUND", "Tenant not found.")

    return SubscriptionResponse(
        tier=row.subscription_tier,
        status=row.subscription_status,
        trial_ends_at=row.trial_ends_at,
        subscription_ends_at=row.subscription_ends_at,
    )


# ── POST /subscription/upgrade ───────────────────────────────────────────────


@router.post("/upgrade", response_model=SubscriptionUpgradeResponse)
async def upgrade_subscription(
    request: Request,
    body: SubscriptionUpgradeRequest,
    session: AsyncSession = Depends(get_session),
):
    claims = await _get_tenant_from_token(request)
    if not claims:
        return _error_response(401, "UNAUTHORIZED", "Missing or invalid access token.")

    tenant_id = claims["sub"]

    result = await session.execute(
        text(
            """
            SELECT subscription_tier, subscription_status
            FROM tenants WHERE id = :tid
            """
        ),
        {"tid": tenant_id},
    )
    row = result.fetchone()

    if not row:
        return _error_response(404, "NOT_FOUND", "Tenant not found.")

    try:
        current_tier = Tier(row.subscription_tier)
        target_tier = Tier(body.target_tier)
    except ValueError:
        return _error_response(400, "VALIDATION_ERROR", "Invalid tier specified.")

    if not can_upgrade(current_tier, target_tier):
        return _error_response(
            400, "VALIDATION_ERROR",
            f"Cannot upgrade from {current_tier.value} to {target_tier.value}. "
            "Target tier must be higher than current tier.",
            {"current_tier": current_tier.value, "target_tier": target_tier.value},
        )

    # In production, this would integrate with a payment gateway (Razorpay, Stripe, etc.)
    # For now, we perform the upgrade directly.
    await session.execute(
        text(
            """
            UPDATE tenants
            SET subscription_tier = :tier, subscription_status = 'ACTIVE', updated_at = NOW()
            WHERE id = :tid
            """
        ),
        {"tid": tenant_id, "tier": target_tier.value},
    )
    await session.commit()

    logger.info(
        "subscription_upgraded",
        tenant_id=tenant_id,
        from_tier=current_tier.value,
        to_tier=target_tier.value,
    )

    return SubscriptionUpgradeResponse(
        tier=target_tier.value,
        status="ACTIVE",
        message=f"Successfully upgraded to {target_tier.value}.",
    )


# ── GET /subscription/tiers ──────────────────────────────────────────────────


@router.get("/tiers", response_model=TiersResponse)
async def list_tiers():
    all_tiers = get_all_tiers()
    tier_items = [TierInfo(**t) for t in all_tiers]
    return TiersResponse(tiers=tier_items)
