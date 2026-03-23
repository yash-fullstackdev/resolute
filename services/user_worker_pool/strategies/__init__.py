"""
Strategy registry — Technical strategies only.

Maps strategy name strings to their implementation class.
Used by WorkerPoolManager when constructing a UserWorker's strategy instances.
"""

from __future__ import annotations

from .base import BaseStrategy

from .ttm_squeeze import TTMSqueezeStrategy
from .supertrend_strategy import SupertrendStrategy
from .vwap_supertrend import VWAPSupertrendStrategy
from .ema_breakdown import EMABreakdownStrategy
from .rsi_vwap_scalp import RSIVWAPScalpStrategy
from .ema33_ob import EMA33OBStrategy
from .smc_order_block import SMCOrderBlockStrategy
from .brahmaastra import BrahmaastraStrategy
from .ema5_mean_reversion import EMA5MeanReversionStrategy
from .parent_child_momentum import ParentChildMomentumStrategy

STRATEGY_REGISTRY: dict[str, type[BaseStrategy]] = {
    "ttm_squeeze": TTMSqueezeStrategy,
    "supertrend_strategy": SupertrendStrategy,
    "vwap_supertrend": VWAPSupertrendStrategy,
    "ema_breakdown": EMABreakdownStrategy,
    "rsi_vwap_scalp": RSIVWAPScalpStrategy,
    "ema33_ob": EMA33OBStrategy,
    "smc_order_block": SMCOrderBlockStrategy,
    # S1 — 9:15–10:15 ORB + Trap
    "brahmaastra": BrahmaastraStrategy,
    # S2 — 5 EMA mean reversion (5m PE / 15m CE)
    "ema5_mean_reversion": EMA5MeanReversionStrategy,
    # S3 — 1H parent + 5m child dual-TF momentum
    "parent_child_momentum": ParentChildMomentumStrategy,
}


def get_strategy_class(name: str) -> type[BaseStrategy] | None:
    return STRATEGY_REGISTRY.get(name)


def list_strategy_names() -> list[str]:
    return list(STRATEGY_REGISTRY.keys())


__all__ = [
    "STRATEGY_REGISTRY",
    "get_strategy_class",
    "list_strategy_names",
    "BaseStrategy",
]
