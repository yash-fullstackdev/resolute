"""
Subscription tier definitions and gating logic.
"""

from enum import Enum

import structlog

logger = structlog.get_logger(service="auth_service")


class Tier(str, Enum):
    SIGNAL = "SIGNAL"
    SEMI_AUTO = "SEMI_AUTO"
    FULL_AUTO = "FULL_AUTO"


class SubscriptionStatus(str, Enum):
    ACTIVE = "ACTIVE"
    TRIAL = "TRIAL"
    EXPIRED = "EXPIRED"
    SUSPENDED = "SUSPENDED"


# Tier hierarchy: higher index = more capabilities
TIER_HIERARCHY = [Tier.SIGNAL, Tier.SEMI_AUTO, Tier.FULL_AUTO]

TIER_FEATURES: dict[Tier, dict] = {
    Tier.SIGNAL: {
        "name": "Signal",
        "description": "Receive trading signals and alerts. Manual execution only.",
        "features": [
            "Real-time option chain data",
            "AI-generated trading signals",
            "Signal notifications (email/push)",
            "Basic portfolio view",
            "Custom strategy builder (up to 3)",
        ],
        "max_custom_strategies": 3,
        "auto_execution": False,
        "semi_auto_execution": False,
        "broker_connect": False,
        "max_underlyings": 5,
        "max_trades_per_day": 10,
        "price_monthly_inr": 999,
        "price_yearly_inr": 9990,
    },
    Tier.SEMI_AUTO: {
        "name": "Semi-Auto",
        "description": "Signal + one-click execution with human approval for each trade.",
        "features": [
            "Everything in Signal tier",
            "Broker connectivity (Dhan, Zerodha)",
            "One-click order execution",
            "Human approval required for each trade",
            "Position management dashboard",
            "Custom strategy builder (up to 10)",
            "Paper trading mode",
        ],
        "max_custom_strategies": 10,
        "auto_execution": False,
        "semi_auto_execution": True,
        "broker_connect": True,
        "max_underlyings": 15,
        "max_trades_per_day": 25,
        "price_monthly_inr": 2499,
        "price_yearly_inr": 24990,
    },
    Tier.FULL_AUTO: {
        "name": "Full Auto",
        "description": "Fully automated trading with discipline engine, risk gates, and circuit breakers.",
        "features": [
            "Everything in Semi-Auto tier",
            "Fully automated order execution",
            "Discipline engine with stop-loss/target management",
            "Circuit breakers and risk gates",
            "Advanced position Greeks tracking",
            "Custom strategy builder (unlimited)",
            "Priority signal processing",
            "Dedicated support",
        ],
        "max_custom_strategies": 100,
        "auto_execution": True,
        "semi_auto_execution": True,
        "broker_connect": True,
        "max_underlyings": 30,
        "max_trades_per_day": 50,
        "price_monthly_inr": 4999,
        "price_yearly_inr": 49990,
    },
}


def tier_index(tier: Tier) -> int:
    """Return the numeric index of a tier in the hierarchy."""
    return TIER_HIERARCHY.index(tier)


def is_tier_sufficient(user_tier: Tier, required_tier: Tier) -> bool:
    """Check if the user's tier meets or exceeds the required tier."""
    return tier_index(user_tier) >= tier_index(required_tier)


def check_feature_access(user_tier: Tier, feature: str) -> bool:
    """
    Check if a specific feature is available for the given tier.

    Supported feature checks:
        - broker_connect
        - auto_execution
        - semi_auto_execution
    """
    tier_info = TIER_FEATURES.get(user_tier)
    if tier_info is None:
        logger.warning("unknown_tier_feature_check", tier=user_tier, feature=feature)
        return False
    return bool(tier_info.get(feature, False))


def get_tier_limit(user_tier: Tier, limit_name: str) -> int:
    """
    Get a numeric limit for the given tier.

    Supported limits:
        - max_custom_strategies
        - max_underlyings
        - max_trades_per_day
    """
    tier_info = TIER_FEATURES.get(user_tier)
    if tier_info is None:
        logger.warning("unknown_tier_limit_check", tier=user_tier, limit=limit_name)
        return 0
    return int(tier_info.get(limit_name, 0))


def can_upgrade(current_tier: Tier, target_tier: Tier) -> bool:
    """Check if upgrading from current_tier to target_tier is valid."""
    return tier_index(target_tier) > tier_index(current_tier)


def get_available_upgrades(current_tier: Tier) -> list[dict]:
    """Return list of tiers available as upgrades from the current tier."""
    current_idx = tier_index(current_tier)
    upgrades = []
    for tier in TIER_HIERARCHY[current_idx + 1:]:
        info = TIER_FEATURES[tier]
        upgrades.append({
            "tier": tier.value,
            "name": info["name"],
            "description": info["description"],
            "price_monthly_inr": info["price_monthly_inr"],
            "price_yearly_inr": info["price_yearly_inr"],
        })
    return upgrades


def get_all_tiers() -> list[dict]:
    """Return information about all available tiers."""
    result = []
    for tier in TIER_HIERARCHY:
        info = TIER_FEATURES[tier]
        result.append({
            "tier": tier.value,
            "name": info["name"],
            "description": info["description"],
            "features": info["features"],
            "price_monthly_inr": info["price_monthly_inr"],
            "price_yearly_inr": info["price_yearly_inr"],
            "max_custom_strategies": info["max_custom_strategies"],
            "auto_execution": info["auto_execution"],
            "semi_auto_execution": info["semi_auto_execution"],
            "broker_connect": info["broker_connect"],
        })
    return result
