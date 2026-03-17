"""
Chain router — options chain snapshots and market regime data.

GET /api/v1/chain/{underlying}  → Latest chain snapshot via NATS request-reply
GET /api/v1/regime              → Current market regime per underlying (shared data)
"""

import json
import uuid
from datetime import datetime, timezone

import structlog
from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

logger = structlog.get_logger(service="dashboard_api", module="chain")

router = APIRouter(prefix="/api/v1", tags=["chain"])

# Timeout for NATS request-reply in seconds
NATS_REQUEST_TIMEOUT = 5.0


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


@router.get("/chain/{underlying}")
async def get_chain(request: Request, underlying: str):
    """
    Get the latest options chain snapshot for an underlying.
    Uses NATS request-reply to fetch the shared chain snapshot from signal_engine.
    This is shared data — no tenant scoping needed.
    """
    nats_client = request.app.state.nats

    # Determine segment from underlying
    segment = "mcx" if underlying.upper() in ("GOLD", "SILVER", "CRUDEOIL", "NATURALGAS") else "nse"
    subject = f"chain.{segment}.{underlying.upper()}"

    try:
        response = await nats_client.request(
            subject,
            b"",
            timeout=NATS_REQUEST_TIMEOUT,
        )
        chain_data = json.loads(response.data.decode())
        logger.info("chain_retrieved", underlying=underlying, segment=segment)
        return chain_data
    except Exception as exc:
        logger.warning("chain_request_failed", underlying=underlying, error=str(exc))
        return {
            "success": True,
            "data": {
                "underlying": underlying,
                "spot_price": 0,
                "expiry": "",
                "strikes": [],
                "pcr": 0,
                "message": "Chain data not available. Signal engine may not be processing this underlying yet.",
            },
        }


@router.get("/regime")
async def get_regime(request: Request):
    """
    Get current market regime per underlying.
    This is shared data — no tenant scoping needed.
    Fetches from Redis cache or NATS.
    """
    redis = request.app.state.redis

    try:
        # Try Redis cache first
        cached = await redis.get("market:regime:all")
        if cached:
            regimes = json.loads(cached)
            return {"regimes": regimes}
    except Exception as exc:
        logger.warning("regime_redis_cache_miss", error=str(exc))

    # Fall back to NATS request
    nats_client = request.app.state.nats
    try:
        response = await nats_client.request(
            "regime.current",
            b"",
            timeout=NATS_REQUEST_TIMEOUT,
        )
        regime_data = json.loads(response.data.decode())

        # Cache in Redis for 30s
        try:
            await redis.setex("market:regime:all", 30, json.dumps(regime_data))
        except Exception:
            pass

        return {"regimes": regime_data}
    except Exception as exc:
        logger.warning("regime_request_failed", error=str(exc))
        return {
            "success": True,
            "data": {
                "NIFTY": "UNKNOWN",
                "BANKNIFTY": "UNKNOWN",
                "FINNIFTY": "UNKNOWN",
            },
        }
