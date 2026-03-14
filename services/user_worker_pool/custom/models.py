"""
Data models for the custom AI strategy builder.

Mirrors the spec exactly — CustomStrategyDefinition, Condition,
ConditionOperator, SpreadConfig, LegTemplate, AIReview, StrategySuggestion.
"""

from __future__ import annotations

import enum
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from .indicators import IndicatorConfig


# ---------------------------------------------------------------------------
# Condition operator
# ---------------------------------------------------------------------------

class ConditionOperator(str, enum.Enum):
    GT = ">"
    GTE = ">="
    LT = "<"
    LTE = "<="
    EQ = "=="
    NEQ = "!="
    CROSSES_ABOVE = "CROSSES_ABOVE"
    CROSSES_BELOW = "CROSSES_BELOW"
    TOUCHED = "TOUCHED"
    BETWEEN = "BETWEEN"
    INCREASING = "INCREASING"
    DECREASING = "DECREASING"


# ---------------------------------------------------------------------------
# Condition
# ---------------------------------------------------------------------------

@dataclass
class Condition:
    """A single evaluatable condition.

    Examples::

        RSI_14 < 30
        EMA_20 CROSSES_ABOVE EMA_50
        MACD.histogram > 0
        BOLLINGER_BANDS.lower TOUCHED (price touched lower band)
    """

    left_operand: str
    left_field: str | None = None
    operator: ConditionOperator = ConditionOperator.GT
    right_operand: str = ""
    right_field: str | None = None
    right_value: float | None = None


# ---------------------------------------------------------------------------
# Spread / leg templates
# ---------------------------------------------------------------------------

@dataclass
class LegTemplate:
    """Template for a single leg in a multi-leg strategy."""

    action: str          # "BUY" | "SELL"
    option_type: str     # "CE" | "PE"
    strike_offset: int   # 0 = ATM, +1 = 1 OTM, -1 = 1 ITM
    quantity_ratio: int = 1


@dataclass
class SpreadConfig:
    """Configuration for multi-leg custom strategies."""

    legs: list[LegTemplate] = field(default_factory=list)


# ---------------------------------------------------------------------------
# AI review / suggestion
# ---------------------------------------------------------------------------

@dataclass
class AIReview:
    """AI-generated review of a custom strategy."""

    overall_rating: str         # "STRONG" | "MODERATE" | "WEAK" | "RISKY"
    risk_score: float           # 0-100 (100 = very risky)
    issues: list[str] = field(default_factory=list)
    suggestions: list[str] = field(default_factory=list)
    overfitting_risk: str = "LOW"     # "LOW" | "MEDIUM" | "HIGH"
    regime_coverage: dict[str, str] = field(default_factory=dict)


@dataclass
class StrategySuggestion:
    """A single AI-generated suggestion for strategy improvement."""

    change_type: str            # "PARAM_TWEAK" | "ADD_CONDITION" | "REMOVE_CONDITION" | "SYMBOL_FOCUS"
    description: str = ""
    expected_improvement: str = ""
    confidence: float = 0.0     # 0-1


# ---------------------------------------------------------------------------
# Custom strategy definition
# ---------------------------------------------------------------------------

@dataclass
class CustomStrategyDefinition:
    """User-defined strategy stored in DB (``custom_strategies`` table).

    Evaluated by ``CustomStrategyWorker`` inside ``UserWorker``.
    """

    id: str = ""
    tenant_id: str = ""
    name: str = ""
    description: str = ""
    category: str = "BUYING"        # "BUYING" | "SELLING" | "HYBRID"
    status: str = "DRAFT"           # "DRAFT" | "BACKTESTED" | "ACTIVE" | "PAUSED" | "ARCHIVED"

    # Symbol deployment
    target_symbols: list[str] = field(default_factory=list)
    target_segments: list[str] = field(default_factory=list)

    # Indicator instances
    indicators: list[IndicatorConfig] = field(default_factory=list)

    # Conditions: entry is OR-of-AND-groups, exit is flat OR list
    entry_conditions: list[list[Condition]] = field(default_factory=list)
    exit_conditions: list[Condition] = field(default_factory=list)

    # Options leg template
    option_action: str = "BUY_CALL"
    strike_selection: str = "ATM"
    delta_target: float | None = None
    dte_min: int = 0
    dte_max: int = 30
    spread_config: SpreadConfig | None = None

    # Risk parameters
    stop_loss_pct: float = 30.0
    profit_target_pct: float = 60.0
    time_stop_rule: str = "eod"
    time_stop_value: str | None = None
    max_positions_per_symbol: int = 1

    # Metadata
    created_at: datetime = field(default_factory=datetime.utcnow)
    updated_at: datetime = field(default_factory=datetime.utcnow)
    backtest_results: dict[str, Any] | None = None
    ai_review_notes: str | None = None
