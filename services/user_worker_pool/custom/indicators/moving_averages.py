"""
Moving-average family indicators — SMA, EMA, WMA, DEMA.

All functions accept a NumPy float64 array of close prices and return the
full computed series (same length, with NaN padding where insufficient data).
"""

from __future__ import annotations

import numpy as np
import structlog

log = structlog.get_logger(__name__)


# ---------------------------------------------------------------------------
# Simple Moving Average
# ---------------------------------------------------------------------------

def compute_sma(close: np.ndarray, period: int) -> np.ndarray:
    """Compute Simple Moving Average over *period* bars.

    Returns an array of the same length as *close*.  The first ``period - 1``
    values are ``NaN``.
    """
    if len(close) < period:
        return np.full_like(close, np.nan, dtype=np.float64)

    result = np.full(len(close), np.nan, dtype=np.float64)
    # Use cumulative-sum trick for O(n) computation
    cumsum = np.cumsum(close)
    cumsum = np.insert(cumsum, 0, 0.0)
    result[period - 1:] = (cumsum[period:] - cumsum[:-period]) / period
    return result


# ---------------------------------------------------------------------------
# Exponential Moving Average
# ---------------------------------------------------------------------------

def compute_ema(close: np.ndarray, period: int) -> np.ndarray:
    """Compute Exponential Moving Average using the standard multiplier
    ``2 / (period + 1)``.

    The seed value is the SMA of the first *period* values.
    Returns an array with NaN for the first ``period - 2`` entries.
    """
    n = len(close)
    if n < period:
        return np.full(n, np.nan, dtype=np.float64)

    result = np.full(n, np.nan, dtype=np.float64)
    multiplier = 2.0 / (period + 1)

    # Seed with SMA of first *period* values
    result[period - 1] = np.mean(close[:period])

    for i in range(period, n):
        result[i] = close[i] * multiplier + result[i - 1] * (1.0 - multiplier)

    return result


# ---------------------------------------------------------------------------
# Weighted Moving Average
# ---------------------------------------------------------------------------

def compute_wma(close: np.ndarray, period: int) -> np.ndarray:
    """Compute Weighted Moving Average (linearly-weighted).

    Weight of the most recent bar is *period*, second-most is *period - 1*,
    and so on.
    """
    n = len(close)
    if n < period:
        return np.full(n, np.nan, dtype=np.float64)

    weights = np.arange(1, period + 1, dtype=np.float64)
    denom = weights.sum()
    result = np.full(n, np.nan, dtype=np.float64)

    for i in range(period - 1, n):
        window = close[i - period + 1: i + 1]
        result[i] = np.dot(window, weights) / denom

    return result


# ---------------------------------------------------------------------------
# Double Exponential Moving Average
# ---------------------------------------------------------------------------

def compute_dema(close: np.ndarray, period: int) -> np.ndarray:
    """Compute Double Exponential Moving Average.

    ``DEMA = 2 * EMA(close, period) - EMA(EMA(close, period), period)``
    """
    ema1 = compute_ema(close, period)

    # Build a clean array for the second EMA (drop leading NaNs)
    valid_mask = ~np.isnan(ema1)
    if not np.any(valid_mask):
        return np.full_like(close, np.nan, dtype=np.float64)

    ema2 = compute_ema(ema1[valid_mask], period)

    # Re-align to original length
    result = np.full(len(close), np.nan, dtype=np.float64)
    start = int(np.argmax(valid_mask))

    # ema2 is shorter; valid values start at (period - 1) of the valid segment
    ema2_valid_mask = ~np.isnan(ema2)
    if not np.any(ema2_valid_mask):
        return result

    ema2_start = int(np.argmax(ema2_valid_mask))
    length = len(ema2) - ema2_start
    output_start = start + ema2_start

    if output_start + length > len(result):
        length = len(result) - output_start

    ema1_slice = ema1[output_start: output_start + length]
    ema2_slice = ema2[ema2_start: ema2_start + length]
    result[output_start: output_start + length] = 2.0 * ema1_slice - ema2_slice

    return result
