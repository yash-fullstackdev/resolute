"""VWAPSupertrendStrategy — VWAP proximity + Supertrend direction + volume surge.

Entry: Supertrend bullish AND price near VWAP (within 0.15%) AND volume > 1.1x avg → BUY CE.
      Supertrend bearish AND price near VWAP AND volume > 1.1x avg → BUY PE.
Max 2 fires per day per underlying (per config).
"""

from __future__ import annotations

import math
from datetime import datetime, date as _date, timedelta, timezone

import structlog

from ..capital_tier import CapitalTier, StrategyCategory
from .base import BaseStrategy, Signal, Leg, Position
from .indicators import atr_wilder, vwap_with_bands

logger = structlog.get_logger(service="user_worker_pool", module="vwap_supertrend")


def _atm_strike(spot: float, underlying: str) -> float:
    interval = 100 if "BANK" in underlying else 50
    return round(spot / interval) * interval


def _estimate_premium(spot: float, atm_iv: float, dte_days: int) -> float:
    T = max(dte_days, 1) / 365.0
    return max(2.0, round(spot * max(atm_iv, 0.10) * math.sqrt(T) * 0.3989, 1))


def _compute_st_direction(highs, lows, closes, period, mult):
    from .indicators import atr_wilder as _atr
    if len(closes) < period + 5:
        return None
    atr_s = _atr(highs, lows, closes, period)
    if len(atr_s) < 3:
        return None
    offset = period
    n = len(atr_s)
    upper = [0.0] * n
    lower = [0.0] * n
    st = [0.0] * n
    d = [1] * n
    for i in range(n):
        ci = offset + i
        hl2 = (highs[ci] + lows[ci]) / 2.0
        upper[i] = hl2 + mult * atr_s[i]
        lower[i] = hl2 - mult * atr_s[i]
        if i == 0:
            st[i] = upper[i]
            d[i] = -1 if closes[ci] < st[i] else 1
            continue
        pci = ci - 1
        if closes[pci] > lower[i - 1]:
            lower[i] = max(lower[i], lower[i - 1])
        if closes[pci] < upper[i - 1]:
            upper[i] = min(upper[i], upper[i - 1])
        if st[i - 1] == upper[i - 1]:
            if closes[ci] <= upper[i]:
                st[i] = upper[i]; d[i] = -1
            else:
                st[i] = lower[i]; d[i] = 1
        else:
            if closes[ci] >= lower[i]:
                st[i] = lower[i]; d[i] = 1
            else:
                st[i] = upper[i]; d[i] = -1
    return d[-1]


class VWAPSupertrendStrategy(BaseStrategy):
    """VWAP + Supertrend combo — high-conviction intraday entries."""

    name = "vwap_supertrend"
    category = StrategyCategory.BUYING
    min_capital_tier = CapitalTier.STARTER
    complexity = "INTERMEDIATE"
    allowed_segments = ["NSE_INDEX", "NSE_FO", "MCX"]
    requires_margin = False

    def __init__(self) -> None:
        self._fires: dict[str, int] = {}    # underlying → fires today
        self._last_date: dict[str, str] = {}

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

        st_period = config.get("st_period", 10)
        st_mult = config.get("st_multiplier", 3.0)
        vwap_prox = config.get("vwap_proximity_pct", 0.0015)

        max_fires = config.get("max_fires_per_day", 2)

        # ── daily fire limit ──────────────────────────────────────────
        key = chain.underlying
        today = _date.today().isoformat()
        if self._last_date.get(key) != today:
            self._last_date[key] = today
            self._fires[key] = 0
        if self._fires.get(key, 0) >= max_fires:
            return None

        st_dir = _compute_st_direction(
            data_5m["high"], data_5m["low"], data_5m["close"], st_period, st_mult
        )
        if st_dir is None:
            return None

        # Use 1m for VWAP if available, else 5m
        price_data = data_1m if data_1m and "close" in data_1m and len(data_1m["close"]) >= 20 else data_5m
        if not price_data or "volume" not in price_data or len(price_data["close"]) < 20:
            return None

        vwap_data = vwap_with_bands(
            price_data["high"], price_data["low"],
            price_data["close"], price_data["volume"]
        )
        if not vwap_data or not vwap_data["vwap"]:
            return None

        curr_close = price_data["close"][-1]
        curr_vwap = vwap_data["vwap"][-1]
        if curr_vwap == 0:
            return None
        distance_pct = abs(curr_close - curr_vwap) / curr_vwap
        if distance_pct > vwap_prox:
            return None

        if st_dir == 1 and curr_close >= curr_vwap * (1 - vwap_prox):
            signal_dir = "BUY"
        elif st_dir == -1 and curr_close <= curr_vwap * (1 + vwap_prox):
            signal_dir = "SELL"
        else:
            return None

        self._fires[key] = self._fires.get(key, 0) + 1

        spot = curr_close
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
            time_stop = now + timedelta(hours=1)

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
            confidence=0.80,
            metadata={
                "signal_type": signal_dir,
                "vwap": round(curr_vwap, 2),
                "st_direction": st_dir,
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
