"""
BaseStrategy — abstract base class for all option strategies.

Every strategy must declare its category, minimum capital tier, and implement
evaluate() and should_exit().
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from datetime import datetime, date
from typing import Any

from ..capital_tier import CapitalTier, StrategyCategory


# ---------------------------------------------------------------------------
# Shared data models
# ---------------------------------------------------------------------------

@dataclass
class Leg:
    """A single option leg within a multi-leg order."""
    option_type: str          # "CE" | "PE"
    strike: float
    expiry: date
    action: str               # "BUY" | "SELL"
    lots: int = 1
    premium: float = 0.0      # premium per unit at entry


@dataclass
class Signal:
    """Output of BaseStrategy.evaluate() — represents a trade signal."""
    strategy_name: str
    underlying: str
    segment: str              # "NSE_INDEX" | "NSE_FO" | "MCX"
    direction: str            # "BULLISH" | "BEARISH" | "NEUTRAL"
    legs: list[Leg]
    entry_price: float        # total premium cost per lot (all legs combined)
    stop_loss_pct: float      # premium loss % at which to exit
    stop_loss_price: float    # absolute stop-loss price
    target_pct: float         # profit target %
    target_price: float       # absolute target price
    time_stop: datetime       # hard time to exit
    max_loss_inr: float       # maximum loss per lot in INR
    expiry: date
    confidence: float = 0.0   # 0.0 - 1.0
    signal_type: str = "OPTIONS"  # "OPTIONS" | "DIRECT" (no chain required)
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass
class Position:
    """An open option position (may be multi-leg)."""
    position_id: str
    tenant_id: str
    strategy_name: str
    underlying: str
    segment: str
    legs: list[Leg]
    entry_time: datetime
    entry_cost_inr: float     # total premium paid
    current_value_inr: float  # current mark-to-market value
    stop_loss_price: float
    target_price: float
    time_stop: datetime
    lots: int = 1
    status: str = "OPEN"      # "OPEN" | "CLOSED"
    exit_time: datetime | None = None
    exit_value_inr: float = 0.0
    exit_reason: str = ""
    pnl_inr: float = 0.0
    stop_loss_moved: bool = False
    time_stop_extended: bool = False
    override_count: int = 0


@dataclass
class Order:
    """A validated order ready for the order router."""
    order_id: str
    tenant_id: str
    strategy_name: str
    underlying: str
    segment: str
    legs: list[Leg]
    stop_loss_price: float
    target_price: float
    time_stop: datetime
    lots: int
    order_type: str = "NEW"   # "NEW" | "EXIT"
    position_id: str | None = None


@dataclass
class FillConfirmation:
    """Fill confirmation received from order_router."""
    order_id: str
    tenant_id: str
    position_id: str
    fill_type: str            # "OPEN" | "CLOSE" | "STOP_HIT" | "TIME_STOP" | "TARGET_HIT"
    fill_price: float
    filled_at: datetime
    pnl_inr: float = 0.0


# ---------------------------------------------------------------------------
# BaseStrategy ABC
# ---------------------------------------------------------------------------

class BaseStrategy(ABC):
    """Abstract base for all option strategies."""

    name: str = ""
    category: StrategyCategory = StrategyCategory.BUYING
    min_capital_tier: CapitalTier = CapitalTier.STARTER
    complexity: str = "SIMPLE"
    allowed_segments: list[str] = ["NSE_INDEX", "NSE_FO"]
    requires_margin: bool = False

    @abstractmethod
    def evaluate(
        self,
        chain,            # OptionsChainSnapshot
        regime,           # MarketRegime
        open_positions: list[Position],
        config: dict,
    ) -> Signal | None:
        """Evaluate current market state and return a Signal if conditions met.

        Must be deterministic and side-effect free.
        Returns None if no signal.
        """
        ...

    @abstractmethod
    def should_exit(
        self,
        position: Position,
        current_chain,     # OptionsChainSnapshot
        config: dict,
    ) -> bool:
        """Return True if *position* should be exited now."""
        ...

    def calculate_position_size(
        self,
        signal: Signal,
        portfolio_value: float,
        open_positions: list[Position],
        config: dict,
    ) -> int:
        """Return number of lots.  Default: fixed-fraction sizing."""
        max_risk_pct = config.get("max_risk_per_trade_pct", 2.0)
        max_risk_inr = portfolio_value * (max_risk_pct / 100)
        if signal.max_loss_inr <= 0:
            return 1
        lots = int(max_risk_inr / signal.max_loss_inr)
        return max(1, lots)

    def margin_required_per_lot(self, chain, config: dict) -> float:
        """For SELLING strategies: estimate margin blocked per lot.

        Returns 0 for BUYING strategies.
        """
        return 0.0

    # ------------------------------------------------------------------
    # Helpers available to all strategies
    # ------------------------------------------------------------------

    @staticmethod
    def find_atm_strike(chain, option_type: str = "CE") -> Any | None:
        """Find the ATM strike from the chain snapshot."""
        if not chain.strikes:
            return None
        spot = chain.underlying_price
        closest = min(chain.strikes, key=lambda s: abs(s.strike - spot))
        return closest

    @staticmethod
    def find_strike_near(chain, target_strike: float, option_type: str) -> Any | None:
        """Find the chain strike closest to *target_strike*.

        Used by strategies that compute their own target (e.g. 1-ITM, 1-OTM)
        and need the nearest available contract.
        """
        if not chain.strikes:
            return None
        return min(chain.strikes, key=lambda s: abs(s.strike - target_strike))

    @staticmethod
    def find_otm_strike(chain, option_type: str, steps: int = 1) -> Any | None:
        """Find an OTM strike *steps* away from ATM.

        For CE: higher strikes are OTM.  For PE: lower strikes are OTM.
        """
        if not chain.strikes:
            return None
        spot = chain.underlying_price
        sorted_strikes = sorted(chain.strikes, key=lambda s: s.strike)

        # Find ATM index
        atm_idx = min(
            range(len(sorted_strikes)),
            key=lambda i: abs(sorted_strikes[i].strike - spot),
        )

        if option_type == "CE":
            target_idx = atm_idx + steps
        else:
            target_idx = atm_idx - steps

        if 0 <= target_idx < len(sorted_strikes):
            return sorted_strikes[target_idx]
        return None

    @staticmethod
    def has_existing_position(
        strategy_name: str,
        underlying: str,
        open_positions: list[Position],
    ) -> bool:
        """Check if there is already an open position for this strategy+underlying."""
        return any(
            p.strategy_name == strategy_name
            and p.underlying == underlying
            and p.status == "OPEN"
            for p in open_positions
        )

    @staticmethod
    def get_dte(chain) -> int:
        """Days to expiry from chain snapshot."""
        from datetime import date as _date
        today = _date.today()
        return (chain.expiry - today).days
