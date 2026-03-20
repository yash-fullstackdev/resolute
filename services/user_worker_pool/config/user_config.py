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
class InstanceConfig:
    """Configuration for a single strategy instance."""
    instance_id: str
    instance_name: str
    strategy_name: str
    mode: str = "disabled"        # "live" | "paper" | "disabled"
    session: str = "all"          # "morning" | "afternoon" | "all"
    max_daily_loss_pts: float | None = None
    instruments: list[str] = field(default_factory=list)
    params: dict[str, Any] = field(default_factory=dict)
    bias_config: dict[str, Any] | None = None


@dataclass
class UserStrategyConfig:
    """Per-user strategy configuration."""
    tenant_id: str
    portfolio_value_inr: float = 50_000.0
    strategies: dict[str, dict[str, Any]] = field(default_factory=dict)
    enabled_strategy_names: list[str] = field(default_factory=list)
    strategy_instruments: dict[str, list[str]] = field(default_factory=dict)
    instances: list[InstanceConfig] = field(default_factory=list)

    def get_strategy_config(self, strategy_name: str) -> dict[str, Any]:
        """Get merged config for a specific strategy."""
        defaults = DEFAULT_STRATEGY_CONFIG.get(strategy_name, {})
        overrides = self.strategies.get(strategy_name, {})
        merged = {**defaults, **overrides}
        return merged

    def get_instance_config(self, instance_id: str) -> dict[str, Any]:
        """Get merged config for a specific instance."""
        for inst in self.instances:
            if inst.instance_id == instance_id:
                defaults = DEFAULT_STRATEGY_CONFIG.get(inst.strategy_name, {})
                return {**defaults, **inst.params}
        return {}

    def get_strategy_instruments(self, strategy_name: str) -> list[str]:
        """Get instruments this strategy should monitor (empty = all)."""
        return self.strategy_instruments.get(strategy_name, [])

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
                import json as _json

                # Load ALL instance rows (multiple per strategy allowed)
                rows = await self._db.fetch(
                    """
                    SELECT id, strategy_name, instance_name, params, enabled,
                           portfolio_value_inr, trading_mode, session, max_daily_loss_pts
                    FROM user_strategy_configs
                    WHERE tenant_id = $1
                    ORDER BY strategy_name, updated_at
                    """,
                    tenant_id,
                    tenant_id=tenant_id,
                )

                for row in rows:
                    strategy_name = row["strategy_name"]
                    try:
                        raw = row["params"]
                        if isinstance(raw, str):
                            user_overrides = _json.loads(raw)
                        elif isinstance(raw, dict):
                            user_overrides = dict(raw)
                        else:
                            user_overrides = {}
                    except (ValueError, TypeError):
                        user_overrides = {}

                    # Extract instruments and bias_config from params JSONB
                    instruments = user_overrides.pop("instruments", [])
                    if not isinstance(instruments, list):
                        instruments = []
                    bias_config = user_overrides.pop("bias_config", None)

                    # Backward compat: populate strategy_instruments for old code paths
                    if instruments:
                        config.strategy_instruments[strategy_name] = instruments

                    # Portfolio value from any row
                    if row["portfolio_value_inr"]:
                        config.portfolio_value_inr = float(row["portfolio_value_inr"])

                    # Backward compat: populate strategies dict (first instance wins)
                    if strategy_name not in config.strategies:
                        user_overrides["enabled"] = row["enabled"]
                        config.strategies[strategy_name] = user_overrides

                    if row["enabled"]:
                        if strategy_name not in config.enabled_strategy_names:
                            config.enabled_strategy_names.append(strategy_name)

                    # Build instance config
                    mode = row.get("trading_mode", "disabled") or "disabled"
                    if mode == "disabled" and row["enabled"]:
                        mode = "paper"  # backward compat: enabled=True but no trading_mode

                    inst = InstanceConfig(
                        instance_id=str(row["id"]),
                        instance_name=row.get("instance_name") or strategy_name,
                        strategy_name=strategy_name,
                        mode=mode,
                        session=row.get("session") or "all",
                        max_daily_loss_pts=row.get("max_daily_loss_pts"),
                        instruments=instruments,
                        params=user_overrides,
                        bias_config=bias_config if isinstance(bias_config, dict) else None,
                    )
                    config.instances.append(inst)

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
