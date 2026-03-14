"""
Health check router — public, unauthenticated endpoint.

GET /health → Returns service health with dependency checks.
"""

import os
import time
from datetime import datetime, timezone

import structlog
from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

from ..db import check_db_health

logger = structlog.get_logger(service="dashboard_api", module="health")

router = APIRouter(tags=["health"])

APP_VERSION = os.environ.get("APP_VERSION", "1.0.0")


@router.get("/health")
async def health_check(request: Request):
    """
    Public health check endpoint — returns 200 if service is running, 503 if degraded.
    Checks database, Redis, and NATS connectivity.
    """
    checks = {}
    overall_status = "healthy"

    # Database check
    db_healthy, db_latency = await check_db_health()
    checks["database"] = {"status": "up" if db_healthy else "down", "latency_ms": db_latency}
    if not db_healthy:
        overall_status = "unhealthy"

    # Redis check
    redis = request.app.state.redis
    try:
        start = time.monotonic()
        await redis.ping()
        redis_latency = round((time.monotonic() - start) * 1000, 2)
        checks["redis"] = {"status": "up", "latency_ms": redis_latency}
    except Exception:
        checks["redis"] = {"status": "down", "latency_ms": 0}
        overall_status = "unhealthy"

    # NATS check
    nats_client = request.app.state.nats
    try:
        is_connected = nats_client.is_connected
        checks["nats"] = {"status": "up" if is_connected else "down", "latency_ms": None}
        if not is_connected:
            overall_status = "degraded" if overall_status == "healthy" else overall_status
    except Exception:
        checks["nats"] = {"status": "unknown", "latency_ms": None}
        if overall_status == "healthy":
            overall_status = "degraded"

    status_code = 200 if overall_status == "healthy" else 503

    return JSONResponse(
        status_code=status_code,
        content={
            "status": overall_status,
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "version": APP_VERSION,
            "checks": checks,
        },
    )
