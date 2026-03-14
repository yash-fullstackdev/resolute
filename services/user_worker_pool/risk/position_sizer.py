"""
PositionSizer -- per-trade sizing rules, capital-tier aware.

BUYING strategies:
  - Never risk > max_risk_per_trade_pct (default 2%) of portfolio
  - Weekly index options (DTE <= 7): max 1.5% per trade
  - Event straddles/strangles: max 3% per trade
  - Total open premiums: max 30% of portfolio simultaneously
  - Max 1 open position per underlying per strategy

SELLING strategies (additional):
  - Margin utilisation must not exceed 60%
  - Short straddle/strangle: max 1 position per underlying
  - Credit spreads: max 2 positions per underlying

HYBRID strategies:
  - Same premium rules as BUYING
  - Plus margin check for short leg
"""

from __future__ import annotations

from ..capital_tier import CapitalTier, StrategyCategory, is_strategy_allowed
from ..strategies.base import Signal, Position

import structlog

logger = structlog.get_logger(service="user_worker_pool", module="position_sizer")

# Max risk per trade by strategy type
_SPECIAL_RISK_LIMITS: dict[str, float] = {
    "long_straddle": 3.0,
    "long_strangle": 3.0,
    "event_directional": 3.0,
    "mcx_crude_put": 2.0,
}

_MAX_PORTFOLIO_PREMIUM_PCT = 30.0  # max 30% in open premiums
_MAX_MARGIN_UTILISATION_PCT = 60.0


class PositionSizer:
    """Calculate position sizes respecting capital tier and portfolio limits."""

    def calculate_lots(
        self,
        signal: Signal,
        portfolio_value: float,
        open_positions: list[Position],
        capital_tier: CapitalTier,
    ) -> int:
        """Return number of lots for a new signal.

        Applies risk-per-trade limits, weekly option limits, and portfolio limits.
        Returns 0 if the trade should be blocked.
        """
        if signal.max_loss_inr <= 0:
            return 1

        # Determine max risk %
        base_risk_pct = 2.0
        strategy_risk_pct = _SPECIAL_RISK_LIMITS.get(signal.strategy_name, base_risk_pct)

        # Weekly index options (DTE <= 7): cap at 1.5%
        from datetime import date
        dte = (signal.expiry - date.today()).days
        if dte <= 7 and signal.segment == "NSE_INDEX":
            strategy_risk_pct = min(strategy_risk_pct, 1.5)

        max_risk_inr = portfolio_value * (strategy_risk_pct / 100.0)
        lots = int(max_risk_inr / signal.max_loss_inr)
        lots = max(1, lots)

        # Portfolio-level check: total open premiums must not exceed 30%
        total_open_premiums = sum(
            p.entry_cost_inr for p in open_positions if p.status == "OPEN"
        )
        new_trade_cost = signal.entry_price * lots
        max_total_premiums = portfolio_value * (_MAX_PORTFOLIO_PREMIUM_PCT / 100.0)

        if total_open_premiums + new_trade_cost > max_total_premiums:
            # Reduce lots to fit
            available = max_total_premiums - total_open_premiums
            if available <= 0 or signal.entry_price <= 0:
                return 0
            lots = max(1, int(available / signal.entry_price))
            # Re-check
            if total_open_premiums + signal.entry_price * lots > max_total_premiums:
                return 0

        return lots

    def check_portfolio_limits(
        self,
        new_signal: Signal,
        open_positions: list[Position],
        portfolio_value: float,
        capital_tier: CapitalTier,
    ) -> tuple[bool, str]:
        """Return (can_trade, reason_if_blocked).

        Validates:
        1. Max 1 position per underlying per strategy
        2. Total premiums < 30% of portfolio
        3. Capital tier allows this strategy category
        """
        # Capital tier check
        strategy_category = StrategyCategory(
            new_signal.metadata.get("category", "BUYING")
        ) if "category" in new_signal.metadata else StrategyCategory.BUYING

        # Determine category from strategy name
        from ..strategies import STRATEGY_REGISTRY
        strategy_cls = STRATEGY_REGISTRY.get(new_signal.strategy_name)
        if strategy_cls:
            strategy_category = strategy_cls.category

        if not is_strategy_allowed(strategy_category, capital_tier):
            return False, (
                f"Capital tier {capital_tier.value} does not allow "
                f"{strategy_category.value} strategies"
            )

        # Max 1 position per underlying per strategy
        for p in open_positions:
            if (
                p.strategy_name == new_signal.strategy_name
                and p.underlying == new_signal.underlying
                and p.status == "OPEN"
            ):
                return False, (
                    f"Already have open {new_signal.strategy_name} position "
                    f"on {new_signal.underlying}"
                )

        # Total premiums check
        total_open_premiums = sum(
            p.entry_cost_inr for p in open_positions if p.status == "OPEN"
        )
        max_total = portfolio_value * (_MAX_PORTFOLIO_PREMIUM_PCT / 100.0)
        if total_open_premiums + new_signal.entry_price > max_total:
            return False, (
                f"Total open premiums ({total_open_premiums:.0f}) + new trade "
                f"({new_signal.entry_price:.0f}) would exceed "
                f"{_MAX_PORTFOLIO_PREMIUM_PCT}% of portfolio ({max_total:.0f})"
            )

        # Selling-specific: straddle/strangle max 1 per underlying
        naked_strategies = {"short_straddle", "short_strangle"}
        if new_signal.strategy_name in naked_strategies:
            for p in open_positions:
                if (
                    p.strategy_name in naked_strategies
                    and p.underlying == new_signal.underlying
                    and p.status == "OPEN"
                ):
                    return False, (
                        f"Cannot stack naked shorts on {new_signal.underlying}"
                    )

        # Credit spreads: max 2 per underlying
        spread_strategies = {"credit_spread_call", "credit_spread_put"}
        if new_signal.strategy_name in spread_strategies:
            spread_count = sum(
                1 for p in open_positions
                if p.strategy_name in spread_strategies
                and p.underlying == new_signal.underlying
                and p.status == "OPEN"
            )
            if spread_count >= 2:
                return False, (
                    f"Max 2 credit spread positions on {new_signal.underlying}"
                )

        return True, ""

    def check_margin_for_selling(
        self,
        signal: Signal,
        chain,
        tenant_id: str,
        current_margin_used: float = 0.0,
        total_margin_available: float = 0.0,
    ) -> tuple[bool, str]:
        """For SELLING/HYBRID strategies: verify sufficient margin.

        Rejects if margin utilisation would exceed 60% after this trade.
        """
        if total_margin_available <= 0:
            return False, "No margin information available"

        # Estimate margin for this trade
        from ..strategies import STRATEGY_REGISTRY
        strategy_cls = STRATEGY_REGISTRY.get(signal.strategy_name)
        if strategy_cls is None:
            return False, f"Unknown strategy {signal.strategy_name}"

        strategy_instance = strategy_cls()
        margin_per_lot = strategy_instance.margin_required_per_lot(
            chain, signal.metadata
        )

        total_lots = signal.metadata.get("lots", 1)
        new_margin = margin_per_lot * total_lots

        projected_utilisation = (
            (current_margin_used + new_margin) / total_margin_available * 100
        )

        if projected_utilisation > _MAX_MARGIN_UTILISATION_PCT:
            return False, (
                f"Margin utilisation would be {projected_utilisation:.1f}% "
                f"(max {_MAX_MARGIN_UTILISATION_PCT}%)"
            )

        return True, ""
