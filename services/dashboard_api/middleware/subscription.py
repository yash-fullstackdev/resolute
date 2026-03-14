"""
Subscription tier gating middleware for dashboard_api.

Enforces minimum subscription tier requirements per endpoint.

Tier hierarchy:
  SIGNAL    → lowest (signals, chain, performance, journal read-only)
  SEMI_AUTO → adds plan, config, discipline, custom strategies
  FULL_AUTO → adds position delete, ws/live, AI endpoints, auto-execution
"""

import os
import uuid
from datetime import datetime, timezone

import structlog
from fastapi import Request
from fastapi.responses import JSONResponse
from starlette.middleware.base import BaseHTTPMiddleware

logger = structlog.get_logger(service="dashboard_api", module="subscription_middleware")

# Tier levels for comparison (higher number = more access)
TIER_LEVELS = {
    "SIGNAL": 1,
    "SEMI_AUTO": 2,
    "FULL_AUTO": 3,
}

# Mapping: (method, path_prefix) → minimum tier required
# Paths not listed here default to SIGNAL (the base tier).
# Order matters: more specific prefixes checked first.
TIER_REQUIREMENTS: list[tuple[str | None, str, str]] = [
    # (method_or_None, path_prefix, min_tier)
    # method=None means any method

    # FULL_AUTO tier requirements
    ("DELETE", "/api/v1/positions/", "FULL_AUTO"),
    (None, "/ws/v1/signals", "FULL_AUTO"),
    (None, "/api/v1/strategies/ai/", "FULL_AUTO"),

    # SEMI_AUTO tier requirements
    (None, "/api/v1/plan", "SEMI_AUTO"),
    ("PUT", "/api/v1/config/", "SEMI_AUTO"),
    (None, "/api/v1/discipline", "SEMI_AUTO"),
    ("PATCH", "/api/v1/journal/", "SEMI_AUTO"),
    (None, "/api/v1/strategies/custom", "SEMI_AUTO"),

    # SIGNAL tier — everything else under /api/v1/ is accessible
    # No entries needed: SIGNAL is the default
]

# Public paths that skip tier checking entirely
TIER_EXEMPT_PATHS = frozenset({
    "/health",
    "/metrics",
    "/docs",
    "/openapi.json",
})

TIER_EXEMPT_PREFIXES = (
    "/admin/",
)


def _get_minimum_tier(method: str, path: str) -> str | None:
    """Determine the minimum subscription tier required for a request.

    Returns None for paths that don't require tier checking,
    otherwise returns the minimum tier string.
    """
    if path in TIER_EXEMPT_PATHS:
        return None

    for prefix in TIER_EXEMPT_PREFIXES:
        if path.startswith(prefix):
            return None

    for req_method, req_prefix, min_tier in TIER_REQUIREMENTS:
        if path.startswith(req_prefix):
            if req_method is None or req_method == method:
                return min_tier

    # Default: any authenticated user (SIGNAL tier) can access
    return "SIGNAL"


def _make_error(code: str, message: str, status: int) -> JSONResponse:
    return JSONResponse(
        status_code=status,
        content={
            "error": {
                "code": code,
                "message": message,
                "details": {},
            },
            "request_id": str(uuid.uuid4()),
            "timestamp": datetime.now(timezone.utc).isoformat(),
        },
    )


class SubscriptionTierMiddleware(BaseHTTPMiddleware):
    """
    Checks that the user's subscription tier (from JWT) meets the minimum
    required tier for the requested endpoint.

    Must run AFTER JWTAuthMiddleware (depends on request.state.tier).
    """

    async def dispatch(self, request: Request, call_next):
        path = request.url.path
        method = request.method

        min_tier = _get_minimum_tier(method, path)

        # No tier requirement for this path
        if min_tier is None:
            return await call_next(request)

        # Get user tier from request state (set by JWTAuthMiddleware)
        user_tier = getattr(request.state, "tier", None)
        if user_tier is None:
            # No tier on request state means auth middleware didn't run or
            # this is a public path — skip tier check
            return await call_next(request)

        user_level = TIER_LEVELS.get(user_tier, 0)
        required_level = TIER_LEVELS.get(min_tier, 0)

        if user_level < required_level:
            logger.info(
                "tier_insufficient",
                tenant_id=getattr(request.state, "tenant_id", "unknown"),
                user_tier=user_tier,
                required_tier=min_tier,
                path=path,
                method=method,
            )
            return _make_error(
                "FORBIDDEN",
                f"This endpoint requires {min_tier} tier or higher. "
                f"Your current tier is {user_tier}. Please upgrade your subscription.",
                403,
            )

        return await call_next(request)
