"""
Strategies router — custom AI strategy builder endpoints.

GET    /api/v1/strategies/indicators           → Available indicators with param schemas
GET    /api/v1/strategies/recommended          → AI-recommended strategies for user tier
POST   /api/v1/strategies/custom               → Create new custom strategy (draft)
GET    /api/v1/strategies/custom               → List user's custom strategies
GET    /api/v1/strategies/custom/{id}          → Get custom strategy detail
PUT    /api/v1/strategies/custom/{id}          → Update custom strategy definition
DELETE /api/v1/strategies/custom/{id}          → Archive custom strategy
POST   /api/v1/strategies/custom/{id}/backtest → Run backtest
POST   /api/v1/strategies/custom/{id}/activate → Deploy strategy to worker
POST   /api/v1/strategies/ai/build             → AI builds from natural language
POST   /api/v1/strategies/ai/review            → AI reviews a strategy
"""

import json
import os
import uuid
from datetime import datetime, timezone

import structlog
from fastapi import APIRouter, Query, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field
from sqlalchemy import text

from ..db import rls_session
from ..models.schemas import (
    AIBuildRequest,
    AIReviewRequest,
    BacktestRequest,
    CustomStrategyInput,
    StrategyConfigUpdateInput,
)

logger = structlog.get_logger(service="dashboard_api", module="strategies")

router = APIRouter(prefix="/api/v1/strategies", tags=["strategies"])

# Custom strategy limits by tier
MAX_CUSTOM_STRATEGIES = {
    "SIGNAL": 0,
    "SEMI_AUTO": 3,
    "FULL_AUTO": 20,
}


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


# ── Indicator Library ────────────────────────────────────────────────────────

INDICATOR_LIBRARY = [
    {
        "indicator_type": "RSI",
        "name": "Relative Strength Index",
        "category": "LEADING",
        "description": "Momentum oscillator measuring speed and magnitude of price changes. Values 0-100.",
        "params_schema": [
            {"name": "period", "type": "int", "default": 14, "min_value": 2, "max_value": 100, "description": "Lookback period"},
        ],
        "output_fields": ["value"],
    },
    {
        "indicator_type": "EMA",
        "name": "Exponential Moving Average",
        "category": "LAGGING",
        "description": "Weighted moving average giving more importance to recent prices.",
        "params_schema": [
            {"name": "period", "type": "int", "default": 20, "min_value": 2, "max_value": 500, "description": "Lookback period"},
        ],
        "output_fields": ["value"],
    },
    {
        "indicator_type": "SMA",
        "name": "Simple Moving Average",
        "category": "LAGGING",
        "description": "Arithmetic mean of prices over a specified period.",
        "params_schema": [
            {"name": "period", "type": "int", "default": 20, "min_value": 2, "max_value": 500, "description": "Lookback period"},
        ],
        "output_fields": ["value"],
    },
    {
        "indicator_type": "MACD",
        "name": "Moving Average Convergence Divergence",
        "category": "LAGGING",
        "description": "Trend-following momentum indicator showing relationship between two EMAs.",
        "params_schema": [
            {"name": "fast", "type": "int", "default": 12, "min_value": 2, "max_value": 100, "description": "Fast EMA period"},
            {"name": "slow", "type": "int", "default": 26, "min_value": 5, "max_value": 200, "description": "Slow EMA period"},
            {"name": "signal", "type": "int", "default": 9, "min_value": 2, "max_value": 50, "description": "Signal line period"},
        ],
        "output_fields": ["line", "signal", "histogram"],
    },
    {
        "indicator_type": "BOLLINGER_BANDS",
        "name": "Bollinger Bands",
        "category": "LAGGING",
        "description": "Volatility bands placed above and below a moving average.",
        "params_schema": [
            {"name": "period", "type": "int", "default": 20, "min_value": 5, "max_value": 100, "description": "MA period"},
            {"name": "std_dev", "type": "float", "default": 2.0, "min_value": 0.5, "max_value": 5.0, "description": "Standard deviation multiplier"},
        ],
        "output_fields": ["upper", "middle", "lower"],
    },
    {
        "indicator_type": "SUPERTREND",
        "name": "SuperTrend",
        "category": "LAGGING",
        "description": "Trend indicator based on ATR. Gives buy/sell signals.",
        "params_schema": [
            {"name": "period", "type": "int", "default": 10, "min_value": 3, "max_value": 50, "description": "ATR period"},
            {"name": "multiplier", "type": "float", "default": 3.0, "min_value": 1.0, "max_value": 10.0, "description": "ATR multiplier"},
        ],
        "output_fields": ["value", "direction"],
    },
    {
        "indicator_type": "STOCHASTIC",
        "name": "Stochastic Oscillator",
        "category": "LEADING",
        "description": "Momentum indicator comparing closing price to price range over a period.",
        "params_schema": [
            {"name": "k_period", "type": "int", "default": 14, "min_value": 3, "max_value": 100, "description": "%K period"},
            {"name": "d_period", "type": "int", "default": 3, "min_value": 1, "max_value": 50, "description": "%D smoothing period"},
        ],
        "output_fields": ["k", "d"],
    },
    {
        "indicator_type": "VWAP",
        "name": "Volume Weighted Average Price",
        "category": "VOLUME",
        "description": "Average price weighted by volume — key intraday benchmark.",
        "params_schema": [],
        "output_fields": ["value"],
    },
    {
        "indicator_type": "ATR",
        "name": "Average True Range",
        "category": "VOLATILITY",
        "description": "Measures market volatility using high-low range.",
        "params_schema": [
            {"name": "period", "type": "int", "default": 14, "min_value": 2, "max_value": 100, "description": "Lookback period"},
        ],
        "output_fields": ["value"],
    },
    {
        "indicator_type": "ADX",
        "name": "Average Directional Index",
        "category": "LAGGING",
        "description": "Measures trend strength regardless of direction. Values 0-100.",
        "params_schema": [
            {"name": "period", "type": "int", "default": 14, "min_value": 5, "max_value": 100, "description": "Lookback period"},
        ],
        "output_fields": ["adx", "plus_di", "minus_di"],
    },
    {
        "indicator_type": "IV_RANK",
        "name": "Implied Volatility Rank",
        "category": "OPTIONS",
        "description": "Current IV as percentile of 52-week IV range. Key for selling strategies.",
        "params_schema": [],
        "output_fields": ["value"],
    },
    {
        "indicator_type": "IV_PERCENTILE",
        "name": "Implied Volatility Percentile",
        "category": "OPTIONS",
        "description": "Percentage of days IV was below current level in the past year.",
        "params_schema": [],
        "output_fields": ["value"],
    },
    {
        "indicator_type": "PCR_OI",
        "name": "Put-Call Ratio (OI)",
        "category": "OPTIONS",
        "description": "Ratio of put open interest to call open interest.",
        "params_schema": [],
        "output_fields": ["value"],
    },
    {
        "indicator_type": "OBV",
        "name": "On Balance Volume",
        "category": "VOLUME",
        "description": "Cumulative volume indicator using volume flow to predict price changes.",
        "params_schema": [],
        "output_fields": ["value"],
    },
    {
        "indicator_type": "CCI",
        "name": "Commodity Channel Index",
        "category": "LEADING",
        "description": "Measures price deviation from its statistical mean.",
        "params_schema": [
            {"name": "period", "type": "int", "default": 20, "min_value": 5, "max_value": 100, "description": "Lookback period"},
        ],
        "output_fields": ["value"],
    },
    {
        "indicator_type": "INDIA_VIX",
        "name": "India VIX",
        "category": "VOLATILITY",
        "description": "India's fear gauge — direct VIX feed as an indicator.",
        "params_schema": [],
        "output_fields": ["value"],
    },
    {
        "indicator_type": "MAX_PAIN",
        "name": "Max Pain Strike",
        "category": "OPTIONS",
        "description": "Strike price where option sellers lose the least money.",
        "params_schema": [],
        "output_fields": ["value"],
    },
    {
        "indicator_type": "OI_CHANGE",
        "name": "Open Interest Change",
        "category": "OPTIONS",
        "description": "Detects OI buildup/unwinding patterns.",
        "params_schema": [],
        "output_fields": ["call_oi_change", "put_oi_change", "net_oi_change"],
    },
]


@router.get("/status")
async def get_instance_status(request: Request):
    """Get runtime status for all strategy instances — shows if they're running."""
    tenant_id = request.state.tenant_id

    pool = getattr(request.app.state, "worker_pool", None)
    if pool is None:
        return {"success": True, "data": [], "worker_running": False}

    worker = pool.workers.get(tenant_id)
    if worker is None:
        return {"success": True, "data": [], "worker_running": False}

    try:
        statuses = worker.get_instance_statuses()
    except Exception:
        statuses = []

    # Add candle store health info
    candle_store = getattr(worker, "_candle_store", None)
    feed_healthy = False
    if candle_store:
        # Check if any symbol has fresh ticks
        for sym in getattr(candle_store, "_last_tick_time", {}).keys():
            if not candle_store.is_tick_stale(sym, 60):
                feed_healthy = True
                break

    return {
        "success": True,
        "data": statuses,
        "worker_running": True,
        "feed_healthy": feed_healthy,
        "total_instances": len(statuses),
    }


@router.get("")
async def list_strategies(request: Request):
    """List all built-in and custom strategies for the current user."""
    tenant_id = request.state.tenant_id
    tier = request.state.tier

    built_in_defs = [
        ("ttm_squeeze", "TTM Squeeze", "TECHNICAL", "STARTER", "Momentum breakout when Bollinger Bands squeeze inside Keltner Channels — fires on release with momentum confirmation"),
        ("supertrend_strategy", "Supertrend", "TECHNICAL", "STARTER", "Trend-following entry on Supertrend direction flip — catches major trend changes using ATR-based trailing bands"),
        ("vwap_supertrend", "VWAP + Supertrend", "TECHNICAL", "STARTER", "High-conviction entry combining VWAP proximity with Supertrend direction and volume confirmation"),
        ("ema_breakdown", "EMA Breakdown", "TECHNICAL", "STARTER", "EMA 2/11 crossover with RSI momentum — catches trends early with ATR regime filter"),
        ("rsi_vwap_scalp", "RSI VWAP Scalp", "TECHNICAL", "STARTER", "Mean-reversion scalp at VWAP bands — buys RSI oversold at lower band, sells overbought at upper band"),
        ("ema33_ob", "EMA 33 Pullback", "TECHNICAL", "STARTER", "33 EMA pullback-rejection with RSI zone filter and VWAP confirmation — catches trend continuations"),
        ("smc_order_block", "SMC Order Block", "TECHNICAL", "STARTER", "Smart Money Concepts — enters at institutional Order Blocks after Break of Structure with FVG and sweep confluence"),
        ("brahmaastra", "Brahmaastra", "BUYING", "STARTER", "9:15–10:15 AM first-hour strategy: Opening Range Breakout + PDH/PDL trap formation with wick rejection — 50% partial book at 1:1, kill switch at 10:30"),
        ("ema5_mean_reversion", "5 EMA Mean Reversion", "BUYING", "STARTER", "Counter-trend option buying on 5 EMA exhaustion — buys CE/PE when price floats far from 5 EMA and snaps back, 1:3 RR with 3-loss daily circuit breaker"),
        ("parent_child_momentum", "Parent-Child Momentum", "BUYING", "STARTER", "1H parent (EMA 10/30/100 + MACD) gates direction, 5m child MACD crossover triggers entry — 10:00–14:30 window, OTM strikes, structural swing SL"),
    ]

    # Configurable params for technical strategies (shown on UI)
    # Keys MUST match backtest STRATEGY_PARAMS for localStorage sync
    technical_params = {
        "ttm_squeeze": [
            {"name": "bb_period", "type": "number", "default_value": 20, "current_value": 20, "description": "Bollinger Bands lookback period", "min": 5, "max": 50},
            {"name": "bb_std", "type": "number", "default_value": 2.0, "current_value": 2.0, "description": "Bollinger Bands std deviation multiplier", "min": 0.5, "max": 4.0},
            {"name": "kc_period", "type": "number", "default_value": 20, "current_value": 20, "description": "Keltner Channel period", "min": 5, "max": 50},
            {"name": "kc_atr_period", "type": "number", "default_value": 10, "current_value": 10, "description": "Keltner Channel ATR period", "min": 5, "max": 30},
            {"name": "kc_mult", "type": "number", "default_value": 1.5, "current_value": 1.5, "description": "Keltner Channel ATR multiplier", "min": 0.5, "max": 5.0},
            {"name": "max_sl_points", "type": "number", "default_value": 50, "current_value": 50, "description": "Max stop loss in index points", "min": 1, "max": 500},
        ],
        "supertrend_strategy": [
            {"name": "period", "type": "number", "default_value": 10, "current_value": 10, "description": "Supertrend ATR period", "min": 3, "max": 50},
            {"name": "multiplier", "type": "number", "default_value": 3.0, "current_value": 3.0, "description": "Supertrend ATR multiplier", "min": 1.0, "max": 10.0},
            {"name": "max_sl_points", "type": "number", "default_value": 20, "current_value": 20, "description": "Max stop loss in index points", "min": 1, "max": 200},
        ],
        "vwap_supertrend": [
            {"name": "st_period", "type": "number", "default_value": 10, "current_value": 10, "description": "Supertrend ATR period", "min": 3, "max": 50},
            {"name": "st_multiplier", "type": "number", "default_value": 3.0, "current_value": 3.0, "description": "Supertrend ATR multiplier", "min": 1.0, "max": 10.0},
            {"name": "vwap_proximity_pct", "type": "number", "default_value": 0.0015, "current_value": 0.0015, "description": "Max distance from VWAP (decimal)", "min": 0.0005, "max": 0.01},
            {"name": "max_sl_points", "type": "number", "default_value": 20, "current_value": 20, "description": "Max stop loss in index points", "min": 1, "max": 200},
        ],
        "ema_breakdown": [
            {"name": "ema_short", "type": "number", "default_value": 2, "current_value": 2, "description": "Fast EMA period", "min": 1, "max": 50},
            {"name": "ema_long", "type": "number", "default_value": 11, "current_value": 11, "description": "Slow EMA period", "min": 2, "max": 100},
            {"name": "rsi_period", "type": "number", "default_value": 14, "current_value": 14, "description": "RSI lookback period", "min": 2, "max": 50},
            {"name": "breakaway_pct", "type": "number", "default_value": 0.0008, "current_value": 0.0008, "description": "Min breakaway from EMA (decimal)", "min": 0.0001, "max": 0.005},
            {"name": "max_sl_points", "type": "number", "default_value": 20, "current_value": 20, "description": "Max stop loss in index points", "min": 1, "max": 200},
        ],
        "rsi_vwap_scalp": [
            {"name": "rsi_period", "type": "number", "default_value": 14, "current_value": 14, "description": "RSI lookback period", "min": 2, "max": 50},
            {"name": "rsi_oversold", "type": "number", "default_value": 30, "current_value": 30, "description": "RSI oversold threshold (BUY)", "min": 5, "max": 50},
            {"name": "rsi_overbought", "type": "number", "default_value": 70, "current_value": 70, "description": "RSI overbought threshold (SELL)", "min": 50, "max": 95},
            {"name": "max_sl_points", "type": "number", "default_value": 15, "current_value": 15, "description": "Max stop loss in index points", "min": 1, "max": 200},
        ],
        "ema33_ob": [
            {"name": "ema_period", "type": "number", "default_value": 33, "current_value": 33, "description": "EMA period", "min": 5, "max": 200},
            {"name": "rsi_period", "type": "number", "default_value": 14, "current_value": 14, "description": "RSI lookback period", "min": 2, "max": 50},
            {"name": "rsi_bull_threshold", "type": "number", "default_value": 60, "current_value": 60, "description": "RSI bullish zone threshold", "min": 50, "max": 90},
            {"name": "rsi_bear_threshold", "type": "number", "default_value": 40, "current_value": 40, "description": "RSI bearish zone threshold", "min": 10, "max": 50},
            {"name": "pullback_atr_mult", "type": "number", "default_value": 0.5, "current_value": 0.5, "description": "Pullback distance (x ATR)", "min": 0.1, "max": 3.0},
            {"name": "rejection_body_pct", "type": "number", "default_value": 0.0004, "current_value": 0.0004, "description": "Min rejection body size (decimal)", "min": 0.0001, "max": 0.005},
            {"name": "max_sl_points", "type": "number", "default_value": 20, "current_value": 20, "description": "Max stop loss in index points", "min": 1, "max": 200},
        ],
        "smc_order_block": [
            {"name": "ob_length", "type": "number", "default_value": 6, "current_value": 6, "description": "Swing pivot lookback bars", "min": 3, "max": 20},
            {"name": "fvg_threshold", "type": "number", "default_value": 0.0005, "current_value": 0.0005, "description": "Min FVG gap size (decimal)", "min": 0.0001, "max": 0.005},
            {"name": "max_sl_points", "type": "number", "default_value": 20, "current_value": 20, "description": "Max stop loss in index points", "min": 1, "max": 200},
        ],
        "brahmaastra": [
            {"name": "gap_threshold_pct", "type": "number", "default_value": 0.4, "current_value": 0.4, "description": "Min gap % from prev close to qualify", "min": 0.1, "max": 2.0},
            {"name": "wick_ratio_min", "type": "number", "default_value": 1.5, "current_value": 1.5, "description": "Min wick-to-body ratio for trap candles", "min": 0.5, "max": 5.0},
            {"name": "partial_book_pct", "type": "number", "default_value": 50, "current_value": 50, "description": "% of position to book at 1:1 RR", "min": 0, "max": 100},
            {"name": "partial_book_rr", "type": "number", "default_value": 1.0, "current_value": 1.0, "description": "RR ratio for partial booking", "min": 0.5, "max": 2.0},
            {"name": "final_target_rr", "type": "number", "default_value": 1.5, "current_value": 1.5, "description": "Final target RR for remaining position", "min": 1.0, "max": 5.0},
            {"name": "strike_selection", "type": "select", "default_value": "ATM", "current_value": "ATM", "description": "Strike to buy", "options": ["ATM", "ITM"]},
        ],
        "ema5_mean_reversion": [
            {"name": "ema_period", "type": "number", "default_value": 5, "current_value": 5, "description": "EMA period", "min": 3, "max": 20},
            {"name": "min_distance_ema_pct", "type": "number", "default_value": 0.002, "current_value": 0.002, "description": "Min distance from EMA to qualify (decimal)", "min": 0.0005, "max": 0.01},
            {"name": "rr_min", "type": "number", "default_value": 3.0, "current_value": 3.0, "description": "Minimum reward:risk ratio", "min": 1.5, "max": 5.0},
            {"name": "daily_loss_limit", "type": "number", "default_value": 3, "current_value": 3, "description": "Max SL hits per day before halting", "min": 1, "max": 5},
            {"name": "min_india_vix", "type": "number", "default_value": 12.0, "current_value": 12.0, "description": "Min VIX to trade (below = skip)", "min": 8.0, "max": 20.0},
            {"name": "max_india_vix", "type": "number", "default_value": 35.0, "current_value": 35.0, "description": "Max VIX to trade (above = skip)", "min": 20.0, "max": 60.0},
            {"name": "strike_selection", "type": "select", "default_value": "ATM", "current_value": "ATM", "description": "Strike to buy", "options": ["ATM", "ITM"]},
        ],
        "parent_child_momentum": [
            {"name": "ema_short", "type": "number", "default_value": 10, "current_value": 10, "description": "Fast EMA period (1H parent)", "min": 3, "max": 30},
            {"name": "ema_mid", "type": "number", "default_value": 30, "current_value": 30, "description": "Mid EMA period (1H parent)", "min": 10, "max": 60},
            {"name": "ema_long", "type": "number", "default_value": 100, "current_value": 100, "description": "Slow EMA period (1H parent)", "min": 50, "max": 200},
            {"name": "macd_fast", "type": "number", "default_value": 48, "current_value": 48, "description": "MACD fast period (1H parent)", "min": 12, "max": 100},
            {"name": "macd_slow", "type": "number", "default_value": 104, "current_value": 104, "description": "MACD slow period (1H parent)", "min": 26, "max": 200},
            {"name": "macd_signal", "type": "number", "default_value": 36, "current_value": 36, "description": "MACD signal period (1H parent)", "min": 9, "max": 72},
            {"name": "strike_selection", "type": "select", "default_value": "1_OTM", "current_value": "1_OTM", "description": "Strike to buy", "options": ["ATM", "1_OTM", "2_OTM"]},
            {"name": "profit_target_pct", "type": "number", "default_value": 25, "current_value": 25, "description": "Premium profit target %", "min": 10, "max": 100},
        ],
    }

    # Load ALL user instances from DB (multiple per strategy allowed)
    user_instances: dict[str, list[dict]] = {}  # strategy_name → list of instance dicts
    try:
        async with rls_session(tenant_id) as session:
            result = await session.execute(
                text("""
                    SELECT id, strategy_name, instance_name, enabled, params,
                           trading_mode, session, max_daily_loss_pts, updated_at
                    FROM user_strategy_configs
                    WHERE tenant_id = :tenant_id
                    ORDER BY strategy_name, updated_at
                """),
                {"tenant_id": tenant_id},
            )
            for row in result.mappings().all():
                sname = row["strategy_name"]
                raw_params = row["params"] if isinstance(row["params"], dict) else {}
                instruments = raw_params.pop("instruments", [])
                bias_config = raw_params.pop("bias_config", None)
                exit_config = raw_params.pop("exit_config", None)

                inst = {
                    "instance_id": str(row["id"]),
                    "instance_name": row["instance_name"] or sname,
                    "enabled": row["enabled"],
                    "mode": row["trading_mode"] or "disabled",
                    "session": row["session"] or "all",
                    "max_daily_loss_pts": row["max_daily_loss_pts"],
                    "instruments": instruments if isinstance(instruments, list) else [],
                    "params": raw_params,
                    "updated_at": row["updated_at"].isoformat() if row["updated_at"] else None,
                }
                if bias_config and isinstance(bias_config, dict):
                    inst["bias_config"] = bias_config
                if exit_config and isinstance(exit_config, dict):
                    inst["exit_config"] = exit_config
                user_instances.setdefault(sname, []).append(inst)
    except Exception as exc:
        logger.warning("user_configs_load_failed", tenant_id=tenant_id, error=str(exc))

    built_in = []
    for sid, name, cat, tier_req, desc in built_in_defs:
        instances = user_instances.get(sid, [])

        # Merge saved param values into technical_params for the first instance
        # (backward compat — single instance case)
        first_params = instances[0]["params"] if instances else {}
        params_with_overrides = []
        for p in technical_params.get(sid, []):
            p_copy = dict(p)
            if p_copy["name"] in first_params:
                p_copy["current_value"] = first_params[p_copy["name"]]
            params_with_overrides.append(p_copy)

        entry = {
            "id": sid,
            "name": name,
            "display_name": name,
            "description": desc,
            "category": cat,
            "min_capital_tier": tier_req,
            "is_custom": False,
            "params": params_with_overrides,
            "instances": instances,
            # Backward compat fields (from first instance if exists)
            "enabled": instances[0]["enabled"] if instances else False,
            "instruments": instances[0]["instruments"] if instances else [],
        }
        if instances and instances[0].get("bias_config"):
            entry["bias_config"] = instances[0]["bias_config"]
        built_in.append(entry)

    return {"success": True, "data": built_in}


class StrategyToggleBody(BaseModel):
    enabled: bool
    instruments: list[str] = Field(default_factory=list)
    params: dict | None = None
    bias_config: dict | None = None  # per-strategy bias filter config


@router.patch("/{strategy_id}")
async def toggle_strategy(request: Request, strategy_id: str):
    """Toggle a built-in strategy's enabled state and persist to user_strategy_configs."""
    tenant_id = request.state.tenant_id

    body_bytes = await request.body()
    body = StrategyToggleBody.model_validate_json(body_bytes)

    try:
        async with rls_session(tenant_id) as session:
            # Check if config row exists
            existing = await session.execute(
                text("""
                    SELECT strategy_name FROM user_strategy_configs
                    WHERE tenant_id = :tenant_id AND strategy_name = :strategy_name
                """),
                {"tenant_id": tenant_id, "strategy_name": strategy_id},
            )

            # Build params with instruments and bias_config
            merged_params = body.params or {}
            if body.instruments:
                merged_params["instruments"] = body.instruments
            if body.bias_config is not None:
                merged_params["bias_config"] = body.bias_config

            if existing.first() is None:
                # Insert new config
                await session.execute(
                    text("""
                        INSERT INTO user_strategy_configs
                            (tenant_id, strategy_name, enabled, params, updated_at)
                        VALUES
                            (:tenant_id, :strategy_name, :enabled, CAST(:params AS jsonb), NOW())
                    """),
                    {
                        "tenant_id": tenant_id,
                        "strategy_name": strategy_id,
                        "enabled": body.enabled,
                        "params": json.dumps(merged_params),
                    },
                )
            else:
                # Update existing
                await session.execute(
                    text("""
                        UPDATE user_strategy_configs
                        SET enabled = :enabled,
                            params = COALESCE(params, '{}'::jsonb) || CAST(:params AS jsonb),
                            updated_at = NOW()
                        WHERE tenant_id = :tenant_id AND strategy_name = :strategy_name
                    """),
                    {
                        "tenant_id": tenant_id,
                        "strategy_name": strategy_id,
                        "enabled": body.enabled,
                        "params": json.dumps(merged_params),
                    },
                )

        # Publish config reload event to NATS so worker picks up changes
        nats_client = getattr(request.app.state, "nats", None)
        if nats_client:
            try:
                reload_msg = {
                    "tenant_id": tenant_id,
                    "strategy_name": strategy_id,
                    "enabled": body.enabled,
                    "event": "STRATEGY_TOGGLED",
                    "timestamp": datetime.now(timezone.utc).isoformat(),
                }
                await nats_client.publish(
                    f"worker.config_reload.{tenant_id}",
                    json.dumps(reload_msg).encode(),
                )
            except Exception as exc:
                logger.error("strategy_toggle_publish_failed", tenant_id=tenant_id, error=str(exc))

        logger.info(
            "strategy_toggled",
            tenant_id=tenant_id,
            strategy_id=strategy_id,
            enabled=body.enabled,
        )
    except Exception as exc:
        logger.error("strategy_toggle_failed", tenant_id=tenant_id, error=str(exc))
        return _error("INTERNAL", f"Failed to toggle strategy: {exc}", 500)

    return {
        "success": True,
        "data": {
            "id": strategy_id,
            "enabled": body.enabled,
            "instruments": body.instruments,
        },
    }


# ── Instance CRUD ─────────────────────────────────────────────────────────────

async def _ensure_feed_subscriptions(request: Request, instruments: list[str]):
    """Subscribe feed_gateway to any instruments not yet subscribed.

    Resolves security_ids from index map or scrip master,
    publishes to NATS feed.subscribe for each new instrument.
    """
    if not instruments:
        return

    from .watchlist import _publish_feed_subscriptions
    try:
        await _publish_feed_subscriptions(request, instruments)
    except Exception as exc:
        logger.warning("feed_subscribe_failed", instruments=instruments, error=str(exc))


class InstanceCreateBody(BaseModel):
    strategy_name: str
    instance_name: str
    instruments: list[str] = Field(default_factory=list)
    params: dict = Field(default_factory=dict)
    bias_config: dict | None = None
    exit_config: dict | None = None
    session: str = "all"
    mode: str = "paper"
    max_daily_loss_pts: float | None = None


class InstanceUpdateBody(BaseModel):
    instance_name: str | None = None
    enabled: bool | None = None
    instruments: list[str] | None = None
    params: dict | None = None
    bias_config: dict | None = None
    session: str | None = None
    mode: str | None = None
    max_daily_loss_pts: float | None = Field(default=None)


@router.post("/instances")
async def create_instance(request: Request, body: InstanceCreateBody):
    """Create a new strategy instance."""
    tenant_id = request.state.tenant_id

    merged_params = dict(body.params)
    if body.instruments:
        merged_params["instruments"] = body.instruments
        await _ensure_feed_subscriptions(request, body.instruments)
    if body.bias_config is not None:
        merged_params["bias_config"] = body.bias_config
    if body.exit_config is not None:
        merged_params["exit_config"] = body.exit_config

    try:
        async with rls_session(tenant_id) as session:
            result = await session.execute(
                text("""
                    INSERT INTO user_strategy_configs
                        (tenant_id, strategy_name, instance_name, enabled, params,
                         trading_mode, session, max_daily_loss_pts, updated_at)
                    VALUES
                        (:tenant_id, :strategy_name, :instance_name, :enabled,
                         CAST(:params AS jsonb), :mode, :session, :max_daily_loss, NOW())
                    RETURNING id
                """),
                {
                    "tenant_id": tenant_id,
                    "strategy_name": body.strategy_name,
                    "instance_name": body.instance_name,
                    "enabled": body.mode != "disabled",
                    "params": json.dumps(merged_params),
                    "mode": body.mode,
                    "session": body.session,
                    "max_daily_loss": body.max_daily_loss_pts,
                },
            )
            row = result.first()
            instance_id = str(row[0]) if row else None

        logger.info("instance_created", tenant_id=tenant_id,
                     strategy=body.strategy_name, instance=body.instance_name)

        # Publish config reload
        nats_client = getattr(request.app.state, "nats", None)
        if nats_client:
            try:
                await nats_client.publish(
                    f"worker.config_reload.{tenant_id}",
                    json.dumps({"tenant_id": tenant_id, "event": "INSTANCE_CREATED"}).encode(),
                )
            except Exception:
                pass

        return {"success": True, "data": {"instance_id": instance_id}}

    except Exception as exc:
        logger.error("instance_create_failed", tenant_id=tenant_id, error=str(exc))
        return _error("INTERNAL", f"Failed to create instance: {exc}", 500)


@router.patch("/instances/{instance_id}")
async def update_instance(request: Request, instance_id: str):
    """Update an existing strategy instance."""
    tenant_id = request.state.tenant_id

    body_bytes = await request.body()
    body = InstanceUpdateBody.model_validate_json(body_bytes)

    try:
        async with rls_session(tenant_id) as session:
            # Build dynamic SET clause
            updates = []
            bind_params: dict = {"tenant_id": tenant_id, "instance_id": instance_id}

            if body.instance_name is not None:
                updates.append("instance_name = :instance_name")
                bind_params["instance_name"] = body.instance_name

            if body.enabled is not None:
                updates.append("enabled = :enabled")
                bind_params["enabled"] = body.enabled

            if body.session is not None:
                updates.append("session = :session")
                bind_params["session"] = body.session

            if body.mode is not None:
                updates.append("trading_mode = :mode")
                bind_params["mode"] = body.mode

            if body.max_daily_loss_pts is not None:
                updates.append("max_daily_loss_pts = :max_daily_loss")
                bind_params["max_daily_loss"] = body.max_daily_loss_pts

            # Handle params merge (instruments + bias_config stored inside params JSONB)
            if body.params is not None or body.instruments is not None or body.bias_config is not None:
                merged = body.params or {}
                if body.instruments is not None:
                    merged["instruments"] = body.instruments
                if body.bias_config is not None:
                    merged["bias_config"] = body.bias_config
                updates.append("params = COALESCE(params, '{}'::jsonb) || CAST(:params AS jsonb)")
                bind_params["params"] = json.dumps(merged)

            if updates:
                updates.append("updated_at = NOW()")
                set_clause = ", ".join(updates)
                await session.execute(
                    text(f"""
                        UPDATE user_strategy_configs
                        SET {set_clause}
                        WHERE tenant_id = :tenant_id AND id = CAST(:instance_id AS uuid)
                    """),
                    bind_params,
                )

        logger.info("instance_updated", tenant_id=tenant_id, instance_id=instance_id)

        # Publish config reload
        nats_client = getattr(request.app.state, "nats", None)
        if nats_client:
            try:
                await nats_client.publish(
                    f"worker.config_reload.{tenant_id}",
                    json.dumps({"tenant_id": tenant_id, "event": "INSTANCE_UPDATED"}).encode(),
                )
            except Exception:
                pass

        return {"success": True}

    except Exception as exc:
        logger.error("instance_update_failed", tenant_id=tenant_id, error=str(exc))
        return _error("INTERNAL", f"Failed to update instance: {exc}", 500)


@router.delete("/instances/{instance_id}")
async def delete_instance(request: Request, instance_id: str):
    """Delete a strategy instance."""
    tenant_id = request.state.tenant_id

    try:
        async with rls_session(tenant_id) as session:
            await session.execute(
                text("""
                    DELETE FROM user_strategy_configs
                    WHERE tenant_id = :tenant_id AND id = CAST(:instance_id AS uuid)
                """),
                {"tenant_id": tenant_id, "instance_id": instance_id},
            )

        logger.info("instance_deleted", tenant_id=tenant_id, instance_id=instance_id)

        nats_client = getattr(request.app.state, "nats", None)
        if nats_client:
            try:
                await nats_client.publish(
                    f"worker.config_reload.{tenant_id}",
                    json.dumps({"tenant_id": tenant_id, "event": "INSTANCE_DELETED"}).encode(),
                )
            except Exception:
                pass

        return {"success": True}

    except Exception as exc:
        logger.error("instance_delete_failed", tenant_id=tenant_id, error=str(exc))
        return _error("INTERNAL", f"Failed to delete instance: {exc}", 500)


@router.post("/deploy")
async def deploy_to_instance(request: Request):
    """Deploy a backtest configuration as a new strategy instance (paper or live)."""
    tenant_id = request.state.tenant_id

    body_bytes = await request.body()
    body = InstanceCreateBody.model_validate_json(body_bytes)

    # Default to paper mode for safety
    if body.mode not in ("paper", "live"):
        body.mode = "paper"

    merged_params = dict(body.params)
    if body.instruments:
        merged_params["instruments"] = body.instruments
        await _ensure_feed_subscriptions(request, body.instruments)
    if body.bias_config is not None:
        merged_params["bias_config"] = body.bias_config
    if body.exit_config is not None:
        merged_params["exit_config"] = body.exit_config

    try:
        async with rls_session(tenant_id) as session:
            result = await session.execute(
                text("""
                    INSERT INTO user_strategy_configs
                        (tenant_id, strategy_name, instance_name, enabled, params,
                         trading_mode, session, max_daily_loss_pts, updated_at)
                    VALUES
                        (:tenant_id, :strategy_name, :instance_name, TRUE,
                         CAST(:params AS jsonb), :mode, :session, :max_daily_loss, NOW())
                    RETURNING id
                """),
                {
                    "tenant_id": tenant_id,
                    "strategy_name": body.strategy_name,
                    "instance_name": body.instance_name,
                    "params": json.dumps(merged_params),
                    "mode": body.mode,
                    "session": body.session,
                    "max_daily_loss": body.max_daily_loss_pts,
                },
            )
            row = result.first()
            instance_id = str(row[0]) if row else None

        logger.info("instance_deployed", tenant_id=tenant_id,
                     strategy=body.strategy_name, instance=body.instance_name, mode=body.mode)

        nats_client = getattr(request.app.state, "nats", None)
        if nats_client:
            try:
                await nats_client.publish(
                    f"worker.config_reload.{tenant_id}",
                    json.dumps({"tenant_id": tenant_id, "event": "INSTANCE_DEPLOYED"}).encode(),
                )
            except Exception:
                pass

        return {
            "success": True,
            "data": {
                "instance_id": instance_id,
                "instance_name": body.instance_name,
                "mode": body.mode,
            },
        }

    except Exception as exc:
        logger.error("deploy_failed", tenant_id=tenant_id, error=str(exc))
        return _error("INTERNAL", f"Failed to deploy instance: {exc}", 500)


@router.get("/{strategy_id}/config")
async def get_strategy_config(request: Request, strategy_id: str):
    """Get the user's config for a specific strategy."""
    tenant_id = request.state.tenant_id

    try:
        async with rls_session(tenant_id) as session:
            result = await session.execute(
                text("""
                    SELECT strategy_name, enabled, params, updated_at
                    FROM user_strategy_configs
                    WHERE tenant_id = :tenant_id AND strategy_name = :strategy_name
                """),
                {"tenant_id": tenant_id, "strategy_name": strategy_id},
            )
            row = result.mappings().first()
    except Exception as exc:
        logger.warning("strategy_config_query_failed", tenant_id=tenant_id, error=str(exc))
        row = None

    if not row:
        return {
            "success": True,
            "data": {
                "strategy_name": strategy_id,
                "enabled": False,
                "instruments": [],
                "params": {},
            },
        }

    params = row["params"] if isinstance(row["params"], dict) else {}
    instruments = params.pop("instruments", [])

    return {
        "success": True,
        "data": {
            "strategy_name": row["strategy_name"],
            "enabled": row["enabled"],
            "instruments": instruments,
            "params": params,
            "updated_at": row["updated_at"].isoformat() if row["updated_at"] else None,
        },
    }


class AIChatRequest(BaseModel):
    message: str = Field(min_length=1, max_length=2000)
    strategy_id: str | None = None
    history: list[dict[str, str]] = Field(default_factory=list)


AI_SYSTEM_PROMPT = (
    "You are an expert options trading strategy builder for Indian markets (NSE, MCX). "
    "Help the user design trading strategies using technical indicators like RSI, MACD, "
    "SuperTrend, Bollinger Bands, VWAP, IV Rank, PCR, etc. When the user describes a "
    "strategy, suggest specific entry/exit conditions, stop loss rules, and position sizing. "
    "Keep responses concise and actionable. Use Indian market terminology (NIFTY, BANKNIFTY, "
    "lots, expiry, etc.)."
)


@router.post("/ai/chat")
async def ai_chat(request: Request, body: AIChatRequest):
    """AI strategy chat — uses Claude API for strategy building assistance."""
    api_key = os.environ.get("ANTHROPIC_API_KEY", "")
    if not api_key:
        return {
            "reply": "Set ANTHROPIC_API_KEY in .env to enable AI strategy chat",
            "suggestions": [],
        }

    try:
        import anthropic

        client = anthropic.Anthropic(api_key=api_key)

        # Build messages from history + current message
        messages = []
        for entry in body.history:
            role = entry.get("role", "user")
            content = entry.get("content", "")
            if role in ("user", "assistant") and content:
                messages.append({"role": role, "content": content})
        messages.append({"role": "user", "content": body.message})

        response = client.messages.create(
            model="claude-sonnet-4-20250514",
            max_tokens=1024,
            system=AI_SYSTEM_PROMPT,
            messages=messages,
        )

        reply_text = response.content[0].text if response.content else ""

        return {
            "reply": reply_text,
            "suggestions": [],
        }

    except Exception as exc:
        logger.error("ai_chat_error", error=str(exc), service="dashboard_api")
        return {
            "reply": f"AI chat encountered an error: {str(exc)}",
            "suggestions": [],
        }


@router.get("/indicators")
async def list_indicators(request: Request):
    """List all available indicators with their parameter schemas."""
    return {"indicators": INDICATOR_LIBRARY}


@router.get("/recommended")
async def get_recommended_strategies(request: Request):
    """
    Get AI-recommended strategies based on the user's subscription tier.
    """
    tier = request.state.tier
    tenant_id = request.state.tenant_id

    recommendations = []

    # Always show buying strategies
    recommendations.extend([
        {
            "name": "RSI Reversal Buyer",
            "description": "Buy calls/puts on RSI oversold/overbought reversals with MACD confirmation.",
            "category": "BUYING",
            "indicators_used": ["RSI", "MACD"],
            "suitable_for": "Trending markets with clear reversals",
            "expected_win_rate": "45-55%",
        },
        {
            "name": "SuperTrend Momentum",
            "description": "Follow SuperTrend buy signals with volume confirmation using VWAP.",
            "category": "BUYING",
            "indicators_used": ["SUPERTREND", "VWAP", "OBV"],
            "suitable_for": "Strong trending markets",
            "expected_win_rate": "40-50%",
        },
        {
            "name": "Bollinger Squeeze Breakout",
            "description": "Buy when Bollinger Bands squeeze and ADX confirms trend strength.",
            "category": "BUYING",
            "indicators_used": ["BOLLINGER_BANDS", "ADX", "ATR"],
            "suitable_for": "Low volatility consolidation followed by breakout",
            "expected_win_rate": "42-52%",
        },
    ])

    # Show selling strategies for SEMI_AUTO+ tiers
    if tier in ("SEMI_AUTO", "FULL_AUTO"):
        recommendations.extend([
            {
                "name": "High IV Rank Seller",
                "description": "Sell strangles when IV Rank > 50 and PCR is neutral.",
                "category": "SELLING",
                "indicators_used": ["IV_RANK", "PCR_OI", "ATR"],
                "suitable_for": "High IV environments with range-bound markets",
                "expected_win_rate": "60-70%",
            },
            {
                "name": "Iron Condor Range Trader",
                "description": "Sell iron condors when IV is elevated and ADX shows no trend.",
                "category": "SELLING",
                "indicators_used": ["IV_RANK", "ADX", "BOLLINGER_BANDS"],
                "suitable_for": "Range-bound markets with elevated IV",
                "expected_win_rate": "55-65%",
            },
        ])

    logger.info("recommended_strategies_retrieved", tenant_id=tenant_id, tier=tier)
    return {"recommendations": recommendations}


# ── Custom Strategy CRUD ─────────────────────────────────────────────────────


@router.post("/custom")
async def create_custom_strategy(request: Request, body: CustomStrategyInput):
    """
    Create a new custom strategy (saved as DRAFT).
    Requires SEMI_AUTO tier. Enforces max strategy limits per tier.
    """
    tenant_id = request.state.tenant_id
    tier = request.state.tier

    max_allowed = MAX_CUSTOM_STRATEGIES.get(tier, 0)

    async with rls_session(tenant_id) as session:
        # Check current count
        count_result = await session.execute(
            text("""
                SELECT COUNT(*) FROM custom_strategies
                WHERE tenant_id = :tenant_id AND status != 'ARCHIVED'
            """),
            {"tenant_id": tenant_id},
        )
        current_count = count_result.scalar() or 0

        if current_count >= max_allowed:
            return _error(
                "FORBIDDEN",
                f"Custom strategy limit reached ({max_allowed} for {tier} tier). "
                "Upgrade your subscription or archive existing strategies.",
                403,
                details={"current": current_count, "max": max_allowed, "tier": tier},
            )

        # Check name uniqueness
        name_check = await session.execute(
            text("""
                SELECT id FROM custom_strategies
                WHERE tenant_id = :tenant_id AND name = :name AND status != 'ARCHIVED'
            """),
            {"tenant_id": tenant_id, "name": body.name},
        )
        if name_check.first():
            return _error(
                "VALIDATION_ERROR",
                f"A strategy named '{body.name}' already exists.",
                400,
            )

        strategy_id = str(uuid.uuid4())
        await session.execute(
            text("""
                INSERT INTO custom_strategies
                    (id, tenant_id, name, description, category, status,
                     target_symbols, target_segments, indicators,
                     entry_conditions, exit_conditions,
                     option_action, strike_selection, dte_min, dte_max,
                     stop_loss_pct, profit_target_pct,
                     time_stop_rule, max_positions_per_symbol,
                     created_at, updated_at)
                VALUES
                    (:id, :tenant_id, :name, :description, :category, 'DRAFT',
                     :target_symbols::jsonb, :target_segments::jsonb, :indicators::jsonb,
                     :entry_conditions::jsonb, :exit_conditions::jsonb,
                     :option_action, :strike_selection, :dte_min, :dte_max,
                     :stop_loss_pct, :profit_target_pct,
                     'eod', 1, NOW(), NOW())
            """),
            {
                "id": strategy_id,
                "tenant_id": tenant_id,
                "name": body.name,
                "description": body.description,
                "category": body.category,
                "target_symbols": json.dumps(body.target_symbols),
                "target_segments": json.dumps(["NSE_FO"]),
                "indicators": json.dumps([ind.model_dump() for ind in body.indicators]),
                "entry_conditions": json.dumps(
                    [[c.model_dump() for c in group] for group in body.entry_conditions]
                ),
                "exit_conditions": json.dumps(
                    [c.model_dump() for c in body.exit_conditions]
                ),
                "option_action": body.option_action,
                "strike_selection": body.strike_selection,
                "dte_min": body.dte_min,
                "dte_max": body.dte_max,
                "stop_loss_pct": body.stop_loss_pct,
                "profit_target_pct": body.profit_target_pct,
            },
        )

    logger.info(
        "custom_strategy_created",
        tenant_id=tenant_id,
        strategy_id=strategy_id,
        name=body.name,
    )

    return {
        "success": True,
        "data": {
            "id": strategy_id,
            "name": body.name,
            "status": "DRAFT",
        },
    }


@router.get("/custom")
async def list_custom_strategies(
    request: Request,
    status: str | None = None,
    limit: int = Query(default=20, le=50),
    offset: int = Query(default=0, ge=0),
):
    """List the authenticated user's custom strategies."""
    tenant_id = request.state.tenant_id

    async with rls_session(tenant_id) as session:
        query = "SELECT * FROM custom_strategies WHERE tenant_id = :tenant_id"
        params: dict = {"tenant_id": tenant_id}

        if status:
            query += " AND status = :status"
            params["status"] = status

        query += " ORDER BY updated_at DESC LIMIT :limit OFFSET :offset"
        params["limit"] = limit
        params["offset"] = offset

        result = await session.execute(text(query), params)
        rows = result.mappings().all()

        count_query = "SELECT COUNT(*) FROM custom_strategies WHERE tenant_id = :tenant_id"
        count_params: dict = {"tenant_id": tenant_id}
        if status:
            count_query += " AND status = :status"
            count_params["status"] = status
        count_result = await session.execute(text(count_query), count_params)
        total = count_result.scalar() or 0

    return {
        "strategies": [dict(r) for r in rows],
        "total": total,
    }


@router.get("/custom/{strategy_id}")
async def get_custom_strategy(request: Request, strategy_id: str):
    """Get a single custom strategy detail."""
    tenant_id = request.state.tenant_id

    async with rls_session(tenant_id) as session:
        result = await session.execute(
            text("""
                SELECT * FROM custom_strategies
                WHERE id = :id AND tenant_id = :tenant_id
            """),
            {"id": strategy_id, "tenant_id": tenant_id},
        )
        row = result.mappings().first()

    if not row:
        return _error("NOT_FOUND", f"Custom strategy {strategy_id} not found.", 404)

    return dict(row)


@router.put("/custom/{strategy_id}")
async def update_custom_strategy(
    request: Request,
    strategy_id: str,
    body: CustomStrategyInput,
):
    """Update a custom strategy definition. Can only update DRAFT or PAUSED strategies."""
    tenant_id = request.state.tenant_id

    async with rls_session(tenant_id) as session:
        existing = await session.execute(
            text("""
                SELECT status FROM custom_strategies
                WHERE id = :id AND tenant_id = :tenant_id
            """),
            {"id": strategy_id, "tenant_id": tenant_id},
        )
        row = existing.mappings().first()

        if not row:
            return _error("NOT_FOUND", f"Custom strategy {strategy_id} not found.", 404)

        if row["status"] not in ("DRAFT", "PAUSED", "BACKTESTED"):
            return _error(
                "VALIDATION_ERROR",
                f"Cannot update strategy in '{row['status']}' state. Pause it first.",
                400,
            )

        await session.execute(
            text("""
                UPDATE custom_strategies
                SET name = :name, description = :description, category = :category,
                    target_symbols = :target_symbols::jsonb,
                    indicators = :indicators::jsonb,
                    entry_conditions = :entry_conditions::jsonb,
                    exit_conditions = :exit_conditions::jsonb,
                    option_action = :option_action,
                    strike_selection = :strike_selection,
                    dte_min = :dte_min, dte_max = :dte_max,
                    stop_loss_pct = :stop_loss_pct,
                    profit_target_pct = :profit_target_pct,
                    status = 'DRAFT',
                    backtest_results = NULL,
                    updated_at = NOW()
                WHERE id = :id AND tenant_id = :tenant_id
            """),
            {
                "id": strategy_id,
                "tenant_id": tenant_id,
                "name": body.name,
                "description": body.description,
                "category": body.category,
                "target_symbols": json.dumps(body.target_symbols),
                "indicators": json.dumps([ind.model_dump() for ind in body.indicators]),
                "entry_conditions": json.dumps(
                    [[c.model_dump() for c in group] for group in body.entry_conditions]
                ),
                "exit_conditions": json.dumps(
                    [c.model_dump() for c in body.exit_conditions]
                ),
                "option_action": body.option_action,
                "strike_selection": body.strike_selection,
                "dte_min": body.dte_min,
                "dte_max": body.dte_max,
                "stop_loss_pct": body.stop_loss_pct,
                "profit_target_pct": body.profit_target_pct,
            },
        )

    logger.info("custom_strategy_updated", tenant_id=tenant_id, strategy_id=strategy_id)
    return {"message": "Custom strategy updated.", "id": strategy_id, "status": "DRAFT"}


@router.delete("/custom/{strategy_id}")
async def delete_custom_strategy(request: Request, strategy_id: str):
    """Archive (soft delete) a custom strategy."""
    tenant_id = request.state.tenant_id

    async with rls_session(tenant_id) as session:
        existing = await session.execute(
            text("""
                SELECT status FROM custom_strategies
                WHERE id = :id AND tenant_id = :tenant_id
            """),
            {"id": strategy_id, "tenant_id": tenant_id},
        )
        if not existing.first():
            return _error("NOT_FOUND", f"Custom strategy {strategy_id} not found.", 404)

        await session.execute(
            text("""
                UPDATE custom_strategies
                SET status = 'ARCHIVED', updated_at = NOW()
                WHERE id = :id AND tenant_id = :tenant_id
            """),
            {"id": strategy_id, "tenant_id": tenant_id},
        )

    logger.info("custom_strategy_archived", tenant_id=tenant_id, strategy_id=strategy_id)
    return {"message": "Custom strategy archived.", "id": strategy_id, "status": "ARCHIVED"}


@router.post("/custom/{strategy_id}/backtest")
async def backtest_custom_strategy(
    request: Request,
    strategy_id: str,
    body: BacktestRequest,
):
    """
    Run a backtest on a custom strategy.
    Publishes a backtest request to NATS and returns the job ID.
    Results are sent back asynchronously.
    """
    tenant_id = request.state.tenant_id

    async with rls_session(tenant_id) as session:
        existing = await session.execute(
            text("""
                SELECT id, status FROM custom_strategies
                WHERE id = :id AND tenant_id = :tenant_id
            """),
            {"id": strategy_id, "tenant_id": tenant_id},
        )
        row = existing.mappings().first()

        if not row:
            return _error("NOT_FOUND", f"Custom strategy {strategy_id} not found.", 404)

    # Try to publish backtest request to NATS; fall back to mock results
    nats_client = getattr(request.app.state, "nats", None)
    backtest_id = str(uuid.uuid4())
    try:
        if nats_client is not None:
            msg = {
                "backtest_id": backtest_id,
                "strategy_id": strategy_id,
                "tenant_id": tenant_id,
                "start_date": body.start_date.isoformat(),
                "end_date": body.end_date.isoformat(),
                "initial_capital": body.initial_capital,
                "timestamp": datetime.now(timezone.utc).isoformat(),
            }
            await nats_client.publish(
                f"backtest.request.{tenant_id}",
                json.dumps(msg).encode(),
            )

            logger.info(
                "backtest_requested",
                tenant_id=tenant_id,
                strategy_id=strategy_id,
                backtest_id=backtest_id,
            )

            return {
                "success": True,
                "data": {
                    "backtest_id": backtest_id,
                    "strategy_id": strategy_id,
                    "status": "SUBMITTED",
                    "message": "Backtest submitted. Results will be available shortly.",
                },
            }
    except Exception as exc:
        logger.warning("backtest_publish_failed", tenant_id=tenant_id, error=str(exc))

    # Return mock backtest results when NATS is unavailable or not connected
    logger.info(
        "backtest_mock_returned",
        tenant_id=tenant_id,
        strategy_id=strategy_id,
    )

    return {
        "success": True,
        "data": {
            "strategy_id": strategy_id,
            "period": "2024-01-01 to 2024-12-31",
            "total_trades": 0,
            "win_rate": 0,
            "total_pnl": 0,
            "max_drawdown": 0,
            "sharpe_ratio": 0,
            "status": "NO_DATA",
            "message": "Backtest requires historical tick data. Run the backtest runner with: make backtest",
        },
    }


@router.post("/custom/{strategy_id}/activate")
async def activate_custom_strategy(request: Request, strategy_id: str):
    """
    Activate a custom strategy — deploy it to the user's worker.
    Strategy must be in DRAFT or BACKTESTED state.
    """
    tenant_id = request.state.tenant_id

    async with rls_session(tenant_id) as session:
        existing = await session.execute(
            text("""
                SELECT status FROM custom_strategies
                WHERE id = :id AND tenant_id = :tenant_id
            """),
            {"id": strategy_id, "tenant_id": tenant_id},
        )
        row = existing.mappings().first()

        if not row:
            return _error("NOT_FOUND", f"Custom strategy {strategy_id} not found.", 404)

        if row["status"] not in ("DRAFT", "BACKTESTED", "PAUSED"):
            return _error(
                "VALIDATION_ERROR",
                f"Cannot activate strategy in '{row['status']}' state.",
                400,
            )

        await session.execute(
            text("""
                UPDATE custom_strategies
                SET status = 'ACTIVE', updated_at = NOW()
                WHERE id = :id AND tenant_id = :tenant_id
            """),
            {"id": strategy_id, "tenant_id": tenant_id},
        )

    # Notify worker via NATS
    nats_client = request.app.state.nats
    try:
        msg = {
            "tenant_id": tenant_id,
            "strategy_id": strategy_id,
            "event": "CUSTOM_STRATEGY_ACTIVATED",
            "timestamp": datetime.now(timezone.utc).isoformat(),
        }
        await nats_client.publish(
            f"worker.config_reload.{tenant_id}",
            json.dumps(msg).encode(),
        )
    except Exception as exc:
        logger.error("strategy_activate_publish_failed", tenant_id=tenant_id, error=str(exc))

    logger.info("custom_strategy_activated", tenant_id=tenant_id, strategy_id=strategy_id)

    return {"message": "Strategy activated.", "id": strategy_id, "status": "ACTIVE"}


# ── AI Endpoints (FULL_AUTO tier) ────────────────────────────────────────────


@router.post("/ai/build")
async def ai_build_strategy(request: Request, body: AIBuildRequest):
    """
    AI builds a custom strategy from natural language description.
    Requires FULL_AUTO tier (enforced by subscription middleware).
    Publishes to NATS for async processing, returns job ID.
    """
    tenant_id = request.state.tenant_id
    tier = request.state.tier

    nats_client = request.app.state.nats
    job_id = str(uuid.uuid4())

    try:
        msg = {
            "job_id": job_id,
            "tenant_id": tenant_id,
            "tier": tier,
            "description": body.description,
            "preferred_segments": body.preferred_segments,
            "event": "AI_BUILD_REQUEST",
            "timestamp": datetime.now(timezone.utc).isoformat(),
        }
        await nats_client.publish(
            f"strategies.ai.build.{tenant_id}",
            json.dumps(msg).encode(),
        )
    except Exception as exc:
        logger.error("ai_build_publish_failed", tenant_id=tenant_id, error=str(exc))
        return _error("SERVICE_UNAVAILABLE", "Failed to submit AI build request.", 503)

    logger.info("ai_build_requested", tenant_id=tenant_id, job_id=job_id)

    return {
        "job_id": job_id,
        "status": "SUBMITTED",
        "message": "AI strategy build request submitted. Check back shortly for results.",
    }


@router.post("/ai/review")
async def ai_review_strategy(request: Request, body: AIReviewRequest):
    """
    AI reviews a custom strategy for logical consistency, risk assessment,
    overfitting risk, and improvement suggestions.
    Requires FULL_AUTO tier (enforced by subscription middleware).
    """
    tenant_id = request.state.tenant_id

    # Verify strategy exists and belongs to tenant
    async with rls_session(tenant_id) as session:
        existing = await session.execute(
            text("""
                SELECT id FROM custom_strategies
                WHERE id = :id AND tenant_id = :tenant_id
            """),
            {"id": body.strategy_id, "tenant_id": tenant_id},
        )
        if not existing.first():
            return _error("NOT_FOUND", f"Custom strategy {body.strategy_id} not found.", 404)

    nats_client = request.app.state.nats
    job_id = str(uuid.uuid4())

    try:
        msg = {
            "job_id": job_id,
            "tenant_id": tenant_id,
            "strategy_id": body.strategy_id,
            "event": "AI_REVIEW_REQUEST",
            "timestamp": datetime.now(timezone.utc).isoformat(),
        }
        await nats_client.publish(
            f"strategies.ai.review.{tenant_id}",
            json.dumps(msg).encode(),
        )
    except Exception as exc:
        logger.error("ai_review_publish_failed", tenant_id=tenant_id, error=str(exc))
        return _error("SERVICE_UNAVAILABLE", "Failed to submit AI review request.", 503)

    logger.info("ai_review_requested", tenant_id=tenant_id, job_id=job_id)

    return {
        "job_id": job_id,
        "strategy_id": body.strategy_id,
        "status": "SUBMITTED",
        "message": "AI review request submitted. Check back shortly for results.",
    }
