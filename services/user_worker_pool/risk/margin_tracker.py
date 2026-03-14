"""
MarginTracker -- tracks margin utilisation per user for selling strategies.

Selling strategies MUST check margin before placing orders.  This module
provides helpers to query available margin (from broker or estimate) and
validate that new orders will not exceed the 60% utilisation cap.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from ..strategies.base import Signal, Position

import structlog

logger = structlog.get_logger(service="user_worker_pool", module="margin_tracker")

_MARGIN_SAFETY_MULTIPLIER = 1.20  # require 120% of estimated margin


@dataclass
class MarginCheckResult:
    """Result of a margin sufficiency check."""
    sufficient: bool
    reason: str
    required: float = 0.0
    available: float = 0.0
    utilisation_pct: float = 0.0


class MarginTracker:
    """Tracks margin utilisation per user for selling strategies.

    Queries broker API for actual margin available, or estimates from local
    state (portfolio_value - blocked_margin).
    """

    def __init__(self, order_router=None):
        self._order_router = order_router

    def get_available_margin(
        self,
        tenant_id: str,
        portfolio_value: float = 0.0,
        blocked_margin: float = 0.0,
    ) -> float:
        """Fetch available margin from broker or estimate from local state.

        If a broker connection is available via order_router, query it.
        Otherwise, estimate as portfolio_value - blocked_margin.
        """
        # Try broker API first
        if self._order_router is not None:
            try:
                broker_margin = self._order_router.get_margin(tenant_id)
                if broker_margin is not None and broker_margin > 0:
                    logger.debug(
                        "margin_from_broker",
                        tenant_id=tenant_id,
                        margin=broker_margin,
                    )
                    return broker_margin
            except Exception:
                logger.warning(
                    "margin_broker_fallback",
                    tenant_id=tenant_id,
                    exc_info=True,
                )

        # Fallback: estimate from local state
        estimated = portfolio_value - blocked_margin
        logger.debug(
            "margin_estimated",
            tenant_id=tenant_id,
            portfolio_value=portfolio_value,
            blocked_margin=blocked_margin,
            estimated=estimated,
        )
        return max(0.0, estimated)

    def estimate_margin_required(
        self,
        signal: Signal,
        chain: Any,
        config: dict | None = None,
    ) -> float:
        """Estimate SPAN margin for the signal's legs.

        Uses strategy.margin_required_per_lot() x quantity.
        """
        from ..strategies import STRATEGY_REGISTRY

        config = config or {}
        strategy_cls = STRATEGY_REGISTRY.get(signal.strategy_name)
        if strategy_cls is None:
            logger.warning(
                "margin_unknown_strategy",
                strategy=signal.strategy_name,
            )
            return 0.0

        strategy_instance = strategy_cls()
        margin_per_lot = strategy_instance.margin_required_per_lot(chain, config)
        lots = signal.metadata.get("lots", 1)
        total_margin = margin_per_lot * lots

        logger.debug(
            "margin_estimated_for_signal",
            strategy=signal.strategy_name,
            margin_per_lot=margin_per_lot,
            lots=lots,
            total_margin=total_margin,
        )
        return total_margin

    def check_margin_sufficient(
        self,
        signal: Signal,
        chain: Any,
        available_margin: float,
        config: dict | None = None,
    ) -> tuple[bool, str]:
        """Check if available margin is sufficient for the new signal.

        Returns (sufficient, reason_if_blocked).
        Blocks order if available_margin < 120% of required margin (safety buffer).
        """
        config = config or {}
        required = self.estimate_margin_required(signal, chain, config)

        if required <= 0:
            return True, ""

        required_with_buffer = required * _MARGIN_SAFETY_MULTIPLIER

        if available_margin <= 0:
            return False, (
                f"No margin available. Required: {required_with_buffer:,.0f} "
                f"(incl. {int((_MARGIN_SAFETY_MULTIPLIER - 1) * 100)}% safety buffer)"
            )

        utilisation_pct = required_with_buffer / available_margin * 100

        if available_margin < required_with_buffer:
            reason = (
                f"Insufficient margin. Required: {required_with_buffer:,.0f} "
                f"(incl. {int((_MARGIN_SAFETY_MULTIPLIER - 1) * 100)}% buffer), "
                f"Available: {available_margin:,.0f}, "
                f"Utilisation would be: {utilisation_pct:.1f}%"
            )
            logger.info(
                "margin_insufficient",
                strategy=signal.strategy_name,
                required=required_with_buffer,
                available=available_margin,
                utilisation_pct=utilisation_pct,
            )
            return False, reason

        logger.debug(
            "margin_sufficient",
            strategy=signal.strategy_name,
            required=required_with_buffer,
            available=available_margin,
            utilisation_pct=utilisation_pct,
        )
        return True, ""

    def get_current_utilisation(
        self,
        open_positions: list[Position],
        chain: Any,
        available_margin: float,
    ) -> float:
        """Calculate current margin utilisation percentage across all open
        selling/hybrid positions.

        Returns utilisation as a percentage (0-100+).
        """
        from ..strategies import STRATEGY_REGISTRY

        total_blocked = 0.0
        for pos in open_positions:
            if pos.status != "OPEN":
                continue
            strategy_cls = STRATEGY_REGISTRY.get(pos.strategy_name)
            if strategy_cls is None:
                continue
            instance = strategy_cls()
            if not instance.requires_margin:
                continue
            margin_per_lot = instance.margin_required_per_lot(chain, {})
            total_blocked += margin_per_lot * pos.lots

        if available_margin <= 0:
            return 100.0 if total_blocked > 0 else 0.0

        return (total_blocked / available_margin) * 100
