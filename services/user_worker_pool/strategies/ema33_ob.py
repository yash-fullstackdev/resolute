"""EMA33OBStrategy — 33 EMA Option Buying.

Entry logic (ALL required):
  1. Price above/below 33 EMA on 5m.
  2. RSI > 60 for longs, < 40 for shorts (hard block on 40-60 zone).
  3. Price above/below VWAP (if volume data available).
  4. Previous candle pulled back to within 0.5 ATR of EMA.
  5. Current candle rejected the EMA and closed back in trend direction.

Grade A (hourly proxy bullish): confidence 0.90.
Grade B (mixed): confidence 0.72.
Max 3 fires per day per underlying.
"""

from __future__ import annotations

import math
from datetime import datetime, date as _date, timedelta, timezone

import structlog

from ..capital_tier import CapitalTier, StrategyCategory
from .base import BaseStrategy, Signal, Leg, Position
from .indicators import ema, rsi_wilder, atr_wilder, vwap_with_bands

logger = structlog.get_logger(service="user_worker_pool", module="ema33_ob")


def _atm_strike(spot: float, underlying: str) -> float:
    interval = 100 if "BANK" in underlying else 50
    return round(spot / interval) * interval


def _estimate_premium(spot: float, atm_iv: float, dte_days: int) -> float:
    T = max(dte_days, 1) / 365.0
    return max(2.0, round(spot * max(atm_iv, 0.10) * math.sqrt(T) * 0.3989, 1))


class EMA33OBStrategy(BaseStrategy):
    """33 EMA pullback-rejection option buying — sniper approach."""

    name = "ema33_ob"
    category = StrategyCategory.BUYING
    min_capital_tier = CapitalTier.STARTER
    complexity = "INTERMEDIATE"
    allowed_segments = ["NSE_INDEX", "NSE_FO", "MCX"]
    requires_margin = False

    def __init__(self) -> None:
        self._fires: dict[str, int] = {}
        self._last_date: dict[str, str] = {}

    def _grade(self, closes_5m: list[float], signal_direction: str, ema_period: int) -> str:
        """A/B based on last 12 bars acting as hourly proxy."""
        if len(closes_5m) < 12:
            return "B"
        hourly_slice = closes_5m[-12:]
        period = min(ema_period, len(hourly_slice))
        h_ema = ema(hourly_slice, period)
        if not h_ema:
            return "B"
        hourly_trend = "BUY" if hourly_slice[-1] > h_ema[-1] else "SELL"
        return "A" if hourly_trend == signal_direction else "B"

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

        ema_period = config.get("ema_period", 33)
        rsi_period = config.get("rsi_period", 14)
        rsi_bull = config.get("rsi_bull_threshold", 60)
        rsi_bear = config.get("rsi_bear_threshold", 40)
        pullback_mult = config.get("pullback_atr_mult", 0.5)
        rejection_pct = config.get("rejection_body_pct", 0.0004)
        max_fires = config.get("max_fires_per_day", 3)

        closes_5m = data_5m["close"]
        highs_5m = data_5m["high"]
        lows_5m = data_5m["low"]
        opens_5m = data_5m.get("open", closes_5m)

        if len(closes_5m) < ema_period + 10:
            return None

        # ── daily fire limit ──────────────────────────────────────────
        key = chain.underlying
        today = _date.today().isoformat()
        if self._last_date.get(key) != today:
            self._last_date[key] = today
            self._fires[key] = 0
        if self._fires.get(key, 0) >= max_fires:
            return None

        ema33 = ema(closes_5m, ema_period)
        rsi_v = rsi_wilder(closes_5m, rsi_period)
        atr_v = atr_wilder(highs_5m, lows_5m, closes_5m, 14)
        if not ema33 or not rsi_v or not atr_v:
            return None

        ema_now = ema33[-1]
        rsi_now = rsi_v[-1]
        atr_now = atr_v[-1]
        curr = closes_5m[-1]
        prev = closes_5m[-2]
        o_curr = opens_5m[-1] if len(opens_5m) > 0 else curr

        # Hard block: RSI in no-trade zone
        if rsi_bear < rsi_now < rsi_bull:
            return None

        # VWAP filter (optional — requires volume)
        vwap_val = None
        pd = data_1m if data_1m and len(data_1m.get("close", [])) >= 20 else data_5m
        if pd and "volume" in pd and len(pd["volume"]) >= 20:
            vd = vwap_with_bands(pd["high"], pd["low"], pd["close"], pd["volume"])
            if vd and vd["vwap"]:
                vwap_val = vd["vwap"][-1]

        # Pullback: previous candle within 0.5 ATR of EMA
        pullback = abs(prev - ema_now) <= pullback_mult * atr_now

        signal_dir = None
        sl = None
        grade = "C"

        # LONG: above EMA, RSI > bull, above VWAP, pullback + bullish rejection
        if (curr > ema_now
                and rsi_now > rsi_bull
                and (vwap_val is None or curr > vwap_val)
                and pullback
                and curr > prev
                and (curr - min(o_curr, curr)) >= rejection_pct * curr):
            signal_dir = "BUY"
            sl = lows_5m[-2] - 0.1 * atr_now
            grade = self._grade(closes_5m, "BUY", ema_period)

        # SHORT: below EMA, RSI < bear, below VWAP, pullback + bearish rejection
        elif (curr < ema_now
              and rsi_now < rsi_bear
              and (vwap_val is None or curr < vwap_val)
              and pullback
              and curr < prev
              and (max(o_curr, curr) - curr) >= rejection_pct * curr):
            signal_dir = "SELL"
            sl = highs_5m[-2] + 0.1 * atr_now
            grade = self._grade(closes_5m, "SELL", ema_period)

        if not signal_dir or grade == "C":
            return None

        self._fires[key] = self._fires.get(key, 0) + 1

        spot = data_1m["close"][-1] if data_1m and "close" in data_1m else curr
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
        target_pct = config.get("target_pct", 100.0)   # 1x ATR minimum target
        stop_loss_price = premium * (1.0 - stop_loss_pct / 100.0)
        target_price = premium * (1.0 + target_pct / 100.0)

        now = datetime.now(timezone.utc)
        time_stop = now.replace(hour=9, minute=50, second=0, microsecond=0)
        if time_stop <= now:
            time_stop = now + timedelta(hours=2)

        confidence = 0.90 if grade == "A" else 0.72

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
            confidence=confidence,
            metadata={
                "signal_type": signal_dir,
                "grade": grade,
                "ema33": round(ema_now, 2),
                "rsi": round(rsi_now, 1),
                "vwap": round(vwap_val, 2) if vwap_val else None,
                "sl_underlying": round(sl, 2) if sl else None,
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
