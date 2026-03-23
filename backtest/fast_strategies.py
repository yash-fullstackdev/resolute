"""Pre-computed strategy signals — vectorized for backtest speed.

Instead of calling strategy.evaluate() at each 5m close (~50k Python calls),
compute signal arrays ONCE for the full 5m dataset using numpy.

Each function returns a signal array: +1=BUY, -1=SELL, 0=no signal.
"""

from __future__ import annotations

import numpy as np
from numpy.lib.stride_tricks import sliding_window_view

from .bias_engine import _ema_full, _atr_full, _rsi_full, _supertrend_full


# ── TTM Squeeze ──────────────────────────────────────────────────────────────

def precompute_ttm_squeeze(closes, highs, lows, params=None):
    """Vectorized TTM squeeze signal for full 5m dataset."""
    p = params or {}
    bb_period = int(p.get("bb_period", 20))
    bb_std_mult = float(p.get("bb_std", 2.0))
    kc_period = int(p.get("kc_period", 20))
    kc_atr_period = int(p.get("kc_atr_period", 10))
    kc_mult = float(p.get("kc_mult", 1.5))

    n = len(closes)
    signals = np.zeros(n, dtype=np.int8)
    min_len = max(bb_period, kc_period) + kc_atr_period + 5
    if n < min_len:
        return signals

    # Bollinger Bands (rolling mean/std via cumsum)
    cs = np.cumsum(closes)
    cs2 = np.cumsum(closes ** 2)
    cs_p = np.concatenate([[0.0], cs])
    cs2_p = np.concatenate([[0.0], cs2])

    bb_roll_sum = cs_p[bb_period:] - cs_p[:-bb_period]
    bb_mean = bb_roll_sum / bb_period
    bb_var = np.maximum((cs2_p[bb_period:] - cs2_p[:-bb_period]) / bb_period - bb_mean ** 2, 0)
    bb_std = np.sqrt(bb_var)
    # bb_mean[i] corresponds to closes[bb_period-1+i], length = n - bb_period + 1
    bb_upper = bb_mean + bb_std_mult * bb_std
    bb_lower = bb_mean - bb_std_mult * bb_std

    # Keltner Channels: EMA(kc_period) ± kc_mult * ATR(kc_atr_period)
    ema_mid = _ema_full(closes, kc_period)  # starts at bar kc_period-1
    atr_vals = _atr_full(highs, lows, closes, kc_atr_period)  # starts at bar kc_atr_period

    if len(ema_mid) == 0 or len(atr_vals) == 0:
        return signals

    # Align KC: both start at different offsets
    kc_ema_off = kc_period - 1
    kc_atr_off = kc_atr_period
    kc_start = max(kc_ema_off, kc_atr_off)
    kc_len = min(len(ema_mid) - (kc_start - kc_ema_off), len(atr_vals) - (kc_start - kc_atr_off))
    if kc_len < 3:
        return signals

    kc_ema_slice = ema_mid[kc_start - kc_ema_off:kc_start - kc_ema_off + kc_len]
    kc_atr_slice = atr_vals[kc_start - kc_atr_off:kc_start - kc_atr_off + kc_len]
    kc_upper = kc_ema_slice + kc_mult * kc_atr_slice
    kc_lower = kc_ema_slice - kc_mult * kc_atr_slice

    # Align BB to KC's bar range
    bb_off = bb_period - 1
    # Both mapped to closes index: bb[i] → closes[bb_off + i], kc[i] → closes[kc_start + i]
    overlap_start = max(bb_off, kc_start)
    overlap_end = min(bb_off + len(bb_upper), kc_start + kc_len)
    if overlap_end - overlap_start < 3:
        return signals

    bb_u = bb_upper[overlap_start - bb_off:overlap_end - bb_off]
    bb_l = bb_lower[overlap_start - bb_off:overlap_end - bb_off]
    kc_u = kc_upper[overlap_start - kc_start:overlap_end - kc_start]
    kc_l = kc_lower[overlap_start - kc_start:overlap_end - kc_start]

    # Squeeze: BB inside KC
    is_squeeze = (bb_l > kc_l) & (bb_u < kc_u)

    # Momentum
    if n < bb_period:
        return signals
    windows = sliding_window_view(closes, bb_period)
    roll_max = windows.max(axis=1)
    roll_min = windows.min(axis=1)
    midline = (roll_max + roll_min) / 2
    momentum = closes[bb_period - 1:] - midline  # starts at bar bb_period - 1

    # Align momentum to overlap range
    mom_off = bb_period - 1
    mom_slice = momentum[overlap_start - mom_off:overlap_end - mom_off]
    if len(mom_slice) < len(bb_u):
        mom_slice = np.pad(mom_slice, (0, len(bb_u) - len(mom_slice)))

    olen = len(bb_u)
    if olen < 3 or len(mom_slice) < olen:
        return signals

    # Signal: was squeezed (bar -3 or -2), released (bar -1), momentum increasing
    for i in range(2, olen):
        was_sq = is_squeeze[i - 2] or is_squeeze[i - 1]
        released = not is_squeeze[i]
        if not (was_sq and released):
            continue
        mc = mom_slice[i]
        mp = mom_slice[i - 1]
        bar_idx = overlap_start + i
        if mc > 0 and mc > mp:
            signals[bar_idx] = 1
        elif mc < 0 and mc < mp:
            signals[bar_idx] = -1

    return signals


# ── Supertrend Strategy ──────────────────────────────────────────────────────

def precompute_supertrend_strategy(closes, highs, lows, params=None):
    """Vectorized Supertrend direction flip signal."""
    p = params or {}
    period = int(p.get("period", 10))
    mult = float(p.get("multiplier", 3.0))

    n = len(closes)
    signals = np.zeros(n, dtype=np.int8)
    st = _supertrend_full(highs, lows, closes, period, mult)
    if st is None or len(st) < 3:
        return signals

    offset = period
    # Signal on direction FLIP
    for i in range(1, len(st)):
        if st[i] == 1 and st[i - 1] == -1:
            signals[offset + i] = 1  # flipped to BUY
        elif st[i] == -1 and st[i - 1] == 1:
            signals[offset + i] = -1  # flipped to SELL

    return signals


# ── EMA Breakdown ─────────────────────────────────────────────────────────────

def precompute_ema_breakdown(closes, highs, lows, params=None):
    """Vectorized EMA 2/11 crossover + RSI confirmation."""
    p = params or {}
    sp = int(p.get("ema_short", 2))
    lp = int(p.get("ema_long", 11))
    rsi_period = int(p.get("rsi_period", 14))
    breakaway = float(p.get("breakaway_pct", 0.0008))

    n = len(closes)
    signals = np.zeros(n, dtype=np.int8)

    ema_s = _ema_full(closes, sp)
    ema_l = _ema_full(closes, lp)
    rsi = _rsi_full(closes, rsi_period)
    atr = _atr_full(highs, lows, closes, 14)

    if len(ema_s) == 0 or len(ema_l) == 0 or len(rsi) == 0 or len(atr) == 0:
        return signals

    # Align all to common range
    start = max(sp - 1, lp - 1, rsi_period, 14) + 1  # need prev bar
    for i in range(start, n):
        si = i - (sp - 1)
        li = i - (lp - 1)
        ri = i - rsi_period
        ai = i - 14
        if si < 1 or li < 1 or si >= len(ema_s) or li >= len(ema_l) or ri >= len(rsi) or ai >= len(atr):
            continue

        es_curr, es_prev = ema_s[si], ema_s[si - 1]
        el_curr, el_prev = ema_l[li], ema_l[li - 1]
        rsi_val = rsi[ri]
        atr_pct = atr[ai] / closes[i] if closes[i] > 0 else 0

        if atr_pct < 0.0006:
            continue

        if es_curr > el_curr:
            fresh = es_prev <= el_prev and es_curr > el_curr
            strong = es_prev > el_prev and closes[i] > el_curr * (1 + breakaway) and closes[i] > closes[i - 1]
            if (fresh or strong) and 50 < rsi_val < 65 and closes[i] > el_curr:
                signals[i] = 1
        elif es_curr < el_curr:
            fresh = es_prev >= el_prev and es_curr < el_curr
            strong = es_prev < el_prev and closes[i] < el_curr * (1 - breakaway) and closes[i] < closes[i - 1]
            if (fresh or strong) and 35 < rsi_val < 50 and closes[i] < el_curr:
                signals[i] = -1

    return signals


# ── EMA33 Order Block ────────────────────────────────────────────────────────

def precompute_ema33_ob(closes, highs, lows, opens, params=None):
    """Vectorized EMA33 pullback-rejection."""
    p = params or {}
    ema_period = int(p.get("ema_period", 33))
    rsi_period = int(p.get("rsi_period", 14))
    rsi_bull = float(p.get("rsi_bull_threshold", 60))
    rsi_bear = float(p.get("rsi_bear_threshold", 40))
    pullback_mult = float(p.get("pullback_atr_mult", 0.5))
    rejection_pct = float(p.get("rejection_body_pct", 0.0004))

    n = len(closes)
    signals = np.zeros(n, dtype=np.int8)

    ema33 = _ema_full(closes, ema_period)
    rsi = _rsi_full(closes, rsi_period)
    atr = _atr_full(highs, lows, closes, 14)

    if len(ema33) == 0 or len(rsi) == 0 or len(atr) == 0:
        return signals

    start = max(ema_period - 1, rsi_period, 14) + 1
    for i in range(start, n):
        ei = i - (ema_period - 1)
        ri = i - rsi_period
        ai = i - 14
        if ei >= len(ema33) or ri >= len(rsi) or ai >= len(atr):
            continue

        ema_now = ema33[ei]
        rsi_now = rsi[ri]
        atr_now = atr[ai]
        curr = closes[i]
        prev = closes[i - 1]
        o_curr = opens[i] if i < len(opens) else curr

        if rsi_bear < rsi_now < rsi_bull:
            continue

        pullback = abs(prev - ema_now) <= pullback_mult * atr_now

        if (curr > ema_now and rsi_now > rsi_bull and pullback
                and curr > prev and (curr - min(o_curr, curr)) >= rejection_pct * curr):
            signals[i] = 1
        elif (curr < ema_now and rsi_now < rsi_bear and pullback
              and curr < prev and (max(o_curr, curr) - curr) >= rejection_pct * curr):
            signals[i] = -1

    return signals


# ── SMC Order Block — BigBeluga-aligned state machine ────────────────────────

def _smc_build_ob(h, l, atr_ob, start, end, use_max, ob_length=5):
    """Build an ATR(50)-sized OB at the extreme candle in h/l[start:end].

    Uses ATR(50) scaled by ob_length/5 — matching BigBeluga's formula:
      atr_display = ta.atr(200) / (5 / len)  →  wider zones for longer length.
    ATR(50) on 5m gives zones ~3-5x wider than ATR(14), matching chart display.
    """
    start = max(0, start)
    end   = min(end, len(h))
    if start >= end:
        return None
    scale = max(ob_length, 1) / 5.0   # ob_length=5 → scale=1.0, ob_length=10 → 2.0
    if use_max:
        best = start
        for i in range(start + 1, end):
            if h[i] > h[best]:
                best = i
        a   = (atr_ob[best] if best < len(atr_ob) else 1.0) * scale
        top = h[best]
        btm = max(top - a, l[best])
    else:
        best = start
        for i in range(start + 1, end):
            if l[i] < l[best]:
                best = i
        a   = (atr_ob[best] if best < len(atr_ob) else 1.0) * scale
        btm = l[best]
        top = min(btm + a, h[best])
    if top <= btm:
        return None
    return {"top": top, "btm": btm}


def _smc_extreme(vals, start, end, use_max):
    start = max(0, start)
    end   = min(end, len(vals))
    if start >= end:
        return vals[max(0, start - 1)]
    best = vals[start]
    for i in range(start + 1, end):
        if (use_max and vals[i] > best) or (not use_max and vals[i] < best):
            best = vals[i]
    return best


def precompute_smc_order_block(closes, highs, lows, opens=None, params=None):
    """BigBeluga-aligned SMC: BOS/CHoCH state machine + ATR-sized Order Block entry.

    Runs the full BigBeluga market structure state machine bar-by-bar:
      - Creates OBs only on confirmed BOS/CHoCH events (not recalculated).
      - OB size: ATR-based (top=low+ATR for bullish, btm=high-ATR for bearish).
      - Mitigation: OB removed when min/max(close,open) closes through it.
      - Signal emitted when price is inside an aligned unmitigated OB + momentum.
    """
    if opens is None:
        opens = closes

    p         = params or {}
    ob_length = int(p.get("ob_length", 5))
    ob_limit  = 5

    n = len(closes)
    signals = np.zeros(n, dtype=np.int8)
    if n < 55:
        return signals

    # ATR(14) — regime gate: is market volatile enough to trade?
    atr_raw = _atr_full(highs, lows, closes, 14)
    atr_a   = [0.0] * n
    for i, v in enumerate(atr_raw):
        if 14 + i < n:
            atr_a[14 + i] = float(v)

    # ATR(50) — OB sizing: BigBeluga uses atr(200); ATR(50) is a practical match
    # giving zones ~3-4x wider than ATR(14), proportional to recent swing size.
    atr_ob_raw = _atr_full(highs, lows, closes, 50)
    atr_ob = [0.0] * n
    for i, v in enumerate(atr_ob_raw):
        if 50 + i < n:
            atr_ob[50 + i] = float(v)

    # Convert to plain Python lists for fast indexed access
    h = list(float(x) for x in highs)
    l = list(float(x) for x in lows)
    c = list(float(x) for x in closes)
    o = list(float(x) for x in opens)

    # ── BigBeluga state machine ───────────────────────────────────────────────
    trend    = 0
    phase    = 1         # 1=finding initial break, 2=active
    ms_bos   = h[0]     # initial: close above = bullish break
    ms_choch = l[0]     # initial: close below = bearish break
    ms_main  = 0.0      # running extreme
    ms_seg   = 0        # segment start index
    run_high = h[0]
    run_low  = l[0]

    bull_obs: list[dict] = []
    bear_obs: list[dict] = []

    for i in range(1, n):
        hi, li, ci, oi = h[i], l[i], c[i], o[i]
        p_c, p_o = c[i - 1], o[i - 1]

        crossup = hi > run_high
        crossdn = li < run_low
        if crossup or crossdn:
            run_high = hi
            run_low  = li

        # ── Phase 1: initial structure identification ─────────────────────
        if phase == 1:
            if ci >= ms_bos:
                trend = 1; phase = 2; ms_main = hi; ms_seg = i
                ob = _smc_build_ob(h, l, atr_ob, 0, i, use_max=False, ob_length=ob_length)
                if ob: bear_obs.append(ob)
                ms_bos = None
            elif ci <= ms_choch:
                trend = -1; phase = 2; ms_main = li; ms_seg = i
                ob = _smc_build_ob(h, l, atr_ob, 0, i, use_max=True, ob_length=ob_length)
                if ob: bull_obs.append(ob)
                ms_bos = None
            else:
                if hi > ms_bos:   ms_bos   = hi
                if li < ms_choch: ms_choch = li

        # ── Phase 2: active structure tracking ───────────────────────────
        elif phase == 2:
            if trend == 1:
                if hi >= ms_main:
                    ms_main = hi
                # BOS setup: crossdn + 2 consecutive bearish closes
                if ms_bos is None and crossdn and ci < oi and p_c < p_o:
                    ms_bos = ms_main
                if ms_bos is not None:
                    if hi >= ms_bos and ci <= ms_bos:
                        ms_bos = hi  # upsweep
                    elif ci >= ms_bos:
                        ob = _smc_build_ob(h, l, atr_ob, ms_seg, i + 1, use_max=False, ob_length=ob_length)
                        if ob: bull_obs.append(ob)
                        ms_choch = _smc_extreme(l, ms_seg, i + 1, use_max=False)
                        ms_bos = None; ms_seg = i
                if ci <= ms_choch:
                    trend = -1
                    ob = _smc_build_ob(h, l, atr_ob, ms_seg, i + 1, use_max=True, ob_length=ob_length)
                    if ob: bear_obs.append(ob)
                    ms_choch = ms_bos if ms_bos is not None else ms_choch
                    ms_bos = None; ms_main = li; ms_seg = i
                elif li <= ms_choch and ci >= ms_choch:
                    ms_choch = li  # dnsweep
            else:
                if li <= ms_main:
                    ms_main = li
                # BOS setup: crossup + 2 consecutive bullish closes
                if ms_bos is None and crossup and ci > oi and p_c > p_o:
                    ms_bos = ms_main
                if ms_bos is not None:
                    if li <= ms_bos and ci >= ms_bos:
                        ms_bos = li  # dnsweep
                    elif ci <= ms_bos:
                        ob = _smc_build_ob(h, l, atr_ob, ms_seg, i + 1, use_max=True, ob_length=ob_length)
                        if ob: bear_obs.append(ob)
                        ms_choch = _smc_extreme(h, ms_seg, i + 1, use_max=True)
                        ms_bos = None; ms_seg = i
                if ci >= ms_choch:
                    trend = 1
                    ob = _smc_build_ob(h, l, atr_ob, ms_seg, i + 1, use_max=False, ob_length=ob_length)
                    if ob: bull_obs.append(ob)
                    ms_choch = ms_bos if ms_bos is not None else ms_choch
                    ms_bos = None; ms_main = hi; ms_seg = i
                elif hi >= ms_choch and ci <= ms_choch:
                    ms_choch = hi  # upsweep

        # ── Signal: check BEFORE mitigation so we catch recovery candles ─
        # BigBeluga entry trigger: low < ob.top (wick touched the zone).
        # We also require close >= ob.btm (recovered above zone bottom = bullish).
        if trend != 0 and phase == 2 and i >= 3:
            curr_atr = atr_a[i]
            if curr_atr > 0 and (ci <= 0 or curr_atr / ci >= 0.0004):
                if trend == 1 and bull_obs:
                    # Wick into bullish demand OB and close holds above btm + momentum
                    for ob in bull_obs:
                        if li <= ob["top"] and ci >= ob["btm"] and c[i] > c[i - 2]:
                            signals[i] = 1
                            break
                elif trend == -1 and bear_obs:
                    # Wick into bearish supply OB and close holds below top + momentum
                    for ob in bear_obs:
                        if hi >= ob["btm"] and ci <= ob["top"] and c[i] < c[i - 2]:
                            signals[i] = -1
                            break

        # ── Mitigation (BigBeluga "Close" method) ────────────────────────
        bull_obs = [ob for ob in bull_obs if min(ci, oi) >= ob["btm"]]
        bear_obs = [ob for ob in bear_obs if max(ci, oi) <= ob["top"]]
        if len(bull_obs) > ob_limit: bull_obs = bull_obs[-ob_limit:]
        if len(bear_obs) > ob_limit: bear_obs = bear_obs[-ob_limit:]

    return signals


# ── RSI VWAP Scalp (pre-filter on RSI extremes) ─────────────────────────────

def precompute_rsi_vwap_scalp(closes, highs, lows, params=None):
    """Pre-filter: mark bars where RSI is in overbought/oversold zone."""
    p = params or {}
    rsi_period = int(p.get("rsi_period", 14))
    rsi_oversold = float(p.get("rsi_oversold", 30))
    rsi_overbought = float(p.get("rsi_overbought", 70))

    n = len(closes)
    signals = np.zeros(n, dtype=np.int8)
    rsi = _rsi_full(closes, rsi_period)
    if len(rsi) == 0:
        return signals

    offset = rsi_period
    length = min(len(rsi), n - offset)
    # Mark as possible signal where RSI is extreme (actual VWAP check done by strategy)
    signals[offset:offset + length] = np.where(
        rsi[:length] < rsi_oversold, 1,
        np.where(rsi[:length] > rsi_overbought, -1, 0)
    ).astype(np.int8)
    return signals


# ── VWAP Supertrend (pre-filter on Supertrend direction changes) ─────────────

def precompute_vwap_supertrend(closes, highs, lows, params=None):
    """Pre-filter: mark bars near Supertrend direction flips."""
    p = params or {}
    period = int(p.get("st_period", 10))
    mult = float(p.get("st_multiplier", 3.0))

    n = len(closes)
    signals = np.zeros(n, dtype=np.int8)
    st = _supertrend_full(highs, lows, closes, period, mult)
    if st is None or len(st) < 2:
        return signals

    offset = period
    # Mark bars where supertrend has a direction (possible signal if VWAP aligns)
    length = min(len(st), n - offset)
    signals[offset:offset + length] = st[:length]
    return signals


# ── S1: Brahmaastra helpers ───────────────────────────────────────────────────

def build_prev_day_arrays(closes_1m, highs_1m, lows_1m, timestamps_1m, timestamps_target):
    """Build per-bar prev-day close/high/low arrays aligned to *timestamps_target*.

    For each bar in timestamps_target, looks up the IST calendar date and returns
    the previous trading day's close, high, and low from 1m data.

    Returns (pdc_arr, pdh_arr, pdl_arr) — float64 arrays, same length as timestamps_target.
    Bars where prev-day data doesn't exist are filled with 0.0.
    """
    IST_OFFSET = 19800
    ts1 = np.asarray(timestamps_1m, dtype=np.float64)
    cl1 = np.asarray(closes_1m,    dtype=np.float64)
    hi1 = np.asarray(highs_1m,     dtype=np.float64)
    lo1 = np.asarray(lows_1m,      dtype=np.float64)

    day_of_1m = ((ts1 + IST_OFFSET) // 86400).astype(np.int64)

    # Build daily summary: last close, max high, min low per IST day
    daily_close: dict[int, float] = {}
    daily_high:  dict[int, float] = {}
    daily_low:   dict[int, float] = {}
    for i in range(len(ts1)):
        d = int(day_of_1m[i])
        daily_close[d] = float(cl1[i])           # last 1m close of that day
        daily_high[d]  = max(daily_high.get(d, float(hi1[i])), float(hi1[i]))
        daily_low[d]   = min(daily_low.get(d, float(lo1[i])), float(lo1[i]))

    sorted_days = sorted(daily_close.keys())

    ts_tgt = np.asarray(timestamps_target, dtype=np.float64)
    day_of_tgt = ((ts_tgt + IST_OFFSET) // 86400).astype(np.int64)

    n = len(ts_tgt)
    pdc_arr = np.zeros(n, dtype=np.float64)
    pdh_arr = np.zeros(n, dtype=np.float64)
    pdl_arr = np.zeros(n, dtype=np.float64)

    for i in range(n):
        d = int(day_of_tgt[i])
        # Find prev trading day
        pos = np.searchsorted(sorted_days, d)
        if pos > 0:
            prev_d = sorted_days[pos - 1]
            pdc_arr[i] = daily_close.get(prev_d, 0.0)
            pdh_arr[i] = daily_high.get(prev_d,  0.0)
            pdl_arr[i] = daily_low.get(prev_d,   0.0)

    return pdc_arr, pdh_arr, pdl_arr


# ── S1: Brahmaastra ──────────────────────────────────────────────────────────

def precompute_brahmaastra(closes_15m, highs_15m, lows_15m, opens_15m,
                           timestamps_15m, params=None):
    """Vectorized Brahmaastra signals on 15m candle array.

    Returns int8 array (length = len(closes_15m)):  +1=BULLISH CE, -1=BEARISH PE, 0=none.

    Logic per bar (executed after 9:45 IST):
      - Gap filter: uses first bar open vs prev-day close stored in params.
      - ORB: finds the 9:15 and 9:30 bars → RH/RL.
      - Confirms when a 15m body closes outside the range AND the current bar
        breaks that candle's extremum.
      - Also checks PDH/PDL trap from params.
    """
    p = params or {}
    gap_threshold = float(p.get("gap_threshold_pct", 0.4))
    wick_ratio_min = float(p.get("wick_ratio_min", 1.5))
    # PDH/PDL injected per-day by optimizer
    pdh_arr = p.get("pdh_arr")  # array same length as timestamps_15m
    pdl_arr = p.get("pdl_arr")
    pdc_arr = p.get("pdc_arr")  # previous day close per bar

    n = len(closes_15m)
    signals = np.zeros(n, dtype=np.int8)
    if n < 4:
        return signals

    ts  = np.array(timestamps_15m, dtype=np.float64)
    op  = np.array(opens_15m,      dtype=np.float64)
    hi  = np.array(highs_15m,      dtype=np.float64)
    lo  = np.array(lows_15m,       dtype=np.float64)
    cl  = np.array(closes_15m,     dtype=np.float64)

    IST_OFFSET = 19800  # 5h30m in seconds
    ts_ist = ts + IST_OFFSET
    bar_min = ((ts_ist % 86400) // 60).astype(np.int32)  # minutes since midnight IST

    # ORB range: bars at 9:15 (555 min) and 9:30 (570 min)
    ORB_S = 9 * 60 + 15   # 555
    ORB_E = 9 * 60 + 45   # 585
    EXEC_E = 10 * 60 + 15  # 615
    KILL   = 10 * 60 + 30  # 630

    # Group by calendar day
    day_ids = (ts_ist // 86400).astype(np.int64)
    unique_days = np.unique(day_ids)

    for day in unique_days:
        mask = day_ids == day
        idx  = np.where(mask)[0]
        if len(idx) < 3:
            continue

        mins_day = bar_min[idx]

        # ── Gap filter ───────────────────────────────────────────────────
        pdc = 0.0
        if pdc_arr is not None and len(pdc_arr) > idx[0]:
            pdc = float(pdc_arr[idx[0]])
        first_open = float(op[idx[0]])
        if pdc > 0:
            gap = abs(first_open - pdc) / pdc * 100.0
        else:
            gap = 0.0

        # ── ORB range ────────────────────────────────────────────────────
        orb_mask = (mins_day >= ORB_S) & (mins_day < ORB_E)
        orb_idx  = idx[orb_mask]
        if len(orb_idx) < 2:
            continue
        rh = float(np.max(hi[orb_idx]))
        rl = float(np.min(lo[orb_idx]))

        # PDH / PDL for this day
        pdh = float(pdh_arr[idx[0]]) if pdh_arr is not None and len(pdh_arr) > idx[0] else 0.0
        pdl = float(pdl_arr[idx[0]]) if pdl_arr is not None and len(pdl_arr) > idx[0] else 0.0

        # ── Scan execution window bars ───────────────────────────────────
        exec_mask = (mins_day >= ORB_E) & (mins_day < EXEC_E)
        exec_idx  = idx[exec_mask]

        for i in exec_idx:
            o_i, h_i, l_i, c_i = float(op[i]), float(hi[i]), float(lo[i]), float(cl[i])

            # ORB: bullish confirmation (green body closes above RH)
            if gap >= gap_threshold:
                if c_i > rh and o_i <= rh:
                    signals[i] = 1
                    continue
                if c_i < rl and o_i >= rl:
                    signals[i] = -1
                    continue

            # Trap: bearish at PDH
            if pdh > 0 and h_i > pdh and c_i < pdh:
                body = abs(c_i - o_i) or 0.01
                upper_wick = (h_i - max(o_i, c_i)) / body
                if upper_wick >= wick_ratio_min:
                    signals[i] = -1
                    continue

            # Trap: bullish at PDL
            if pdl > 0 and l_i < pdl and c_i > pdl:
                body = abs(c_i - o_i) or 0.01
                lower_wick = (min(o_i, c_i) - l_i) / body
                if lower_wick >= wick_ratio_min:
                    signals[i] = 1

    return signals


# ── S2: 5 EMA Mean Reversion ─────────────────────────────────────────────────

def precompute_ema5_reversion(closes_5m, highs_5m, lows_5m,
                               closes_15m, highs_15m, lows_15m,
                               params=None):
    """Vectorized EMA5 mean-reversion signals.

    Scans both 5m (PE) and 15m (CE) arrays.  Returns signal on the 5m array
    (+1=CE, -1=PE, 0=none).  The 15m signals are mapped back to the nearest 5m bar.
    """
    p = params or {}
    ema_period   = int(p.get("ema_period", 5))
    min_dist_pct = float(p.get("min_distance_ema_pct", 0.002))

    n5 = len(closes_5m)
    signals = np.zeros(n5, dtype=np.int8)

    cl5 = np.array(closes_5m,  dtype=np.float64)
    hi5 = np.array(highs_5m,   dtype=np.float64)
    lo5 = np.array(lows_5m,    dtype=np.float64)

    # ── 5m EMA for PE signals ────────────────────────────────────────────
    k  = 2.0 / (ema_period + 1)
    ema5 = np.full(n5, np.nan)
    if n5 >= ema_period:
        ema5[ema_period - 1] = np.mean(cl5[:ema_period])
        for i in range(ema_period, n5):
            ema5[i] = cl5[i] * k + ema5[i - 1] * (1 - k)

    for i in range(ema_period + 1, n5 - 1):
        if np.isnan(ema5[i]):
            continue
        ev = ema5[i]
        lo_i = float(lo5[i])
        hi_i = float(hi5[i])
        # PE: candle floats above EMA (low > EMA) with sufficient distance
        if lo_i > ev and (lo_i - ev) / ev >= min_dist_pct:
            signals[i] = -1
            continue

    # ── 15m EMA for CE signals — map back to 5m ──────────────────────────
    if closes_15m is not None and len(closes_15m) >= ema_period + 2:
        cl15 = np.array(closes_15m, dtype=np.float64)
        hi15 = np.array(highs_15m,  dtype=np.float64)
        n15  = len(cl15)
        ema15 = np.full(n15, np.nan)
        if n15 >= ema_period:
            ema15[ema_period - 1] = np.mean(cl15[:ema_period])
            for i in range(ema_period, n15):
                ema15[i] = cl15[i] * k + ema15[i - 1] * (1 - k)

        # Each 15m bar = 3 × 5m bars  (index mapping: 15m bar i → 5m bars 3i..3i+2)
        for i15 in range(ema_period + 1, n15 - 1):
            if np.isnan(ema15[i15]):
                continue
            ev  = ema15[i15]
            hi_ = float(hi15[i15])
            # CE: candle high < EMA (floating below mean) with sufficient distance
            if hi_ < ev and (ev - hi_) / ev >= min_dist_pct:
                # Map to the corresponding 5m bar (last bar of this 15m period)
                i5 = min(i15 * 3 + 2, n5 - 1)
                if signals[i5] == 0:   # don't overwrite a PE signal
                    signals[i5] = 1

    return signals


# ── S3: Parent-Child Momentum ────────────────────────────────────────────────

def precompute_parent_child(closes_5m, highs_5m, lows_5m,
                             closes_1h,
                             params=None):
    """Vectorized Parent-Child momentum signals on 5m array.

    Parent (1H) must have EMA(10>30>100) + MACD(48,104,36) > 0 for bullish.
    Child (5m) must have same EMA stack + MACD just crossed green/red.
    Entry when price breaks signal candle high/low.

    Returns int8 signal on 5m array (+1=CE, -1=PE).
    """
    p = params or {}
    ema_s = int(p.get("ema_short",   10))
    ema_m = int(p.get("ema_mid",     30))
    ema_l = int(p.get("ema_long",   100))
    mf    = int(p.get("macd_fast",   48))
    ms    = int(p.get("macd_slow",  104))
    mg    = int(p.get("macd_signal", 36))

    n5 = len(closes_5m)
    signals = np.zeros(n5, dtype=np.int8)

    if closes_1h is None or len(closes_1h) < ema_l + mg + 5:
        return signals
    if n5 < ema_l + mg + 5:
        return signals

    cl5 = np.array(closes_5m, dtype=np.float64)
    cl1h = np.array(closes_1h, dtype=np.float64)
    n1h  = len(cl1h)

    # ── Helper: vectorized EMA ──────────────────────────────────────────
    def fast_ema(arr, period):
        out = np.full(len(arr), np.nan)
        if len(arr) < period:
            return out
        k_ = 2.0 / (period + 1)
        out[period - 1] = np.mean(arr[:period])
        for j in range(period, len(arr)):
            out[j] = arr[j] * k_ + out[j - 1] * (1 - k_)
        return out

    def fast_macd_hist(arr, fast_, slow_, sig_):
        ef = fast_ema(arr, fast_)
        es = fast_ema(arr, slow_)
        ml = ef - es
        sv = np.full(len(arr), np.nan)
        fv = slow_ - 1
        if len(arr) < fv + sig_:
            return np.full(len(arr), np.nan)
        ks = 2.0 / (sig_ + 1)
        valid = ml[fv:]
        sv_seg = np.full(len(valid), np.nan)
        sv_seg[sig_ - 1] = np.nanmean(valid[:sig_])
        for j in range(sig_, len(valid)):
            if np.isnan(valid[j]):
                sv_seg[j] = sv_seg[j - 1]
            else:
                sv_seg[j] = valid[j] * ks + sv_seg[j - 1] * (1 - ks)
        sv[fv:] = sv_seg
        return ml - sv

    # Precompute parent (1H) indicators
    p_es  = fast_ema(cl1h, ema_s)
    p_em  = fast_ema(cl1h, ema_m)
    p_el  = fast_ema(cl1h, ema_l)
    p_h   = fast_macd_hist(cl1h, mf, ms, mg)

    # Precompute child (5m) indicators
    c_es  = fast_ema(cl5, ema_s)
    c_em  = fast_ema(cl5, ema_m)
    c_el  = fast_ema(cl5, ema_l)
    c_h   = fast_macd_hist(cl5, mf, ms, mg)

    # Each 1H bar = 12 × 5m bars
    BAR_RATIO = 12

    for i5 in range(ema_l + mg + 5, n5 - 1):
        if np.isnan(c_es[i5]) or np.isnan(c_em[i5]) or np.isnan(c_el[i5]):
            continue
        if np.isnan(c_h[i5]) or np.isnan(c_h[i5 - 1]):
            continue

        # Parent bar index (approximate)
        i1h = min(i5 // BAR_RATIO, n1h - 1)
        if i1h < 1:
            continue
        if np.isnan(p_es[i1h]) or np.isnan(p_em[i1h]) or np.isnan(p_el[i1h]):
            continue
        if np.isnan(p_h[i1h]):
            continue

        # Bullish parent
        if (cl1h[i1h] > p_es[i1h] > p_em[i1h] > p_el[i1h] and p_h[i1h] > 0):
            # Bullish child: EMA stack + MACD just turned green
            if (cl5[i5] > c_es[i5] > c_em[i5] > c_el[i5] and
                    c_h[i5 - 1] <= 0 and c_h[i5] > 0):
                signals[i5] = 1
                continue

        # Bearish parent
        if (cl1h[i1h] < p_es[i1h] < p_em[i1h] < p_el[i1h] and p_h[i1h] < 0):
            # Bearish child: EMA stack + MACD just turned red
            if (cl5[i5] < c_es[i5] < c_em[i5] < c_el[i5] and
                    c_h[i5 - 1] >= 0 and c_h[i5] < 0):
                signals[i5] = -1

    return signals


# ── Per-strategy session config (shared by optimizer AND multi_runner) ────────
# Both engines import this so they always use identical windows.
# entry_start / entry_cutoff : IST minutes — window where new entries are allowed
# force_exit_min             : IST minutes — hard square-off (strategy kill switch)
# max_fires                  : max new entries per calendar day
# unified_window             : True = single contiguous window; False = morning+afternoon split

STRATEGY_SESSION_CONFIG: dict[str, dict] = {
    "brahmaastra": {
        "entry_start": 555,     # 9:15 IST
        "entry_cutoff": 615,    # 10:15 IST
        "force_exit_min": 630,  # 10:30 IST kill switch
        "max_fires": 2,
        "unified_window": True,
    },
    "ema5_mean_reversion": {
        "entry_start": 560,     # 9:20 IST
        "entry_cutoff": 870,    # 14:30 IST
        "force_exit_min": 920,  # 15:20 IST
        "max_fires": 6,
        "unified_window": False,  # uses morning + afternoon split
    },
    "parent_child_momentum": {
        "entry_start": 600,     # 10:00 IST
        "entry_cutoff": 870,    # 14:30 IST
        "force_exit_min": 915,  # 15:15 IST
        "max_fires": 4,
        "unified_window": True,
    },
}

# Defaults for any strategy not listed above
_DEFAULT_SESSION = {
    "entry_start": 560,
    "entry_cutoff": 870,
    "force_exit_min": 915,
    "max_fires": 5,
    "unified_window": False,
}


def get_strategy_session(strategy_name: str) -> dict:
    """Return session config for *strategy_name*, falling back to defaults."""
    return {**_DEFAULT_SESSION, **STRATEGY_SESSION_CONFIG.get(strategy_name, {})}


# ── Exit defaults per strategy ────────────────────────────────────────────────
# sl_atr_mult / tp_atr_mult: ATR multipliers for stop-loss and take-profit
# max_hold_bars : hard cap on bars before force-close (must fit inside the
#                 effective window between entry and force_exit_min)
#
# Brahmaastra: window is 9:15–10:30 (75 bars). Entries allowed 9:15–10:15.
#   Worst case: enter at 10:15 → 15 bars until kill switch.
#   Best case: enter at 9:15 → 75 bars. Use 12 as default (conservative ORB hold).
#
# EMA5 Mean Reversion: full day window (9:20–15:20 = 360 bars), mean-reversion
#   trades should not be held too long — 24 bars (2h) is sensible.
#
# Parent-Child Momentum: 10:00–15:15 (315 bars), trend-following, 16 bars (80m).

STRATEGY_EXIT_DEFAULTS: dict[str, dict] = {
    "brahmaastra":           {"sl_atr_mult": 0.5, "tp_atr_mult": 0.75, "max_hold_bars": 12},
    "ema5_mean_reversion":   {"sl_atr_mult": 0.5, "tp_atr_mult": 1.5,  "max_hold_bars": 24},
    "parent_child_momentum": {"sl_atr_mult": 1.0, "tp_atr_mult": 1.5,  "max_hold_bars": 16},
}

_DEFAULT_EXIT = {"sl_atr_mult": 0.5, "tp_atr_mult": 1.5, "max_hold_bars": 20}


def get_strategy_exit_defaults(strategy_name: str) -> dict:
    """Return exit defaults for *strategy_name*, falling back to generic defaults."""
    return {**_DEFAULT_EXIT, **STRATEGY_EXIT_DEFAULTS.get(strategy_name, {})}


# ── Registry ─────────────────────────────────────────────────────────────────

FAST_STRATEGY_MAP = {
    "ttm_squeeze": precompute_ttm_squeeze,
    "supertrend_strategy": precompute_supertrend_strategy,
    "ema_breakdown": precompute_ema_breakdown,
    "ema33_ob": precompute_ema33_ob,
    "smc_order_block": None,   # handled in precompute_strategy_signals (needs opens)
    "rsi_vwap_scalp": precompute_rsi_vwap_scalp,
    "vwap_supertrend": precompute_vwap_supertrend,
    # New strategies registered with sentinel values — actual dispatch is in
    # precompute_strategy_signals() which passes the correct extra arrays.
    "brahmaastra":          None,
    "ema5_mean_reversion":  None,
    "parent_child_momentum": None,
}


def precompute_strategy_signals(
    strategy_name,
    closes_5m, highs_5m, lows_5m, opens_5m=None,
    params=None,
    # Extra arrays required by multi-timeframe strategies
    closes_15m=None, highs_15m=None, lows_15m=None, opens_15m=None, timestamps_15m=None,
    closes_1h=None,
):
    """Pre-compute signal array for a strategy.

    Returns np.int8 array (same length as closes_5m) or None if no fast path.
    Multi-timeframe strategies (brahmaastra, ema5_mean_reversion,
    parent_child_momentum) require the extra *_15m / *_1h arrays to be passed.
    """
    if strategy_name == "smc_order_block":
        return precompute_smc_order_block(
            closes_5m, highs_5m, lows_5m,
            opens_5m if opens_5m is not None else closes_5m,
            params,
        )

    if strategy_name == "ema33_ob":
        return precompute_ema33_ob(
            closes_5m, highs_5m, lows_5m,
            opens_5m if opens_5m is not None else closes_5m,
            params,
        )

    if strategy_name == "brahmaastra":
        if closes_15m is None or timestamps_15m is None:
            return None
        sig_15m = precompute_brahmaastra(
            closes_15m, highs_15m, lows_15m, opens_15m if opens_15m is not None else closes_15m,
            timestamps_15m, params,
        )
        # Remap 15m signal array → 5m signal array.
        # A 15m signal at bar i maps to the LAST 5m bar of that 15m period
        # (index i*3 + 2 in the 5m array, clamped to n_5m-1).
        n_5m = len(closes_5m)
        sig_5m = np.zeros(n_5m, dtype=np.int8)
        for i15, sv in enumerate(sig_15m):
            if sv == 0:
                continue
            i5 = min(i15 * 3 + 2, n_5m - 1)
            if i5 >= 0:
                sig_5m[i5] = sv
        return sig_5m

    if strategy_name == "ema5_mean_reversion":
        return precompute_ema5_reversion(
            closes_5m, highs_5m, lows_5m,
            closes_15m, highs_15m, lows_15m,
            params,
        )

    if strategy_name == "parent_child_momentum":
        return precompute_parent_child(
            closes_5m, highs_5m, lows_5m,
            closes_1h,
            params,
        )

    fn = FAST_STRATEGY_MAP.get(strategy_name)
    if fn is None:
        return None
    return fn(closes_5m, highs_5m, lows_5m, params)
