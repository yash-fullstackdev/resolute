"""SupertrendStrategy — trend-following entry on Supertrend direction flip.

Entry: bearish→bullish flip → BUY CE; bullish→bearish flip → BUY PE.
"""

from __future__ import annotations

import math
from datetime import datetime, timedelta, timezone

import structlog

from ..capital_tier import CapitalTier, StrategyCategory
from .base import BaseStrategy, Signal, Leg, Position
from .indicators import atr_wilder

logger = structlog.get_logger(service="user_worker_pool", module="supertrend_strategy")


def _atm_strike(spot: float, underlying: str) -> float:
    interval = 100 if "BANK" in underlying else 50
    return round(spot / interval) * interval


def _estimate_premium(spot: float, atm_iv: float, dte_days: int) -> float:
    T = max(dte_days, 1) / 365.0
    return max(2.0, round(spot * max(atm_iv, 0.10) * math.sqrt(T) * 0.3989, 1))


def _compute_supertrend(highs, lows, closes, period, multiplier):
    """Returns (direction_series, supertrend_series, atr_series) or (None, None, None)."""
    if len(closes) < period + 5:
        return None, None, None
    atr_series = atr_wilder(highs, lows, closes, period)
    if len(atr_series) < 3:
        return None, None, None

    offset = period
    n = len(atr_series)
    upper_band = [0.0] * n
    lower_band = [0.0] * n
    supertrend = [0.0] * n
    direction = [1] * n

    for i in range(n):
        ci = offset + i
        hl2 = (highs[ci] + lows[ci]) / 2.0
        upper_band[i] = hl2 + multiplier * atr_series[i]
        lower_band[i] = hl2 - multiplier * atr_series[i]
        if i == 0:
            supertrend[i] = upper_band[i]
            direction[i] = -1 if closes[ci] < supertrend[i] else 1
            continue
        prev_ci = ci - 1
        if closes[prev_ci] > lower_band[i - 1]:
            lower_band[i] = max(lower_band[i], lower_band[i - 1])
        if closes[prev_ci] < upper_band[i - 1]:
            upper_band[i] = min(upper_band[i], upper_band[i - 1])
        prev_st = supertrend[i - 1]
        if prev_st == upper_band[i - 1]:
            if closes[ci] <= upper_band[i]:
                supertrend[i] = upper_band[i]
                direction[i] = -1
            else:
                supertrend[i] = lower_band[i]
                direction[i] = 1
        else:
            if closes[ci] >= lower_band[i]:
                supertrend[i] = lower_band[i]
                direction[i] = 1
            else:
                supertrend[i] = upper_band[i]
                direction[i] = -1

    return direction, supertrend, atr_series


class SupertrendStrategy(BaseStrategy):
    """Supertrend direction-flip entry."""

    name = "supertrend_strategy"
    category = StrategyCategory.BUYING
    min_capital_tier = CapitalTier.STARTER
    complexity = "SIMPLE"
    allowed_segments = ["NSE_INDEX", "NSE_FO", "MCX"]
    requires_margin = False

    def evaluate(self, chain, regime, open_positions, config):
        instruments = config.get("instruments", [])
        if instruments and chain.underlying not in instruments:
            return None

        if self.has_existing_position(self.name, chain.underlying, open_positions):
            return None

        data_5m: dict = chain.candles_5m
        data_1m: dict = chain.candles_1m
        if not data_5m or "close" not in data_5m:
            return None

        period = config.get("period", 10)
        multiplier = config.get("multiplier", 3.0)

        direction, supertrend, atr_series = _compute_supertrend(
            data_5m["high"], data_5m["low"], data_5m["close"], period, multiplier
        )
        if direction is None or len(direction) < 2:
            return None

        if direction[-2] == -1 and direction[-1] == 1:
            signal_dir = "BUY"
        elif direction[-2] == 1 and direction[-1] == -1:
            signal_dir = "SELL"
        else:
            return None

        spot = data_1m["close"][-1] if data_1m and "close" in data_1m else data_5m["close"][-1]
        option_type = "CE" if signal_dir == "BUY" else "PE"
        dte = self.get_dte(chain)

        if chain.strikes:
            strike_data = self.find_atm_strike(chain, option_type)
            if strike_data is None:
                return None
            premium = strike_data.call_ltp if option_type == "CE" else strike_data.put_ltp
            if premium <= 0:
                premium = _estimate_premium(spot, chain.atm_iv, dte)
            strike_val = strike_data.strike
        else:
            strike_val = _atm_strike(spot, chain.underlying)
            premium = _estimate_premium(spot, chain.atm_iv, dte)

        stop_loss_pct = config.get("stop_loss_pct", 40.0)
        target_pct = config.get("target_pct", 80.0)
        stop_loss_price = premium * (1.0 - stop_loss_pct / 100.0)
        target_price = premium * (1.0 + target_pct / 100.0)

        now = datetime.now(timezone.utc)
        time_stop = now.replace(hour=9, minute=50, second=0, microsecond=0)
        if time_stop <= now:
            time_stop = now + timedelta(hours=2)

        leg = Leg(
            option_type=option_type,
            strike=strike_val,
            expiry=chain.expiry,
            action="BUY",
            lots=1,
            premium=premium,
        )

        return Signal(
            strategy_name=self.name,
            underlying=chain.underlying,
            segment=config.get("segment", "NSE_INDEX"),
            direction="BULLISH" if signal_dir == "BUY" else "BEARISH",
            legs=[leg],
            entry_price=premium,
            stop_loss_pct=stop_loss_pct,
            stop_loss_price=stop_loss_price,
            target_pct=target_pct,
            target_price=target_price,
            time_stop=time_stop,
            max_loss_inr=premium,
            expiry=chain.expiry,
            confidence=0.75,
            metadata={
                "supertrend": round(supertrend[-1], 2),
                "atr": round(atr_series[-1], 2),
                "signal_type": signal_dir,
            },
        )

    def should_exit(self, position, current_chain, config):
        data_1m = current_chain.candles_1m
        if not data_1m or "close" not in data_1m:
            return False
        curr_price = data_1m["close"][-1]
        return (curr_price <= position.stop_loss_price
                or curr_price >= position.target_price
                or datetime.now(timezone.utc) >= position.time_stop)
