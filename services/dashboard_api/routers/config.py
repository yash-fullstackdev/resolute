"""
Config router — user strategy configuration and trading mode.

GET /api/v1/config                     → This user's strategy config
PUT /api/v1/config/strategy/{name}     → Update this user's strategy params
PUT /api/v1/config/trading-mode        → Switch live/paper mode
"""

import json
import uuid
from datetime import datetime, timezone

import structlog
from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse
from sqlalchemy import text

from ..db import rls_session
from ..models.schemas import StrategyConfigUpdateInput, TradingModeInput

logger = structlog.get_logger(service="dashboard_api", module="config")

router = APIRouter(prefix="/api/v1/config", tags=["config"])


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
async def get_config(request: Request):
    """Get the authenticated user's full strategy configuration."""
    tenant_id = request.state.tenant_id

    async with rls_session(tenant_id) as session:
        # Get strategy configs
        result = await session.execute(
            text("""
                SELECT strategy_name, enabled, params, portfolio_value_inr,
                       max_risk_per_trade_pct, updated_at
                FROM user_strategy_configs
                WHERE tenant_id = :tenant_id
                ORDER BY strategy_name
            """),
            {"tenant_id": tenant_id},
        )
        strategies = result.mappings().all()

        # Get trading mode from tenant record
        mode_result = await session.execute(
            text("SELECT trading_mode FROM tenants WHERE id = :tenant_id"),
            {"tenant_id": tenant_id},
        )
        mode_row = mode_result.mappings().first()
        trading_mode = mode_row["trading_mode"] if mode_row else "PAPER"

    logger.info("config_retrieved", tenant_id=tenant_id, strategies_count=len(strategies))

    return {
        "tenant_id": tenant_id,
        "trading_mode": trading_mode,
        "strategies": [
            {
                "strategy_name": s["strategy_name"],
                "enabled": s["enabled"],
                "params": s["params"] if isinstance(s["params"], dict) else {},
                "portfolio_value_inr": float(s["portfolio_value_inr"]),
                "max_risk_per_trade_pct": float(s["max_risk_per_trade_pct"]),
                "updated_at": s["updated_at"].isoformat() if s["updated_at"] else None,
            }
            for s in strategies
        ],
    }


@router.put("/strategy/{strategy_name}")
async def update_strategy_config(
    request: Request,
    strategy_name: str,
    body: StrategyConfigUpdateInput,
):
    """
    Update strategy parameters for the authenticated user.
    Writes to user_strategy_configs table and publishes config reload to NATS.
    Requires SEMI_AUTO tier (enforced by subscription middleware).
    """
    tenant_id = request.state.tenant_id

    async with rls_session(tenant_id) as session:
        # Check if config exists
        existing = await session.execute(
            text("""
                SELECT strategy_name FROM user_strategy_configs
                WHERE tenant_id = :tenant_id AND strategy_name = :strategy_name
            """),
            {"tenant_id": tenant_id, "strategy_name": strategy_name},
        )

        if existing.first() is None:
            # Insert new config
            await session.execute(
                text("""
                    INSERT INTO user_strategy_configs
                        (tenant_id, strategy_name, enabled, params, portfolio_value_inr,
                         max_risk_per_trade_pct, updated_at)
                    VALUES
                        (:tenant_id, :strategy_name, :enabled, :params::jsonb,
                         :portfolio_value_inr, :max_risk_per_trade_pct, NOW())
                """),
                {
                    "tenant_id": tenant_id,
                    "strategy_name": strategy_name,
                    "enabled": body.enabled if body.enabled is not None else True,
                    "params": json.dumps(body.params),
                    "portfolio_value_inr": body.portfolio_value_inr or 100_000,
                    "max_risk_per_trade_pct": body.max_risk_per_trade_pct or 2.0,
                },
            )
        else:
            # Build dynamic update
            set_clauses = ["updated_at = NOW()"]
            params: dict = {
                "tenant_id": tenant_id,
                "strategy_name": strategy_name,
            }

            if body.enabled is not None:
                set_clauses.append("enabled = :enabled")
                params["enabled"] = body.enabled

            if body.params:
                set_clauses.append("params = :params::jsonb")
                params["params"] = json.dumps(body.params)

            if body.portfolio_value_inr is not None:
                set_clauses.append("portfolio_value_inr = :portfolio_value_inr")
                params["portfolio_value_inr"] = body.portfolio_value_inr

            if body.max_risk_per_trade_pct is not None:
                set_clauses.append("max_risk_per_trade_pct = :max_risk_per_trade_pct")
                params["max_risk_per_trade_pct"] = body.max_risk_per_trade_pct

            await session.execute(
                text(f"""
                    UPDATE user_strategy_configs
                    SET {', '.join(set_clauses)}
                    WHERE tenant_id = :tenant_id AND strategy_name = :strategy_name
                """),
                params,
            )

    # Publish config reload event to NATS
    nats_client = request.app.state.nats
    try:
        reload_msg = {
            "tenant_id": tenant_id,
            "strategy_name": strategy_name,
            "event": "CONFIG_UPDATED",
            "timestamp": datetime.now(timezone.utc).isoformat(),
        }
        await nats_client.publish(
            f"worker.config_reload.{tenant_id}",
            json.dumps(reload_msg).encode(),
        )
        logger.info(
            "config_reload_published",
            tenant_id=tenant_id,
            strategy_name=strategy_name,
        )
    except Exception as exc:
        logger.error(
            "config_reload_publish_failed",
            tenant_id=tenant_id,
            error=str(exc),
        )

    logger.info(
        "strategy_config_updated",
        tenant_id=tenant_id,
        strategy_name=strategy_name,
    )

    return {"message": f"Strategy config '{strategy_name}' updated.", "strategy_name": strategy_name}


@router.put("/trading-mode")
async def update_trading_mode(request: Request, body: TradingModeInput):
    """
    Switch the authenticated user between LIVE and PAPER trading mode.
    Requires SEMI_AUTO tier (enforced by subscription middleware).
    """
    tenant_id = request.state.tenant_id

    async with rls_session(tenant_id) as session:
        await session.execute(
            text("UPDATE tenants SET trading_mode = :mode WHERE id = :tenant_id"),
            {"tenant_id": tenant_id, "mode": body.mode},
        )

    # Publish mode change to NATS
    nats_client = request.app.state.nats
    try:
        msg = {
            "tenant_id": tenant_id,
            "trading_mode": body.mode,
            "event": "TRADING_MODE_CHANGED",
            "timestamp": datetime.now(timezone.utc).isoformat(),
        }
        await nats_client.publish(
            f"worker.config_reload.{tenant_id}",
            json.dumps(msg).encode(),
        )
    except Exception as exc:
        logger.error(
            "trading_mode_publish_failed",
            tenant_id=tenant_id,
            error=str(exc),
        )

    logger.info(
        "trading_mode_updated",
        tenant_id=tenant_id,
        mode=body.mode,
    )

    return {"message": f"Trading mode switched to {body.mode}.", "trading_mode": body.mode}
