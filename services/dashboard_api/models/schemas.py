"""
Pydantic response/request models for dashboard_api.

All response models follow the standardized error format from the security spec.
"""

import uuid
from datetime import date, datetime
from typing import Any

from pydantic import BaseModel, Field, field_validator


# ── Standardized Error Response ──────────────────────────────────────────────


class ErrorDetail(BaseModel):
    code: str
    message: str
    details: dict[str, Any] = Field(default_factory=dict)


class ErrorResponse(BaseModel):
    error: ErrorDetail
    request_id: str = Field(default_factory=lambda: str(uuid.uuid4()))
    timestamp: datetime = Field(default_factory=datetime.utcnow)


# ── Health Check ─────────────────────────────────────────────────────────────


class HealthCheckItem(BaseModel):
    status: str
    latency_ms: float | None = None


class HealthCheckResponse(BaseModel):
    status: str
    timestamp: datetime
    version: str
    checks: dict[str, HealthCheckItem]


# ── Order Leg ────────────────────────────────────────────────────────────────


class OrderLegResponse(BaseModel):
    symbol: str
    exchange: str
    instrument_type: str
    strike: float
    expiry: date | None = None
    action: str
    quantity: int
    lot_size: int
    order_type: str
    limit_price: float | None = None
    product: str


# ── Position Greeks ──────────────────────────────────────────────────────────


class PositionGreeksResponse(BaseModel):
    delta: float
    gamma: float
    theta: float
    vega: float
    net_premium: float


# ── Order Response ───────────────────────────────────────────────────────────


class OrderResponse(BaseModel):
    id: str
    signal_id: str
    broker_order_id: str | None = None
    leg: OrderLegResponse
    status: str
    fill_price: float | None = None
    fill_time: datetime | None = None
    error: str | None = None


# ── Position Response ────────────────────────────────────────────────────────


class PositionResponse(BaseModel):
    id: str
    tenant_id: str
    strategy_name: str
    underlying: str
    legs: list[OrderResponse]
    entry_time: datetime
    entry_cost_inr: float
    current_value_inr: float
    unrealised_pnl_inr: float
    realised_pnl_inr: float
    stop_loss_price: float
    target_price: float
    time_stop: datetime
    status: str
    greeks: PositionGreeksResponse


class PositionListResponse(BaseModel):
    positions: list[PositionResponse]
    total: int


# ── Signal Response ──────────────────────────────────────────────────────────


class SignalResponse(BaseModel):
    id: str
    tenant_id: str
    timestamp: datetime
    strategy_name: str
    underlying: str
    segment: str
    direction: str
    strategy_source: str
    custom_strategy_id: str | None = None
    strength: float
    regime: str
    legs: list[OrderLegResponse]
    max_loss_inr: float
    target_profit_inr: float
    stop_loss_pct: float
    time_stop: datetime
    rationale: str


class SignalListResponse(BaseModel):
    signals: list[SignalResponse]
    total: int


# ── Performance Response ─────────────────────────────────────────────────────


class PerformanceResponse(BaseModel):
    tenant_id: str
    total_pnl_inr: float
    realised_pnl_inr: float
    unrealised_pnl_inr: float
    total_trades: int
    winning_trades: int
    losing_trades: int
    win_rate: float
    avg_win_inr: float
    avg_loss_inr: float
    max_drawdown_inr: float
    sharpe_ratio: float | None = None
    profit_factor: float | None = None
    period_start: datetime | None = None
    period_end: datetime | None = None


class DailyPnlItem(BaseModel):
    date: date
    pnl_inr: float
    trades: int
    cumulative_pnl_inr: float


class DailyPerformanceResponse(BaseModel):
    tenant_id: str
    daily: list[DailyPnlItem]


# ── Trading Plan ─────────────────────────────────────────────────────────────


class TradingPlanInput(BaseModel):
    enabled_strategies: list[str] = Field(min_length=1, max_length=20)
    active_underlyings: list[str] = Field(min_length=1, max_length=30)
    max_trades_per_day: int = Field(ge=1, le=50)
    daily_loss_limit_inr: float = Field(ge=1000, le=10_000_000)
    daily_profit_target_inr: float | None = Field(None, ge=0)
    notes: str = Field(max_length=2000, default="")


class PlanResponse(BaseModel):
    id: str
    tenant_id: str
    date: date
    status: str  # "DRAFT" | "LOCKED"
    enabled_strategies: list[str]
    active_underlyings: list[str]
    max_trades_per_day: int
    daily_loss_limit_inr: float
    daily_profit_target_inr: float | None = None
    notes: str
    locked_at: datetime | None = None
    created_at: datetime
    updated_at: datetime


class PlanListResponse(BaseModel):
    plans: list[PlanResponse]
    total: int


# ── Discipline Score ─────────────────────────────────────────────────────────


class DisciplineScoreResponse(BaseModel):
    tenant_id: str
    score: float  # 0–100
    components: dict[str, float]
    trend: str  # "IMPROVING" | "STABLE" | "DECLINING"
    last_updated: datetime


class CircuitBreakerResponse(BaseModel):
    tenant_id: str
    is_halted: bool
    halted_at: datetime | None = None
    halt_reason: str | None = None
    reset_at: datetime | None = None
    daily_loss_inr: float
    daily_loss_limit_inr: float


# ── Override ─────────────────────────────────────────────────────────────────


class OverrideRequestInput(BaseModel):
    position_id: str = Field(pattern=r"^[0-9a-f-]{36}$")
    override_type: str = Field(
        pattern=r"^(STOP_LOSS_MOVE|EARLY_EXIT|TIME_STOP_EXTEND)$"
    )
    proposed_value: float = Field(gt=0)
    reason: str = Field(min_length=10, max_length=500)


class OverrideResponse(BaseModel):
    id: str
    tenant_id: str
    position_id: str
    override_type: str
    proposed_value: float
    reason: str
    status: str  # "PENDING_COOLDOWN" | "AWAITING_CONFIRM" | "CONFIRMED" | "EXPIRED" | "REJECTED"
    cooldown_expires_at: datetime | None = None
    confirmed_at: datetime | None = None
    created_at: datetime


class OverrideListResponse(BaseModel):
    overrides: list[OverrideResponse]
    total: int


# ── Journal ──────────────────────────────────────────────────────────────────


class JournalEntryResponse(BaseModel):
    id: str
    tenant_id: str
    position_id: str
    strategy_name: str
    underlying: str
    direction: str
    entry_time: datetime
    exit_time: datetime | None = None
    entry_price_inr: float
    exit_price_inr: float | None = None
    pnl_inr: float
    pnl_pct: float
    plan_adherence: bool
    override_used: bool
    post_trade_notes: str | None = None
    auto_notes: str | None = None
    tags: list[str] = Field(default_factory=list)
    created_at: datetime
    updated_at: datetime


class JournalPatchInput(BaseModel):
    post_trade_notes: str = Field(min_length=1, max_length=5000)
    tags: list[str] = Field(default_factory=list, max_length=20)


class JournalListResponse(BaseModel):
    entries: list[JournalEntryResponse]
    total: int
    page: int
    page_size: int


# ── Weekly Report ────────────────────────────────────────────────────────────


class WeeklyReportResponse(BaseModel):
    id: str
    tenant_id: str
    week_start: date
    week_end: date
    total_pnl_inr: float
    total_trades: int
    win_rate: float
    discipline_score: float
    plan_adherence_rate: float
    overrides_count: int
    circuit_breaker_events: int
    top_strategy: str | None = None
    worst_strategy: str | None = None
    summary: str
    created_at: datetime


class WeeklyReportListResponse(BaseModel):
    reports: list[WeeklyReportResponse]
    total: int


# ── Options Chain ────────────────────────────────────────────────────────────


class StrikeDataResponse(BaseModel):
    strike: float
    call_ltp: float
    call_iv: float
    call_delta: float
    call_gamma: float
    call_theta: float
    call_vega: float
    call_oi: int
    call_volume: int
    put_ltp: float
    put_iv: float
    put_delta: float
    put_gamma: float
    put_theta: float
    put_vega: float
    put_oi: int
    put_volume: int


class ChainResponse(BaseModel):
    underlying: str
    underlying_price: float
    timestamp: datetime
    expiry: date
    strikes: list[StrikeDataResponse]
    pcr_oi: float
    pcr_volume: float
    atm_iv: float
    iv_rank: float
    iv_percentile: float


class RegimeResponse(BaseModel):
    underlying: str
    regime: str  # "BULL_LOW_VOL" | "BEAR_HIGH_VOL" | "PRE_EVENT" | "COMMODITY_MACRO"
    confidence: float
    timestamp: datetime


class RegimeListResponse(BaseModel):
    regimes: list[RegimeResponse]


# ── Config ───────────────────────────────────────────────────────────────────


class StrategyConfigResponse(BaseModel):
    strategy_name: str
    enabled: bool
    params: dict[str, Any]
    portfolio_value_inr: float
    max_risk_per_trade_pct: float
    updated_at: datetime


class ConfigResponse(BaseModel):
    tenant_id: str
    trading_mode: str  # "LIVE" | "PAPER"
    strategies: list[StrategyConfigResponse]


class StrategyConfigUpdateInput(BaseModel):
    enabled: bool | None = None
    params: dict[str, Any] = Field(default_factory=dict)
    portfolio_value_inr: float | None = Field(None, ge=10_000, le=100_000_000)
    max_risk_per_trade_pct: float | None = Field(None, ge=0.5, le=5.0)


class TradingModeInput(BaseModel):
    mode: str = Field(pattern=r"^(LIVE|PAPER)$")


# ── Custom Strategy ──────────────────────────────────────────────────────────


class ConditionInput(BaseModel):
    left_operand: str = Field(max_length=50)
    left_field: str | None = Field(None, max_length=30)
    operator: str = Field(
        pattern=r"^(>|>=|<|<=|==|!=|CROSSES_ABOVE|CROSSES_BELOW|TOUCHED|BETWEEN|INCREASING|DECREASING)$"
    )
    right_operand: str = Field(max_length=50)
    right_field: str | None = Field(None, max_length=30)
    right_value: float | None = None


class IndicatorInput(BaseModel):
    indicator_type: str
    params: dict[str, Any]
    label: str = Field(max_length=50)


class CustomStrategyInput(BaseModel):
    name: str = Field(min_length=3, max_length=50, pattern=r"^[a-zA-Z0-9_\s-]+$")
    description: str = Field(max_length=500, default="")
    category: str = Field(pattern=r"^(BUYING|SELLING|HYBRID)$")
    target_symbols: list[str] = Field(min_length=1, max_length=30)
    indicators: list[IndicatorInput] = Field(min_length=1, max_length=10)
    entry_conditions: list[list[ConditionInput]] = Field(min_length=1, max_length=5)
    exit_conditions: list[ConditionInput] = Field(min_length=1, max_length=10)
    option_action: str
    strike_selection: str = Field(
        pattern=r"^(ATM|1_OTM|2_OTM|1_ITM|OTM_1|OTM_2|OTM_3|ITM_1|ITM_2|DELTA_BASED)$"
    )
    dte_min: int = Field(ge=1, le=90)
    dte_max: int = Field(ge=1, le=180)
    stop_loss_pct: float = Field(ge=5, le=100)
    profit_target_pct: float = Field(ge=10, le=500)


class CustomStrategyResponse(BaseModel):
    id: str
    tenant_id: str
    name: str
    description: str
    category: str
    status: str  # "DRAFT" | "BACKTESTED" | "ACTIVE" | "PAUSED" | "ARCHIVED"
    target_symbols: list[str]
    indicators: list[dict[str, Any]]
    entry_conditions: list[list[dict[str, Any]]]
    exit_conditions: list[dict[str, Any]]
    option_action: str
    strike_selection: str
    delta_target: float | None = None
    dte_min: int
    dte_max: int
    stop_loss_pct: float
    profit_target_pct: float
    backtest_results: dict[str, Any] | None = None
    ai_review_notes: str | None = None
    created_at: datetime
    updated_at: datetime


class CustomStrategyListResponse(BaseModel):
    strategies: list[CustomStrategyResponse]
    total: int


class BacktestRequest(BaseModel):
    start_date: date
    end_date: date
    initial_capital: float = Field(ge=10_000, le=100_000_000)


class BacktestResponse(BaseModel):
    strategy_id: str
    start_date: date
    end_date: date
    initial_capital: float
    final_capital: float
    total_pnl: float
    total_trades: int
    win_rate: float
    max_drawdown: float
    sharpe_ratio: float | None = None
    profit_factor: float | None = None
    results_by_symbol: dict[str, Any] = Field(default_factory=dict)


class AIBuildRequest(BaseModel):
    description: str = Field(min_length=10, max_length=2000)
    preferred_segments: list[str] = Field(default_factory=list)


class AIBuildResponse(BaseModel):
    strategy: CustomStrategyResponse
    ai_notes: str


class AIReviewRequest(BaseModel):
    strategy_id: str = Field(pattern=r"^[0-9a-f-]{36}$")


class AIReviewResponse(BaseModel):
    overall_rating: str  # "STRONG" | "MODERATE" | "WEAK" | "RISKY"
    risk_score: float
    issues: list[str]
    suggestions: list[str]
    overfitting_risk: str  # "LOW" | "MEDIUM" | "HIGH"
    regime_coverage: dict[str, str]


# ── Indicator Library ────────────────────────────────────────────────────────


class IndicatorParamSchema(BaseModel):
    name: str
    type: str  # "int" | "float"
    default: Any
    min_value: Any | None = None
    max_value: Any | None = None
    description: str = ""


class IndicatorInfo(BaseModel):
    indicator_type: str
    name: str
    category: str  # "LAGGING" | "LEADING" | "VOLUME" | "VOLATILITY" | "OPTIONS"
    description: str
    params_schema: list[IndicatorParamSchema]
    output_fields: list[str]


class IndicatorListResponse(BaseModel):
    indicators: list[IndicatorInfo]


class RecommendedStrategyResponse(BaseModel):
    name: str
    description: str
    category: str
    indicators_used: list[str]
    suitable_for: str
    expected_win_rate: str


class RecommendedListResponse(BaseModel):
    recommendations: list[RecommendedStrategyResponse]


# ── Admin Models ─────────────────────────────────────────────────────────────


class AdminTenantResponse(BaseModel):
    id: str
    email: str
    name: str
    subscription_tier: str
    subscription_status: str
    trial_ends_at: datetime | None = None
    subscription_ends_at: datetime | None = None
    is_active: bool
    created_at: datetime


class AdminTenantListResponse(BaseModel):
    tenants: list[AdminTenantResponse]
    total: int


class AdminTenantDetailResponse(AdminTenantResponse):
    email_verified: bool
    total_trades: int | None = None
    total_pnl_inr: float | None = None
    active_positions: int | None = None
    custom_strategies_count: int | None = None


class AdminSuspendResponse(BaseModel):
    tenant_id: str
    subscription_status: str
    message: str


class AdminSystemHealthResponse(BaseModel):
    status: str
    services: dict[str, HealthCheckItem]
    timestamp: datetime


class AdminWorkerInfo(BaseModel):
    tenant_id: str
    email: str
    status: str
    started_at: datetime | None = None
    strategies_active: int


class AdminWorkersResponse(BaseModel):
    workers: list[AdminWorkerInfo]
    total: int


class AdminMetricsResponse(BaseModel):
    total_tenants: int
    active_tenants: int
    total_trades_today: int
    total_pnl_today_inr: float
    active_websockets: int
    active_workers: int
    timestamp: datetime


# ── Generic ──────────────────────────────────────────────────────────────────


class MessageResponse(BaseModel):
    message: str
