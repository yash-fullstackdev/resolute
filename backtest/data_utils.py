"""Numpy-based data utilities used by the Python fallback engine."""

from __future__ import annotations
import numpy as np

IST_OFFSET = 330 * 60       # seconds
SESSION_START_MIN = 9 * 60 + 15  # 09:15 IST in minutes


def aggregate_numpy(candles: dict, tf_minutes: int) -> dict:
    """Aggregate 1m candle dict into N-minute candles using numpy."""
    ts = candles["timestamp"]
    n = len(ts)
    if n == 0:
        return {k: np.array([], dtype=np.float64) for k in candles}

    ist_ts = ts.astype(np.int64) + IST_OFFSET
    days = ist_ts // 86400
    min_of_day = (ist_ts % 86400) // 60
    session_min = min_of_day - SESSION_START_MIN
    groups = days * 10000 + session_min // tf_minutes

    out = {"open": [], "high": [], "low": [], "close": [], "volume": [], "timestamp": []}
    prev_group = None
    bar_o = bar_h = bar_l = bar_c = bar_v = bar_ts = 0.0

    for i in range(n):
        g = groups[i]
        if g != prev_group:
            if prev_group is not None:
                out["open"].append(bar_o)
                out["high"].append(bar_h)
                out["low"].append(bar_l)
                out["close"].append(bar_c)
                out["volume"].append(bar_v)
                out["timestamp"].append(bar_ts)
            prev_group = g
            bar_o = float(candles["open"][i])
            bar_h = float(candles["high"][i])
            bar_l = float(candles["low"][i])
            bar_c = float(candles["close"][i])
            bar_v = float(candles["volume"][i])
            bar_ts = float(candles["timestamp"][i])
        else:
            bar_h = max(bar_h, float(candles["high"][i]))
            bar_l = min(bar_l, float(candles["low"][i]))
            bar_c = float(candles["close"][i])
            bar_v += float(candles["volume"][i])

    if prev_group is not None:
        out["open"].append(bar_o); out["high"].append(bar_h); out["low"].append(bar_l)
        out["close"].append(bar_c); out["volume"].append(bar_v); out["timestamp"].append(bar_ts)

    return {k: np.array(v, dtype=np.float64) for k, v in out.items()}


def build_tf_close_map(candles_1m: dict, tf_minutes: int) -> list[bool]:
    """Returns list of bool: True at 1m bar index where TF bar closes."""
    ts = candles_1m["timestamp"]
    n = len(ts)
    if n == 0:
        return []

    ist_ts = ts.astype(np.int64) + IST_OFFSET
    days = ist_ts // 86400
    min_of_day = (ist_ts % 86400) // 60
    session_min = min_of_day - SESSION_START_MIN
    groups = days * 10000 + session_min // tf_minutes

    result = [False] * n
    for i in range(1, n):
        if groups[i] != groups[i - 1]:
            result[i - 1] = True
    if n > 0:
        result[n - 1] = True
    return result


def build_1m_to_tf_index(candles_1m: dict, tf_minutes: int) -> list[int]:
    """Returns list mapping each 1m bar to its corresponding TF bar index."""
    ts = candles_1m["timestamp"]
    n = len(ts)
    if n == 0:
        return []

    ist_ts = ts.astype(np.int64) + IST_OFFSET
    days = ist_ts // 86400
    min_of_day = (ist_ts % 86400) // 60
    session_min = min_of_day - SESSION_START_MIN
    groups = days * 10000 + session_min // tf_minutes

    result = [0] * n
    tf_idx = 0
    prev_g = groups[0]
    for i in range(n):
        if groups[i] != prev_g:
            tf_idx += 1
            prev_g = groups[i]
        result[i] = tf_idx
    return result
