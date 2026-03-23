"""BiasEvaluator — real-time and batch bias filtering.

This is the SINGLE SOURCE OF TRUTH for bias computation.
Used by:
  - Live trading (user_worker_pool) — called on every chain update
  - Backtest (multi_runner) — called via precompute_bias_array() for batch mode

No mock objects. No synthetic data. Pure OHLCV indicator math.
"""

from __future__ import annotations

import math
from typing import Any

import numpy as np
from numpy.lib.stride_tricks import sliding_window_view


# ── Indicator functions ───────────────────────────────────────────────────────
# All operate on numpy arrays. All are deterministic and stateless.

def ema_full(closes: np.ndarray, period: int) -> np.ndarray:
    """EMA over full array. Returns array of length max(0, len(closes) - period + 1)."""
    n = len(closes)
    if n < period or period < 1:
        return np.array([], dtype=np.float64)
    k = 2.0 / (period + 1)
    result = np.empty(n - period + 1, dtype=np.float64)
    result[0] = np.mean(closes[:period])
    for i in range(1, len(result)):
        result[i] = float(closes[period + i - 1]) * k + result[i - 1] * (1.0 - k)
    return result


def atr_full(highs: np.ndarray, lows: np.ndarray, closes: np.ndarray, period: int) -> np.ndarray:
    """Wilder ATR over full array. Returns array starting at bar index `period`."""
    n = len(closes)
    if n < period + 1:
        return np.array([], dtype=np.float64)
    tr = np.empty(n - 1, dtype=np.float64)
    for i in range(1, n):
        hl = float(highs[i]) - float(lows[i])
        hc = abs(float(highs[i]) - float(closes[i - 1]))
        lc = abs(float(lows[i]) - float(closes[i - 1]))
        tr[i - 1] = max(hl, hc, lc)
    if len(tr) < period:
        return np.array([], dtype=np.float64)
    result = np.empty(len(tr) - period + 1, dtype=np.float64)
    result[0] = np.mean(tr[:period])
    alpha = 1.0 / period
    for i in range(1, len(result)):
        result[i] = result[i - 1] * (1 - alpha) + tr[period + i - 1] * alpha
    return result


def rsi_full(closes: np.ndarray, period: int = 14) -> np.ndarray:
    """Wilder RSI over full array. Returns array starting at bar index `period`."""
    n = len(closes)
    if n < period + 1:
        return np.array([], dtype=np.float64)
    deltas = np.diff(closes.astype(np.float64))
    gains = np.where(deltas > 0, deltas, 0.0)
    losses = np.where(deltas < 0, -deltas, 0.0)
    avg_gain = float(np.mean(gains[:period]))
    avg_loss = float(np.mean(losses[:period]))
    result = np.empty(len(deltas) - period + 1, dtype=np.float64)
    result[0] = 100.0 if avg_loss == 0 else 100.0 - 100.0 / (1.0 + avg_gain / avg_loss)
    for i in range(1, len(result)):
        avg_gain = (avg_gain * (period - 1) + gains[period + i - 1]) / period
        avg_loss = (avg_loss * (period - 1) + losses[period + i - 1]) / period
        result[i] = 100.0 if avg_loss == 0 else 100.0 - 100.0 / (1.0 + avg_gain / avg_loss)
    return result


def supertrend_full(
    highs: np.ndarray, lows: np.ndarray, closes: np.ndarray,
    period: int = 10, multiplier: float = 3.0,
) -> np.ndarray | None:
    """Supertrend direction over full array. Returns int8 array (+1/-1) starting at bar `period`."""
    atr_arr = atr_full(highs, lows, closes, period)
    if len(atr_arr) < 3:
        return None
    offset = period
    m = len(atr_arr)
    upper = np.empty(m, dtype=np.float64)
    lower = np.empty(m, dtype=np.float64)
    st = np.empty(m, dtype=np.float64)
    direction = np.ones(m, dtype=np.int8)
    for i in range(m):
        ci = offset + i
        hl2 = (float(highs[ci]) + float(lows[ci])) / 2.0
        upper[i] = hl2 + multiplier * atr_arr[i]
        lower[i] = hl2 - multiplier * atr_arr[i]
        if i == 0:
            st[i] = upper[i]
            direction[i] = -1 if float(closes[ci]) < st[i] else 1
            continue
        if float(closes[ci - 1]) > lower[i - 1]:
            lower[i] = max(lower[i], lower[i - 1])
        if float(closes[ci - 1]) < upper[i - 1]:
            upper[i] = min(upper[i], upper[i - 1])
        prev = st[i - 1]
        if prev == upper[i - 1]:
            if float(closes[ci]) <= upper[i]:
                st[i] = upper[i]; direction[i] = -1
            else:
                st[i] = lower[i]; direction[i] = 1
        else:
            if float(closes[ci]) >= lower[i]:
                st[i] = lower[i]; direction[i] = 1
            else:
                st[i] = upper[i]; direction[i] = -1
    return direction


def macd_full(closes: np.ndarray, fast: int = 12, slow: int = 26, signal: int = 9):
    """MACD line and signal line. Returns (macd_line, signal_line) arrays."""
    ema_fast = ema_full(closes, fast)
    ema_slow = ema_full(closes, slow)
    if len(ema_fast) == 0 or len(ema_slow) == 0:
        return np.array([]), np.array([])
    trim = len(ema_fast) - len(ema_slow)
    macd_line = ema_fast[trim:] - ema_slow
    if len(macd_line) < signal:
        return macd_line, np.array([])
    k = 2.0 / (signal + 1)
    sig = np.empty(len(macd_line) - signal + 1, dtype=np.float64)
    sig[0] = float(np.mean(macd_line[:signal]))
    for i in range(1, len(sig)):
        sig[i] = float(macd_line[signal + i - 1]) * k + sig[i - 1] * (1.0 - k)
    return macd_line, sig


# ── Candle aggregation ────────────────────────────────────────────────────────

def aggregate_candles(data: dict, target_tf_secs: int) -> dict:
    """Aggregate candle dict to higher timeframe. Returns dict of numpy arrays."""
    timestamps = data.get("timestamp")
    if timestamps is None or len(timestamps) == 0:
        empty = np.array([], dtype=np.float64)
        return {"open": empty, "high": empty, "low": empty, "close": empty, "timestamp": empty}

    if not isinstance(timestamps, np.ndarray):
        timestamps = np.array(timestamps, dtype=np.float64)

    periods = (timestamps // target_tf_secs * target_tf_secs).astype(np.float64)
    breaks = np.where(np.diff(periods) != 0)[0] + 1
    starts = np.concatenate([[0], breaks])
    ends = np.concatenate([breaks, [len(timestamps)]])
    n_bars = len(starts) - 1  # drop last partial bar
    if n_bars < 1:
        empty = np.array([], dtype=np.float64)
        return {"open": empty, "high": empty, "low": empty, "close": empty, "timestamp": empty}

    opens = data["open"] if isinstance(data["open"], np.ndarray) else np.array(data["open"], dtype=np.float64)
    highs = data["high"] if isinstance(data["high"], np.ndarray) else np.array(data["high"], dtype=np.float64)
    lows = data["low"] if isinstance(data["low"], np.ndarray) else np.array(data["low"], dtype=np.float64)
    closes = data["close"] if isinstance(data["close"], np.ndarray) else np.array(data["close"], dtype=np.float64)

    r_ts = np.empty(n_bars, dtype=np.float64)
    r_o = np.empty(n_bars, dtype=np.float64)
    r_h = np.empty(n_bars, dtype=np.float64)
    r_l = np.empty(n_bars, dtype=np.float64)
    r_c = np.empty(n_bars, dtype=np.float64)

    for k in range(n_bars):
        s, e = starts[k], ends[k]
        r_ts[k] = periods[s]
        r_o[k] = opens[s]
        r_h[k] = highs[s:e].max()
        r_l[k] = lows[s:e].min()
        r_c[k] = closes[e - 1]

    return {"open": r_o, "high": r_h, "low": r_l, "close": r_c, "timestamp": r_ts}


# ── Filter evaluators ─────────────────────────────────────────────────────────
# Each returns a direction array (int8): +1=BUY, -1=SELL, 0=NEUTRAL.
# Length matches input closes. 0 for bars with insufficient data.

def _eval_ema_crossover(data: dict, params: dict) -> np.ndarray:
    closes = data["close"]
    n = len(closes)
    sp = int(params.get("short", 2))
    lp = int(params.get("long", 11))
    dirs = np.zeros(n, dtype=np.int8)
    ema_s = ema_full(closes, sp)
    ema_l = ema_full(closes, lp)
    if len(ema_s) == 0 or len(ema_l) == 0:
        return dirs
    trim = len(ema_s) - len(ema_l)
    s_aligned = ema_s[trim:]
    start = lp - 1
    length = min(len(s_aligned), len(ema_l), n - start)
    dirs[start:start + length] = np.where(
        s_aligned[:length] > ema_l[:length], 1,
        np.where(s_aligned[:length] < ema_l[:length], -1, 0)
    ).astype(np.int8)
    return dirs


def _eval_supertrend(data: dict, params: dict) -> np.ndarray:
    closes = data["close"]
    n = len(closes)
    dirs = np.zeros(n, dtype=np.int8)
    st = supertrend_full(data["high"], data["low"], closes,
                         int(params.get("period", 10)), float(params.get("multiplier", 3.0)))
    if st is None:
        return dirs
    offset = int(params.get("period", 10))
    length = min(len(st), n - offset)
    dirs[offset:offset + length] = st[:length]
    return dirs


def _eval_rsi_zone(data: dict, params: dict) -> np.ndarray:
    closes = data["close"]
    n = len(closes)
    period = int(params.get("period", 14))
    ob = float(params.get("overbought", 70))
    os_ = float(params.get("oversold", 30))
    dirs = np.zeros(n, dtype=np.int8)
    rsi = rsi_full(closes, period)
    if len(rsi) == 0:
        return dirs
    offset = period
    length = min(len(rsi), n - offset)
    dirs[offset:offset + length] = np.where(
        rsi[:length] > ob, 1, np.where(rsi[:length] < os_, -1, 0)
    ).astype(np.int8)
    return dirs


def _eval_ttm_momentum(data: dict, params: dict) -> np.ndarray:
    closes = data["close"]
    n = len(closes)
    period = int(params.get("period", 20))
    dirs = np.zeros(n, dtype=np.int8)
    if n < period:
        return dirs
    windows = sliding_window_view(closes.astype(np.float64), period)
    roll_max = windows.max(axis=1)
    roll_min = windows.min(axis=1)
    midline = (roll_max + roll_min) / 2
    mom = closes[period - 1:].astype(np.float64) - midline
    offset = period - 1
    length = min(len(mom), n - offset)
    dirs[offset:offset + length] = np.where(
        mom[:length] > 0, 1, np.where(mom[:length] < 0, -1, 0)
    ).astype(np.int8)
    return dirs


def _eval_macd_signal(data: dict, params: dict) -> np.ndarray:
    closes = data["close"]
    n = len(closes)
    fast = int(params.get("fast", 12))
    slow = int(params.get("slow", 26))
    signal = int(params.get("signal", 9))
    dirs = np.zeros(n, dtype=np.int8)
    macd_line, sig_line = macd_full(closes, fast, slow, signal)
    if len(sig_line) == 0:
        return dirs
    offset = (slow - 1) + (signal - 1)
    trim = len(macd_line) - len(sig_line)
    length = min(len(sig_line), n - offset)
    m = macd_line[trim:trim + length]
    s = sig_line[:length]
    dirs[offset:offset + length] = np.where(m > s, 1, np.where(m < s, -1, 0)).astype(np.int8)
    return dirs


def _eval_ema_zone(data: dict, params: dict) -> np.ndarray:
    closes = data["close"]
    n = len(closes)
    ema_period = int(params.get("ema_period", 33))
    rsi_period = int(params.get("rsi_period", 14))
    rsi_bull = float(params.get("rsi_bull", 60))
    rsi_bear = float(params.get("rsi_bear", 40))
    dirs = np.zeros(n, dtype=np.int8)
    ema_arr = ema_full(closes, ema_period)
    rsi_arr = rsi_full(closes, rsi_period)
    if len(ema_arr) == 0 or len(rsi_arr) == 0:
        return dirs
    start = max(ema_period - 1, rsi_period)
    for b in range(start, n):
        ei = b - (ema_period - 1)
        ri = b - rsi_period
        if 0 <= ei < len(ema_arr) and 0 <= ri < len(rsi_arr):
            if closes[b] > ema_arr[ei] and rsi_arr[ri] > rsi_bull:
                dirs[b] = 1
            elif closes[b] < ema_arr[ei] and rsi_arr[ri] < rsi_bear:
                dirs[b] = -1
    return dirs


def _eval_price_vs_ema(data: dict, params: dict) -> np.ndarray:
    closes = data["close"]
    n = len(closes)
    period = int(params.get("period", 20))
    dirs = np.zeros(n, dtype=np.int8)
    ema_arr = ema_full(closes, period)
    if len(ema_arr) == 0:
        return dirs
    offset = period - 1
    length = min(len(ema_arr), n - offset)
    c_slice = closes[offset:offset + length].astype(np.float64)
    dirs[offset:offset + length] = np.where(
        c_slice > ema_arr[:length], 1, np.where(c_slice < ema_arr[:length], -1, 0)
    ).astype(np.int8)
    return dirs


def _eval_bollinger_squeeze(data: dict, params: dict) -> np.ndarray:
    closes = data["close"]
    n = len(closes)
    period = int(params.get("period", 20))
    std_mult = float(params.get("std_mult", 2.0))
    dirs = np.zeros(n, dtype=np.int8)
    if n < period + 1:
        return dirs
    c = closes.astype(np.float64)
    cs = np.cumsum(c)
    cs2 = np.cumsum(c ** 2)
    cs_p = np.concatenate([[0.0], cs])
    cs2_p = np.concatenate([[0.0], cs2])
    idx = np.arange(period - 1, n)
    roll_sum = cs_p[idx + 1] - cs_p[idx + 1 - period]
    roll_sum2 = cs2_p[idx + 1] - cs2_p[idx + 1 - period]
    roll_mean = roll_sum / period
    roll_var = np.maximum(roll_sum2 / period - roll_mean ** 2, 0.0)
    roll_std = np.sqrt(roll_var)
    bw = np.where(roll_mean > 0, 2 * std_mult * roll_std / roll_mean, 0.0)
    expanding = np.zeros(len(bw), dtype=bool)
    expanding[1:] = bw[1:] > bw[:-1]
    c_slice = c[period - 1:]
    buy = expanding & (c_slice > roll_mean)
    sell = expanding & (c_slice < roll_mean)
    result = np.zeros(len(bw), dtype=np.int8)
    result[buy] = 1
    result[sell] = -1
    dirs[period - 1:] = result
    return dirs


# ── Filter registry ──────────────────────────────────────────────────────────

FILTER_EVALUATORS: dict[str, Any] = {
    "ema_crossover": _eval_ema_crossover,
    "supertrend": _eval_supertrend,
    "rsi_zone": _eval_rsi_zone,
    "ttm_momentum": _eval_ttm_momentum,
    "macd_signal": _eval_macd_signal,
    "ema_zone": _eval_ema_zone,
    "price_vs_ema": _eval_price_vs_ema,
    "bollinger_squeeze": _eval_bollinger_squeeze,
}

VALID_FILTER_TYPES = frozenset(FILTER_EVALUATORS.keys())
# Strategy-instance bias is handled separately (not in FILTER_EVALUATORS)
STRATEGY_INSTANCE_TYPE = "strategy_instance"


# ── BiasEvaluator ─────────────────────────────────────────────────────────────

class BiasEvaluator:
    """Evaluates bias for a single strategy.

    Used identically in:
      - Live trading: evaluator.get_current_bias(candle_data)
      - Backtest: evaluator.precompute_bias_array(candle_data)

    Config format:
      {
        "bias_filters": [
          {"type": "ema_crossover", "timeframe": 5, "params": {"short": 2, "long": 11}},
          {"type": "strategy_instance", "timeframe": 5, "params": {"strategy_name": "ema_breakdown"}},
        ],
        "min_agreement": 2,
        "mode": "bias_filtered"  # or "independent"
      }
    """

    def __init__(self, bias_config: dict):
        raw_filters: list[dict] = bias_config.get("bias_filters", [])
        self.min_agreement: int = int(bias_config.get("min_agreement", 2))
        self.mode: str = bias_config.get("mode", "independent")

        # Accept both indicator types and strategy_instance type
        self.filters: list[dict] = [
            f for f in raw_filters
            if f.get("type") in VALID_FILTER_TYPES or f.get("type") == STRATEGY_INSTANCE_TYPE
        ]

    @property
    def is_active(self) -> bool:
        """True if this evaluator actually filters signals."""
        return self.mode == "bias_filtered" and len(self.filters) > 0

    def get_current_bias(
        self,
        candle_data_5m: dict,
        candle_data_1m: dict | None = None,
        live_strategy_signals: dict[str, int] | None = None,
    ) -> str | None:
        """Evaluate bias at the CURRENT (last) bar.

        Args:
            candle_data_5m: dict with numpy arrays (close, high, low, timestamp)
            candle_data_1m: optional 1m candles for sub-5m timeframe filters
            live_strategy_signals: optional dict mapping instance_name → int (+1/-1/0)
                                   used when filter type is "strategy_instance"

        Returns: "BUY", "SELL", or None (no clear bias)
        """
        if not self.is_active:
            return None

        if not candle_data_5m or "close" not in candle_data_5m:
            return None

        closes = candle_data_5m["close"]
        if not isinstance(closes, np.ndarray):
            closes = np.array(closes, dtype=np.float64)
        if len(closes) < 15:
            return None

        votes_buy = 0
        votes_sell = 0

        for filt in self.filters:
            ftype = filt.get("type", "")
            params = filt.get("params", {})

            # ── strategy_instance bias (uses live signal from another instance) ──
            if ftype == STRATEGY_INSTANCE_TYPE:
                if live_strategy_signals:
                    # Prefer instance_name (live path); fall back to strategy_name (backtest compat)
                    ref_name = str(params.get("instance_name") or params.get("strategy_name", ""))
                    sig = int(live_strategy_signals.get(ref_name, 0))
                    if sig == 1:
                        votes_buy += 1
                    elif sig == -1:
                        votes_sell += 1
                continue

            tf = int(filt.get("timeframe", 5))
            evaluator_fn = FILTER_EVALUATORS.get(ftype)
            if evaluator_fn is None:
                continue

            # Get candle data for this filter's timeframe
            if tf == 5:
                data = candle_data_5m
            elif tf == 1 and candle_data_1m is not None:
                data = candle_data_1m
            elif tf > 5:
                # Aggregate 5m → higher TF
                data = aggregate_candles(candle_data_5m, tf * 60)
            elif tf < 5 and candle_data_1m is not None:
                # Aggregate 1m → target TF
                data = aggregate_candles(candle_data_1m, tf * 60)
            else:
                data = candle_data_5m  # fallback

            # Ensure numpy arrays
            for k in ("close", "high", "low"):
                if k in data and not isinstance(data[k], np.ndarray):
                    data = {**data, k: np.array(data[k], dtype=np.float64)}

            if len(data.get("close", [])) < 10:
                continue

            # Evaluate and get the LAST bar's direction
            dirs = evaluator_fn(data, params)
            if len(dirs) > 0:
                last_dir = int(dirs[-1])
                if last_dir == 1:
                    votes_buy += 1
                elif last_dir == -1:
                    votes_sell += 1

        if votes_buy >= self.min_agreement:
            return "BUY"
        if votes_sell >= self.min_agreement:
            return "SELL"
        return None

    def precompute_bias_array(
        self,
        data_1m: dict,
        precomputed_signals: dict[str, Any] | None = None,
    ) -> tuple[list, list]:
        """Batch-compute bias for ALL 5m bars at once (backtest mode).

        Args:
            data_1m: raw 1m candle data
            precomputed_signals: optional dict mapping strategy_name → np.ndarray (int8)
                                 used when filter type is "strategy_instance"

        Returns: (bias_cache, atr_cache) where each is a list of length n_5m_bars.
        """
        timestamps = data_1m.get("timestamp")
        if timestamps is None or len(timestamps) == 0:
            return [], []

        if not isinstance(timestamps, np.ndarray):
            timestamps = np.array(timestamps, dtype=np.float64)

        # Build 5m candles
        data_5m = aggregate_candles(data_1m, 300)
        n_primary = len(data_5m["close"])
        if n_primary == 0:
            return [], []

        primary_ts = data_5m["timestamp"]

        # Collect unique timeframes needed
        tf_set = {5}
        for f in self.filters:
            tf_set.add(int(f.get("timeframe", 5)))

        # Build candles for each timeframe
        tf_data = {5: data_5m}
        for tf in tf_set:
            if tf != 5:
                tf_data[tf] = aggregate_candles(data_1m, tf * 60)

        # Evaluate each filter
        all_dirs = []
        for filt in self.filters:
            ftype = filt.get("type", "")
            params = filt.get("params", {})

            # ── strategy_instance bias (uses precomputed signal array) ──────────
            if ftype == STRATEGY_INSTANCE_TYPE:
                if precomputed_signals:
                    ref_name = str(params.get("strategy_name", ""))
                    sig_arr = precomputed_signals.get(ref_name)
                    if sig_arr is not None and len(sig_arr) > 0:
                        # Align to n_primary bars
                        arr = np.asarray(sig_arr, dtype=np.int8)
                        if len(arr) >= n_primary:
                            all_dirs.append(arr[:n_primary])
                        else:
                            padded = np.zeros(n_primary, dtype=np.int8)
                            padded[:len(arr)] = arr
                            all_dirs.append(padded)
                continue

            tf = int(filt.get("timeframe", 5))
            evaluator_fn = FILTER_EVALUATORS.get(ftype)
            if evaluator_fn is None:
                continue

            data = tf_data.get(tf, data_5m)
            if len(data.get("close", [])) == 0:
                continue

            # Ensure numpy
            for k in ("close", "high", "low"):
                if k in data and not isinstance(data[k], np.ndarray):
                    data = {**data, k: np.array(data[k], dtype=np.float64)}

            dirs_on_tf = evaluator_fn(data, params)

            if tf == 5:
                mapped = dirs_on_tf[:n_primary]
                if len(mapped) < n_primary:
                    padded = np.zeros(n_primary, dtype=np.int8)
                    padded[:len(mapped)] = mapped
                    mapped = padded
            else:
                # Map to 5m grid using vectorized searchsorted
                tf_timestamps = data["timestamp"]
                tf_bar_duration = tf * 60
                deadlines = primary_ts - tf_bar_duration
                indices = np.searchsorted(tf_timestamps, deadlines, side="right")
                mapped = np.zeros(n_primary, dtype=np.int8)
                valid = indices > 0
                safe_idx = np.clip(indices - 1, 0, max(len(dirs_on_tf) - 1, 0))
                mapped[valid] = dirs_on_tf[safe_idx[valid]]

            all_dirs.append(mapped)

        # Consensus voting — vectorized
        bias_cache: list[str | None] = [None] * n_primary
        if all_dirs:
            stacked = np.array(all_dirs, dtype=np.int8)
            votes_buy = np.sum(stacked == 1, axis=0)
            votes_sell = np.sum(stacked == -1, axis=0)
            for b in range(n_primary):
                if votes_buy[b] >= self.min_agreement:
                    bias_cache[b] = "BUY"
                elif votes_sell[b] >= self.min_agreement:
                    bias_cache[b] = "SELL"

        # ATR(14) on 5m
        atr_arr = atr_full(data_5m["high"], data_5m["low"], data_5m["close"], 14)
        atr_cache: list[float | None] = [None] * n_primary
        offset = 14
        for i in range(len(atr_arr)):
            if offset + i < n_primary:
                atr_cache[offset + i] = float(atr_arr[i])

        return bias_cache, atr_cache
