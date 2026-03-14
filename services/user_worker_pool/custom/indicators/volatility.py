"""
Volatility indicators — Bollinger Bands, ATR, Keltner Channel, Donchian Channel.
"""

from __future__ import annotations

import numpy as np
import structlog

from .moving_averages import compute_sma, compute_ema

log = structlog.get_logger(__name__)


# ---------------------------------------------------------------------------
# Bollinger Bands
# ---------------------------------------------------------------------------

def compute_bollinger_bands(
    close: np.ndarray,
    period: int = 20,
    num_std: float = 2.0,
) -> tuple[np.ndarray, np.ndarray, np.ndarray, np.ndarray]:
    """Bollinger Bands.

    Returns ``(upper, middle, lower, width)``.
    ``middle = SMA(close, period)``
    ``upper = middle + num_std * stddev``
    ``lower = middle - num_std * stddev``
    ``width = (upper - lower) / middle``
    """
    n = len(close)
    middle = compute_sma(close, period)
    upper = np.full(n, np.nan, dtype=np.float64)
    lower = np.full(n, np.nan, dtype=np.float64)
    width = np.full(n, np.nan, dtype=np.float64)

    for i in range(period - 1, n):
        window = close[i - period + 1: i + 1]
        std = np.std(window, ddof=0)
        upper[i] = middle[i] + num_std * std
        lower[i] = middle[i] - num_std * std
        if middle[i] != 0:
            width[i] = (upper[i] - lower[i]) / middle[i]
        else:
            width[i] = 0.0

    return upper, middle, lower, width


# ---------------------------------------------------------------------------
# ATR — Average True Range (Wilder's smoothing)
# ---------------------------------------------------------------------------

def compute_atr(
    high: np.ndarray,
    low: np.ndarray,
    close: np.ndarray,
    period: int = 14,
) -> np.ndarray:
    """Average True Range using Wilder's smoothing."""
    n = len(close)
    tr = np.zeros(n, dtype=np.float64)
    tr[0] = high[0] - low[0]

    for i in range(1, n):
        tr[i] = max(
            high[i] - low[i],
            abs(high[i] - close[i - 1]),
            abs(low[i] - close[i - 1]),
        )

    atr = np.full(n, np.nan, dtype=np.float64)
    if n < period:
        return atr

    atr[period - 1] = np.mean(tr[:period])
    for i in range(period, n):
        atr[i] = (atr[i - 1] * (period - 1) + tr[i]) / period

    return atr


# ---------------------------------------------------------------------------
# Keltner Channel
# ---------------------------------------------------------------------------

def compute_keltner_channel(
    high: np.ndarray,
    low: np.ndarray,
    close: np.ndarray,
    ema_period: int = 20,
    atr_period: int = 10,
    multiplier: float = 1.5,
) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    """Keltner Channel.

    Returns ``(upper, middle, lower)``.
    ``middle = EMA(close, ema_period)``
    ``upper = middle + multiplier * ATR``
    ``lower = middle - multiplier * ATR``
    """
    middle = compute_ema(close, ema_period)
    atr = compute_atr(high, low, close, atr_period)

    upper = middle + multiplier * atr
    lower = middle - multiplier * atr

    return upper, middle, lower


# ---------------------------------------------------------------------------
# Donchian Channel
# ---------------------------------------------------------------------------

def compute_donchian_channel(
    high: np.ndarray,
    low: np.ndarray,
    period: int = 20,
) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    """Donchian Channel.

    Returns ``(upper, middle, lower)``.
    ``upper = highest high over period``
    ``lower = lowest low over period``
    ``middle = (upper + lower) / 2``
    """
    n = len(high)
    upper = np.full(n, np.nan, dtype=np.float64)
    lower = np.full(n, np.nan, dtype=np.float64)
    middle = np.full(n, np.nan, dtype=np.float64)

    for i in range(period - 1, n):
        upper[i] = np.max(high[i - period + 1: i + 1])
        lower[i] = np.min(low[i - period + 1: i + 1])
        middle[i] = (upper[i] + lower[i]) / 2.0

    return upper, middle, lower
