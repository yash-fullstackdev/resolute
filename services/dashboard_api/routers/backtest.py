"""
Backtest router.

POST /api/v1/backtest/run         → run a full backtest (synchronous, uses thread pool)
GET  /api/v1/backtest/strategies  → list strategies available for backtesting
GET  /api/v1/backtest/instruments → list available instruments and date ranges
"""

from __future__ import annotations

import asyncio
import concurrent.futures
import sys
import uuid
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any

import structlog
from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

logger = structlog.get_logger(service="dashboard_api", module="backtest")

router = APIRouter(prefix="/api/v1/backtest", tags=["backtest"])

# Thread pool for running CPU-bound backtest
_executor = concurrent.futures.ThreadPoolExecutor(max_workers=4)


# ── Request / Response models ─────────────────────────────────────────────────

class StrategyRunConfig(BaseModel):
    strategy_name: str
    instance_name: str | None = None
    params: dict[str, Any] = Field(default_factory=dict)
    primary_timeframe: int = 5
    capital_allocation: float = 100_000.0
    active_start: str = "09:20"        # HH:MM IST
    active_end: str = "14:30"
    square_off_time: str = "15:15"
    max_positions: int = 3
    max_drawdown_pct: float = 20.0
    max_loss_per_day: float = 0.0
    max_hold_bars: int = 20            # time-stop in 1m candles


class BacktestRunRequest(BaseModel):
    instruments: list[str] = Field(default_factory=lambda: ["NIFTY_50"])
    start_date: str                    # YYYY-MM-DD
    end_date: str                      # YYYY-MM-DD
    initial_capital: float = 500_000.0
    strategies: list[StrategyRunConfig]
    slippage_mode: str = "fixed"
    slippage_value: float = 0.5
    brokerage_preset: str = "zerodha_fno"
    lot_sizes: dict[str, int] = Field(default_factory=lambda: {"NIFTY_50": 75, "BANK_NIFTY": 30})


# ── Multi-strategy request models ─────────────────────────────────────────────

class BiasFilter(BaseModel):
    type: str                       # ema_crossover, supertrend, rsi_zone, ttm_momentum, macd_signal, ema_zone, price_vs_ema, bollinger_squeeze
    timeframe: int = 5              # candle timeframe in minutes
    params: dict[str, Any] = Field(default_factory=dict)


class BiasConfig(BaseModel):
    # New dynamic format
    bias_filters: list[BiasFilter] | None = None
    min_agreement: int = 2
    cooldown_bars: int = 10
    # Legacy toggle format (used if bias_filters is None)
    use_ema_bias: bool = True
    ema_short: int = 2
    ema_long: int = 11
    use_supertrend: bool = True
    st_period: int = 10
    st_multiplier: float = 3.0
    use_ttm_squeeze: bool = True
    use_ema33_zone: bool = True


class StrategySlot(BaseModel):
    name: str
    session: str = "all"            # "morning" | "afternoon" | "all"
    mode: str = "independent"       # "bias_filtered" | "independent"
    concurrent: bool = True
    max_fires_per_day: int = 5
    time_stop_bars: int = 20
    params: dict[str, Any] = Field(default_factory=dict)  # strategy-specific params


class ExitConfig(BaseModel):
    sl_atr_mult: float = 0.5
    tp_atr_mult: float = 1.5
    max_hold_bars: int = 20
    slippage_pts: float = 0.5


class MultiBacktestRequest(BaseModel):
    instrument: str = "NIFTY_50"
    start_date: str
    end_date: str
    bias_config: BiasConfig = Field(default_factory=BiasConfig)
    strategies: list[StrategySlot]
    exit_config: ExitConfig = Field(default_factory=ExitConfig)


def _error(code: str, message: str, status: int, details: dict | None = None) -> JSONResponse:
    return JSONResponse(
        status_code=status,
        content={
            "error": {"code": code, "message": message, "details": details or {}},
            "request_id": str(uuid.uuid4()),
            "timestamp": datetime.now(timezone.utc).isoformat(),
        },
    )


# ── Instruments endpoint ───────────────────────────────────────────────────────

@router.get("/instruments")
async def get_instruments(request: Request):
    """Return available instruments and their date ranges from data/ directory."""
    data_dir = _get_data_dir()
    instruments = []

    for inst_dir in sorted(data_dir.iterdir()):
        if not inst_dir.is_dir():
            continue
        files = sorted(inst_dir.glob("*_1m.json"))
        if not files:
            continue
        dates = []
        for f in files:
            name = f.stem.replace("_1m", "")
            try:
                dates.append(date.fromisoformat(name))
            except ValueError:
                pass
        if dates:
            instruments.append({
                "name": inst_dir.name,
                "display_name": inst_dir.name.replace("_", " ").title(),
                "start_date": str(min(dates)),
                "end_date": str(max(dates)),
                "trading_days": len(dates),
            })

    return JSONResponse(content={"instruments": instruments})


# ── Strategies endpoint ───────────────────────────────────────────────────────

@router.get("/strategies")
async def get_backtest_strategies(request: Request):
    """Return all strategies available for backtesting."""
    try:
        registry = _get_registry()
    except Exception as e:
        return _error("REGISTRY_ERROR", str(e), 500)

    strategies = []
    for name, cls in registry.items():
        category = getattr(cls, "category", None)
        category_str = str(category.value) if category else "TECHNICAL"
        strategies.append({
            "name": name,
            "display_name": name.replace("_", " ").title(),
            "category": category_str,
            "min_capital_tier": str(getattr(cls, "min_capital_tier", "STARTER")),
            "complexity": getattr(cls, "complexity", "SIMPLE"),
            "description": cls.__doc__.strip().split("\n")[0] if cls.__doc__ else "",
        })

    return JSONResponse(content={"strategies": strategies})


# ── Run endpoint ──────────────────────────────────────────────────────────────

@router.post("/run")
async def run_backtest(request: Request):
    """
    Run a backtest. Auto-detects multi-strategy (bias_config present) vs legacy format.
    """
    tenant_id = getattr(request.state, "tenant_id", "unknown")
    raw_body = await request.json()

    # Detect multi-strategy format (has bias_config or strategies[].name)
    is_multi = "bias_config" in raw_body or (
        raw_body.get("strategies") and isinstance(raw_body["strategies"][0], dict)
        and "name" in raw_body["strategies"][0]
    )

    if is_multi:
        try:
            body = MultiBacktestRequest(**raw_body)
        except Exception as e:
            return _error("VALIDATION_ERROR", str(e), 400)

        logger.info("backtest.multi.start", tenant_id=tenant_id,
                    instrument=body.instrument, strategies=[s.name for s in body.strategies])
        try:
            result = await asyncio.get_event_loop().run_in_executor(
                _executor, _run_multi_sync, body
            )
        except ValueError as e:
            return _error("VALIDATION_ERROR", str(e), 400)
        except Exception as e:
            import traceback
            logger.error("backtest.multi.error", error=str(e),
                         traceback=traceback.format_exc(), tenant_id=tenant_id)
            return _error("ENGINE_ERROR", f"Backtest failed: {e}", 500)
    else:
        try:
            body = BacktestRunRequest(**raw_body)
        except Exception as e:
            return _error("VALIDATION_ERROR", str(e), 400)

        logger.info("backtest.run.start", tenant_id=tenant_id,
                    instruments=body.instruments, strategies=[s.strategy_name for s in body.strategies])
        try:
            result = await asyncio.get_event_loop().run_in_executor(
                _executor, _run_sync, body
            )
        except ValueError as e:
            return _error("VALIDATION_ERROR", str(e), 400)
        except Exception as e:
            import traceback
            logger.error("backtest.run.error", error=str(e),
                         traceback=traceback.format_exc(), tenant_id=tenant_id)
            return _error("ENGINE_ERROR", f"Backtest failed: {e}", 500)

    logger.info("backtest.run.complete", tenant_id=tenant_id,
                total_trades=result.get("metrics", {}).get("total_trades", 0))
    return JSONResponse(content=result)


def _run_multi_sync(body: MultiBacktestRequest) -> dict:
    """Multi-strategy walk-forward backtest."""
    for root in [Path("/app"), Path(__file__).parent.parent.parent.parent]:
        if (root / "backtest").exists() and str(root) not in sys.path:
            sys.path.insert(0, str(root))
            break

    from backtest.multi_runner import run as multi_run

    config = {
        "instrument": body.instrument,
        "start_date": body.start_date,
        "end_date": body.end_date,
        "data_dir": str(_get_data_dir()),
        "bias_config": body.bias_config.model_dump(),
        "strategies": [s.model_dump() for s in body.strategies],
        "exit_config": body.exit_config.model_dump(),
    }
    return multi_run(config)


def _run_sync(body: BacktestRunRequest) -> dict:
    """Synchronous backtest execution (runs in thread pool)."""
    # Ensure /app (Docker) or repo root (local dev) is on sys.path
    for root in [Path("/app"), Path(__file__).parent.parent.parent.parent]:
        if (root / "backtest").exists() and str(root) not in sys.path:
            sys.path.insert(0, str(root))
            break

    from backtest.config import (
        BacktestConfig,
        BrokerageConfig,
        SlippageConfig,
        StrategyBacktestConfig,
    )
    from backtest.runner import run
    from datetime import time

    def _parse_time(s: str) -> time:
        h, m = s.split(":")
        return time(int(h), int(m))

    strategies = []
    for sc in body.strategies:
        strategies.append(StrategyBacktestConfig(
            strategy_name=sc.strategy_name,
            instance_name=sc.instance_name,
            params=sc.params,
            primary_timeframe=sc.primary_timeframe,
            capital_allocation=sc.capital_allocation,
            active_start=_parse_time(sc.active_start),
            active_end=_parse_time(sc.active_end),
            square_off_time=_parse_time(sc.square_off_time),
            max_positions=sc.max_positions,
            max_drawdown_pct=sc.max_drawdown_pct,
            max_loss_per_day=sc.max_loss_per_day,
            max_hold_bars=sc.max_hold_bars,
        ))

    config = BacktestConfig(
        instruments=body.instruments,
        start_date=date.fromisoformat(body.start_date),
        end_date=date.fromisoformat(body.end_date),
        data_dir=_get_data_dir(),
        initial_capital=body.initial_capital,
        strategies=strategies,
        slippage=SlippageConfig(mode=body.slippage_mode, value=body.slippage_value),
        brokerage=BrokerageConfig(preset=body.brokerage_preset),
        lot_sizes=body.lot_sizes,
    )
    return run(config)


# ── Helpers ───────────────────────────────────────────────────────────────────

def _get_data_dir() -> Path:
    import os
    # Prefer env var (set in Docker), fall back to repo-relative path for local dev
    env_path = os.environ.get("BACKTEST_DATA_PATH")
    if env_path:
        return Path(env_path)
    return Path(__file__).parent.parent.parent.parent / "data"


def _get_registry() -> dict:
    import os
    # In Docker: /app is the working dir, strategies at /app/services/user_worker_pool/strategies
    # In local dev: repo root is 4 levels up from this file
    candidates = [
        Path("/app"),                                          # Docker
        Path(__file__).parent.parent.parent.parent,           # local dev
        Path(os.environ.get("APP_ROOT", "/app")),             # explicit override
    ]
    for root in candidates:
        if (root / "services" / "user_worker_pool" / "strategies").exists():
            if str(root) not in sys.path:
                sys.path.insert(0, str(root))
            break
    from services.user_worker_pool.strategies import STRATEGY_REGISTRY
    return STRATEGY_REGISTRY
