"""
Signals router — recent signals + live WebSocket stream.

GET       /api/v1/signals       → Recent 100 signals for this tenant
WebSocket /ws/v1/signals        → Live signal stream (FULL_AUTO only)
"""

import json
import uuid
from datetime import datetime, timezone

import jwt
import structlog
from fastapi import APIRouter, Query, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import JSONResponse
from sqlalchemy import text

from ..db import rls_session

logger = structlog.get_logger(service="dashboard_api", module="signals")

router = APIRouter(tags=["signals"])


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


@router.get("/api/v1/signals")
async def list_signals(
    request: Request,
    strategy_name: str | None = None,
    underlying: str | None = None,
    direction: str | None = None,
    limit: int = Query(default=100, le=200),
):
    """Get recent signals for the authenticated tenant (last 100 by default)."""
    tenant_id = request.state.tenant_id

    async with rls_session(tenant_id) as session:
        query = "SELECT * FROM signals WHERE tenant_id = :tenant_id"
        params: dict = {"tenant_id": tenant_id}

        if strategy_name:
            query += " AND strategy_name = :strategy_name"
            params["strategy_name"] = strategy_name

        if underlying:
            query += " AND underlying = :underlying"
            params["underlying"] = underlying

        if direction:
            query += " AND direction = :direction"
            params["direction"] = direction

        query += " ORDER BY timestamp DESC LIMIT :limit"
        params["limit"] = limit

        result = await session.execute(text(query), params)
        rows = result.mappings().all()

    logger.info("signals_listed", tenant_id=tenant_id, count=len(rows))

    return {
        "signals": [dict(r) for r in rows],
        "total": len(rows),
    }


@router.websocket("/ws/v1/signals")
async def signals_websocket(websocket: WebSocket, token: str = Query(...)):
    """
    Live signal WebSocket stream scoped to the authenticated tenant.
    Requires FULL_AUTO tier.

    Authentication is via token query parameter (WebSockets can't use headers).
    Subscribes to NATS subject signals.{tenant_id}.* and forwards messages.
    """
    import os

    JWT_SECRET = os.environ.get("JWT_SECRET", "")
    JWT_ALGORITHM = os.environ.get("JWT_ALGORITHM", "HS256")

    # Validate JWT from query param
    try:
        payload = jwt.decode(token, JWT_SECRET, algorithms=[JWT_ALGORITHM])
    except jwt.InvalidTokenError as exc:
        logger.warning("ws_auth_failed", error=str(exc))
        await websocket.close(code=4001, reason="Invalid token")
        return

    tenant_id = payload.get("sub")
    tier = payload.get("tier")
    jti = payload.get("jti")

    if not all([tenant_id, tier, jti]):
        await websocket.close(code=4001, reason="Invalid token claims")
        return

    # Check tier
    if tier != "FULL_AUTO":
        await websocket.close(
            code=4003,
            reason="FULL_AUTO tier required for live signals",
        )
        return

    # Check Redis blacklist
    redis = websocket.app.state.redis
    try:
        is_blacklisted = await redis.get(f"jwt:blacklist:{jti}")
        if is_blacklisted:
            await websocket.close(code=4001, reason="Token revoked")
            return
    except Exception as exc:
        logger.error("ws_redis_blacklist_check_failed", error=str(exc))

    # Accept the WebSocket connection
    await websocket.accept()

    # Track active connection
    from prometheus_client import Gauge

    try:
        active_ws = websocket.app.state.active_ws_gauge
        active_ws.inc()
    except Exception:
        pass

    logger.info("ws_connected", tenant_id=tenant_id)

    # Subscribe to NATS for this tenant's signals
    nats_client = websocket.app.state.nats
    subject = f"signals.{tenant_id}.>"

    async def message_handler(msg):
        try:
            data = json.loads(msg.data.decode())
            await websocket.send_json(data)
        except Exception as exc:
            logger.error(
                "ws_send_failed",
                tenant_id=tenant_id,
                error=str(exc),
            )

    sub = None
    try:
        sub = await nats_client.subscribe(subject, cb=message_handler)
        logger.info("ws_nats_subscribed", tenant_id=tenant_id, subject=subject)

        # Keep connection alive, listen for client messages (ping/pong, close)
        while True:
            try:
                data = await websocket.receive_text()
                # Client can send ping or close
                if data == "ping":
                    await websocket.send_json({"type": "pong"})
            except WebSocketDisconnect:
                break
    except Exception as exc:
        logger.error("ws_error", tenant_id=tenant_id, error=str(exc))
    finally:
        if sub:
            try:
                await sub.unsubscribe()
            except Exception:
                pass
        try:
            active_ws = websocket.app.state.active_ws_gauge
            active_ws.dec()
        except Exception:
            pass
        logger.info("ws_disconnected", tenant_id=tenant_id)
