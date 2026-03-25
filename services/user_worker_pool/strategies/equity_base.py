"""
BaseEquityStrategy — abstract base class for equity (cash segment) strategies.

PARALLEL to BaseStrategy (not a subclass). Equity strategies receive batch
snapshots from the equity_poller, not options chain updates. They trade on
NSE_EQ with MARKET orders.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any


# ---------------------------------------------------------------------------
# Shared data models for equity strategies
# ---------------------------------------------------------------------------

@dataclass
class EquitySignal:
    """Output of BaseEquityStrategy.evaluate_signal() — an equity trade signal."""
    strategy_name: str
    symbol: str
    security_id: str
    direction: str              # "BUY" | "SELL"
    entry_price: float
    entry_bucket: int
    score: int
    signals_fired: list[str]
    quantity: int
    tp_price: float
    sl_price: float
    hard_exit_bucket: int
    gap_pct: float
    move_pct: float
    vol_rate: float
    open_price: float
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass
class EquityPosition:
    """An open equity position being tracked for exit."""
    symbol: str
    security_id: str
    direction: str              # "BUY" | "SELL"
    entry_price: float
    entry_bucket: int
    quantity: int
    tp_price: float
    sl_price: float
    hard_exit_bucket: int
    signal_id: str = ""
    position_id: str = ""


@dataclass
class EquityExitResult:
    """Result of an exit check — reason + computed P&L."""
    reason: str                 # "TP" | "SL" | "TIME"
    exit_price: float
    exit_bucket: int
    return_pct: float
    pnl_rupees: float


# ---------------------------------------------------------------------------
# BaseEquityStrategy ABC
# ---------------------------------------------------------------------------

class BaseEquityStrategy(ABC):
    """Abstract base for equity (cash segment) strategies.

    Unlike BaseStrategy which receives options chain snapshots,
    equity strategies receive per-stock batch snapshots with
    price, volume, OI, and derived fields.
    """

    name: str = ""
    category: str = "HYBRID"          # "BUYING" | "HYBRID" | "SELLING"
    min_capital_tier: str = "GROWTH"   # "STARTER" | "GROWTH" | "PRO"
    exchange: str = "NSE_EQ"
    order_type: str = "MARKET"

    @abstractmethod
    def evaluate_signal(
        self,
        snapshot: dict,
        bucket: int,
        direction: str,
        config: dict,
        fired_today: set,
        open_positions: dict,
        *,
        snapshot_history: list[dict] | None = None,
    ) -> EquitySignal | None:
        """Evaluate current snapshot and return an EquitySignal if conditions met.

        Args:
            snapshot: Current-bucket snapshot dict for one symbol.
            bucket: Current 1-minute bucket number (1 = 9:15 IST).
            direction: "BUY" or "SELL" — caller iterates both.
            config: Merged strategy config dict.
            fired_today: Set of "symbol_direction" strings already fired today.
            open_positions: Dict of symbol → EquityPosition for open trades.
            snapshot_history: All snapshots for this symbol today (for
                indicators that aggregate across the entry window).

        Returns:
            EquitySignal if all conditions met, None otherwise.
        """
        ...

    @abstractmethod
    def check_exit(
        self,
        position: EquityPosition,
        snapshot: dict,
        bucket: int,
        config: dict,
    ) -> EquityExitResult | None:
        """Check if an open position should be exited.

        Args:
            position: The open equity position.
            snapshot: Current snapshot dict for the position's symbol.
            bucket: Current 1-minute bucket number.
            config: Merged strategy config dict.

        Returns:
            EquityExitResult if exit triggered, None otherwise.
        """
        ...
