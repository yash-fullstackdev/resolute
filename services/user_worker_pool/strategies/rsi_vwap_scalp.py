"""RSIVWAPScalpStrategy — mean-reversion scalp at VWAP bands.

Entry:
  BUY: RSI < 30 AND price at/below VWAP lower band.
  SELL: RSI > 70 AND price at/above VWAP upper band.
Max 3 fires per day per underlying.
"""

from __future__ import annotations

import math
from datetime import datetime, date as _date, timedelta, timezone

import structlog

from ..capital_tier import CapitalTier, StrategyCategory
from .base import BaseStrategy, Signal, Leg, Position
from .indicators import rsi_wilder, vwap_with_bands

logger = structlog.get_logger(service="user_worker_pool", module="rsi_vwap_scalp")


def _atm_strike(spot: float, underlying: str) -> float:
    interval = 100 if "BANK" in underlying else 50
    return round(spot / interval) * interval


def _estimate_premium(spot: float, atm_iv: float, dte_days: int) -> float:
    T = max(dte_days, 1) / 365.0
    return max(2.0, round(spot * max(atm_iv, 0.10) * math.sqrt(T) * 0.3989, 1))


class RSIVWAPScalpStrategy(BaseStrategy):
    """RSI + VWAP mean-reversion scalp."""

    name = "rsi_vwap_scalp"
    category = StrategyCategory.BUYING
    min_capital_tier = CapitalTier.STARTER
    complexity = "SIMPLE"
    allowed_segments = ["NSE_INDEX", "NSE_FO", "MCX"]
    requires_margin = False

    def __init__(self) -> None:
        self._fires: dict[str, int] = {}
        self._last_date: dict[str, str] = {}

    def evaluate(self, chain, regime, open_positions, config):
        instruments = config.get("instruments", [])
        if instruments and chain.underlying not in instruments:
            return None

        if self.has_existing_position(self.name, chain.underlying, open_positions):
            return None

        # Use 1m data for scalping
        data_1m: dict = chain.candles_1m
        if not data_1m or "close" not in data_1m or len(data_1m["close"]) < 20:
            return None

        rsi_period = config.get("rsi_period", 14)
        rsi_oversold = config.get("rsi_oversold", 30)
        rsi_overbought = config.get("rsi_overbought", 70)
        max_fires = config.get("max_fires_per_day", 3)

        # ── daily fire limit ──────────────────────────────────────────
        key = chain.underlying
        today = _date.today().isoformat()
        if self._last_date.get(key) != today:
            self._last_date[key] = today
            self._fires[key] = 0
        if self._fires.get(key, 0) >= max_fires:
            return None

        closes = data_1m["close"]
        highs = data_1m.get("high", closes)
        lows = data_1m.get("low", closes)
        volumes = data_1m.get("volume", [])

        if not volumes or len(volumes) < 20:
            return None

        rsi_vals = rsi_wilder(closes, rsi_period)
        if not rsi_vals:
            return None
        rsi = rsi_vals[-1]

        vwap_data = vwap_with_bands(highs, lows, closes, volumes)
        if not vwap_data or not vwap_data["vwap"]:
            return None

        curr_close = closes[-1]
        lower_1 = vwap_data["lower_1"][-1]
        upper_1 = vwap_data["upper_1"][-1]

        if rsi < rsi_oversold and curr_close <= lower_1:
            signal_dir = "BUY"
        elif rsi > rsi_overbought and curr_close >= upper_1:
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

        # Scalp: tighter stops
        stop_loss_pct = config.get("stop_loss_pct", 30.0)
        target_pct = config.get("target_pct", 50.0)
        stop_loss_price = premium * (1.0 - stop_loss_pct / 100.0)
        target_price = premium * (1.0 + target_pct / 100.0)

        now = datetime.now(timezone.utc)
        # Scalp time stop: 30 minutes from entry
        time_stop = now + timedelta(minutes=30)

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
            confidence=0.70,
            metadata={
                "signal_type": signal_dir,
                "rsi": round(rsi, 1),
                "vwap": round(vwap_data["vwap"][-1], 2),
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
