"""
dashboard_api — FastAPI application entry point.

Responsibilities:
  - Multi-tenant REST API + WebSocket server
  - All endpoints require JWT authentication (except /health, /metrics)
  - tenant_id from JWT is injected into every query
  - PostgreSQL RLS provides second enforcement layer
  - NATS for inter-service communication
  - Redis for JWT blacklist, caching, rate limit storage
  - Prometheus metrics on /metrics
"""

import os
import time
import uuid
from contextlib import asynccontextmanager
from datetime import datetime, timezone

import structlog
from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from nats.aio.client import Client as NATSClient
from prometheus_client import Counter, Gauge, Histogram, generate_latest
from redis.asyncio import Redis
from slowapi import Limiter, _rate_limit_exceeded_handler
from slowapi.errors import RateLimitExceeded
from slowapi.util import get_remote_address
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.responses import Response

from .db import check_db_health, dispose_engine
from .middleware.auth import JWTAuthMiddleware
from .middleware.subscription import SubscriptionTierMiddleware
from .models.schemas import ErrorDetail, ErrorResponse
from .routers import (
    admin,
    chain,
    config,
    discipline,
    health,
    journal,
    performance,
    plan,
    positions,
    reports,
    signals,
    strategies,
    user,
)

# ── Structlog Configuration ─────────────────────────────────────────────────

structlog.configure(
    processors=[
        structlog.contextvars.merge_contextvars,
        structlog.processors.add_log_level,
        structlog.processors.TimeStamper(fmt="iso"),
        structlog.processors.StackInfoRenderer(),
        structlog.processors.format_exc_info,
        structlog.processors.JSONRenderer(),
    ],
    wrapper_class=structlog.make_filtering_bound_logger(
        int(os.environ.get("LOG_LEVEL", "20"))  # 20 = INFO
    ),
    context_class=dict,
    logger_factory=structlog.PrintLoggerFactory(),
    cache_logger_on_first_use=True,
)

logger = structlog.get_logger(service="dashboard_api")

# ── Configuration ────────────────────────────────────────────────────────────

APP_VERSION = os.environ.get("APP_VERSION", "1.0.0")
REDIS_URL = os.environ.get("REDIS_URL", "redis://localhost:6379/0")
REDIS_RATE_LIMIT_URL = os.environ.get("REDIS_RATE_LIMIT_URL", "redis://localhost:6379/1")
NATS_URL = os.environ.get("NATS_URL", "nats://localhost:4222")
ALLOWED_ORIGINS = os.environ.get("FRONTEND_URL", "http://localhost:3000").split(",")
ENVIRONMENT = os.environ.get("ENVIRONMENT", "development")


# ── Prometheus Metrics ───────────────────────────────────────────────────────

api_requests_total = Counter(
    "api_requests_total",
    "Total API requests",
    ["method", "endpoint", "status"],
)
api_request_duration = Histogram(
    "api_request_duration_seconds",
    "API request latency",
    ["endpoint"],
)
active_websocket_connections = Gauge(
    "active_ws_connections",
    "Active WebSocket connections",
)


# ── Rate Limiter ─────────────────────────────────────────────────────────────


def get_user_key(request: Request) -> str:
    """Extract rate-limit key: prefer tenant_id from JWT, fall back to IP."""
    tenant_id = getattr(request.state, "tenant_id", None)
    if tenant_id:
        return f"user:{tenant_id}"
    return get_remote_address(request)


limiter = Limiter(
    key_func=get_user_key,
    storage_uri=REDIS_RATE_LIMIT_URL,
    default_limits=["120/minute"],
)


# ── Security Headers Middleware ──────────────────────────────────────────────


class SecurityHeadersMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next):
        response = await call_next(request)
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["X-Frame-Options"] = "DENY"
        response.headers["X-XSS-Protection"] = "1; mode=block"
        response.headers["Strict-Transport-Security"] = "max-age=31536000; includeSubDomains"
        response.headers["Content-Security-Policy"] = "default-src 'self'"
        response.headers["Referrer-Policy"] = "strict-origin-when-cross-origin"
        return response


# ── CSRF Middleware ──────────────────────────────────────────────────────────


class CSRFMiddleware(BaseHTTPMiddleware):
    """
    Validate X-CSRF-Token header matches csrf_token cookie on state-changing requests.
    GET and OPTIONS requests are exempt. WebSocket upgrades are exempt.
    """

    async def dispatch(self, request: Request, call_next):
        if request.method in ("POST", "PUT", "PATCH", "DELETE"):
            # Skip CSRF for API calls with Bearer auth (SPA pattern)
            # CSRF is primarily for cookie-based auth; JWT Bearer is not vulnerable
            auth_header = request.headers.get("Authorization", "")
            if not auth_header.startswith("Bearer "):
                cookie_token = request.cookies.get("csrf_token")
                header_token = request.headers.get("X-CSRF-Token")
                if not cookie_token or cookie_token != header_token:
                    return JSONResponse(
                        status_code=403,
                        content={
                            "error": {
                                "code": "FORBIDDEN",
                                "message": "CSRF validation failed.",
                                "details": {},
                            },
                            "request_id": str(uuid.uuid4()),
                            "timestamp": datetime.now(timezone.utc).isoformat(),
                        },
                    )
        return await call_next(request)


# ── Request ID + Logging + Metrics Middleware ────────────────────────────────


class RequestLoggingMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next):
        request_id = request.headers.get("X-Request-ID", str(uuid.uuid4()))
        start = time.monotonic()

        structlog.contextvars.clear_contextvars()
        structlog.contextvars.bind_contextvars(request_id=request_id)

        try:
            response = await call_next(request)
            duration_ms = round((time.monotonic() - start) * 1000, 2)
            duration_s = (time.monotonic() - start)

            # Log request
            logger.info(
                "http_request",
                method=request.method,
                path=request.url.path,
                status=response.status_code,
                duration_ms=duration_ms,
                tenant_id=getattr(request.state, "tenant_id", None),
            )

            # Prometheus metrics
            endpoint = request.url.path
            api_requests_total.labels(
                method=request.method,
                endpoint=endpoint,
                status=response.status_code,
            ).inc()
            api_request_duration.labels(endpoint=endpoint).observe(duration_s)

            response.headers["X-Request-ID"] = request_id
            return response
        except Exception as exc:
            duration_ms = round((time.monotonic() - start) * 1000, 2)
            logger.error(
                "http_request_error",
                method=request.method,
                path=request.url.path,
                duration_ms=duration_ms,
                error=str(exc),
            )
            raise


# ── Lifespan ─────────────────────────────────────────────────────────────────


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Application startup and shutdown lifecycle."""
    # ── Startup ──
    logger.info("dashboard_api_starting", version=APP_VERSION)

    # Redis connection
    redis = Redis.from_url(REDIS_URL, decode_responses=True)
    app.state.redis = redis
    try:
        await redis.ping()
        logger.info("redis_connected")
    except Exception as exc:
        logger.error("redis_connection_failed", error=str(exc))
        raise

    # NATS connection
    nats_client = NATSClient()
    try:
        await nats_client.connect(NATS_URL)
        app.state.nats = nats_client
        logger.info("nats_connected", url=NATS_URL)
    except Exception as exc:
        logger.error("nats_connection_failed", error=str(exc))
        raise

    # WebSocket gauge reference for routers
    app.state.active_ws_gauge = active_websocket_connections

    # Verify database connectivity
    db_healthy, db_latency = await check_db_health()
    if db_healthy:
        logger.info("database_connected", latency_ms=db_latency)
    else:
        logger.error("database_connection_failed")

    logger.info("dashboard_api_started", version=APP_VERSION)

    yield

    # ── Shutdown ──
    logger.info("dashboard_api_shutting_down")

    # Close NATS
    if nats_client.is_connected:
        await nats_client.drain()
        await nats_client.close()
        logger.info("nats_disconnected")

    # Close Redis
    await redis.aclose()
    logger.info("redis_disconnected")

    # Close DB pool
    await dispose_engine()

    logger.info("dashboard_api_stopped")


# ── App Creation ─────────────────────────────────────────────────────────────

app = FastAPI(
    title="Dashboard API — India Options Builder",
    description="Multi-tenant REST API + WebSocket server for the India Options Builder platform.",
    version=APP_VERSION,
    lifespan=lifespan,
    docs_url="/docs" if ENVIRONMENT != "production" else None,
    redoc_url=None,
)

# ── Rate Limiter ─────────────────────────────────────────────────────────────

app.state.limiter = limiter
app.add_exception_handler(RateLimitExceeded, _rate_limit_exceeded_handler)

# ── Middleware (order matters: last added = outermost = runs first) ───────────

# CSRF protection (innermost)
app.add_middleware(CSRFMiddleware)

# JWT authentication (sets request.state.tenant_id, .email, .tier)
app.add_middleware(JWTAuthMiddleware)

# Subscription tier gating (after auth, uses request.state.tier)
app.add_middleware(SubscriptionTierMiddleware)

# Request logging + metrics (before auth so we log all requests)
app.add_middleware(RequestLoggingMiddleware)

# Security headers
app.add_middleware(SecurityHeadersMiddleware)

# CORS — must be outermost (added last so it runs first, handles OPTIONS preflight)
app.add_middleware(
    CORSMiddleware,
    allow_origins=ALLOWED_ORIGINS,
    allow_credentials=True,
    allow_methods=["GET", "POST", "PUT", "PATCH", "DELETE", "OPTIONS"],
    allow_headers=["Authorization", "Content-Type", "X-CSRF-Token", "X-Request-ID"],
)


# ── Routers ──────────────────────────────────────────────────────────────────

app.include_router(health.router)
app.include_router(positions.router)
app.include_router(signals.router)
app.include_router(performance.router)
app.include_router(config.router)
app.include_router(plan.router)
app.include_router(discipline.router)
app.include_router(journal.router)
app.include_router(reports.router)
app.include_router(chain.router)
app.include_router(strategies.router)
app.include_router(admin.router)
app.include_router(user.router)


# ── Prometheus Metrics Endpoint ──────────────────────────────────────────────


@app.get("/metrics")
async def metrics():
    """Prometheus metrics endpoint."""
    return Response(
        content=generate_latest(),
        media_type="text/plain; version=0.0.4; charset=utf-8",
    )


# ── Error Handlers ───────────────────────────────────────────────────────────


@app.exception_handler(RateLimitExceeded)
async def rate_limit_handler(request: Request, exc: RateLimitExceeded):
    logger.warning(
        "rate_limit_exceeded",
        path=request.url.path,
        client=request.client.host if request.client else "unknown",
    )
    body = ErrorResponse(
        error=ErrorDetail(
            code="RATE_LIMITED",
            message="Too many requests. Please try again later.",
            details={"retry_after": str(exc.detail)},
        ),
        timestamp=datetime.now(timezone.utc),
    )
    return JSONResponse(status_code=429, content=body.model_dump(mode="json"))


@app.exception_handler(RequestValidationError)
async def validation_error_handler(request: Request, exc: RequestValidationError):
    logger.warning(
        "validation_error",
        path=request.url.path,
        errors=str(exc.errors()),
    )
    body = ErrorResponse(
        error=ErrorDetail(
            code="VALIDATION_ERROR",
            message="Request validation failed.",
            details={"errors": exc.errors()},
        ),
        timestamp=datetime.now(timezone.utc),
    )
    return JSONResponse(status_code=422, content=body.model_dump(mode="json"))


@app.exception_handler(404)
async def not_found_handler(request: Request, exc):
    body = ErrorResponse(
        error=ErrorDetail(
            code="NOT_FOUND",
            message="The requested resource was not found.",
            details={"path": request.url.path},
        ),
        timestamp=datetime.now(timezone.utc),
    )
    return JSONResponse(status_code=404, content=body.model_dump(mode="json"))


@app.exception_handler(500)
async def internal_error_handler(request: Request, exc):
    logger.error(
        "internal_server_error",
        path=request.url.path,
        error=str(exc),
    )
    body = ErrorResponse(
        error=ErrorDetail(
            code="INTERNAL_ERROR",
            message="An internal server error occurred.",
            details={},
        ),
        timestamp=datetime.now(timezone.utc),
    )
    return JSONResponse(status_code=500, content=body.model_dump(mode="json"))
