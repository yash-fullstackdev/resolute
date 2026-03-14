"""
Capital tiers and strategy categories.

Defines the tiered access model that gates strategy categories based on user
portfolio value.  Enforcement points: UserWorker.run(), PositionSizer, and
dashboard API.
"""

from __future__ import annotations

import enum


# ---------------------------------------------------------------------------
# Capital tiers
# ---------------------------------------------------------------------------

class CapitalTier(str, enum.Enum):
    STARTER = "STARTER"              # 10,000 - 50,000
    GROWTH = "GROWTH"                # 50,001 - 2,00,000
    PRO = "PRO"                      # 2,00,001 - 10,00,000
    INSTITUTIONAL = "INSTITUTIONAL"  # 10,00,001+


CAPITAL_TIER_RANGES: dict[CapitalTier, tuple[float, float]] = {
    CapitalTier.STARTER:       (10_000, 50_000),
    CapitalTier.GROWTH:        (50_001, 2_00_000),
    CapitalTier.PRO:           (2_00_001, 10_00_000),
    CapitalTier.INSTITUTIONAL: (10_00_001, float("inf")),
}

_TIER_ORDER: list[CapitalTier] = [
    CapitalTier.STARTER,
    CapitalTier.GROWTH,
    CapitalTier.PRO,
    CapitalTier.INSTITUTIONAL,
]


def get_capital_tier(portfolio_value_inr: float) -> CapitalTier:
    """Determine the capital tier for a given portfolio value in INR.

    Raises ``ValueError`` if the value is below the minimum (10,000).
    """
    for tier, (low, high) in CAPITAL_TIER_RANGES.items():
        if low <= portfolio_value_inr <= high:
            return tier
    raise ValueError(
        f"Portfolio value {portfolio_value_inr} below minimum 10,000"
    )


# ---------------------------------------------------------------------------
# Strategy categories
# ---------------------------------------------------------------------------

class StrategyCategory(str, enum.Enum):
    BUYING = "BUYING"    # Long options only -- max loss = premium paid
    SELLING = "SELLING"  # Short options -- requires margin, unlimited risk potential
    HYBRID = "HYBRID"    # Defined-risk spreads combining buy + sell legs


CATEGORY_MIN_TIER: dict[StrategyCategory, CapitalTier] = {
    StrategyCategory.BUYING:  CapitalTier.STARTER,
    StrategyCategory.HYBRID:  CapitalTier.GROWTH,
    StrategyCategory.SELLING: CapitalTier.PRO,
}


def is_strategy_allowed(
    strategy_category: StrategyCategory,
    user_tier: CapitalTier,
) -> bool:
    """Return True if *user_tier* is at or above the minimum tier required for
    *strategy_category*."""
    min_tier = CATEGORY_MIN_TIER[strategy_category]
    return _TIER_ORDER.index(user_tier) >= _TIER_ORDER.index(min_tier)
