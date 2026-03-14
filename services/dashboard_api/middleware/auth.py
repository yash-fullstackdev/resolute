"""
JWT authentication middleware for dashboard_api.

Extracts Bearer token from Authorization header, validates signature,
checks expiry, verifies jti not in Redis blacklist, and injects
request.state.tenant_id, request.state.email, request.state.tier.

Returns 401 on invalid/expired/revoked token.
"""

import os
import uuid
from datetime import datetime, timezone

import jwt
import structlog
from fastapi import Request
from fastapi.responses import JSONResponse
from starlette.middleware.base import BaseHTTPMiddleware

logger = structlog.get_logger(service="dashboard_api", module="auth_middleware")

JWT_SECRET = os.environ.get("JWT_SECRET", "")
JWT_ALGORITHM = os.environ.get("JWT_ALGORITHM", "HS256")

# Paths that do NOT require authentication
PUBLIC_PATHS = frozenset({
    "/health",
    "/metrics",
    "/docs",
    "/openapi.json",
    "/redoc",
})

# Path prefixes that do NOT require user JWT (admin has its own validation)
PUBLIC_PREFIXES = (
    "/admin/",
)


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


class JWTAuthMiddleware(BaseHTTPMiddleware):
    """
    Validates JWT Bearer tokens on all requests except public paths.

    On success, populates:
      - request.state.tenant_id  (str, UUID)
      - request.state.email      (str)
      - request.state.tier       (str, e.g. "SIGNAL" | "SEMI_AUTO" | "FULL_AUTO")
      - request.state.jti        (str, JWT ID)
    """

    async def dispatch(self, request: Request, call_next):
        path = request.url.path

        # Skip auth for public endpoints
        if path in PUBLIC_PATHS:
            return await call_next(request)

        # Skip auth for admin endpoints (they have their own auth in the router)
        for prefix in PUBLIC_PREFIXES:
            if path.startswith(prefix):
                return await call_next(request)

        # WebSocket upgrades: token is in query param
        if request.scope.get("type") == "websocket":
            return await call_next(request)

        # Extract Bearer token
        auth_header = request.headers.get("Authorization", "")
        if not auth_header.startswith("Bearer "):
            return _make_error(
                "UNAUTHORIZED",
                "Missing or malformed Authorization header. Expected: Bearer <token>",
                401,
            )

        token = auth_header[7:]

        try:
            payload = jwt.decode(token, JWT_SECRET, algorithms=[JWT_ALGORITHM])
        except jwt.ExpiredSignatureError:
            return _make_error("UNAUTHORIZED", "Token has expired.", 401)
        except jwt.InvalidTokenError as exc:
            logger.warning("jwt_validation_failed", error=str(exc))
            return _make_error("UNAUTHORIZED", "Invalid token.", 401)

        # Extract required claims
        tenant_id = payload.get("sub")
        email = payload.get("email")
        tier = payload.get("tier")
        jti = payload.get("jti")

        if not all([tenant_id, email, tier, jti]):
            return _make_error(
                "UNAUTHORIZED",
                "Token is missing required claims.",
                401,
            )

        # Check Redis blacklist
        redis = request.app.state.redis
        try:
            is_blacklisted = await redis.get(f"jwt:blacklist:{jti}")
            if is_blacklisted:
                logger.info("jwt_blacklisted", jti=jti, tenant_id=tenant_id)
                return _make_error("UNAUTHORIZED", "Token has been revoked.", 401)
        except Exception as exc:
            # If Redis is down, fail open with a warning (security vs availability trade-off)
            logger.error("redis_blacklist_check_failed", error=str(exc), jti=jti)

        # Inject into request state
        request.state.tenant_id = tenant_id
        request.state.email = email
        request.state.tier = tier
        request.state.jti = jti

        return await call_next(request)
