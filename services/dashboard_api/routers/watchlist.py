"""
Watchlist router — CRUD for per-tenant stock watchlists.

GET    /api/v1/watchlists              → List all watchlists
POST   /api/v1/watchlists              → Create a new watchlist
PUT    /api/v1/watchlists/{id}         → Update watchlist (name/symbols)
DELETE /api/v1/watchlists/{id}         → Delete a watchlist
GET    /api/v1/symbols/search?q=&limit= → Search NSE equity symbols
"""

import csv
import io
import json
import uuid
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from typing import Optional
from urllib.request import urlopen

import structlog
from fastapi import APIRouter, Query, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field
from sqlalchemy import text

from ..db import rls_session

logger = structlog.get_logger(service="dashboard_api", module="watchlist")

router = APIRouter(prefix="/api/v1/watchlists", tags=["watchlists"])
symbols_router = APIRouter(prefix="/api/v1/symbols", tags=["symbols"])

ALLOWED_SYMBOLS = {
    "NIFTY", "BANKNIFTY", "FINNIFTY", "RELIANCE", "HDFCBANK",
    "INFY", "TCS", "ICICIBANK", "SBIN", "TATAMOTORS", "BAJFINANCE",
    "LT", "MARUTI", "AXISBANK", "KOTAKBANK", "WIPRO", "TATASTEEL",
    "SUNPHARMA", "ADANIENT", "HINDALCO",
}

# ── Scrip master cache ──────────────────────────────────────────────────────

DHAN_SCRIP_MASTER_URL = "https://images.dhan.co/api-data/api-scrip-master.csv"

_nse_equity_cache: Optional[list[dict]] = None
_executor = ThreadPoolExecutor(max_workers=1)


def _download_and_parse_scrip_master() -> list[dict]:
    """Download Dhan scrip master CSV and extract NSE equity symbols."""
    logger.info("scrip_master_download_start", url=DHAN_SCRIP_MASTER_URL)
    response = urlopen(DHAN_SCRIP_MASTER_URL, timeout=30)  # noqa: S310
    raw = response.read().decode("utf-8", errors="replace")
    reader = csv.DictReader(io.StringIO(raw))

    symbols: list[dict] = []
    seen: set[str] = set()

    for row in reader:
        exchange = (row.get("SEM_EXM_EXCH_ID") or "").strip()
        instrument = (row.get("SEM_INSTRUMENT_NAME") or "").strip()
        if exchange != "NSE" or instrument != "EQUITY":
            continue

        trading_symbol = (row.get("SEM_TRADING_SYMBOL") or "").strip()
        if not trading_symbol:
            continue

        # Strip -EQ suffix
        symbol = trading_symbol.removesuffix("-EQ")
        if symbol in seen:
            continue
        seen.add(symbol)

        security_id_str = (row.get("SEM_SMST_SECURITY_ID") or "").strip()
        try:
            security_id = int(security_id_str)
        except (ValueError, TypeError):
            security_id = 0

        symbols.append({
            "symbol": symbol,
            "security_id": security_id,
        })

    symbols.sort(key=lambda s: s["symbol"])
    logger.info("scrip_master_parsed", count=len(symbols))
    return symbols


async def _get_nse_equities() -> list[dict]:
    """Return cached NSE equity list, downloading on first call."""
    global _nse_equity_cache  # noqa: PLW0603
    if _nse_equity_cache is not None:
        return _nse_equity_cache

    import asyncio
    loop = asyncio.get_running_loop()
    _nse_equity_cache = await loop.run_in_executor(
        _executor, _download_and_parse_scrip_master
    )
    return _nse_equity_cache


@symbols_router.get("/search")
async def search_symbols(
    request: Request,
    q: str = Query(default="", max_length=30),
    limit: int = Query(default=20, ge=1, le=100),
):
    """Search NSE equity symbols by prefix."""
    equities = await _get_nse_equities()

    if not q:
        return {"success": True, "data": []}

    query_upper = q.strip().upper()
    results = [
        {"symbol": s["symbol"], "security_id": s["security_id"]}
        for s in equities
        if s["symbol"].startswith(query_upper)
    ][:limit]

    return {"success": True, "data": results}


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
