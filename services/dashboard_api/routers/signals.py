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


@router.get("/api/v1/signals/live-trades")
async def get_live_trades(request: Request):
    """Get currently open paper trades with live P&L and SL/TP tracking."""
    tenant_id = request.state.tenant_id
    pool = getattr(request.app.state, "worker_pool", None)
    if pool is None:
        return {"success": True, "data": []}
    worker = pool.workers.get(tenant_id)
    if worker is None:
        return {"success": True, "data": []}
    try:
        trades = worker.get_open_trades()
    except Exception:
        trades = []
    return {"success": True, "data": trades}


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


def _get_live_prices(request: Request) -> dict[str, float]:
    """Get current prices from the worker's candle store."""
    pool = getattr(request.app.state, "worker_pool", None)
    if not pool:
        return {}
    for worker in pool.workers.values():
        cs = getattr(worker, "_candle_store", None)
        if cs:
            prices = {}
            for sym in cs._symbols:
                c = cs.get_candles(sym, "1m")
                if c and "close" in c and len(c["close"]) > 0:
                    prices[sym] = float(c["close"][-1])
            return prices
    return {}


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

    try:
        async with rls_session(tenant_id) as session:
            query = "SELECT * FROM signals WHERE tenant_id = :tenant_id"
            params: dict = {"tenant_id": tenant_id}

            if strategy_name:
                query += " AND strategy = :strategy_name"
                params["strategy_name"] = strategy_name

            if underlying:
                query += " AND underlying = :underlying"
                params["underlying"] = underlying

            if direction:
                query += " AND direction = :direction"
                params["direction"] = direction

            query += " ORDER BY time DESC LIMIT :limit"
            params["limit"] = limit

            result = await session.execute(text(query), params)
            rows = result.mappings().all()
    except Exception as exc:
        logger.warning("signals_query_failed", tenant_id=tenant_id, error=str(exc))
        rows = []

    live_prices = _get_live_prices(request)

    def _map_signal(r: dict) -> dict:
        legs_raw = r.get("legs") or {}
        rationale_raw = r.get("rationale") or ""

        # Parse rationale JSON for entry/SL/TP
        entry_price = None
        stop_loss_price = None
        target_price = None
        metadata = {}
        options = None

        if isinstance(rationale_raw, str) and rationale_raw.startswith("{"):
            try:
                meta = json.loads(rationale_raw)
                entry_price = meta.get("entry_price")
                stop_loss_price = meta.get("stop_loss_price")
                target_price = meta.get("target_price")
                options = meta.get("options")
                metadata = {
                    "instance_name": meta.get("instance"),
                    "trading_mode": meta.get("mode"),
                    "bias_direction": meta.get("bias"),
                }
                if options:
                    metadata["options"] = options
            except (json.JSONDecodeError, TypeError):
                pass
        elif isinstance(rationale_raw, dict):
            entry_price = rationale_raw.get("entry_price")
            stop_loss_price = rationale_raw.get("stop_loss_price")
            target_price = rationale_raw.get("target_price")
            options = rationale_raw.get("options")
            metadata = rationale_raw

        # Parse legs for option data
        legs = []
        if isinstance(legs_raw, dict):
            # legs_raw is sig_payload — extract options if present
            if legs_raw.get("options"):
                options = legs_raw["options"]
            entry_price = entry_price or legs_raw.get("entry_price")
            stop_loss_price = stop_loss_price or legs_raw.get("stop_loss_price")
            target_price = target_price or legs_raw.get("target_price")
        elif isinstance(legs_raw, list):
            legs = legs_raw

        # Compute index risk/reward
        sig_dir = r.get("direction", "BUY")
        idx_risk = idx_reward = 0.0
        if entry_price and stop_loss_price:
            if sig_dir == "BUY":
                idx_risk = round(entry_price - stop_loss_price, 2)
                idx_reward = round((target_price or 0) - entry_price, 2)
            else:
                idx_risk = round(stop_loss_price - entry_price, 2)
                idx_reward = round(entry_price - (target_price or 0), 2)

        # Live price tracking
        underlying = r.get("underlying", "")
        current_price = live_prices.get(underlying)
        # Check aliases
        if current_price is None:
            aliases = {"NIFTY": "NIFTY_50", "NIFTY_50": "NIFTY", "BANKNIFTY": "BANK_NIFTY", "BANK_NIFTY": "BANKNIFTY"}
            alias = aliases.get(underlying)
            if alias:
                current_price = live_prices.get(alias)

        live_pnl = None
        trade_status = "OPEN"
        if current_price and entry_price:
            if sig_dir == "BUY":
                live_pnl = round(current_price - entry_price, 2)
                if stop_loss_price and current_price <= stop_loss_price:
                    trade_status = "SL HIT"
                elif target_price and current_price >= target_price:
                    trade_status = "TARGET HIT"
            else:
                live_pnl = round(entry_price - current_price, 2)
                if stop_loss_price and current_price >= stop_loss_price:
                    trade_status = "SL HIT"
                elif target_price and current_price <= target_price:
                    trade_status = "TARGET HIT"

        return {
            "id": str(r["id"]),
            "strategy_name": r.get("strategy", ""),
            "underlying": underlying,
            "direction": r.get("direction", ""),
            "strength": r.get("strength") or 0.0,
            "regime": r.get("regime") or "UNKNOWN",
            "legs": legs,
            "signal_type": "DIRECT",
            "entry_price": entry_price,
            "stop_loss_price": stop_loss_price,
            "target_price": target_price,
            "current_price": current_price,
            "live_pnl": live_pnl,
            "trade_status": trade_status,
            "index_risk_pts": idx_risk,
            "index_reward_pts": idx_reward,
            "index_rr": f"1:{round(idx_reward / idx_risk, 1)}" if idx_risk > 0 else None,
            "has_options_chain": bool(options),
            "options": options,
            "metadata": metadata,
            "rationale": "",
            "created_at": r["time"].isoformat() if r.get("time") else "",
            "executed": r.get("acted_upon") or False,
        }

    logger.info("signals_listed", tenant_id=tenant_id, count=len(rows))

    return {
        "success": True,
        "data": [_map_signal(dict(r)) for r in rows],
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
