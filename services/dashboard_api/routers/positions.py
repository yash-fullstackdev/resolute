"""
Positions router — open/closed position management.

GET  /api/v1/positions         → List positions for this tenant
GET  /api/v1/positions/{id}    → Single position (must belong to tenant)
DELETE /api/v1/positions/{id}  → Manual close → publish exit signal to NATS (FULL_AUTO)
"""

import uuid
from datetime import datetime, timezone

import structlog
from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse
from sqlalchemy import text

from ..db import rls_session

logger = structlog.get_logger(service="dashboard_api", module="positions")

router = APIRouter(prefix="/api/v1/positions", tags=["positions"])


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


@router.get("")
async def list_positions(
    request: Request,
    status: str | None = None,
    underlying: str | None = None,
    strategy_name: str | None = None,
    limit: int = 50,
    offset: int = 0,
):
    """List all positions for the authenticated tenant, with optional filters."""
    tenant_id = request.state.tenant_id

    async with rls_session(tenant_id) as session:
        # Build dynamic query
        query = "SELECT * FROM positions WHERE tenant_id = :tenant_id"
        params: dict = {"tenant_id": tenant_id}

        if status:
            query += " AND status = :status"
            params["status"] = status

        if underlying:
            query += " AND underlying = :underlying"
            params["underlying"] = underlying

        if strategy_name:
            query += " AND strategy = :strategy_name"
            params["strategy_name"] = strategy_name

        query += " ORDER BY entry_time DESC LIMIT :limit OFFSET :offset"
        params["limit"] = min(limit, 200)
        params["offset"] = offset

        result = await session.execute(text(query), params)
        rows = result.mappings().all()

    def _map_position(r: dict) -> dict:
        legs = r.get("legs") or []
        segment = r.get("segment", "")
        # For equity positions, derive direction from legs or strategy
        direction = segment
        if isinstance(legs, dict) and legs.get("direction"):
            direction = legs["direction"]
        elif isinstance(legs, list) and legs:
            first_leg = legs[0] if isinstance(legs[0], dict) else {}
            direction = first_leg.get("action", segment)

        pnl = r.get("realised_pnl_inr") or 0.0
        entry_cost = r.get("entry_cost_inr") or 0.0
        pnl_pct = round(pnl / entry_cost * 100, 2) if entry_cost > 0 else 0.0

        return {
            "id": str(r["id"]),
            "tenant_id": str(r["tenant_id"]),
            "strategy_name": r.get("strategy", ""),
            "underlying": r.get("underlying", ""),
            "segment": segment,
            "direction": direction,
            "status": r.get("status", "OPEN"),
            "legs": legs if isinstance(legs, list) else [],
            "entry_time": r["entry_time"].isoformat() if r.get("entry_time") else "",
            "exit_time": r["exit_time"].isoformat() if r.get("exit_time") else None,
            "total_pnl": pnl,
            "total_pnl_pct": pnl_pct,
            "unrealized_pnl": 0.0 if r.get("status") == "OPEN" else pnl,
            "realized_pnl": pnl,
            "stop_loss": r.get("stop_loss_price"),
            "target": r.get("target_price"),
            "capital_deployed": entry_cost,
            "max_drawdown": 0.0,
            "exit_reason": r.get("exit_reason", ""),
            "created_at": r["entry_time"].isoformat() if r.get("entry_time") else "",
            "updated_at": r["exit_time"].isoformat() if r.get("exit_time") else "",
        }

    logger.info("positions_listed", tenant_id=tenant_id, count=len(rows))

    return {
        "success": True,
        "data": [_map_position(dict(r)) for r in rows],
    }


@router.get("/{position_id}")
async def get_position(request: Request, position_id: str):
    """Get a single position by ID (must belong to the authenticated tenant)."""
    tenant_id = request.state.tenant_id

    async with rls_session(tenant_id) as session:
        result = await session.execute(
            text("SELECT * FROM positions WHERE id = :id AND tenant_id = :tenant_id"),
            {"id": position_id, "tenant_id": tenant_id},
        )
        row = result.mappings().first()

    if not row:
        return _error(
            "NOT_FOUND",
            f"Position {position_id} not found.",
            404,
        )

    logger.info("position_retrieved", tenant_id=tenant_id, position_id=position_id)
    return dict(row)


@router.delete("/{position_id}")
async def close_position(request: Request, position_id: str):
    """
    Manually close a position by publishing an exit signal to NATS.
    Requires FULL_AUTO tier (enforced by subscription middleware).
    """
    tenant_id = request.state.tenant_id

    # Verify position exists and belongs to tenant
    async with rls_session(tenant_id) as session:
        result = await session.execute(
            text(
                "SELECT * FROM positions WHERE id = :id AND tenant_id = :tenant_id AND status = 'OPEN'"
            ),
            {"id": position_id, "tenant_id": tenant_id},
        )
        row = result.mappings().first()

    if not row:
        return _error(
            "NOT_FOUND",
            f"Open position {position_id} not found.",
            404,
        )

    # Publish exit signal to NATS
    nats_client = request.app.state.nats
    subject = f"signals.{tenant_id}.exit.{position_id}"
    exit_signal = {
        "id": str(uuid.uuid4()),
        "tenant_id": tenant_id,
        "position_id": position_id,
        "direction": "EXIT",
        "reason": "MANUAL_CLOSE",
        "timestamp": datetime.now(timezone.utc).isoformat(),
    }

    import json
    try:
        await nats_client.publish(subject, json.dumps(exit_signal).encode())
        logger.info(
            "exit_signal_published",
            tenant_id=tenant_id,
            position_id=position_id,
            subject=subject,
        )
    except Exception as exc:
        logger.error(
            "exit_signal_publish_failed",
            tenant_id=tenant_id,
            position_id=position_id,
            error=str(exc),
        )
        return _error(
            "SERVICE_UNAVAILABLE",
            "Failed to publish exit signal. Please try again.",
            503,
        )

    return {
        "message": f"Exit signal published for position {position_id}.",
        "position_id": position_id,
        "signal_id": exit_signal["id"],
    }


# ── Broker data (proxied from Dhan via auth_service) ─────────────────────────

async def _get_broker_creds(tenant_id: str) -> dict | None:
    """Fetch decrypted broker credentials from auth_service internal API."""
    import os, httpx
    auth_url = os.environ.get("AUTH_SERVICE_URL", "http://auth_service:8001")
    token = os.environ.get("AUTH_INTERNAL_TOKEN", "")
    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            resp = await client.get(
                f"{auth_url}/internal/broker-creds/{tenant_id}",
                headers={"Authorization": f"Bearer {token}"},
            )
            if resp.status_code != 200:
                return None
            creds = resp.json()
            if isinstance(creds, list) and creds:
                return creds[0]
            return None
    except Exception as exc:
        logger.error("broker_creds_fetch_failed", error=str(exc))
        return None


@router.get("/broker/positions")
async def broker_positions(request: Request):
    """Fetch live intraday positions from Dhan broker API."""
    tenant_id = request.state.tenant_id
    creds = await _get_broker_creds(tenant_id)
    if not creds:
        return {"success": True, "data": [], "error": "No broker connected"}

    import httpx
    access_token = creds.get("access_token") or creds.get("api_key", "")
    client_id = creds.get("client_id", "")

    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            resp = await client.get(
                "https://api.dhan.co/v2/positions",
                headers={"access-token": access_token, "client-id": client_id},
            )
            positions = resp.json() if resp.status_code == 200 else []
    except Exception as exc:
        logger.error("dhan_positions_failed", error=str(exc))
        positions = []

    # Map Dhan format to our format
    mapped = []
    for p in (positions if isinstance(positions, list) else []):
        net_qty = p.get("netQty", 0)
        if net_qty == 0:
            continue
        buy_avg = p.get("buyAvg", 0)
        sell_avg = p.get("sellAvg", 0)
        ltp = p.get("dayEndClose", 0) or p.get("costPrice", 0)
        direction = "BUY" if net_qty > 0 else "SELL"
        entry_price = buy_avg if direction == "BUY" else sell_avg
        pnl = p.get("realizedProfit", 0) + p.get("unrealizedProfit", 0)

        mapped.append({
            "symbol": p.get("tradingSymbol", ""),
            "security_id": str(p.get("securityId", "")),
            "exchange": p.get("exchangeSegment", ""),
            "direction": direction,
            "quantity": abs(net_qty),
            "entry_price": round(entry_price, 2),
            "current_price": round(ltp, 2),
            "pnl": round(pnl, 2),
            "product": p.get("productType", ""),
            "source": "broker",
        })

    return {"success": True, "data": mapped}


@router.get("/broker/holdings")
async def broker_holdings(request: Request):
    """Fetch holdings (delivery stocks) from Dhan broker API."""
    tenant_id = request.state.tenant_id
    creds = await _get_broker_creds(tenant_id)
    if not creds:
        return {"success": True, "data": [], "error": "No broker connected"}

    import httpx
    access_token = creds.get("access_token") or creds.get("api_key", "")
    client_id = creds.get("client_id", "")

    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            resp = await client.get(
                "https://api.dhan.co/v2/holdings",
                headers={"access-token": access_token, "client-id": client_id},
            )
            holdings = resp.json() if resp.status_code == 200 else []
    except Exception as exc:
        logger.error("dhan_holdings_failed", error=str(exc))
        holdings = []

    mapped = []
    for h in (holdings if isinstance(holdings, list) else []):
        qty = h.get("totalQty", 0)
        if qty == 0:
            continue
        avg_cost = h.get("avgCostPrice", 0)
        ltp = h.get("lastTradedPrice", 0)
        pnl = round((ltp - avg_cost) * qty, 2)
        pnl_pct = round((ltp - avg_cost) / avg_cost * 100, 2) if avg_cost > 0 else 0
        current_value = round(ltp * qty, 2)
        invested = round(avg_cost * qty, 2)

        mapped.append({
            "symbol": h.get("tradingSymbol", ""),
            "security_id": str(h.get("securityId", "")),
            "isin": h.get("isin", ""),
            "quantity": qty,
            "avg_cost": round(avg_cost, 2),
            "ltp": round(ltp, 2),
            "current_value": current_value,
            "invested": invested,
            "pnl": pnl,
            "pnl_pct": pnl_pct,
            "source": "broker",
        })

    # Sort by current value desc
    mapped.sort(key=lambda x: x["current_value"], reverse=True)

    total_invested = sum(h["invested"] for h in mapped)
    total_current = sum(h["current_value"] for h in mapped)
    total_pnl = round(total_current - total_invested, 2)

    return {
        "success": True,
        "data": mapped,
        "summary": {
            "total_invested": round(total_invested, 2),
            "total_current": round(total_current, 2),
            "total_pnl": total_pnl,
            "total_pnl_pct": round(total_pnl / total_invested * 100, 2) if total_invested > 0 else 0,
            "holdings_count": len(mapped),
        },
    }
