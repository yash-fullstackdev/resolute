"""
Admin router — platform administration endpoints.

All endpoints require admin JWT validation (ADMIN_JWT_SECRET, separate from user JWT).

GET  /admin/v1/tenants              → All tenants with subscription status
GET  /admin/v1/tenants/{id}         → Single tenant detail
PUT  /admin/v1/tenants/{id}/suspend → Suspend a tenant account
GET  /admin/v1/system/health        → All services health + worker pool status
GET  /admin/v1/system/workers       → Active workers, per-user session status
GET  /admin/v1/system/metrics       → Platform-wide metrics
"""

import json
import os
import uuid
from datetime import datetime, timezone
from functools import wraps

import jwt as pyjwt
import structlog
from fastapi import APIRouter, Query, Request
from fastapi.responses import JSONResponse
from sqlalchemy import text

from ..db import async_session_factory

logger = structlog.get_logger(service="dashboard_api", module="admin")

router = APIRouter(prefix="/admin/v1", tags=["admin"])

ADMIN_JWT_SECRET = os.environ.get("ADMIN_JWT_SECRET", "")
JWT_ALGORITHM = os.environ.get("JWT_ALGORITHM", "HS256")


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


async def _validate_admin_token(request: Request) -> tuple[bool, JSONResponse | None]:
    """
    Validate admin JWT from Authorization header.
    Uses ADMIN_JWT_SECRET (separate from user JWT_SECRET).
    Returns (is_valid, error_response_or_None).
    """
    auth_header = request.headers.get("Authorization", "")
    if not auth_header.startswith("Bearer "):
        return False, _error("UNAUTHORIZED", "Missing admin authorization.", 401)

    token = auth_header[7:]
    try:
        payload = pyjwt.decode(token, ADMIN_JWT_SECRET, algorithms=[JWT_ALGORITHM])
    except pyjwt.ExpiredSignatureError:
        return False, _error("UNAUTHORIZED", "Admin token expired.", 401)
    except pyjwt.InvalidTokenError:
        return False, _error("UNAUTHORIZED", "Invalid admin token.", 401)

    role = payload.get("role")
    if role != "admin":
        return False, _error("FORBIDDEN", "Admin role required.", 403)

    return True, None


# ── Tenant Management ────────────────────────────────────────────────────────


@router.get("/tenants")
async def list_tenants(
    request: Request,
    status: str | None = None,
    tier: str | None = None,
    limit: int = Query(default=50, le=200),
    offset: int = Query(default=0, ge=0),
):
    """List all tenants with subscription status. Admin only."""
    is_valid, err = await _validate_admin_token(request)
    if not is_valid:
        return err

    async with async_session_factory() as session:
        query = """
            SELECT id, email, name, subscription_tier, subscription_status,
                   trial_ends_at, subscription_ends_at, is_active, created_at
            FROM tenants WHERE 1=1
        """
        params: dict = {}

        if status:
            query += " AND subscription_status = :status"
            params["status"] = status

        if tier:
            query += " AND subscription_tier = :tier"
            params["tier"] = tier

        query += " ORDER BY created_at DESC LIMIT :limit OFFSET :offset"
        params["limit"] = limit
        params["offset"] = offset

        result = await session.execute(text(query), params)
        rows = result.mappings().all()

        count_query = "SELECT COUNT(*) FROM tenants WHERE 1=1"
        count_params: dict = {}
        if status:
            count_query += " AND subscription_status = :status"
            count_params["status"] = status
        if tier:
            count_query += " AND subscription_tier = :tier"
            count_params["tier"] = tier

        count_result = await session.execute(text(count_query), count_params)
        total = count_result.scalar() or 0

    logger.info("admin_tenants_listed", count=len(rows), total=total)

    return {
        "tenants": [dict(r) for r in rows],
        "total": total,
    }


@router.get("/tenants/{tenant_id}")
async def get_tenant_detail(request: Request, tenant_id: str):
    """Get detailed information about a specific tenant. Admin only."""
    is_valid, err = await _validate_admin_token(request)
    if not is_valid:
        return err

    async with async_session_factory() as session:
        result = await session.execute(
            text("""
                SELECT id, email, name, subscription_tier, subscription_status,
                       trial_ends_at, subscription_ends_at, is_active, email_verified,
                       created_at
                FROM tenants WHERE id = :tenant_id
            """),
            {"tenant_id": tenant_id},
        )
        tenant = result.mappings().first()

        if not tenant:
            return _error("NOT_FOUND", f"Tenant {tenant_id} not found.", 404)

        # Get trade stats
        stats = await session.execute(
            text("""
                SELECT
                    COUNT(*) AS total_trades,
                    COALESCE(SUM(realised_pnl_inr), 0) AS total_pnl_inr,
                    COUNT(*) FILTER (WHERE status = 'OPEN') AS active_positions
                FROM positions WHERE tenant_id = :tenant_id
            """),
            {"tenant_id": tenant_id},
        )
        stats_row = stats.mappings().first()

        # Get custom strategy count
        cs_count = await session.execute(
            text("""
                SELECT COUNT(*) FROM custom_strategies
                WHERE tenant_id = :tenant_id AND status != 'ARCHIVED'
            """),
            {"tenant_id": tenant_id},
        )
        custom_count = cs_count.scalar() or 0

    tenant_dict = dict(tenant)
    tenant_dict["total_trades"] = stats_row["total_trades"] if stats_row else 0
    tenant_dict["total_pnl_inr"] = float(stats_row["total_pnl_inr"]) if stats_row else 0.0
    tenant_dict["active_positions"] = stats_row["active_positions"] if stats_row else 0
    tenant_dict["custom_strategies_count"] = custom_count

    logger.info("admin_tenant_detail_retrieved", tenant_id=tenant_id)
    return tenant_dict


@router.put("/tenants/{tenant_id}/suspend")
async def suspend_tenant(request: Request, tenant_id: str):
    """Suspend a tenant account. Admin only."""
    is_valid, err = await _validate_admin_token(request)
    if not is_valid:
        return err

    async with async_session_factory() as session:
        async with session.begin():
            result = await session.execute(
                text("SELECT id, subscription_status FROM tenants WHERE id = :tenant_id"),
                {"tenant_id": tenant_id},
            )
            row = result.mappings().first()

            if not row:
                return _error("NOT_FOUND", f"Tenant {tenant_id} not found.", 404)

            if row["subscription_status"] == "SUSPENDED":
                return _error(
                    "VALIDATION_ERROR",
                    "Tenant is already suspended.",
                    400,
                )

            await session.execute(
                text("""
                    UPDATE tenants
                    SET subscription_status = 'SUSPENDED', is_active = false
                    WHERE id = :tenant_id
                """),
                {"tenant_id": tenant_id},
            )

    # Notify worker pool to stop this tenant's worker
    nats_client = request.app.state.nats
    try:
        msg = {
            "tenant_id": tenant_id,
            "event": "TENANT_SUSPENDED",
            "timestamp": datetime.now(timezone.utc).isoformat(),
        }
        await nats_client.publish(
            f"worker.stopped.{tenant_id}",
            json.dumps(msg).encode(),
        )
    except Exception as exc:
        logger.error("suspend_publish_failed", tenant_id=tenant_id, error=str(exc))

    logger.info("admin_tenant_suspended", tenant_id=tenant_id)

    return {
        "tenant_id": tenant_id,
        "subscription_status": "SUSPENDED",
        "message": "Tenant account suspended.",
    }


# ── System Endpoints ─────────────────────────────────────────────────────────


@router.get("/system/health")
async def get_system_health(request: Request):
    """Get health status of all platform services. Admin only."""
    is_valid, err = await _validate_admin_token(request)
    if not is_valid:
        return err

    services = {}

    # Check database
    from ..db import check_db_health
    db_healthy, db_latency = await check_db_health()
    services["database"] = {"status": "up" if db_healthy else "down", "latency_ms": db_latency}

    # Check Redis
    import time
    redis = request.app.state.redis
    try:
        start = time.monotonic()
        await redis.ping()
        redis_latency = round((time.monotonic() - start) * 1000, 2)
        services["redis"] = {"status": "up", "latency_ms": redis_latency}
    except Exception:
        services["redis"] = {"status": "down", "latency_ms": 0}

    # Check NATS
    nats_client = request.app.state.nats
    try:
        nats_connected = nats_client.is_connected
        services["nats"] = {"status": "up" if nats_connected else "down", "latency_ms": None}
    except Exception:
        services["nats"] = {"status": "unknown", "latency_ms": None}

    # Overall status
    all_up = all(s["status"] == "up" for s in services.values())
    any_down = any(s["status"] == "down" for s in services.values())
    overall = "healthy" if all_up else ("unhealthy" if any_down else "degraded")

    return {
        "status": overall,
        "services": services,
        "timestamp": datetime.now(timezone.utc).isoformat(),
    }


@router.get("/system/workers")
async def get_workers(request: Request):
    """Get active worker status for all tenants. Admin only."""
    is_valid, err = await _validate_admin_token(request)
    if not is_valid:
        return err

    # Query Redis for worker status (published by user_worker_pool)
    redis = request.app.state.redis
    workers = []

    try:
        # Scan for worker keys
        cursor = 0
        while True:
            cursor, keys = await redis.scan(cursor, match="worker:status:*", count=100)
            for key in keys:
                data = await redis.get(key)
                if data:
                    worker_info = json.loads(data)
                    workers.append(worker_info)
            if cursor == 0:
                break
    except Exception as exc:
        logger.error("admin_workers_fetch_failed", error=str(exc))

    logger.info("admin_workers_listed", count=len(workers))

    return {
        "workers": workers,
        "total": len(workers),
    }


@router.get("/system/metrics")
async def get_system_metrics(request: Request):
    """Get platform-wide metrics. Admin only."""
    is_valid, err = await _validate_admin_token(request)
    if not is_valid:
        return err

    async with async_session_factory() as session:
        # Total and active tenants
        tenant_stats = await session.execute(
            text("""
                SELECT
                    COUNT(*) AS total_tenants,
                    COUNT(*) FILTER (WHERE is_active = true AND subscription_status = 'ACTIVE') AS active_tenants
                FROM tenants
            """)
        )
        t_row = tenant_stats.mappings().first()

        # Today's trades
        trade_stats = await session.execute(
            text("""
                SELECT
                    COUNT(*) AS total_trades_today,
                    COALESCE(SUM(realised_pnl_inr), 0) AS total_pnl_today_inr
                FROM positions
                WHERE DATE(entry_time) = CURRENT_DATE
            """)
        )
        tr_row = trade_stats.mappings().first()

    # Active WebSocket connections from Prometheus gauge
    active_ws = 0
    try:
        active_ws = int(request.app.state.active_ws_gauge._value.get())
    except Exception:
        pass

    # Active workers from Redis
    redis = request.app.state.redis
    active_workers = 0
    try:
        cursor = 0
        while True:
            cursor, keys = await redis.scan(cursor, match="worker:status:*", count=100)
            active_workers += len(keys)
            if cursor == 0:
                break
    except Exception:
        pass

    return {
        "total_tenants": t_row["total_tenants"] if t_row else 0,
        "active_tenants": t_row["active_tenants"] if t_row else 0,
        "total_trades_today": tr_row["total_trades_today"] if tr_row else 0,
        "total_pnl_today_inr": float(tr_row["total_pnl_today_inr"]) if tr_row else 0.0,
        "active_websockets": active_ws,
        "active_workers": active_workers,
        "timestamp": datetime.now(timezone.utc).isoformat(),
    }
