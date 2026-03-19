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


# ── SMC Order Block (simplified — BOS + momentum) ────────────────────────────

def precompute_smc_order_block(closes, highs, lows, params=None):
    """Simplified SMC: structure break detection + momentum."""
    p = params or {}
    ob_length = int(p.get("ob_length", 6))

    n = len(closes)
    signals = np.zeros(n, dtype=np.int8)

    atr = _atr_full(highs, lows, closes, 14)
    if len(atr) == 0:
        return signals

    # Detect swing highs/lows + BOS
    for i in range(ob_length * 2 + 2, n):
        ai = i - 14
        if ai < 0 or ai >= len(atr):
            continue
        atr_pct = atr[ai] / closes[i] if closes[i] > 0 else 0
        if atr_pct < 0.0004:
            continue

        # Simple BOS: current close breaks above recent swing high / below swing low
        recent_high = max(highs[i - ob_length:i])
        recent_low = min(lows[i - ob_length:i])
        prev_high = max(highs[i - ob_length * 2:i - ob_length])
        prev_low = min(lows[i - ob_length * 2:i - ob_length])

        if closes[i] > recent_high and closes[i] > prev_high:
            signals[i] = 1  # bullish BOS
        elif closes[i] < recent_low and closes[i] < prev_low:
            signals[i] = -1  # bearish BOS

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


# ── Registry ─────────────────────────────────────────────────────────────────

FAST_STRATEGY_MAP = {
    "ttm_squeeze": precompute_ttm_squeeze,
    "supertrend_strategy": precompute_supertrend_strategy,
    "ema_breakdown": precompute_ema_breakdown,
    "ema33_ob": precompute_ema33_ob,
    "smc_order_block": precompute_smc_order_block,
    "rsi_vwap_scalp": precompute_rsi_vwap_scalp,
    "vwap_supertrend": precompute_vwap_supertrend,
}


def precompute_strategy_signals(strategy_name, closes_5m, highs_5m, lows_5m, opens_5m=None, params=None):
    """Pre-compute signal array for a strategy. Returns np.int8 array or None if no fast path."""
    fn = FAST_STRATEGY_MAP.get(strategy_name)
    if fn is None:
        return None
    if strategy_name == "ema33_ob":
        return fn(closes_5m, highs_5m, lows_5m, opens_5m if opens_5m is not None else closes_5m, params)
    return fn(closes_5m, highs_5m, lows_5m, params)
