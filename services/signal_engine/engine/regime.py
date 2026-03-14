"""
Market regime classifier.

Classifies the current market environment based on:
  - India VIX level
  - Underlying price trend vs 20-day moving average
  - Upcoming event proximity (expiry, RBI policy, budget, etc.)
  - Market segment (NSE equity vs MCX commodity)
"""

from __future__ import annotations

import enum
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from typing import Optional

import numpy as np
import structlog

logger = structlog.get_logger(service="signal_engine", module="regime")


class MarketRegime(str, enum.Enum):
    """Canonical regime labels used by strategy selectors."""

    BULL_LOW_VOL = "BULL_LOW_VOL"
    BULL_HIGH_VOL = "BULL_HIGH_VOL"
    BEAR_LOW_VOL = "BEAR_LOW_VOL"
    BEAR_HIGH_VOL = "BEAR_HIGH_VOL"
    SIDEWAYS_LOW_VOL = "SIDEWAYS_LOW_VOL"
    SIDEWAYS_HIGH_VOL = "SIDEWAYS_HIGH_VOL"
    PRE_EVENT = "PRE_EVENT"
    COMMODITY_MACRO = "COMMODITY_MACRO"


# ── VIX thresholds (India VIX) ──────────────────────────────────────────────
_VIX_LOW = 14.0   # Below this => low vol
_VIX_HIGH = 20.0  # Above this => high vol
# Between LOW and HIGH => moderate — classified by trend

# ── Trend thresholds ────────────────────────────────────────────────────────
_TREND_BULL_PCT = 0.005    # spot > 20d MA by 0.5 %
_TREND_BEAR_PCT = -0.005   # spot < 20d MA by 0.5 %


@dataclass
class EventCalendar:
    """Known upcoming events that shift regime to PRE_EVENT."""

    events: list[tuple[date, str]] = field(default_factory=list)

    def days_to_next_event(self, today: date | None = None) -> tuple[int, str]:
        """Return (days_until, event_name) for the nearest future event.

        Returns (999, '') if no events are upcoming.
        """
        today = today or date.today()
        nearest_days = 999
        nearest_name = ""
        for event_date, name in self.events:
            delta = (event_date - today).days
            if 0 <= delta < nearest_days:
                nearest_days = delta
                nearest_name = name
        return nearest_days, nearest_name


@dataclass
class RegimeInput:
    """All inputs required to classify the market regime."""

    vix: float
    spot_price: float
    price_history_20d: list[float]   # most recent 20 closing prices (oldest first)
    segment: str                      # "NSE_INDEX" | "NSE_FO" | "MCX"
    upcoming_events: Optional[EventCalendar] = None


class RegimeClassifier:
    """Stateless classifier — call ``classify`` with current data."""

    PRE_EVENT_WINDOW_DAYS = 3  # classify as PRE_EVENT if event within 3 days

    def classify(self, inp: RegimeInput) -> MarketRegime:
        """Determine the current market regime.

        Order of precedence:
        1. Commodity segment => COMMODITY_MACRO
        2. Upcoming event within window => PRE_EVENT
        3. Trend + volatility combination
        """
        # ── 1. Commodity override ───────────────────────────────────────────
        if inp.segment == "MCX":
            return MarketRegime.COMMODITY_MACRO

        # ── 2. Event override ───────────────────────────────────────────────
        if inp.upcoming_events is not None:
            days_to_event, event_name = inp.upcoming_events.days_to_next_event()
            if days_to_event <= self.PRE_EVENT_WINDOW_DAYS:
                logger.info(
                    "regime_pre_event",
                    event=event_name,
                    days_away=days_to_event,
                )
                return MarketRegime.PRE_EVENT

        # ── 3. Trend detection ──────────────────────────────────────────────
        trend = self._detect_trend(inp.spot_price, inp.price_history_20d)

        # ── 4. Volatility bucket ────────────────────────────────────────────
        is_high_vol = inp.vix >= _VIX_HIGH
        is_low_vol = inp.vix < _VIX_LOW

        if trend == "BULL":
            return MarketRegime.BULL_HIGH_VOL if is_high_vol else MarketRegime.BULL_LOW_VOL
        elif trend == "BEAR":
            return MarketRegime.BEAR_HIGH_VOL if is_high_vol else MarketRegime.BEAR_LOW_VOL
        else:
            return MarketRegime.SIDEWAYS_HIGH_VOL if is_high_vol else MarketRegime.SIDEWAYS_LOW_VOL

    @staticmethod
    def _detect_trend(spot: float, price_history: list[float]) -> str:
        """Classify trend as BULL / BEAR / SIDEWAYS using 20-day simple MA.

        If fewer than 5 prices are available, returns SIDEWAYS.
        """
        if len(price_history) < 5:
            return "SIDEWAYS"

        ma = float(np.mean(price_history[-20:]))

        if ma == 0:
            return "SIDEWAYS"

        deviation = (spot - ma) / ma

        if deviation > _TREND_BULL_PCT:
            return "BULL"
        elif deviation < _TREND_BEAR_PCT:
            return "BEAR"
        else:
            return "SIDEWAYS"
