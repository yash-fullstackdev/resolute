"""Parent-Child Multi-Timeframe Momentum strategy.

Temporal hierarchy:
  - Parent (1H):  10-30-100 EMA stack + MACD(48,104,36) must be aligned
  - Child  (5m):  Same indicators — MACD histogram must cross to green/red
                  and EMA stack aligned
  - Trigger:      Entry only when price breaks above the signal candle high (CE)
                  or below the signal candle low (PE)

Execution window: 10:00–14:30 IST  (avoids early chaos and late Theta)
Hard exit:        15:15 IST

Strike selection: 1–2 OTM (lower premium + Gamma acceleration as strike moves ATM)

Risk:
  - SL = recent swing low (CE) / swing high (PE) on 5m chart
  - Monitor underlying price for SL, not option LTP
  - Profit target: 25% appreciation on option premium  OR  0.75% underlying move
  - Exit if EMA10 crosses back over EMA30 on 5m (trend exhausted)
  - Exit if MACD histogram flips color on 5m (momentum lost)
"""

from __future__ import annotations

import math
from datetime import datetime, timedelta, timezone

import numpy as np
import structlog

from ..capital_tier import CapitalTier, StrategyCategory
from .base import BaseStrategy, Signal, Leg, Position

logger = structlog.get_logger(service="user_worker_pool", module="parent_child_momentum")

IST = timezone(timedelta(hours=5, minutes=30))

_EXEC_START  = (10,  0)
_EXEC_END    = (14, 30)
_HARD_EXIT   = (15, 15)


# ── Indicator helpers ────────────────────────────────────────────────────────

def _ema(closes: np.ndarray, period: int) -> np.ndarray:
    n = len(closes)
    out = np.full(n, np.nan)
    if n < period:
        return out
    k = 2.0 / (period + 1)
    out[period - 1] = float(np.mean(closes[:period]))
    for i in range(period, n):
        out[i] = closes[i] * k + out[i - 1] * (1.0 - k)
    return out


def _macd(closes: np.ndarray, fast: int, slow: int, signal: int
          ) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    """Returns (macd_line, signal_line, histogram) as same-length arrays (nan warmup)."""
    ema_fast   = _ema(closes, fast)
    ema_slow   = _ema(closes, slow)
    macd_line  = ema_fast - ema_slow     # nan where either is nan

    # Signal line: EMA of macd_line (ignore nans)
    n           = len(closes)
    sig_line    = np.full(n, np.nan)
    hist        = np.full(n, np.nan)

    # Find first valid index for macd_line
    first_valid = slow - 1   # ema_slow becomes valid at index slow-1
    if n < slow + signal:
        return macd_line, sig_line, hist

    # Compute signal line EMA only over the valid segment
    valid_macd = macd_line[first_valid:]
    if len(valid_macd) < signal:
        return macd_line, sig_line, hist

    k_s = 2.0 / (signal + 1)
    sig_vals = np.full(len(valid_macd), np.nan)
    sig_vals[signal - 1] = float(np.nanmean(valid_macd[:signal]))
    for i in range(signal, len(valid_macd)):
        if np.isnan(valid_macd[i]):
            sig_vals[i] = sig_vals[i - 1]
        else:
            sig_vals[i] = valid_macd[i] * k_s + sig_vals[i - 1] * (1.0 - k_s)

    sig_line[first_valid:] = sig_vals
    hist = macd_line - sig_line
    return macd_line, sig_line, hist


def _ema_stack_bullish(closes: np.ndarray, s: int, m: int, l: int) -> bool:
    """True if EMA(s) > EMA(m) > EMA(l) and price > EMA(s) at latest bar."""
    if len(closes) < l + 2:
        return False
    es = _ema(closes, s)
    em = _ema(closes, m)
    el = _ema(closes, l)
    if np.isnan(es[-1]) or np.isnan(em[-1]) or np.isnan(el[-1]):
        return False
    return (closes[-1] > es[-1] > em[-1] > el[-1])


def _ema_stack_bearish(closes: np.ndarray, s: int, m: int, l: int) -> bool:
    if len(closes) < l + 2:
        return False
    es = _ema(closes, s)
    em = _ema(closes, m)
    el = _ema(closes, l)
    if np.isnan(es[-1]) or np.isnan(em[-1]) or np.isnan(el[-1]):
        return False
    return (closes[-1] < es[-1] < em[-1] < el[-1])


def _macd_green(closes: np.ndarray, fast: int, slow: int, signal: int) -> bool:
    """True if MACD histogram is positive at the last bar."""
    _, _, hist = _macd(closes, fast, slow, signal)
    if np.isnan(hist[-1]):
        return False
    return hist[-1] > 0.0


def _macd_turned_green(closes: np.ndarray, fast: int, slow: int, signal: int) -> bool:
    """True if MACD histogram JUST crossed from red to green (prev < 0, curr > 0)."""
    _, _, hist = _macd(closes, fast, slow, signal)
    if len(hist) < 2 or np.isnan(hist[-1]) or np.isnan(hist[-2]):
        return False
    return hist[-2] <= 0.0 and hist[-1] > 0.0


def _macd_turned_red(closes: np.ndarray, fast: int, slow: int, signal: int) -> bool:
    """True if MACD histogram JUST crossed from green to red."""
    _, _, hist = _macd(closes, fast, slow, signal)
    if len(hist) < 2 or np.isnan(hist[-1]) or np.isnan(hist[-2]):
        return False
    return hist[-2] >= 0.0 and hist[-1] < 0.0


def _recent_swing_low(lows: np.ndarray, lookback: int) -> float:
    """Lowest low in the last `lookback` bars."""
    if len(lows) < lookback:
        return float(np.min(lows))
    return float(np.min(lows[-lookback:]))


def _recent_swing_high(highs: np.ndarray, lookback: int) -> float:
    """Highest high in the last `lookback` bars."""
    if len(highs) < lookback:
        return float(np.max(highs))
    return float(np.max(highs[-lookback:]))


def _otm_strike(spot: float, underlying: str, option_type: str, offset: int) -> float:
    interval = 100 if "BANK" in underlying else 50
    atm = round(spot / interval) * interval
    if option_type == "CE":
        return atm + interval * offset    # OTM call = higher strike
    return atm - interval * offset        # OTM put = lower strike


def _atm_strike(spot: float, underlying: str) -> float:
    interval = 100 if "BANK" in underlying else 50
    return round(spot / interval) * interval


def _estimate_premium(spot: float, atm_iv: float, dte_days: int) -> float:
    T = max(dte_days, 1) / 365.0
    return max(2.0, round(spot * max(atm_iv, 0.10) * math.sqrt(T) * 0.3989, 1))


def _ist_hm(now_ist: datetime) -> tuple[int, int]:
    return now_ist.hour, now_ist.minute


def _in_exec_window(now_ist: datetime) -> bool:
    t = now_ist.hour * 60 + now_ist.minute
    s = _EXEC_START[0] * 60 + _EXEC_START[1]
    e = _EXEC_END[0]   * 60 + _EXEC_END[1]
    return s <= t < e


# ── Strategy class ───────────────────────────────────────────────────────────

class ParentChildMomentumStrategy(BaseStrategy):
    """Dual-timeframe momentum strategy for index/equity options."""

    name = "parent_child_momentum"
    category = StrategyCategory.BUYING
    min_capital_tier = CapitalTier.STARTER
    complexity = "MODERATE"
    allowed_segments = ["NSE_INDEX", "NSE_FO"]
    requires_margin = False

    def evaluate(self, chain, regime, open_positions, config):
        # ── Instrument guard ─────────────────────────────────────────────
        instruments = config.get("instruments", [])
        if instruments and chain.underlying not in instruments:
            return None

        if self.has_existing_position(self.name, chain.underlying, open_positions):
            return None

        # ── Execution window ─────────────────────────────────────────────
        now_ist = datetime.now(IST)
        if not _in_exec_window(now_ist):
            return None

        # ── VIX filter ───────────────────────────────────────────────────
        vix = getattr(chain, "india_vix", None) or regime.get("india_vix", 0.0)
        min_vix = float(config.get("min_india_vix", 11.0))
        max_vix = float(config.get("max_india_vix", 30.0))
        if vix > 0 and (vix < min_vix or vix > max_vix):
            return None

        # ── Indicator params ─────────────────────────────────────────────
        ema_s = int(config.get("ema_short",  10))
        ema_m = int(config.get("ema_mid",    30))
        ema_l = int(config.get("ema_long",  100))

        mf = int(config.get("macd_fast",    48))
        ms = int(config.get("macd_slow",   104))
        mg = int(config.get("macd_signal",  36))

        swing_bars = int(config.get("swing_lookback_bars", 5))

        # ── Data ─────────────────────────────────────────────────────────
        data_1h:  dict = getattr(chain, "candles_1h",  None) or {}
        data_5m:  dict = chain.candles_5m or {}
        data_1m:  dict = chain.candles_1m or {}

        if not data_1h or "close" not in data_1h:
            return None
        if not data_5m or "close" not in data_5m:
            return None

        cl_1h = np.array(data_1h["close"], dtype=np.float64)
        cl_5m = np.array(data_5m["close"], dtype=np.float64)
        hi_5m = np.array(data_5m["high"],  dtype=np.float64)
        lo_5m = np.array(data_5m["low"],   dtype=np.float64)

        spot = float(data_1m["close"][-1]) if data_1m.get("close") is not None and len(data_1m["close"]) > 0 else float(cl_5m[-1])

        # ── Phase 1: Parent (1H) validation ─────────────────────────────
        parent_bull = (
            _ema_stack_bullish(cl_1h, ema_s, ema_m, ema_l) and
            _macd_green(cl_1h, mf, ms, mg)
        )
        parent_bear = (
            _ema_stack_bearish(cl_1h, ema_s, ema_m, ema_l) and
            not _macd_green(cl_1h, mf, ms, mg)
        )

        if not parent_bull and not parent_bear:
            return None

        # ── Phase 2: Child (5m) alignment + MACD crossover ──────────────
        if parent_bull:
            child_ok = (
                _ema_stack_bullish(cl_5m, ema_s, ema_m, ema_l) and
                _macd_turned_green(cl_5m, mf, ms, mg)
            )
            if not child_ok:
                return None
            direction   = "BULLISH"
            option_type = "CE"
        else:
            child_ok = (
                _ema_stack_bearish(cl_5m, ema_s, ema_m, ema_l) and
                _macd_turned_red(cl_5m, mf, ms, mg)
            )
            if not child_ok:
                return None
            direction   = "BEARISH"
            option_type = "PE"

        # ── Phase 3: Trigger — price breaks signal candle high/low ───────
        # Signal candle = the bar where MACD just crossed (-2 index)
        if len(hi_5m) < 3:
            return None

        signal_candle_high = float(hi_5m[-2])
        signal_candle_low  = float(lo_5m[-2])

        if direction == "BULLISH" and spot <= signal_candle_high:
            return None   # not yet broken above
        if direction == "BEARISH" and spot >= signal_candle_low:
            return None   # not yet broken below

        # ── Structural SL ────────────────────────────────────────────────
        if direction == "BULLISH":
            sl_underlying = _recent_swing_low(lo_5m, swing_bars)
        else:
            sl_underlying = _recent_swing_high(hi_5m, swing_bars)

        # ── Strike selection ─────────────────────────────────────────────
        strike_offset = int(config.get("strike_offset", 1))
        dte = self.get_dte(chain)

        if chain.strikes:
            otm_target  = _otm_strike(spot, chain.underlying, option_type, strike_offset)
            strike_data = self.find_strike_near(chain, otm_target, option_type)
            if strike_data is None:
                strike_data = self.find_atm_strike(chain, option_type)
            if strike_data is None:
                return None
            premium   = strike_data.call_ltp if option_type == "CE" else strike_data.put_ltp
            if premium <= 0:
                premium = _estimate_premium(spot, chain.atm_iv, dte)
            strike_val = strike_data.strike
        else:
            strike_val = _otm_strike(spot, chain.underlying, option_type, strike_offset)
            premium    = _estimate_premium(spot, chain.atm_iv, dte)

        if premium <= 0:
            return None

        # ── Targets ──────────────────────────────────────────────────────
        profit_target_pct = float(config.get("profit_target_pct", 25.0))
        stop_loss_pct     = 70.0   # backstop
        sl_option_price   = premium * (1.0 - stop_loss_pct / 100.0)
        target_price      = premium * (1.0 + profit_target_pct / 100.0)

        # Hard exit at 15:15 IST
        hard_exit_ist = now_ist.replace(hour=_HARD_EXIT[0], minute=_HARD_EXIT[1],
                                        second=0, microsecond=0)
        time_stop = hard_exit_ist.astimezone(timezone.utc)

        leg = Leg(
            option_type=option_type,
            strike=strike_val,
            expiry=chain.expiry,
            action="BUY",
            lots=1,
            premium=premium,
        )

        logger.info(
            "parent_child_signal",
            underlying=chain.underlying,
            direction=direction,
            signal_candle_h=round(signal_candle_high, 2),
            signal_candle_l=round(signal_candle_low, 2),
            sl_underlying=round(sl_underlying, 2),
            option=f"{strike_val}{option_type}",
            premium=round(premium, 2),
        )

        return Signal(
            strategy_name=self.name,
            underlying=chain.underlying,
            segment=config.get("segment", "NSE_INDEX"),
            direction=direction,
            legs=[leg],
            entry_price=premium,
            stop_loss_pct=stop_loss_pct,
            stop_loss_price=sl_option_price,
            target_pct=profit_target_pct,
            target_price=target_price,
            time_stop=time_stop,
            max_loss_inr=premium,
            expiry=chain.expiry,
            confidence=0.70,
            metadata={
                "sl_price":          round(sl_underlying, 2),
                "direction":         direction,
                "entry_underlying":  round(spot, 2),
                "signal_candle_h":   round(signal_candle_high, 2),
                "signal_candle_l":   round(signal_candle_low, 2),
                "monitor_underlying": True,
            },
        )

    def should_exit(self, position: Position, current_chain, config) -> bool:
        data_5m = current_chain.candles_5m or {}
        data_1m = current_chain.candles_1m or {}

        now = datetime.now(timezone.utc)
        if now >= position.time_stop:
            return True

        if not data_5m or "close" not in data_5m:
            return False

        cl_5m = np.array(data_5m["close"], dtype=np.float64)
        hi_5m = np.array(data_5m.get("high", []), dtype=np.float64)
        lo_5m = np.array(data_5m.get("low",  []), dtype=np.float64)

        spot = float(data_1m["close"][-1]) if data_1m.get("close") is not None and len(data_1m["close"]) > 0 else float(cl_5m[-1])

        direction = position.metadata.get("direction", "")
        sl_price  = position.metadata.get("sl_price", 0.0)

        # ── Structural SL (underlying price) ─────────────────────────────
        if sl_price > 0:
            if direction == "BULLISH" and spot <= sl_price:
                return True
            if direction == "BEARISH" and spot >= sl_price:
                return True

        ema_s = int(config.get("ema_short",  10))
        ema_m = int(config.get("ema_mid",    30))
        mf    = int(config.get("macd_fast",   48))
        ms_   = int(config.get("macd_slow",  104))
        mg    = int(config.get("macd_signal", 36))

        # ── EMA10 crosses back over EMA30 ────────────────────────────────
        ema_cross_exit = config.get("ema_crossback_exit", True)
        if ema_cross_exit and len(cl_5m) >= ema_m + 2:
            es = _ema(cl_5m, ema_s)
            em = _ema(cl_5m, ema_m)
            if not (np.isnan(es[-1]) or np.isnan(em[-1]) or
                    np.isnan(es[-2]) or np.isnan(em[-2])):
                if direction == "BULLISH":
                    # Was above, now crossed below
                    if es[-2] > em[-2] and es[-1] <= em[-1]:
                        return True
                elif direction == "BEARISH":
                    if es[-2] < em[-2] and es[-1] >= em[-1]:
                        return True

        # ── MACD histogram flip ──────────────────────────────────────────
        macd_flip_exit = config.get("macd_flip_exit", True)
        if macd_flip_exit and len(cl_5m) >= ms_ + mg + 2:
            if direction == "BULLISH" and _macd_turned_red(cl_5m, mf, ms_, mg):
                return True
            if direction == "BEARISH" and _macd_turned_green(cl_5m, mf, ms_, mg):
                return True

        return False
