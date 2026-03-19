"""Pydantic v2 configuration models for the backtest engine."""

from __future__ import annotations

from datetime import date, time
from pathlib import Path
from typing import Any, Callable

from pydantic import BaseModel, Field, model_validator


class SlippageConfig(BaseModel):
    mode: str = "fixed"           # "fixed" | "percentage"
    value: float = 0.5            # points (fixed) or % of price


class BrokerageConfig(BaseModel):
    preset: str = "zerodha_fno"   # "zerodha_fno" | "flat" | "percentage" | "zero"
    flat_per_trade: float = 20.0
    percentage: float = 0.0

    def to_engine_params(self) -> dict:
        if self.preset == "zero":
            return {"brokerage_per_trade": 0.0, "brokerage_pct": 0.0}
        if self.preset == "percentage":
            return {"brokerage_per_trade": 0.0, "brokerage_pct": self.percentage}
        if self.preset == "flat":
            return {"brokerage_per_trade": self.flat_per_trade, "brokerage_pct": 0.0}
        # zerodha_fno: ₹20/trade flat, no percentage
        return {"brokerage_per_trade": 20.0, "brokerage_pct": 0.0}


class StrategyBacktestConfig(BaseModel):
    strategy_name: str                                  # key in STRATEGY_REGISTRY
    instance_name: str | None = None                    # optional label (same strategy, different params)
    params: dict[str, Any] = Field(default_factory=dict)  # passed to strategy.evaluate() as config

    # Timeframe
    primary_timeframe: int = 5                          # minutes
    additional_timeframes: list[int] = Field(default_factory=lambda: [1])

    # Session control (IST)
    active_start: time = time(9, 20)
    active_end: time = time(14, 30)
    square_off_time: time = time(15, 15)
    active_days: set[int] = Field(default_factory=lambda: {0, 1, 2, 3, 4})  # Mon-Fri

    # Risk & Sizing
    capital_allocation: float                           # INR, required
    position_size_method: str = "fixed_lots"
    position_size_value: float = 1.0
    max_positions: int = 3
    max_drawdown_pct: float = 20.0
    max_loss_per_day: float = 0.0                       # 0 = disabled
    max_hold_bars: int = 20                             # time-stop in 1m candles

    @property
    def effective_name(self) -> str:
        return self.instance_name or self.strategy_name

    def to_engine_dict(self, lot_sizes: dict[str, int], instrument: str = "NIFTY_50") -> dict:
        """Convert to dict consumed by Rust engine."""
        start_min = self.active_start.hour * 60 + self.active_start.minute
        end_min = self.active_end.hour * 60 + self.active_end.minute
        sq_min = self.square_off_time.hour * 60 + self.square_off_time.minute
        return {
            "name": self.effective_name,
            "primary_tf_minutes": self.primary_timeframe,
            "active_start_minutes": start_min,
            "active_end_minutes": end_min,
            "square_off_minutes": sq_min,
            "capital_allocation": self.capital_allocation,
            "max_positions": self.max_positions,
            "max_drawdown_pct": self.max_drawdown_pct,
            "max_loss_per_day": self.max_loss_per_day,
            "lot_size": lot_sizes.get(instrument, 75),
        }


class BacktestConfig(BaseModel):
    # Data
    instruments: list[str] = Field(default_factory=lambda: ["NIFTY_50"])
    start_date: date
    end_date: date
    data_dir: Path = Path("./data/")
    warmup_days: int = 10                               # trading days before start_date for indicator warmup

    # Strategies
    strategies: list[StrategyBacktestConfig]

    # Execution
    initial_capital: float                              # total INR
    slippage: SlippageConfig = Field(default_factory=SlippageConfig)
    brokerage: BrokerageConfig = Field(default_factory=BrokerageConfig)
    ambiguous_bar_resolution: str = "worst_case"        # "worst_case" | "optimistic"
    lot_sizes: dict[str, int] = Field(default_factory=lambda: {
        "NIFTY_50": 75,
        "BANK_NIFTY": 30,
    })

    # Output
    benchmark: str | None = "buy_and_hold"
    export_dir: Path | None = None

    @model_validator(mode="after")
    def validate_config(self) -> "BacktestConfig":
        if self.start_date >= self.end_date:
            raise ValueError("start_date must be before end_date")
        total_alloc = sum(s.capital_allocation for s in self.strategies)
        if total_alloc > self.initial_capital * 1.001:  # 0.1% tolerance for floats
            raise ValueError(
                f"Strategy capital allocations ({total_alloc:,.0f}) exceed "
                f"initial_capital ({self.initial_capital:,.0f})"
            )
        return self

    def to_engine_dict(self) -> dict:
        brok = self.brokerage.to_engine_params()
        return {
            "initial_capital": self.initial_capital,
            "slippage_mode": self.slippage.mode,
            "slippage_value": self.slippage.value,
            "ambiguous_bar_resolution": self.ambiguous_bar_resolution,
            **brok,
        }

    def start_ts(self) -> float:
        """Unix epoch for start of start_date in IST (09:00)."""
        from datetime import datetime, timezone, timedelta
        ist = timezone(timedelta(hours=5, minutes=30))
        dt = datetime(self.start_date.year, self.start_date.month, self.start_date.day,
                      9, 0, tzinfo=ist)
        return dt.timestamp()

    def end_ts(self) -> float:
        from datetime import datetime, timezone, timedelta
        ist = timezone(timedelta(hours=5, minutes=30))
        dt = datetime(self.end_date.year, self.end_date.month, self.end_date.day,
                      15, 31, tzinfo=ist)
        return dt.timestamp()

    def warmup_start_ts(self) -> float:
        """Start of warm-up window — warmup_days before start_date."""
        # Approximate: 1.5x calendar days to cover trading days + weekends/holidays
        from datetime import datetime, timezone, timedelta
        calendar_days = int(self.warmup_days * 1.6) + 5
        ist = timezone(timedelta(hours=5, minutes=30))
        dt = datetime(self.start_date.year, self.start_date.month, self.start_date.day,
                      9, 0, tzinfo=ist) - timedelta(days=calendar_days)
        return dt.timestamp()
