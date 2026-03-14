"""
Indicator library — types, config, and result dataclasses.

Every indicator in the platform is registered in the ``IndicatorType`` enum.
Computation modules live in sibling files (moving_averages, oscillators, etc.).
"""

from __future__ import annotations

import enum
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any


# ---------------------------------------------------------------------------
# Indicator type registry
# ---------------------------------------------------------------------------

class IndicatorType(str, enum.Enum):
    """Complete indicator catalogue available to custom strategy builder."""

    # -- Lagging (trend confirmation) ---------------------------------------
    SMA = "SMA"
    EMA = "EMA"
    WMA = "WMA"
    DEMA = "DEMA"
    MACD = "MACD"
    MACD_HISTOGRAM = "MACD_HISTOGRAM"
    BOLLINGER_BANDS = "BOLLINGER_BANDS"
    SUPERTREND = "SUPERTREND"
    PARABOLIC_SAR = "PARABOLIC_SAR"
    ICHIMOKU = "ICHIMOKU"
    ADX = "ADX"
    MOVING_AVG_RIBBON = "MOVING_AVG_RIBBON"

    # -- Leading (predictive, momentum, oscillators) ------------------------
    RSI = "RSI"
    STOCHASTIC = "STOCHASTIC"
    STOCHASTIC_RSI = "STOCHASTIC_RSI"
    CCI = "CCI"
    WILLIAMS_R = "WILLIAMS_R"
    MFI = "MFI"
    ROC = "ROC"
    MOMENTUM = "MOMENTUM"

    # -- Volume-based -------------------------------------------------------
    VWAP = "VWAP"
    OBV = "OBV"
    VOLUME_PROFILE = "VOLUME_PROFILE"
    AD_LINE = "AD_LINE"

    # -- Volatility ---------------------------------------------------------
    ATR = "ATR"
    BOLLINGER_WIDTH = "BOLLINGER_WIDTH"
    KELTNER_CHANNEL = "KELTNER_CHANNEL"
    DONCHIAN_CHANNEL = "DONCHIAN_CHANNEL"
    INDIA_VIX = "INDIA_VIX"

    # -- Options-specific ---------------------------------------------------
    IV_RANK = "IV_RANK"
    IV_PERCENTILE = "IV_PERCENTILE"
    PCR_OI = "PCR_OI"
    PCR_VOLUME = "PCR_VOLUME"
    MAX_PAIN = "MAX_PAIN"
    OI_CHANGE = "OI_CHANGE"
    CALL_OI_CHANGE = "CALL_OI_CHANGE"
    PUT_OI_CHANGE = "PUT_OI_CHANGE"
    IV_SKEW = "IV_SKEW"


# ---------------------------------------------------------------------------
# Configuration & result dataclasses
# ---------------------------------------------------------------------------

@dataclass
class IndicatorConfig:
    """Configuration for a single indicator instance in a custom strategy."""

    indicator_type: IndicatorType
    params: dict[str, Any] = field(default_factory=dict)
    label: str = ""

    def __post_init__(self) -> None:
        if not self.label:
            param_str = "_".join(str(v) for v in self.params.values())
            self.label = f"{self.indicator_type.value}_{param_str}" if param_str else self.indicator_type.value


@dataclass
class IndicatorResult:
    """Result of computing a single indicator for one symbol at one point in time."""

    label: str
    current_value: float | dict[str, float]
    previous_value: float | dict[str, float]
    history: list[float | dict[str, float]] = field(default_factory=list)
    timestamp: datetime = field(default_factory=datetime.utcnow)


__all__ = [
    "IndicatorType",
    "IndicatorConfig",
    "IndicatorResult",
]
