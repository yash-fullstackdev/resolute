"""
auth_service — FastAPI application entry point.

Responsibilities:
  - User registration, authentication, JWT lifecycle
  - Subscription management
  - Encrypted broker credential vault
  - Internal service-to-service identity endpoints
"""

import os
import time
from contextlib import asynccontextmanager
from datetime import datetime, timezone

import structlog
from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from redis.asyncio import Redis
from slowapi import _rate_limit_exceeded_handler
from slowapi.errors import RateLimitExceeded
from starlette.middleware.base import BaseHTTPMiddleware

from .db import check_db_health, dispose_engine
from .models.schemas import ErrorDetail, ErrorResponse, HealthCheckItem, HealthCheckResponse
from .routers import auth, broker_connect, internal, users

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

logger = structlog.get_logger(service="auth_service")

APP_VERSION = os.environ.get("APP_VERSION", "1.0.0")
REDIS_URL = os.environ.get("REDIS_URL", "redis://localhost:6379/0")
ALLOWED_ORIGINS = os.environ.get("FRONTEND_URL", "http://localhost:3000").split(",")


# ── Security Headers Middleware ───────────────────────────────────────────────


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


# ── Request ID + Logging Middleware ──────────────────────────────────────────


class RequestLoggingMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next):
        import uuid

        request_id = request.headers.get("X-Request-ID", str(uuid.uuid4()))
        start = time.monotonic()

        structlog.contextvars.clear_contextvars()
        structlog.contextvars.bind_contextvars(request_id=request_id)

        try:
            response = await call_next(request)
            duration_ms = round((time.monotonic() - start) * 1000, 2)
            logger.info(
                "http_request",
                method=request.method,
                path=request.url.path,
                status=response.status_code,
                duration_ms=duration_ms,
            )
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
    # Startup
    logger.info("auth_service_starting", version=APP_VERSION, redis_url=REDIS_URL)

    redis = Redis.from_url(REDIS_URL, decode_responses=True)
    app.state.redis = redis

    try:
        await redis.ping()
        logger.info("redis_connected")
    except Exception as exc:
        logger.error("redis_connection_failed", error=str(exc))
        raise

    # Verify database connectivity
    db_healthy, db_latency = await check_db_health()
    if db_healthy:
        logger.info("database_connected", latency_ms=db_latency)
    else:
        logger.error("database_connection_failed")

    logger.info("auth_service_started", version=APP_VERSION)

    yield

    # Shutdown
    logger.info("auth_service_shutting_down")
    await redis.aclose()
    await dispose_engine()
    logger.info("auth_service_stopped")


# ── App Creation ─────────────────────────────────────────────────────────────


app = FastAPI(
    title="Auth Service — India Options Builder",
    description="User authentication, subscription management, and broker credential vault.",
    version=APP_VERSION,
    lifespan=lifespan,
    docs_url="/docs" if os.environ.get("ENVIRONMENT", "development") != "production" else None,
    redoc_url=None,
)

# Rate limiter (shared with auth router)
app.state.limiter = auth.limiter
app.add_exception_handler(RateLimitExceeded, _rate_limit_exceeded_handler)

# CORS
app.add_middleware(
    CORSMiddleware,
    allow_origins=ALLOWED_ORIGINS,
    allow_credentials=True,
    allow_methods=["GET", "POST", "PUT", "PATCH", "DELETE"],
    allow_headers=["Authorization", "Content-Type", "X-CSRF-Token", "X-Request-ID"],
)

# Security headers
app.add_middleware(SecurityHeadersMiddleware)

# Request logging
app.add_middleware(RequestLoggingMiddleware)

# Routers
app.include_router(auth.router)
app.include_router(broker_connect.router)
app.include_router(users.router)
app.include_router(internal.router)


# ── Rate Limit Error Handler ─────────────────────────────────────────────────


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


# ── Validation Error Handler ─────────────────────────────────────────────────


@app.exception_handler(422)
async def validation_error_handler(request: Request, exc):
    body = ErrorResponse(
        error=ErrorDetail(
            code="VALIDATION_ERROR",
            message="Request validation failed.",
            details={"errors": str(exc)},
        ),
        timestamp=datetime.now(timezone.utc),
    )
    return JSONResponse(status_code=422, content=body.model_dump(mode="json"))


# ── Health Check ──────────────────────────────────────────────────────────────


@app.get("/health", response_model=HealthCheckResponse)
async def health_check(request: Request):
    """Public health check endpoint — returns 200 if service is running, 503 if degraded."""
    checks: dict[str, HealthCheckItem] = {}
    overall_status = "healthy"

    # Database check
    db_healthy, db_latency = await check_db_health()
    checks["database"] = HealthCheckItem(
        status="up" if db_healthy else "down",
        latency_ms=db_latency,
    )
    if not db_healthy:
        overall_status = "unhealthy"

    # Redis check
    redis: Redis = request.app.state.redis
    redis_healthy = False
    redis_latency = 0.0
    try:
        start = time.monotonic()
        await redis.ping()
        redis_latency = round((time.monotonic() - start) * 1000, 2)
        redis_healthy = True
    except Exception:
        redis_latency = 0.0

    checks["redis"] = HealthCheckItem(
        status="up" if redis_healthy else "down",
        latency_ms=redis_latency,
    )
    if not redis_healthy:
        overall_status = "unhealthy"

    status_code = 200 if overall_status == "healthy" else 503

    response = HealthCheckResponse(
        status=overall_status,
        timestamp=datetime.now(timezone.utc),
        version=APP_VERSION,
        checks=checks,
    )
    return JSONResponse(status_code=status_code, content=response.model_dump(mode="json"))
