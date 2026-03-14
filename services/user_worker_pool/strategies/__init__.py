"""
Strategy registry.

Maps strategy name strings to their implementation class.  Used by
WorkerPoolManager when constructing a UserWorker's enabled strategy list.
"""

from __future__ import annotations

from .base import BaseStrategy

# -- BUYING strategies (STARTER tier) --
from .long_call import LongCallStrategy
from .long_put import LongPutStrategy
from .long_straddle import LongStraddleStrategy
from .long_strangle import LongStrangleStrategy
from .pcr_contrarian import PCRContrarianStrategy
from .event_directional import EventDirectionalStrategy
from .mcx_gold_silver import MCXGoldSilverStrategy
from .mcx_crude_put import MCXCrudePutStrategy

# -- HYBRID strategies (GROWTH tier) --
from .bull_call_spread import BullCallSpreadStrategy
from .bear_put_spread import BearPutSpreadStrategy
from .iron_butterfly_long import IronButterflyLongStrategy
from .diagonal_spread import DiagonalSpreadStrategy
from .ratio_back_spread import RatioBackSpreadStrategy

# -- SELLING strategies (PRO tier) --
from .short_straddle import ShortStraddleStrategy
from .short_strangle import ShortStrangleStrategy
from .credit_spread_call import CreditSpreadCallStrategy
from .credit_spread_put import CreditSpreadPutStrategy
from .iron_condor import IronCondorStrategy
from .jade_lizard import JadeLizardStrategy
from .covered_call import CoveredCallStrategy

STRATEGY_REGISTRY: dict[str, type[BaseStrategy]] = {
    # BUYING
    "long_call": LongCallStrategy,
    "long_put": LongPutStrategy,
    "long_straddle": LongStraddleStrategy,
    "long_strangle": LongStrangleStrategy,
    "pcr_contrarian": PCRContrarianStrategy,
    "event_directional": EventDirectionalStrategy,
    "mcx_gold_silver": MCXGoldSilverStrategy,
    "mcx_crude_put": MCXCrudePutStrategy,
    # HYBRID
    "bull_call_spread": BullCallSpreadStrategy,
    "bear_put_spread": BearPutSpreadStrategy,
    "iron_butterfly_long": IronButterflyLongStrategy,
    "diagonal_spread": DiagonalSpreadStrategy,
    "ratio_back_spread": RatioBackSpreadStrategy,
    # SELLING
    "short_straddle": ShortStraddleStrategy,
    "short_strangle": ShortStrangleStrategy,
    "credit_spread_call": CreditSpreadCallStrategy,
    "credit_spread_put": CreditSpreadPutStrategy,
    "iron_condor": IronCondorStrategy,
    "jade_lizard": JadeLizardStrategy,
    "covered_call": CoveredCallStrategy,
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
