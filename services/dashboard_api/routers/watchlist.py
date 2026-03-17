"""
Watchlist router — CRUD for per-tenant stock watchlists.

GET    /api/v1/watchlists              → List all watchlists
POST   /api/v1/watchlists              → Create a new watchlist
PUT    /api/v1/watchlists/{id}         → Update watchlist (name/symbols)
DELETE /api/v1/watchlists/{id}         → Delete a watchlist
"""

import json
import uuid
from datetime import datetime, timezone

import structlog
from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field
from sqlalchemy import text

from ..db import rls_session

logger = structlog.get_logger(service="dashboard_api", module="watchlist")

router = APIRouter(prefix="/api/v1/watchlists", tags=["watchlists"])

ALLOWED_SYMBOLS = {
    "NIFTY", "BANKNIFTY", "FINNIFTY", "RELIANCE", "HDFCBANK",
    "INFY", "TCS", "ICICIBANK", "SBIN", "TATAMOTORS", "BAJFINANCE",
    "LT", "MARUTI", "AXISBANK", "KOTAKBANK", "WIPRO", "TATASTEEL",
    "SUNPHARMA", "ADANIENT", "HINDALCO",
}


class WatchlistInput(BaseModel):
    name: str = Field(default="My Watchlist", min_length=1, max_length=50)
    symbols: list[str] = Field(default_factory=list)


def _error(code: str, message: str, status: int) -> JSONResponse:
    return JSONResponse(
        status_code=status,
        content={
            "error": {"code": code, "message": message, "details": {}},
            "request_id": str(uuid.uuid4()),
            "timestamp": datetime.now(timezone.utc).isoformat(),
        },
    )


def _map_watchlist(r: dict) -> dict:
    return {
        "id": str(r["id"]),
        "tenant_id": str(r["tenant_id"]),
        "name": r.get("name", ""),
        "symbols": r.get("symbols") or [],
        "created_at": r["created_at"].isoformat() if r.get("created_at") else "",
        "updated_at": r["updated_at"].isoformat() if r.get("updated_at") else "",
    }


@router.get("")
async def list_watchlists(request: Request):
    """List all watchlists for the authenticated tenant."""
    tenant_id = request.state.tenant_id

    async with rls_session(tenant_id) as session:
        result = await session.execute(
            text("SELECT * FROM watchlists WHERE tenant_id = :tid ORDER BY created_at"),
            {"tid": tenant_id},
        )
        rows = result.mappings().all()

    return {
        "success": True,
        "data": [_map_watchlist(dict(r)) for r in rows],
    }


@router.post("")
async def create_watchlist(request: Request, body: WatchlistInput):
    """Create a new watchlist."""
    tenant_id = request.state.tenant_id
    watchlist_id = str(uuid.uuid4())

    async with rls_session(tenant_id) as session:
        await session.execute(
            text("""
                INSERT INTO watchlists (id, tenant_id, name, symbols, created_at, updated_at)
                VALUES (:id, :tid, :name, CAST(:symbols AS jsonb), NOW(), NOW())
            """),
            {
                "id": watchlist_id,
                "tid": tenant_id,
                "name": body.name,
                "symbols": json.dumps(body.symbols),
            },
        )

    logger.info("watchlist_created", tenant_id=tenant_id, watchlist_id=watchlist_id)
    return {
        "success": True,
        "data": {
            "id": watchlist_id,
            "name": body.name,
            "symbols": body.symbols,
            "message": "Watchlist created.",
        },
    }


@router.put("/{watchlist_id}")
async def update_watchlist(request: Request, watchlist_id: str, body: WatchlistInput):
    """Update a watchlist's name or symbols."""
    tenant_id = request.state.tenant_id

    async with rls_session(tenant_id) as session:
        result = await session.execute(
            text("SELECT id FROM watchlists WHERE id = :id AND tenant_id = :tid"),
            {"id": watchlist_id, "tid": tenant_id},
        )
        if not result.fetchone():
            return _error("NOT_FOUND", "Watchlist not found.", 404)

        await session.execute(
            text("""
                UPDATE watchlists
                SET name = :name, symbols = CAST(:symbols AS jsonb), updated_at = NOW()
                WHERE id = :id AND tenant_id = :tid
            """),
            {
                "id": watchlist_id,
                "tid": tenant_id,
                "name": body.name,
                "symbols": json.dumps(body.symbols),
            },
        )

    logger.info("watchlist_updated", tenant_id=tenant_id, watchlist_id=watchlist_id)
    return {
        "success": True,
        "data": {"id": watchlist_id, "name": body.name, "symbols": body.symbols},
    }


@router.delete("/{watchlist_id}")
async def delete_watchlist(request: Request, watchlist_id: str):
    """Delete a watchlist."""
    tenant_id = request.state.tenant_id

    async with rls_session(tenant_id) as session:
        result = await session.execute(
            text("DELETE FROM watchlists WHERE id = :id AND tenant_id = :tid RETURNING id"),
            {"id": watchlist_id, "tid": tenant_id},
        )
        if not result.fetchone():
            return _error("NOT_FOUND", "Watchlist not found.", 404)

    logger.info("watchlist_deleted", tenant_id=tenant_id, watchlist_id=watchlist_id)
    return {"success": True, "data": {"message": "Watchlist deleted."}}
