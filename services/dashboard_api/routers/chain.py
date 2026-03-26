"""
Chain router — options chain data from Dhan API + market regime.

Architecture for multi-tenant scale:
  - Option chain is SHARED data (same for all users) — no tenant scoping.
  - Redis caches Dhan responses (3s TTL for chain, 5m for expiry list).
  - Single long-lived httpx client — no per-request TCP churn.
  - Dhan rate limit: 1 unique request per 3 seconds — our cache respects this.

GET /api/v1/chain/expiries/{underlying}  → Available expiry dates (cached 5m)
GET /api/v1/chain/{underlying}           → Option chain for nearest expiry
GET /api/v1/chain/{underlying}/{expiry}  → Option chain for specific expiry (cached 3s)
GET /api/v1/regime                       → Current market regime
"""

import json
import os
import uuid
from datetime import datetime, timezone

import httpx
import structlog
from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

logger = structlog.get_logger(service="dashboard_api", module="chain")

router = APIRouter(prefix="/api/v1", tags=["chain"])

DHAN_BASE_URL = "https://api.dhan.co/v2"
FEED_ACCESS_TOKEN = os.environ.get("FEED_ACCESS_TOKEN", "")
FEED_CLIENT_ID = os.environ.get("FEED_CLIENT_ID", "")

# Dhan security IDs for index underlyings
UNDERLYING_MAP: dict[str, dict] = {
    "NIFTY":      {"scrip": 13,  "seg": "IDX_I"},
    "BANKNIFTY":  {"scrip": 25,  "seg": "IDX_I"},
    "SENSEX":     {"scrip": 51,  "seg": "IDX_I"},
    "FINNIFTY":   {"scrip": 27,  "seg": "IDX_I"},
    "MIDCPNIFTY": {"scrip": 442, "seg": "IDX_I"},
}

# Cache TTLs (seconds)
CHAIN_CACHE_TTL = 3       # Matches Dhan's 1-request-per-3s rate limit
EXPIRY_CACHE_TTL = 300    # Expiry list changes rarely — cache 5 minutes

NATS_REQUEST_TIMEOUT = 5.0

# ── Long-lived Dhan HTTP client ─────────────────────────────────────────────
# Single connection pool reused across all requests — avoids TCP/TLS
# handshake per call.  Limits concurrency to 10 so we don't overwhelm Dhan.

_dhan_client: httpx.AsyncClient | None = None


def _get_dhan_client() -> httpx.AsyncClient:
    global _dhan_client
    if _dhan_client is None or _dhan_client.is_closed:
        _dhan_client = httpx.AsyncClient(
            base_url=DHAN_BASE_URL,
            headers={
                "access-token": FEED_ACCESS_TOKEN,
                "client-id": FEED_CLIENT_ID,
                "Content-Type": "application/json",
            },
            timeout=10.0,
            limits=httpx.Limits(
                max_connections=10,
                max_keepalive_connections=5,
            ),
        )
    return _dhan_client


async def close_dhan_client() -> None:
    """Graceful shutdown — call from app lifespan."""
    global _dhan_client
    if _dhan_client and not _dhan_client.is_closed:
        await _dhan_client.aclose()
        _dhan_client = None


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


def _parse_chain_response(data: dict) -> list[dict]:
    """Transform Dhan option chain response into flat rows for the frontend."""
    oc = data.get("oc", {})

    rows = []
    for strike_key, sides in oc.items():
        try:
            strike = float(strike_key)
        except (ValueError, TypeError):
            continue

        ce = sides.get("ce", {})
        pe = sides.get("pe", {})
        ce_greeks = ce.get("greeks", {})
        pe_greeks = pe.get("greeks", {})

        rows.append({
            "strike": strike,
            "call_ltp": ce.get("last_price", 0) or 0,
            "call_oi": ce.get("oi", 0) or 0,
            "call_volume": ce.get("volume", 0) or 0,
            "call_iv": ce.get("implied_volatility", 0) or 0,
            "call_delta": ce_greeks.get("delta", 0) or 0,
            "call_gamma": ce_greeks.get("gamma", 0) or 0,
            "call_theta": ce_greeks.get("theta", 0) or 0,
            "call_vega": ce_greeks.get("vega", 0) or 0,
            "call_bid": ce.get("top_bid_price", 0) or 0,
            "call_ask": ce.get("top_ask_price", 0) or 0,
            "put_ltp": pe.get("last_price", 0) or 0,
            "put_oi": pe.get("oi", 0) or 0,
            "put_volume": pe.get("volume", 0) or 0,
            "put_iv": pe.get("implied_volatility", 0) or 0,
            "put_delta": pe_greeks.get("delta", 0) or 0,
            "put_gamma": pe_greeks.get("gamma", 0) or 0,
            "put_theta": pe_greeks.get("theta", 0) or 0,
            "put_vega": pe_greeks.get("vega", 0) or 0,
            "put_bid": pe.get("top_bid_price", 0) or 0,
            "put_ask": pe.get("top_ask_price", 0) or 0,
        })

    rows.sort(key=lambda r: r["strike"])
    return rows


async def _cached_expiries(redis, underlying: str, mapping: dict) -> list[str]:
    """Fetch expiry list — Redis-cached for 5 minutes."""
    cache_key = f"chain:expiries:{underlying}"

    cached = await redis.get(cache_key)
    if cached:
        return json.loads(cached)

    client = _get_dhan_client()
    resp = await client.post(
        "/optionchain/expirylist",
        json={
            "UnderlyingScrip": mapping["scrip"],
            "UnderlyingSeg": mapping["seg"],
        },
    )
    resp.raise_for_status()
    body = resp.json()

    expiries = body if isinstance(body, list) else body.get("data", [])

    if expiries:
        await redis.setex(cache_key, EXPIRY_CACHE_TTL, json.dumps(expiries))

    logger.info("expiries_fetched_from_dhan", underlying=underlying, count=len(expiries))
    return expiries


async def _cached_chain(redis, underlying: str, expiry: str, mapping: dict) -> dict:
    """Fetch option chain — Redis-cached for 3 seconds (matches Dhan rate limit)."""
    cache_key = f"chain:data:{underlying}:{expiry}"

    cached = await redis.get(cache_key)
    if cached:
        return json.loads(cached)

    client = _get_dhan_client()
    resp = await client.post(
        "/optionchain",
        json={
            "UnderlyingScrip": mapping["scrip"],
            "UnderlyingSeg": mapping["seg"],
            "Expiry": expiry,
        },
    )
    resp.raise_for_status()
    body = resp.json()

    if body.get("status") != "success":
        logger.warning("dhan_chain_not_success", underlying=underlying, status=body.get("status"))
        return {"data": [], "spot_price": 0}

    chain_data = body.get("data", {})
    rows = _parse_chain_response(chain_data)
    spot_price = chain_data.get("last_price", 0) or 0

    result = {"data": rows, "spot_price": spot_price}

    await redis.setex(cache_key, CHAIN_CACHE_TTL, json.dumps(result))

    logger.info("chain_fetched_from_dhan", underlying=underlying, expiry=expiry, strikes=len(rows))
    return result


@router.get("/chain/expiries/{underlying}")
async def get_expiries(request: Request, underlying: str):
    """Fetch available expiry dates for an underlying (cached 5m in Redis)."""
    key = underlying.upper()
    if key not in UNDERLYING_MAP:
        return _error("INVALID_UNDERLYING", f"Unsupported underlying: {key}", 400)

    try:
        expiries = await _cached_expiries(request.app.state.redis, key, UNDERLYING_MAP[key])
        return {"success": True, "data": expiries}
    except Exception as exc:
        logger.warning("expiry_fetch_error", underlying=key, error=str(exc))
        return {"success": True, "data": []}


@router.get("/chain/{underlying}/{expiry}")
async def get_chain_with_expiry(request: Request, underlying: str, expiry: str):
    """Fetch option chain for a specific underlying and expiry (cached 3s in Redis)."""
    key = underlying.upper()
    if key not in UNDERLYING_MAP:
        return _error("INVALID_UNDERLYING", f"Unsupported underlying: {key}", 400)

    try:
        result = await _cached_chain(request.app.state.redis, key, expiry, UNDERLYING_MAP[key])
        return {"success": True, **result}
    except Exception as exc:
        logger.warning("chain_fetch_error", underlying=key, expiry=expiry, error=str(exc))
        return {"success": True, "data": [], "spot_price": 0}


@router.get("/chain/{underlying}")
async def get_chain(request: Request, underlying: str):
    """
    Fetch option chain for nearest expiry.
    First fetches cached expiry list, then gets cached chain for the nearest expiry.
    """
    key = underlying.upper()
    if key not in UNDERLYING_MAP:
        return _error("INVALID_UNDERLYING", f"Unsupported underlying: {key}", 400)

    mapping = UNDERLYING_MAP[key]
    redis = request.app.state.redis

    try:
        expiries = await _cached_expiries(redis, key, mapping)
        if not expiries:
            return {"success": True, "data": [], "spot_price": 0, "expiry": None, "expiries": []}

        nearest_expiry = expiries[0]
        result = await _cached_chain(redis, key, nearest_expiry, mapping)

        return {
            "success": True,
            **result,
            "expiry": nearest_expiry,
            "expiries": expiries,
        }
    except Exception as exc:
        logger.warning("chain_fetch_error", underlying=key, error=str(exc))
        return {"success": True, "data": [], "spot_price": 0, "expiry": None, "expiries": []}


@router.get("/regime")
async def get_regime(request: Request):
    """
    Get current market regime per underlying.
    This is shared data — no tenant scoping needed.
    """
    redis = request.app.state.redis

    default_regime = {
        "regime": "UNKNOWN",
        "description": "Market regime data not available",
    }

    try:
        cached = await redis.get("market:regime:all")
        if cached:
            regimes = json.loads(cached)
            regime_str = regimes if isinstance(regimes, str) else regimes.get("NIFTY", "UNKNOWN") if isinstance(regimes, dict) else "UNKNOWN"
            return {
                "success": True,
                "data": {
                    "regime": regime_str,
                    "description": f"Current market regime: {regime_str}",
                },
            }
    except Exception as exc:
        logger.warning("regime_redis_cache_miss", error=str(exc))

    nats_client = request.app.state.nats
    try:
        response = await nats_client.request(
            "regime.current",
            b"",
            timeout=NATS_REQUEST_TIMEOUT,
        )
        regime_data = json.loads(response.data.decode())

        try:
            await redis.setex("market:regime:all", 30, json.dumps(regime_data))
        except Exception:
            pass

        regime_str = regime_data if isinstance(regime_data, str) else regime_data.get("NIFTY", "UNKNOWN") if isinstance(regime_data, dict) else "UNKNOWN"
        return {
            "success": True,
            "data": {
                "regime": regime_str,
                "description": f"Current market regime: {regime_str}",
            },
        }
    except Exception as exc:
        logger.warning("regime_request_failed", error=str(exc))
        return {
            "success": True,
            "data": default_regime,
        }
