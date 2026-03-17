"""EMABreakdownStrategy — EMA 2/11 crossover or strong continuation with RSI + volume.

Entry:
  BUY: EMA2 > EMA11, fresh cross or continuation, RSI 50–65, volume > 1.1x avg.
  SELL: EMA2 < EMA11, fresh cross or continuation, RSI 35–50, volume > 1.1x avg.
Max 3 fires per day per underlying.
"""

from __future__ import annotations

import math
from datetime import datetime, date as _date, timedelta, timezone

import structlog

from ..capital_tier import CapitalTier, StrategyCategory
from .base import BaseStrategy, Signal, Leg, Position
from .indicators import ema, atr_wilder, rsi_wilder, volume_ratio

logger = structlog.get_logger(service="user_worker_pool", module="ema_breakdown")


def _atm_strike(spot: float, underlying: str) -> float:
    interval = 100 if "BANK" in underlying else 50
    return round(spot / interval) * interval


def _estimate_premium(spot: float, atm_iv: float, dte_days: int) -> float:
    T = max(dte_days, 1) / 365.0
    return max(2.0, round(spot * max(atm_iv, 0.10) * math.sqrt(T) * 0.3989, 1))


class EMABreakdownStrategy(BaseStrategy):
    """EMA 2/11 momentum entry — catches trends early."""

    name = "ema_breakdown"
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

        data_5m: dict = chain.candles_5m
        data_1m: dict = chain.candles_1m
        if not data_5m or "close" not in data_5m:
            return None

        ema_short = config.get("ema_short", 2)
        ema_long = config.get("ema_long", 11)
        rsi_period = config.get("rsi_period", 14)
        breakaway_pct = config.get("breakaway_pct", 0.0008)
        max_fires = config.get("max_fires_per_day", 3)

        closes = data_5m["close"]
        highs = data_5m["high"]
        lows = data_5m["low"]
        volumes = data_5m.get("volume", [])

        if len(closes) < ema_long + 3:
            return None

        # ── daily fire limit ──────────────────────────────────────────
        key = chain.underlying
        today = _date.today().isoformat()
        if self._last_date.get(key) != today:
            self._last_date[key] = today
            self._fires[key] = 0
        if self._fires.get(key, 0) >= max_fires:
            return None

        short_ema = ema(closes, ema_short)
        long_ema = ema(closes, ema_long)
        if not short_ema or not long_ema or len(long_ema) < 3:
            return None

        ema_s_curr = short_ema[-1]
        ema_s_prev = short_ema[-2]
        ema_l_curr = long_ema[-1]
        ema_l_prev = long_ema[-2]
        curr_price = closes[-1]
        prev_price = closes[-2]

        # ATR regime filter
        atr_vals = atr_wilder(highs, lows, closes, 14)
        if not atr_vals:
            return None
        atr_pct = atr_vals[-1] / curr_price if curr_price > 0 else 0
        if atr_pct < 0.0006:
            return None

        rsi_vals = rsi_wilder(closes, rsi_period)
        if not rsi_vals:
            return None
        rsi = rsi_vals[-1]

        if not volumes or len(volumes) < 20:
            return None
        vol_rat = volume_ratio(volumes, period=20)
        if vol_rat < 1.1:
            return None

        signal_dir = None

        if ema_s_curr > ema_l_curr:
            fresh_cross = ema_s_prev <= ema_l_prev and ema_s_curr > ema_l_curr
            strong_cont = (ema_s_prev > ema_l_prev
                           and curr_price > ema_l_curr * (1 + breakaway_pct)
                           and curr_price > prev_price)
            rsi_ok = 50 < rsi < 65
            if (fresh_cross or strong_cont) and rsi_ok and curr_price > ema_l_curr:
                signal_dir = "BUY"
        elif ema_s_curr < ema_l_curr:
            fresh_cross = ema_s_prev >= ema_l_prev and ema_s_curr < ema_l_curr
            strong_cont = (ema_s_prev < ema_l_prev
                           and curr_price < ema_l_curr * (1 - breakaway_pct)
                           and curr_price < prev_price)
            rsi_ok = 35 < rsi < 50
            if (fresh_cross or strong_cont) and rsi_ok and curr_price < ema_l_curr:
                signal_dir = "SELL"

        if not signal_dir:
            return None

        self._fires[key] = self._fires.get(key, 0) + 1

        spot = data_1m["close"][-1] if data_1m and "close" in data_1m else curr_price
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
            confidence=0.80,
            metadata={
                "signal_type": signal_dir,
                "ema_gap": round(ema_s_curr - ema_l_curr, 2),
                "rsi": round(rsi, 1),
                "atr_pct": round(atr_pct * 100, 3),
                "volume_ratio": round(vol_rat, 2),
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
