"""Brahmaastra — 9:15–10:15 AM first-hour ORB + Trap strategy.

Two entry modules run concurrently after market open:

1. Opening Range Breakout (ORB)
   - Gap filter: |open - prev_close| / prev_close ≥ 0.4%
   - Opening range = high/low of the two 15m candles between 9:15–9:45 IST
   - Bullish trigger: 15m candle body closes above RH → entry when price
     breaks that candle's high
   - Bearish trigger: 15m candle body closes below RL → entry when price
     breaks that candle's low

2. Trap Formation (PDH / PDL)
   - Monitors for false breakouts above PDH or false breakdowns below PDL
   - Identifies rejection candle (wick_ratio ≥ 1.5×) at the level
   - Bullish trap: breakdown below PDL fails, CE entry on break back above
     rejection candle high
   - Bearish trap: breakout above PDH fails, PE entry on break below
     rejection candle low

Risk:
   - SL = wick low (CE trap) / wick high (PE trap) / RH/RL for ORB
   - 50% candle rule: if candle is too large, SL = 50% of candle range
   - Partial book 50% at 1:1 RR; trail remainder to 1:1.5
   - Kill switch at 10:30 IST — no new entries after this time
"""

from __future__ import annotations

import math
from datetime import datetime, timedelta, timezone

import numpy as np
import structlog

from ..capital_tier import CapitalTier, StrategyCategory
from .base import BaseStrategy, Signal, Leg, Position

logger = structlog.get_logger(service="user_worker_pool", module="brahmaastra")

IST = timezone(timedelta(hours=5, minutes=30))

# IST times as (hour, minute) tuples
_ORB_START    = (9,  15)
_ORB_END      = (9,  45)
_EXEC_END     = (10, 15)
_KILL_SWITCH  = (10, 30)


def _atm_strike(spot: float, underlying: str) -> float:
    interval = 100 if "BANK" in underlying else 50
    return round(spot / interval) * interval


def _itm_strike(spot: float, underlying: str, option_type: str) -> float:
    """1-strike ITM from ATM."""
    interval = 100 if "BANK" in underlying else 50
    atm = round(spot / interval) * interval
    if option_type == "CE":
        return atm - interval   # ITM call = lower strike
    return atm + interval       # ITM put = higher strike


def _estimate_premium(spot: float, atm_iv: float, dte_days: int) -> float:
    T = max(dte_days, 1) / 365.0
    return max(2.0, round(spot * max(atm_iv, 0.10) * math.sqrt(T) * 0.3989, 1))


def _ist_hm(ts: float) -> tuple[int, int]:
    """Return (hour, minute) in IST for a Unix timestamp."""
    dt = datetime.fromtimestamp(ts, tz=IST)
    return dt.hour, dt.minute


def _in_window(ts: float, start: tuple, end: tuple) -> bool:
    """True if IST (hour, minute) of ts is in [start, end)."""
    h, m = _ist_hm(ts)
    sh, sm = start
    eh, em = end
    t = h * 60 + m
    return (sh * 60 + sm) <= t < (eh * 60 + em)


def _compute_orb_range(candles_15m: dict) -> tuple[float, float] | None:
    """Compute Opening Range High/Low from the 9:15 and 9:30 15m candles."""
    ts  = candles_15m.get("timestamp")
    hi  = candles_15m.get("high")
    lo  = candles_15m.get("low")
    if ts is None or len(ts) < 2:
        return None

    orb_highs = []
    orb_lows  = []
    for i in range(len(ts)):
        if _in_window(ts[i], _ORB_START, _ORB_END):
            orb_highs.append(hi[i])
            orb_lows.append(lo[i])

    if len(orb_highs) < 2:
        return None   # need both 9:15 and 9:30 candles
    return float(max(orb_highs)), float(min(orb_lows))


def _gap_pct(open_price: float, prev_close: float) -> float:
    if prev_close <= 0:
        return 0.0
    return abs(open_price - prev_close) / prev_close * 100.0


def _wick_ratio(o: float, h: float, l: float, c: float) -> tuple[float, float]:
    """Return (lower_wick_ratio, upper_wick_ratio) relative to body size."""
    body = abs(c - o)
    if body < 0.01:
        body = 0.01   # avoid division by zero on doji
    lower = (min(o, c) - l) / body
    upper = (h - max(o, c)) / body
    return lower, upper


def _find_orb_signal(candles_15m: dict, rh: float, rl: float) -> tuple[str, float, float] | None:
    """Scan for a confirmation candle that has closed outside the ORB range.

    Returns (direction, trigger_price, sl_price) or None.
    direction = "BULLISH" (CE) | "BEARISH" (PE)
    """
    ts  = candles_15m.get("timestamp")
    op  = candles_15m.get("open")
    hi  = candles_15m.get("high")
    lo  = candles_15m.get("low")
    cl  = candles_15m.get("close")
    if ts is None or len(ts) < 3:
        return None

    # Only look at candles AFTER the ORB window (9:45 onwards), within exec window
    for i in range(len(ts) - 1, -1, -1):
        if not _in_window(ts[i], _ORB_END, _EXEC_END):
            continue
        o, h, l, c = float(op[i]), float(hi[i]), float(lo[i]), float(cl[i])

        # Bullish: green candle body closes above RH
        if c > rh and o <= rh:
            trigger = h          # entry on break of confirmation candle high
            sl = rh - 0.05 * (h - rh) if (h - rh) > 0 else rh * 0.999  # slightly below RH

            # 50% candle rule: if candle is too large, use midpoint SL
            candle_range = h - l
            raw_risk = trigger - sl
            if raw_risk > 0 and candle_range / raw_risk > 3:
                sl = trigger - candle_range * 0.5

            return "BULLISH", trigger, sl

        # Bearish: red candle body closes below RL
        if c < rl and o >= rl:
            trigger = l          # entry on break of confirmation candle low
            sl = rl + 0.05 * (rl - l) if (rl - l) > 0 else rl * 1.001

            candle_range = h - l
            raw_risk = sl - trigger
            if raw_risk > 0 and candle_range / raw_risk > 3:
                sl = trigger + candle_range * 0.5

            return "BEARISH", trigger, sl

    return None


def _find_trap_signal(
    candles_15m: dict,
    pdh: float,
    pdl: float,
    wick_ratio_min: float,
) -> tuple[str, float, float] | None:
    """Scan the most recent 15m candle for a trap rejection at PDH/PDL.

    Returns (direction, trigger_price, sl_price) or None.
    """
    ts  = candles_15m.get("timestamp")
    op  = candles_15m.get("open")
    hi  = candles_15m.get("high")
    lo  = candles_15m.get("low")
    cl  = candles_15m.get("close")
    if ts is None or len(ts) < 2 or pdh <= 0 or pdl <= 0:
        return None

    # Check only the last completed candle (index -2; -1 is still forming)
    i = len(ts) - 2
    if i < 0:
        return None

    # Must be within execution window
    if not _in_window(ts[i], _ORB_START, _EXEC_END):
        return None

    o, h, l, c = float(op[i]), float(hi[i]), float(lo[i]), float(cl[i])
    lower_wr, upper_wr = _wick_ratio(o, h, l, c)

    # Bearish trap at PDH: price spiked above PDH then rejected
    if h > pdh and c < pdh and upper_wr >= wick_ratio_min:
        trigger = l    # entry when price breaks below rejection candle low
        sl = h         # SL at wick high; 50% rule
        candle_range = h - l
        raw_risk = sl - trigger
        if raw_risk > 0 and candle_range / raw_risk > 3:
            sl = trigger + candle_range * 0.5
        return "BEARISH", trigger, sl

    # Bullish trap at PDL: price spiked below PDL then recovered
    if l < pdl and c > pdl and lower_wr >= wick_ratio_min:
        trigger = h    # entry when price breaks above rejection candle high
        sl = l         # SL at wick low; 50% rule
        candle_range = h - l
        raw_risk = trigger - sl
        if raw_risk > 0 and candle_range / raw_risk > 3:
            sl = trigger - candle_range * 0.5
        return "BULLISH", trigger, sl

    return None


class BrahmaastraStrategy(BaseStrategy):
    """9:15–10:15 AM Opening Range Breakout + Trap Formation strategy."""

    name = "brahmaastra"
    category = StrategyCategory.BUYING
    min_capital_tier = CapitalTier.STARTER
    complexity = "MODERATE"
    allowed_segments = ["NSE_INDEX"]
    requires_margin = False

    def evaluate(self, chain, regime, open_positions, config):
        # ── Guard: only allowed instruments ─────────────────────────────
        instruments = config.get("instruments", [])
        if instruments and chain.underlying not in instruments:
            return None

        if self.has_existing_position(self.name, chain.underlying, open_positions):
            return None

        # ── Guard: kill switch ──────────────────────────────────────────
        now_ist = datetime.now(IST)
        now_hm  = (now_ist.hour, now_ist.minute)
        kh, km  = _KILL_SWITCH
        if now_hm[0] * 60 + now_hm[1] >= kh * 60 + km:
            return None

        # ── Data requirements ───────────────────────────────────────────
        data_15m: dict = getattr(chain, "candles_15m", None) or {}
        data_1m:  dict = chain.candles_1m or {}

        if not data_15m or "close" not in data_15m:
            return None

        spot = float(data_1m["close"][-1]) if data_1m.get("close") is not None and len(data_1m["close"]) > 0 else float(data_15m["close"][-1])

        # ── Gap filter ──────────────────────────────────────────────────
        gap_threshold = config.get("gap_threshold_pct", 0.4)
        pdh_pdl: dict = getattr(chain, "pdh_pdl", {})
        pdc = pdh_pdl.get("pdc", 0.0)
        pdh = pdh_pdl.get("pdh", 0.0)
        pdl = pdh_pdl.get("pdl", 0.0)

        # Use opening candle (9:15) open price as today's open
        ts_arr = data_15m.get("timestamp", [])
        op_arr = data_15m.get("open", [])
        today_open = 0.0
        for i in range(len(ts_arr)):
            h, m = _ist_hm(float(ts_arr[i]))
            if h == 9 and m == 15:
                today_open = float(op_arr[i])
                break
        if today_open <= 0:
            today_open = float(data_15m["open"][0]) if len(data_15m["open"]) > 0 else spot

        gap = _gap_pct(today_open, pdc)

        # ── ORB module (requires gap ≥ threshold) ───────────────────────
        signal_result: tuple | None = None

        if gap >= gap_threshold:
            orb = _compute_orb_range(data_15m)
            if orb is not None:
                rh, rl = orb
                signal_result = _find_orb_signal(data_15m, rh, rl)

        # ── Trap module (always active once we have PDH/PDL) ────────────
        if signal_result is None and pdh > 0 and pdl > 0:
            wick_ratio_min = config.get("wick_ratio_min", 1.5)
            signal_result = _find_trap_signal(data_15m, pdh, pdl, wick_ratio_min)

        if signal_result is None:
            return None

        direction, trigger_price, sl_price = signal_result

        # Only enter if current price has already breached the trigger
        # (algo confirms momentum is live, not pre-ordering)
        if direction == "BULLISH" and spot < trigger_price:
            return None
        if direction == "BEARISH" and spot > trigger_price:
            return None

        # ── Strike selection ────────────────────────────────────────────
        option_type = "CE" if direction == "BULLISH" else "PE"
        dte = self.get_dte(chain)

        strike_sel = config.get("strike_selection", "ATM")
        if chain.strikes:
            if strike_sel == "ATM":
                strike_data = self.find_atm_strike(chain, option_type)
            else:
                # 1_ITM
                itm_target = _itm_strike(spot, chain.underlying, option_type)
                strike_data = self.find_strike_near(chain, itm_target, option_type)
                if strike_data is None:
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

        if premium <= 0:
            return None

        # ── Risk calculation ────────────────────────────────────────────
        # RR-based targets: partial book at 1:1, trail to 1.5
        underlying_risk = abs(trigger_price - sl_price)
        rr_1_target  = trigger_price + underlying_risk if direction == "BULLISH" else trigger_price - underlying_risk
        rr_15_target = trigger_price + 1.5 * underlying_risk if direction == "BULLISH" else trigger_price - 1.5 * underlying_risk

        stop_loss_pct  = config.get("stop_loss_pct", 50.0)
        sl_option_price = premium * (1.0 - stop_loss_pct / 100.0)
        # Use 1.5 RR as final target
        target_option_price = premium * 1.5   # rough proxy

        # Kill-switch as time_stop
        kh_dt = now_ist.replace(hour=kh, minute=km, second=0, microsecond=0, tzinfo=IST)
        if kh_dt.tzinfo is None:
            kh_dt = kh_dt.replace(tzinfo=IST)
        time_stop = kh_dt.astimezone(timezone.utc)

        leg = Leg(
            option_type=option_type,
            strike=strike_val,
            expiry=chain.expiry,
            action="BUY",
            lots=1,
            premium=premium,
        )

        logger.info(
            "brahmaastra_signal",
            underlying=chain.underlying,
            direction=direction,
            trigger=round(trigger_price, 2),
            sl_price=round(sl_price, 2),
            rr1_target=round(rr_1_target, 2),
            gap_pct=round(gap, 3),
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
            target_pct=50.0,
            target_price=target_option_price,
            time_stop=time_stop,
            max_loss_inr=premium,
            expiry=chain.expiry,
            confidence=min(0.95, 0.60 + gap * 0.05),
            metadata={
                "sl_price":       round(sl_price, 2),       # underlying-level SL
                "trigger_price":  round(trigger_price, 2),
                "rr1_target":     round(rr_1_target, 2),
                "rr15_target":    round(rr_15_target, 2),
                "direction":      direction,
                "gap_pct":        round(gap, 3),
                "pdh":            round(pdh, 2),
                "pdl":            round(pdl, 2),
                "partial_booked": False,
            },
        )

    def should_exit(self, position: Position, current_chain, config) -> bool:
        data_1m = current_chain.candles_1m
        if not data_1m or "close" not in data_1m:
            return False

        now = datetime.now(timezone.utc)
        if now >= position.time_stop:
            return True

        curr_price = float(data_1m["close"][-1])

        # Underlying-level SL check (structural)
        sl_price  = position.metadata.get("sl_price", 0.0)
        direction = position.metadata.get("direction", "")
        if sl_price > 0:
            if direction == "BULLISH" and curr_price <= sl_price:
                return True
            if direction == "BEARISH" and curr_price >= sl_price:
                return True

        # 1:1.5 RR final target
        rr15 = position.metadata.get("rr15_target", 0.0)
        if rr15 > 0:
            if direction == "BULLISH" and curr_price >= rr15:
                return True
            if direction == "BEARISH" and curr_price <= rr15:
                return True

        return False
