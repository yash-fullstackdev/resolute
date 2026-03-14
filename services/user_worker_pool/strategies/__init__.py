"""
Strategy registry.

Maps strategy name strings to their implementation class.  Used by
WorkerPoolManager when constructing a UserWorker's enabled strategy list.
"""

from __future__ import annotations

from .base import BaseStrategy
from .long_call import LongCallStrategy
from .long_put import LongPutStrategy
from .bull_call_spread import BullCallSpreadStrategy
from .bear_put_spread import BearPutSpreadStrategy
from .long_straddle import LongStraddleStrategy
from .long_strangle import LongStrangleStrategy
from .pcr_contrarian import PCRContrarianStrategy
from .event_directional import EventDirectionalStrategy
from .mcx_gold_silver import MCXGoldSilverStrategy
from .mcx_crude_put import MCXCrudePutStrategy

STRATEGY_REGISTRY: dict[str, type[BaseStrategy]] = {
    "long_call": LongCallStrategy,
    "long_put": LongPutStrategy,
    "bull_call_spread": BullCallSpreadStrategy,
    "bear_put_spread": BearPutSpreadStrategy,
    "long_straddle": LongStraddleStrategy,
    "long_strangle": LongStrangleStrategy,
    "pcr_contrarian": PCRContrarianStrategy,
    "event_directional": EventDirectionalStrategy,
    "mcx_gold_silver": MCXGoldSilverStrategy,
    "mcx_crude_put": MCXCrudePutStrategy,
}


def get_strategy_class(name: str) -> type[BaseStrategy] | None:
    """Return the strategy class for *name*, or None if unknown."""
    return STRATEGY_REGISTRY.get(name)


def list_strategy_names() -> list[str]:
    """Return all registered strategy names."""
    return list(STRATEGY_REGISTRY.keys())


__all__ = [
    "STRATEGY_REGISTRY",
    "get_strategy_class",
    "list_strategy_names",
    "BaseStrategy",
]
