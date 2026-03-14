"""
UserConfigLoader -- loads per-user strategy configuration from DB,
merged with default strategies.yaml.

Each user can customise strategy parameters (stop_loss_pct, target_pct,
IV thresholds, etc.) through the dashboard.  These overrides are stored
in the user_strategy_configs DB table and merged on top of defaults.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import structlog

logger = structlog.get_logger(service="user_worker_pool", module="user_config")

# Default strategy configuration
DEFAULT_STRATEGY_CONFIG: dict[str, dict[str, Any]] = {
    "long_call": {
        "enabled": True,
        "segment": "NSE_INDEX",
        "iv_rank_max": 60,
        "vix_max": 20,
        "pcr_max": 0.8,
        "stop_loss_pct": 38.0,
        "target_pct": 60.0,
        "max_risk_per_trade_pct": 2.0,
    },
    "long_put": {
        "enabled": True,
        "segment": "NSE_INDEX",
        "iv_rank_max": 60,
        "pcr_min_bearish": 1.2,
        "stop_loss_pct": 38.0,
        "target_pct": 60.0,
        "max_risk_per_trade_pct": 2.0,
    },
    "bull_call_spread": {
        "enabled": False,
        "segment": "NSE_INDEX",
        "iv_rank_min": 30,
        "iv_rank_max": 70,
        "pcr_max": 1.0,
        "spread_width_steps": 2,
        "stop_loss_pct": 45.0,
        "target_pct": 80.0,
        "max_risk_per_trade_pct": 2.0,
    },
    "bear_put_spread": {
        "enabled": False,
        "segment": "NSE_INDEX",
        "iv_rank_min": 30,
        "iv_rank_max": 70,
        "pcr_min": 1.0,
        "spread_width_steps": 2,
        "stop_loss_pct": 45.0,
        "target_pct": 80.0,
        "max_risk_per_trade_pct": 2.0,
    },
    "long_straddle": {
        "enabled": True,
        "segment": "NSE_INDEX",
        "iv_rank_max": 55,
        "expected_move_pct": 3.0,
        "stop_loss_pct": 30.0,
        "target_pct": 50.0,
        "max_risk_per_trade_pct": 3.0,
    },
    "long_strangle": {
        "enabled": True,
        "segment": "NSE_INDEX",
        "iv_rank_max": 50,
        "otm_steps": 1,
        "expected_move_pct": 4.0,
        "stop_loss_pct": 40.0,
        "target_pct": 80.0,
        "max_risk_per_trade_pct": 3.0,
    },
    "pcr_contrarian": {
        "enabled": True,
        "segment": "NSE_INDEX",
        "pcr_extreme_low": 0.70,
        "pcr_extreme_high": 1.50,
        "pcr_persistence_sessions": 2,
        "stop_loss_pct": 35.0,
        "target_pct": 50.0,
        "max_risk_per_trade_pct": 2.0,
    },
    "event_directional": {
        "enabled": True,
        "segment": "NSE_INDEX",
        "event_direction": "NEUTRAL",
        "stop_loss_pct": 100.0,
        "target_pct": 100.0,
        "max_risk_per_trade_pct": 3.0,
    },
    "mcx_gold_silver": {
        "enabled": False,
        "segment": "MCX",
        "iv_rank_max": 50,
        "direction_bias": "BULLISH",
        "stop_loss_pct": 40.0,
        "target_pct": 60.0,
        "max_risk_per_trade_pct": 2.0,
    },
    "mcx_crude_put": {
        "enabled": False,
        "segment": "MCX",
        "iv_rank_max": 60,
        "stop_loss_pct": 35.0,
        "target_pct": 80.0,
        "max_risk_per_trade_pct": 2.0,
    },
}


@dataclass
class UserStrategyConfig:
    """Per-user strategy configuration."""
    tenant_id: str
    portfolio_value_inr: float = 50_000.0
    strategies: dict[str, dict[str, Any]] = field(default_factory=dict)
    enabled_strategy_names: list[str] = field(default_factory=list)

    def get_strategy_config(self, strategy_name: str) -> dict[str, Any]:
        """Get merged config for a specific strategy."""
        defaults = DEFAULT_STRATEGY_CONFIG.get(strategy_name, {})
        overrides = self.strategies.get(strategy_name, {})
        merged = {**defaults, **overrides}
        return merged

    def is_strategy_enabled(self, strategy_name: str) -> bool:
        """Check if a strategy is enabled for this user."""
        if self.enabled_strategy_names:
            return strategy_name in self.enabled_strategy_names
        config = self.get_strategy_config(strategy_name)
        return config.get("enabled", False)


class UserConfigLoader:
    """Loads and caches per-user strategy configuration."""

    def __init__(self, db=None) -> None:
        self._db = db
        self._cache: dict[str, UserStrategyConfig] = {}

    async def load_config(self, tenant_id: str) -> UserStrategyConfig:
        """Load user config from DB, merge with defaults."""
        # Check cache first
        if tenant_id in self._cache:
            return self._cache[tenant_id]

        config = UserStrategyConfig(tenant_id=tenant_id)

        if self._db is not None:
            try:
                # Load portfolio value
                user_row = await self._db.fetchrow(
                    """
                    SELECT portfolio_value_inr
                    FROM user_profiles
                    WHERE tenant_id = $1
                    """,
                    tenant_id,
                    tenant_id=tenant_id,
                )
                if user_row:
                    config.portfolio_value_inr = float(user_row["portfolio_value_inr"])

                # Load strategy configs
                rows = await self._db.fetch(
                    """
                    SELECT strategy_name, config_json, enabled
                    FROM user_strategy_configs
                    WHERE tenant_id = $1
                    """,
                    tenant_id,
                    tenant_id=tenant_id,
                )

                for row in rows:
                    strategy_name = row["strategy_name"]
                    import json
                    try:
                        user_overrides = json.loads(row["config_json"]) if row["config_json"] else {}
                    except (json.JSONDecodeError, TypeError):
                        user_overrides = {}

                    user_overrides["enabled"] = row["enabled"]
                    config.strategies[strategy_name] = user_overrides

                    if row["enabled"]:
                        config.enabled_strategy_names.append(strategy_name)

            except Exception as exc:
                logger.warning(
                    "user_config_load_failed",
                    tenant_id=tenant_id,
                    error=str(exc),
                )

        # If no DB overrides, use defaults
        if not config.enabled_strategy_names:
            config.enabled_strategy_names = [
                name for name, cfg in DEFAULT_STRATEGY_CONFIG.items()
                if cfg.get("enabled", False)
            ]

        self._cache[tenant_id] = config
        logger.info(
            "user_config_loaded",
            tenant_id=tenant_id,
            portfolio_value=config.portfolio_value_inr,
            enabled_strategies=config.enabled_strategy_names,
        )
        return config

    def invalidate_cache(self, tenant_id: str) -> None:
        """Invalidate cached config for a user (e.g. after config update)."""
        self._cache.pop(tenant_id, None)
