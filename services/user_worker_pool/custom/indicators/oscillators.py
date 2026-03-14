"""
Oscillator indicators — RSI, Stochastic, Stochastic RSI, CCI, Williams %R,
MFI, ROC, Momentum.

All functions accept NumPy arrays and return full-length arrays (NaN-padded
where insufficient data exists).
"""

from __future__ import annotations

import numpy as np
import structlog

log = structlog.get_logger(__name__)


# ---------------------------------------------------------------------------
# RSI — Wilder's smoothing
# ---------------------------------------------------------------------------

def compute_rsi(close: np.ndarray, period: int = 14) -> np.ndarray:
    """Relative Strength Index using Wilder's smoothing method.

    Wilder's smoothing is an EMA with ``alpha = 1 / period`` (as opposed to
    the standard ``2 / (period + 1)``).
    """
    n = len(close)
    if n < period + 1:
        return np.full(n, np.nan, dtype=np.float64)

    deltas = np.diff(close)
    gains = np.where(deltas > 0, deltas, 0.0)
    losses = np.where(deltas < 0, -deltas, 0.0)

    result = np.full(n, np.nan, dtype=np.float64)

    # Seed: simple average of first *period* gains/losses
    avg_gain = np.mean(gains[:period])
    avg_loss = np.mean(losses[:period])

    if avg_loss == 0:
        result[period] = 100.0
    else:
        rs = avg_gain / avg_loss
        result[period] = 100.0 - 100.0 / (1.0 + rs)

    # Wilder's smoothing: avg = (prev_avg * (period - 1) + current) / period
    for i in range(period, len(deltas)):
        avg_gain = (avg_gain * (period - 1) + gains[i]) / period
        avg_loss = (avg_loss * (period - 1) + losses[i]) / period

        if avg_loss == 0:
            result[i + 1] = 100.0
        else:
            rs = avg_gain / avg_loss
            result[i + 1] = 100.0 - 100.0 / (1.0 + rs)

    return result


# ---------------------------------------------------------------------------
# Stochastic Oscillator (%K and %D)
# ---------------------------------------------------------------------------

def compute_stochastic(
    high: np.ndarray,
    low: np.ndarray,
    close: np.ndarray,
    k_period: int = 14,
    d_period: int = 3,
) -> tuple[np.ndarray, np.ndarray]:
    """Stochastic Oscillator.

    Returns ``(%K, %D)`` where ``%D`` is the SMA of ``%K`` over *d_period*.
    """
    n = len(close)
    pct_k = np.full(n, np.nan, dtype=np.float64)

    for i in range(k_period - 1, n):
        highest_high = np.max(high[i - k_period + 1: i + 1])
        lowest_low = np.min(low[i - k_period + 1: i + 1])
        denom = highest_high - lowest_low
        if denom == 0:
            pct_k[i] = 50.0
        else:
            pct_k[i] = 100.0 * (close[i] - lowest_low) / denom

    # %D is SMA of %K
    pct_d = np.full(n, np.nan, dtype=np.float64)
    valid = ~np.isnan(pct_k)
    valid_idx = np.where(valid)[0]
    if len(valid_idx) >= d_period:
        for i in range(d_period - 1, len(valid_idx)):
            idx = valid_idx[i]
            pct_d[idx] = np.mean(pct_k[valid_idx[i - d_period + 1]: valid_idx[i] + 1])

    return pct_k, pct_d


# ---------------------------------------------------------------------------
# Stochastic RSI
# ---------------------------------------------------------------------------

def compute_stochastic_rsi(
    close: np.ndarray,
    rsi_period: int = 14,
    stoch_period: int = 14,
    k_smooth: int = 3,
    d_smooth: int = 3,
) -> tuple[np.ndarray, np.ndarray]:
    """Stochastic RSI — applies Stochastic formula to RSI values.

    Returns ``(%K, %D)`` of the stochastic applied to RSI.
    """
    rsi = compute_rsi(close, rsi_period)
    n = len(rsi)
    stoch_rsi_k = np.full(n, np.nan, dtype=np.float64)

    for i in range(n):
        start = max(0, i - stoch_period + 1)
        window = rsi[start: i + 1]
        valid = window[~np.isnan(window)]
        if len(valid) < stoch_period:
            continue
        rsi_min = np.min(valid)
        rsi_max = np.max(valid)
        denom = rsi_max - rsi_min
        if denom == 0:
            stoch_rsi_k[i] = 50.0
        else:
            stoch_rsi_k[i] = 100.0 * (rsi[i] - rsi_min) / denom

    # Smooth %K
    smoothed_k = _sma_on_valid(stoch_rsi_k, k_smooth)
    # %D is SMA of smoothed %K
    pct_d = _sma_on_valid(smoothed_k, d_smooth)

    return smoothed_k, pct_d


def _sma_on_valid(arr: np.ndarray, period: int) -> np.ndarray:
    """SMA computed only over non-NaN values in-place."""
    n = len(arr)
    result = np.full(n, np.nan, dtype=np.float64)
    buf: list[float] = []
    for i in range(n):
        if np.isnan(arr[i]):
            continue
        buf.append(arr[i])
        if len(buf) >= period:
            result[i] = np.mean(buf[-period:])
    return result


# ---------------------------------------------------------------------------
# CCI — Commodity Channel Index
# ---------------------------------------------------------------------------

def compute_cci(
    high: np.ndarray,
    low: np.ndarray,
    close: np.ndarray,
    period: int = 20,
) -> np.ndarray:
    """Commodity Channel Index.

    ``CCI = (TP - SMA(TP, period)) / (0.015 * MeanDeviation)``
    where ``TP = (high + low + close) / 3``.
    """
    tp = (high + low + close) / 3.0
    n = len(tp)
    result = np.full(n, np.nan, dtype=np.float64)

    for i in range(period - 1, n):
        window = tp[i - period + 1: i + 1]
        sma_val = np.mean(window)
        mean_dev = np.mean(np.abs(window - sma_val))
        if mean_dev == 0:
            result[i] = 0.0
        else:
            result[i] = (tp[i] - sma_val) / (0.015 * mean_dev)

    return result


# ---------------------------------------------------------------------------
# Williams %R
# ---------------------------------------------------------------------------

def compute_williams_r(
    high: np.ndarray,
    low: np.ndarray,
    close: np.ndarray,
    period: int = 14,
) -> np.ndarray:
    """Williams %R — oscillator from 0 to -100.

    ``%R = (highest_high - close) / (highest_high - lowest_low) * -100``
    """
    n = len(close)
    result = np.full(n, np.nan, dtype=np.float64)

    for i in range(period - 1, n):
        hh = np.max(high[i - period + 1: i + 1])
        ll = np.min(low[i - period + 1: i + 1])
        denom = hh - ll
        if denom == 0:
            result[i] = -50.0
        else:
            result[i] = -100.0 * (hh - close[i]) / denom

    return result


# ---------------------------------------------------------------------------
# MFI — Money Flow Index (volume-weighted RSI)
# ---------------------------------------------------------------------------

def compute_mfi(
    high: np.ndarray,
    low: np.ndarray,
    close: np.ndarray,
    volume: np.ndarray,
    period: int = 14,
) -> np.ndarray:
    """Money Flow Index.

    Volume-weighted RSI using Typical Price * Volume as raw money flow.
    """
    tp = (high + low + close) / 3.0
    raw_mf = tp * volume
    n = len(close)

    if n < period + 1:
        return np.full(n, np.nan, dtype=np.float64)

    result = np.full(n, np.nan, dtype=np.float64)
    tp_diff = np.diff(tp)

    for i in range(period, n):
        pos_flow = 0.0
        neg_flow = 0.0
        for j in range(i - period + 1, i + 1):
            if j == 0:
                continue
            if tp[j] > tp[j - 1]:
                pos_flow += raw_mf[j]
            elif tp[j] < tp[j - 1]:
                neg_flow += raw_mf[j]

        if neg_flow == 0:
            result[i] = 100.0
        else:
            mf_ratio = pos_flow / neg_flow
            result[i] = 100.0 - 100.0 / (1.0 + mf_ratio)

    return result


# ---------------------------------------------------------------------------
# ROC — Rate of Change
# ---------------------------------------------------------------------------

def compute_roc(close: np.ndarray, period: int = 12) -> np.ndarray:
    """Rate of Change (percentage).

    ``ROC = ((close - close[n-period]) / close[n-period]) * 100``
    """
    n = len(close)
    result = np.full(n, np.nan, dtype=np.float64)

    for i in range(period, n):
        if close[i - period] == 0:
            result[i] = 0.0
        else:
            result[i] = ((close[i] - close[i - period]) / close[i - period]) * 100.0

    return result


# ---------------------------------------------------------------------------
# Momentum
# ---------------------------------------------------------------------------

def compute_momentum(close: np.ndarray, period: int = 10) -> np.ndarray:
    """Price Momentum (absolute difference).

    ``Momentum = close - close[n-period]``
    """
    n = len(close)
    result = np.full(n, np.nan, dtype=np.float64)

    for i in range(period, n):
        result[i] = close[i] - close[i - period]

    return result
