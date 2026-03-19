"""Dynamic bias engine — any number of indicators, any timeframe, configurable consensus.

Each bias filter is a dict:
  {
    "type": "ema_crossover",       # indicator type
    "timeframe": 5,                # candle timeframe in minutes
    "params": {"short": 2, "long": 11}  # indicator-specific params
  }

Supported types:
  - ema_crossover     : EMA short/long crossover
  - supertrend        : Supertrend direction
  - rsi_zone          : RSI overbought/oversold zone
  - ttm_momentum      : TTM Squeeze momentum sign
  - macd_signal       : MACD line vs signal line crossover
  - ema_zone          : Price vs EMA + RSI confirmation (old EMA33 logic)
  - price_vs_ema      : Simple price above/below EMA
  - bollinger_squeeze : Bollinger bandwidth squeeze detection
"""

from __future__ import annotations

import sys
from pathlib import Path

import numpy as np

for _root in [Path("/app"), Path(__file__).parent.parent]:
    if (_root / "services" / "user_worker_pool" / "strategies").exists():
        if str(_root) not in sys.path:
            sys.path.insert(0, str(_root))
        break


# ── Full-array indicator functions ────────────────────────────────────────────

def _ema_full(closes, period):
    n = len(closes)
    if n < period or period < 1:
        return np.array([])
    k = 2.0 / (period + 1)
    result = np.empty(n - period + 1)
    result[0] = np.mean(closes[:period])
    for i in range(1, len(result)):
        result[i] = closes[period + i - 1] * k + result[i - 1] * (1.0 - k)
    return result


def _atr_full(highs, lows, closes, period):
    n = len(closes)
    if n < period + 1:
        return np.array([])
    tr = np.empty(n - 1)
    for i in range(1, n):
        hl = highs[i] - lows[i]
        hc = abs(highs[i] - closes[i - 1])
        lc = abs(lows[i] - closes[i - 1])
        tr[i - 1] = max(hl, hc, lc)
    if len(tr) < period:
        return np.array([])
    result = np.empty(len(tr) - period + 1)
    result[0] = np.mean(tr[:period])
    alpha = 1.0 / period
    for i in range(1, len(result)):
        result[i] = result[i - 1] * (1 - alpha) + tr[period + i - 1] * alpha
    return result


def _rsi_full(closes, period=14):
    n = len(closes)
    if n < period + 1:
        return np.array([])
    deltas = np.diff(closes)
    gains = np.where(deltas > 0, deltas, 0.0)
    losses = np.where(deltas < 0, -deltas, 0.0)
    avg_gain = np.mean(gains[:period])
    avg_loss = np.mean(losses[:period])
    result = np.empty(len(deltas) - period + 1)
    if avg_loss == 0:
        result[0] = 100.0
    else:
        result[0] = 100.0 - 100.0 / (1.0 + avg_gain / avg_loss)
    for i in range(1, len(result)):
        avg_gain = (avg_gain * (period - 1) + gains[period + i - 1]) / period
        avg_loss = (avg_loss * (period - 1) + losses[period + i - 1]) / period
        if avg_loss == 0:
            result[i] = 100.0
        else:
            result[i] = 100.0 - 100.0 / (1.0 + avg_gain / avg_loss)
    return result


def _supertrend_full(highs, lows, closes, period=10, multiplier=3.0):
    atr_arr = _atr_full(highs, lows, closes, period)
    if len(atr_arr) < 3:
        return None
    offset = period
    n = len(atr_arr)
    upper = np.empty(n)
    lower = np.empty(n)
    st = np.empty(n)
    direction = np.ones(n, dtype=np.int8)
    for i in range(n):
        ci = offset + i
        hl2 = (highs[ci] + lows[ci]) / 2.0
        upper[i] = hl2 + multiplier * atr_arr[i]
        lower[i] = hl2 - multiplier * atr_arr[i]
        if i == 0:
            st[i] = upper[i]
            direction[i] = -1 if closes[ci] < st[i] else 1
            continue
        if closes[ci - 1] > lower[i - 1]:
            lower[i] = max(lower[i], lower[i - 1])
        if closes[ci - 1] < upper[i - 1]:
            upper[i] = min(upper[i], upper[i - 1])
        prev = st[i - 1]
        if prev == upper[i - 1]:
            if closes[ci] <= upper[i]:
                st[i] = upper[i]; direction[i] = -1
            else:
                st[i] = lower[i]; direction[i] = 1
        else:
            if closes[ci] >= lower[i]:
                st[i] = lower[i]; direction[i] = 1
            else:
                st[i] = upper[i]; direction[i] = -1
    return direction


def _ttm_momentum_full(closes, period=20):
    """TTM momentum — vectorized using stride tricks for rolling min/max."""
    n = len(closes)
    if n < period:
        return np.array([])
    # Use stride_tricks for rolling window view (O(1) memory, no copy)
    from numpy.lib.stride_tricks import sliding_window_view
    windows = sliding_window_view(closes, period)  # shape: (n-period+1, period)
    roll_max = windows.max(axis=1)
    roll_min = windows.min(axis=1)
    midline = (roll_max + roll_min) / 2
    return closes[period - 1:] - midline


def _macd_full(closes, fast=12, slow=26, signal=9):
    """MACD line and signal line. Returns (macd_line, signal_line) arrays."""
    ema_fast = _ema_full(closes, fast)
    ema_slow = _ema_full(closes, slow)
    if len(ema_fast) == 0 or len(ema_slow) == 0:
        return np.array([]), np.array([])
    # Align to slow EMA (shorter output)
    trim = len(ema_fast) - len(ema_slow)
    macd_line = ema_fast[trim:] - ema_slow
    if len(macd_line) < signal:
        return macd_line, np.array([])
    # Signal line is EMA of MACD
    k = 2.0 / (signal + 1)
    sig = np.empty(len(macd_line) - signal + 1)
    sig[0] = np.mean(macd_line[:signal])
    for i in range(1, len(sig)):
        sig[i] = macd_line[signal + i - 1] * k + sig[i - 1] * (1.0 - k)
    return macd_line, sig


# ── Aggregation ───────────────────────────────────────────────────────────────

def aggregate_np(timestamps, opens, highs, lows, closes, volumes, period_secs):
    """Aggregate 1m numpy arrays → higher TF. Returns dict of numpy arrays."""
    if len(timestamps) == 0:
        empty = np.array([], dtype=np.float64)
        return {"open": empty, "high": empty, "low": empty, "close": empty,
                "volume": empty, "timestamp": empty}

    periods = (timestamps // period_secs * period_secs).astype(np.float64)
    breaks = np.where(np.diff(periods) != 0)[0] + 1
    splits_start = np.concatenate([[0], breaks])
    splits_end = np.concatenate([breaks, [len(timestamps)]])
    n_bars = len(splits_start) - 1
    if n_bars < 1:
        empty = np.array([], dtype=np.float64)
        return {"open": empty, "high": empty, "low": empty, "close": empty,
                "volume": empty, "timestamp": empty}

    r_ts = np.empty(n_bars, dtype=np.float64)
    r_o = np.empty(n_bars, dtype=np.float64)
    r_h = np.empty(n_bars, dtype=np.float64)
    r_l = np.empty(n_bars, dtype=np.float64)
    r_c = np.empty(n_bars, dtype=np.float64)
    r_v = np.empty(n_bars, dtype=np.float64)

    for k in range(n_bars):
        s, e = splits_start[k], splits_end[k]
        r_ts[k] = periods[s]
        r_o[k] = opens[s]
        r_h[k] = highs[s:e].max()
        r_l[k] = lows[s:e].min()
        r_c[k] = closes[e - 1]
        r_v[k] = volumes[s:e].sum()

    return {"open": r_o, "high": r_h, "low": r_l, "close": r_c,
            "volume": r_v, "timestamp": r_ts}


# ── Filter evaluators (fully vectorized — no Python for-loops) ────────────────
# Each returns a direction array: +1=BUY, -1=SELL, 0=NEUTRAL
# Array is same length as input closes, with 0 for bars where insufficient data.

def _eval_ema_crossover(data, params):
    closes = data["close"]
    n = len(closes)
    sp = params.get("short", 2)
    lp = params.get("long", 11)
    dirs = np.zeros(n, dtype=np.int8)
    ema_s = _ema_full(closes, sp)
    ema_l = _ema_full(closes, lp)
    if len(ema_s) == 0 or len(ema_l) == 0:
        return dirs
    # Align both to the longer offset
    trim = len(ema_s) - len(ema_l)
    s_aligned = ema_s[trim:]
    start = lp - 1
    length = min(len(s_aligned), len(ema_l), n - start)
    dirs[start:start + length] = np.where(
        s_aligned[:length] > ema_l[:length], 1,
        np.where(s_aligned[:length] < ema_l[:length], -1, 0)
    ).astype(np.int8)
    return dirs


def _eval_supertrend(data, params):
    closes = data["close"]
    n = len(closes)
    period = params.get("period", 10)
    mult = params.get("multiplier", 3.0)
    dirs = np.zeros(n, dtype=np.int8)
    st = _supertrend_full(data["high"], data["low"], closes, period, mult)
    if st is None:
        return dirs
    offset = period
    length = min(len(st), n - offset)
    dirs[offset:offset + length] = st[:length]
    return dirs


def _eval_rsi_zone(data, params):
    closes = data["close"]
    n = len(closes)
    period = params.get("period", 14)
    ob = params.get("overbought", 70)
    os_ = params.get("oversold", 30)
    dirs = np.zeros(n, dtype=np.int8)
    rsi = _rsi_full(closes, period)
    if len(rsi) == 0:
        return dirs
    offset = period
    length = min(len(rsi), n - offset)
    dirs[offset:offset + length] = np.where(
        rsi[:length] > ob, 1,
        np.where(rsi[:length] < os_, -1, 0)
    ).astype(np.int8)
    return dirs


def _eval_ttm_momentum(data, params):
    closes = data["close"]
    n = len(closes)
    period = params.get("period", 20)
    dirs = np.zeros(n, dtype=np.int8)
    mom = _ttm_momentum_full(closes, period)
    if len(mom) == 0:
        return dirs
    offset = period - 1
    length = min(len(mom), n - offset)
    dirs[offset:offset + length] = np.where(
        mom[:length] > 0, 1, np.where(mom[:length] < 0, -1, 0)
    ).astype(np.int8)
    return dirs


def _eval_macd_signal(data, params):
    closes = data["close"]
    n = len(closes)
    fast = params.get("fast", 12)
    slow = params.get("slow", 26)
    signal = params.get("signal", 9)
    dirs = np.zeros(n, dtype=np.int8)
    macd_line, sig_line = _macd_full(closes, fast, slow, signal)
    if len(sig_line) == 0:
        return dirs
    offset = (slow - 1) + (signal - 1)
    trim = len(macd_line) - len(sig_line)
    length = min(len(sig_line), n - offset)
    m = macd_line[trim:trim + length]
    s = sig_line[:length]
    dirs[offset:offset + length] = np.where(
        m > s, 1, np.where(m < s, -1, 0)
    ).astype(np.int8)
    return dirs


def _eval_ema_zone(data, params):
    """Price vs EMA + RSI confirmation — vectorized."""
    closes = data["close"]
    n = len(closes)
    ema_period = params.get("ema_period", 33)
    rsi_period = params.get("rsi_period", 14)
    rsi_bull = params.get("rsi_bull", 60)
    rsi_bear = params.get("rsi_bear", 40)
    dirs = np.zeros(n, dtype=np.int8)
    ema_arr = _ema_full(closes, ema_period)
    rsi_arr = _rsi_full(closes, rsi_period)
    if len(ema_arr) == 0 or len(rsi_arr) == 0:
        return dirs
    # Align to the later start
    start = max(ema_period - 1, rsi_period)
    end = n
    for b in range(start, end):
        ei = b - (ema_period - 1)
        ri = b - rsi_period
        if 0 <= ei < len(ema_arr) and 0 <= ri < len(rsi_arr):
            if closes[b] > ema_arr[ei] and rsi_arr[ri] > rsi_bull:
                dirs[b] = 1
            elif closes[b] < ema_arr[ei] and rsi_arr[ri] < rsi_bear:
                dirs[b] = -1
    return dirs


def _eval_price_vs_ema(data, params):
    closes = data["close"]
    n = len(closes)
    period = params.get("period", 20)
    dirs = np.zeros(n, dtype=np.int8)
    ema_arr = _ema_full(closes, period)
    if len(ema_arr) == 0:
        return dirs
    offset = period - 1
    length = min(len(ema_arr), n - offset)
    c_slice = closes[offset:offset + length]
    dirs[offset:offset + length] = np.where(
        c_slice > ema_arr[:length], 1,
        np.where(c_slice < ema_arr[:length], -1, 0)
    ).astype(np.int8)
    return dirs


def _eval_bollinger_squeeze(data, params):
    """Bollinger squeeze — O(n) using cumulative sums, fully vectorized."""
    closes = data["close"]
    n = len(closes)
    period = params.get("period", 20)
    std_mult = params.get("std_mult", 2.0)
    dirs = np.zeros(n, dtype=np.int8)
    if n < period + 1:
        return dirs

    # Rolling mean and std via cumulative sums — O(n) total
    cs = np.cumsum(closes)
    cs2 = np.cumsum(closes ** 2)

    # Prepend 0 for easy windowed subtraction
    cs_p = np.concatenate([[0.0], cs])
    cs2_p = np.concatenate([[0.0], cs2])

    # Rolling stats for indices [period-1, n-1]
    # mean[i] = (cs_p[i+1] - cs_p[i+1-period]) / period
    idx = np.arange(period - 1, n)
    roll_sum = cs_p[idx + 1] - cs_p[idx + 1 - period]
    roll_sum2 = cs2_p[idx + 1] - cs2_p[idx + 1 - period]
    roll_mean = roll_sum / period
    roll_var = np.maximum(roll_sum2 / period - roll_mean ** 2, 0.0)
    roll_std = np.sqrt(roll_var)

    # Bandwidth
    bw = np.where(roll_mean > 0, 2 * std_mult * roll_std / roll_mean, 0.0)

    # Expansion: bandwidth increasing vs previous bar
    expanding = np.zeros(len(bw), dtype=bool)
    expanding[1:] = bw[1:] > bw[:-1]

    # Direction: price vs rolling mean
    c_slice = closes[period - 1:]
    buy = expanding & (c_slice > roll_mean)
    sell = expanding & (c_slice < roll_mean)

    result = np.zeros(len(bw), dtype=np.int8)
    result[buy] = 1
    result[sell] = -1
    dirs[period - 1:] = result
    return dirs


FILTER_EVALUATORS = {
    "ema_crossover": _eval_ema_crossover,
    "supertrend": _eval_supertrend,
    "rsi_zone": _eval_rsi_zone,
    "ttm_momentum": _eval_ttm_momentum,
    "macd_signal": _eval_macd_signal,
    "ema_zone": _eval_ema_zone,
    "price_vs_ema": _eval_price_vs_ema,
    "bollinger_squeeze": _eval_bollinger_squeeze,
}


# ── Dynamic bias pre-computation ─────────────────────────────────────────────

def precompute_dynamic_bias(data_1m, bias_filters, min_agreement=2):
    """Pre-compute bias for all bars using dynamic filters on multiple timeframes.

    Args:
        data_1m: dict with numpy arrays (open, high, low, close, volume, timestamp)
        bias_filters: list of filter dicts: [{type, timeframe, params}, ...]
        min_agreement: minimum number of filters that must agree for a bias signal

    Returns:
        dict mapping timeframe_minutes → {
            "bias": list[str|None] of length n_bars_at_that_tf,
            "timestamps": numpy array of bar timestamps
        }
        The 5m bias is always computed (primary evaluation timeframe).
    """
    timestamps = data_1m["timestamp"]

    # Group filters by timeframe
    tf_filters: dict[int, list] = {}
    for f in bias_filters:
        tf = f.get("timeframe", 5)
        tf_filters.setdefault(tf, []).append(f)

    # Always need 5m for strategy evaluation
    if 5 not in tf_filters:
        tf_filters[5] = []

    # Build candles for each unique timeframe
    tf_data: dict[int, dict] = {}
    for tf_min in tf_filters:
        period_secs = tf_min * 60
        tf_data[tf_min] = aggregate_np(
            timestamps, data_1m["open"], data_1m["high"],
            data_1m["low"], data_1m["close"], data_1m["volume"],
            period_secs
        )

    # The primary (5m) timeframe determines the output length
    primary_data = tf_data[5]
    n_primary = len(primary_data["close"])
    primary_ts = primary_data["timestamp"]

    if n_primary == 0:
        return {}, tf_data

    # Evaluate each filter, producing a direction array on its own timeframe
    # Then map each filter's result back to the 5m bar grid
    all_filter_dirs = []  # list of arrays, each of length n_primary

    for filt in bias_filters:
        ftype = filt.get("type", "ema_crossover")
        tf = filt.get("timeframe", 5)
        params = filt.get("params", {})

        evaluator = FILTER_EVALUATORS.get(ftype)
        if evaluator is None:
            continue

        data = tf_data[tf]
        if len(data["close"]) == 0:
            continue

        # Compute direction array on this timeframe
        dirs_on_tf = evaluator(data, params)

        if tf == 5:
            # Same timeframe — use directly
            mapped = dirs_on_tf[:n_primary]
            # Pad if shorter
            if len(mapped) < n_primary:
                padded = np.zeros(n_primary, dtype=np.int8)
                padded[:len(mapped)] = mapped
                mapped = padded
        else:
            # Map higher/lower TF to 5m grid — vectorized with np.searchsorted
            tf_timestamps = data["timestamp"]
            tf_bar_duration = tf * 60
            # For each 5m bar, find latest closed TF bar
            deadlines = primary_ts - tf_bar_duration
            indices = np.searchsorted(tf_timestamps, deadlines, side="right")  # vectorized
            mapped = np.zeros(n_primary, dtype=np.int8)
            valid = indices > 0
            safe_idx = np.clip(indices - 1, 0, len(dirs_on_tf) - 1)
            mapped[valid] = dirs_on_tf[safe_idx[valid]]

        all_filter_dirs.append(mapped)

    # Consensus voting — fully vectorized
    bias_cache = [None] * n_primary
    if all_filter_dirs:
        # Stack all filter direction arrays: shape (n_filters, n_primary)
        stacked = np.array(all_filter_dirs, dtype=np.int8)
        votes_buy = np.sum(stacked == 1, axis=0)   # per-bar buy vote count
        votes_sell = np.sum(stacked == -1, axis=0)  # per-bar sell vote count

        # Convert to list[str|None] for the walk-forward loop
        for b in range(n_primary):
            if votes_buy[b] >= min_agreement:
                bias_cache[b] = "BUY"
            elif votes_sell[b] >= min_agreement:
                bias_cache[b] = "SELL"

    # ATR(14) on 5m for SL/TP
    atr_full = _atr_full(primary_data["high"], primary_data["low"], primary_data["close"], 14)
    atr_cache = [None] * n_primary
    atr_offset = 14
    for i in range(len(atr_full)):
        if atr_offset + i < n_primary:
            atr_cache[atr_offset + i] = float(atr_full[i])

    return {
        "bias": bias_cache,
        "atr": atr_cache,
        "primary_data": primary_data,
        "tf_data": tf_data,
    }


# ── Legacy compatibility ─────────────────────────────────────────────────────

def convert_legacy_bias_config(cfg: dict) -> list[dict]:
    """Convert old-style bias_config to list of bias_filters."""
    filters = []
    if cfg.get("use_ema_bias", True):
        filters.append({
            "type": "ema_crossover",
            "timeframe": 5,
            "params": {"short": cfg.get("ema_short", 2), "long": cfg.get("ema_long", 11)},
        })
    if cfg.get("use_supertrend", True):
        filters.append({
            "type": "supertrend",
            "timeframe": 5,
            "params": {"period": cfg.get("st_period", 10), "multiplier": cfg.get("st_multiplier", 3.0)},
        })
    if cfg.get("use_ttm_squeeze", True):
        filters.append({
            "type": "ttm_momentum",
            "timeframe": 5,
            "params": {"period": 20},
        })
    if cfg.get("use_ema33_zone", True):
        filters.append({
            "type": "ema_zone",
            "timeframe": 5,
            "params": {"ema_period": 33, "rsi_period": 14, "rsi_bull": 60, "rsi_bear": 40},
        })
    return filters
