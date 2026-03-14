"""
Trend indicators — MACD, SuperTrend, Parabolic SAR, ADX, Ichimoku.

All functions accept NumPy arrays and return full-length arrays (NaN-padded
where insufficient data exists).
"""

from __future__ import annotations

import numpy as np
import structlog

from .moving_averages import compute_ema

log = structlog.get_logger(__name__)


# ---------------------------------------------------------------------------
# MACD
# ---------------------------------------------------------------------------

def compute_macd(
    close: np.ndarray,
    fast: int = 12,
    slow: int = 26,
    signal_period: int = 9,
) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    """MACD — Moving Average Convergence Divergence.

    Returns ``(macd_line, signal_line, histogram)``.
    ``macd_line = EMA(close, fast) - EMA(close, slow)``
    ``signal_line = EMA(macd_line, signal_period)``
    ``histogram = macd_line - signal_line``
    """
    ema_fast = compute_ema(close, fast)
    ema_slow = compute_ema(close, slow)

    macd_line = ema_fast - ema_slow  # NaN propagates naturally

    # Signal line: EMA of the MACD line (only over valid values)
    valid_mask = ~np.isnan(macd_line)
    signal_line = np.full_like(macd_line, np.nan, dtype=np.float64)

    if np.any(valid_mask):
        valid_macd = macd_line[valid_mask]
        signal_ema = compute_ema(valid_macd, signal_period)

        valid_indices = np.where(valid_mask)[0]
        for idx, orig_idx in enumerate(valid_indices):
            signal_line[orig_idx] = signal_ema[idx]

    histogram = macd_line - signal_line

    return macd_line, signal_line, histogram


# ---------------------------------------------------------------------------
# ATR (used by SuperTrend)
# ---------------------------------------------------------------------------

def _compute_atr(
    high: np.ndarray,
    low: np.ndarray,
    close: np.ndarray,
    period: int = 14,
) -> np.ndarray:
    """Average True Range using Wilder's smoothing."""
    n = len(close)
    tr = np.full(n, np.nan, dtype=np.float64)
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
# SuperTrend
# ---------------------------------------------------------------------------

def compute_supertrend(
    high: np.ndarray,
    low: np.ndarray,
    close: np.ndarray,
    period: int = 10,
    multiplier: float = 3.0,
) -> tuple[np.ndarray, np.ndarray]:
    """SuperTrend indicator based on ATR.

    Returns ``(supertrend_values, direction)`` where direction is
    ``1.0`` for uptrend (BUY) and ``-1.0`` for downtrend (SELL).
    """
    n = len(close)
    atr = _compute_atr(high, low, close, period)

    hl2 = (high + low) / 2.0
    upper_band = hl2 + multiplier * atr
    lower_band = hl2 - multiplier * atr

    supertrend = np.full(n, np.nan, dtype=np.float64)
    direction = np.full(n, np.nan, dtype=np.float64)

    start = period - 1
    if start >= n:
        return supertrend, direction

    # Initialise
    supertrend[start] = upper_band[start]
    direction[start] = -1.0  # start bearish

    for i in range(start + 1, n):
        if np.isnan(atr[i]):
            continue

        # Final upper/lower bands (carry forward tighter levels)
        if upper_band[i] < upper_band[i - 1] or close[i - 1] > upper_band[i - 1]:
            final_upper = upper_band[i]
        else:
            final_upper = upper_band[i - 1]

        if lower_band[i] > lower_band[i - 1] or close[i - 1] < lower_band[i - 1]:
            final_lower = lower_band[i]
        else:
            final_lower = lower_band[i - 1]

        upper_band[i] = final_upper
        lower_band[i] = final_lower

        # Direction logic
        if direction[i - 1] == -1.0:  # previous was bearish
            if close[i] > upper_band[i]:
                direction[i] = 1.0
                supertrend[i] = lower_band[i]
            else:
                direction[i] = -1.0
                supertrend[i] = upper_band[i]
        else:  # previous was bullish
            if close[i] < lower_band[i]:
                direction[i] = -1.0
                supertrend[i] = upper_band[i]
            else:
                direction[i] = 1.0
                supertrend[i] = lower_band[i]

    return supertrend, direction


# ---------------------------------------------------------------------------
# Parabolic SAR
# ---------------------------------------------------------------------------

def compute_parabolic_sar(
    high: np.ndarray,
    low: np.ndarray,
    af_start: float = 0.02,
    af_step: float = 0.02,
    af_max: float = 0.20,
) -> tuple[np.ndarray, np.ndarray]:
    """Parabolic SAR.

    Returns ``(sar_values, direction)`` where direction is ``1.0`` (long)
    or ``-1.0`` (short).
    """
    n = len(high)
    sar = np.full(n, np.nan, dtype=np.float64)
    direction = np.full(n, np.nan, dtype=np.float64)

    if n < 2:
        return sar, direction

    # Initialise: assume uptrend
    is_long = True
    af = af_start
    ep = high[0]
    sar[0] = low[0]
    direction[0] = 1.0

    for i in range(1, n):
        prev_sar = sar[i - 1]
        new_sar = prev_sar + af * (ep - prev_sar)

        if is_long:
            # SAR cannot be above prior two lows
            new_sar = min(new_sar, low[i - 1])
            if i >= 2:
                new_sar = min(new_sar, low[i - 2])

            if low[i] < new_sar:
                # Reverse to short
                is_long = False
                new_sar = ep
                ep = low[i]
                af = af_start
            else:
                if high[i] > ep:
                    ep = high[i]
                    af = min(af + af_step, af_max)
        else:
            # SAR cannot be below prior two highs
            new_sar = max(new_sar, high[i - 1])
            if i >= 2:
                new_sar = max(new_sar, high[i - 2])

            if high[i] > new_sar:
                # Reverse to long
                is_long = True
                new_sar = ep
                ep = high[i]
                af = af_start
            else:
                if low[i] < ep:
                    ep = low[i]
                    af = min(af + af_step, af_max)

        sar[i] = new_sar
        direction[i] = 1.0 if is_long else -1.0

    return sar, direction


# ---------------------------------------------------------------------------
# ADX — Average Directional Index
# ---------------------------------------------------------------------------

def compute_adx(
    high: np.ndarray,
    low: np.ndarray,
    close: np.ndarray,
    period: int = 14,
) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    """Average Directional Index.

    Returns ``(adx, plus_di, minus_di)``.
    Uses Wilder's smoothing throughout.
    """
    n = len(close)
    adx = np.full(n, np.nan, dtype=np.float64)
    plus_di = np.full(n, np.nan, dtype=np.float64)
    minus_di = np.full(n, np.nan, dtype=np.float64)

    if n < period + 1:
        return adx, plus_di, minus_di

    # True Range
    tr = np.zeros(n, dtype=np.float64)
    plus_dm = np.zeros(n, dtype=np.float64)
    minus_dm = np.zeros(n, dtype=np.float64)

    for i in range(1, n):
        tr[i] = max(
            high[i] - low[i],
            abs(high[i] - close[i - 1]),
            abs(low[i] - close[i - 1]),
        )
        up_move = high[i] - high[i - 1]
        down_move = low[i - 1] - low[i]

        plus_dm[i] = up_move if (up_move > down_move and up_move > 0) else 0.0
        minus_dm[i] = down_move if (down_move > up_move and down_move > 0) else 0.0

    # Wilder's smoothed TR, +DM, -DM
    smoothed_tr = np.zeros(n, dtype=np.float64)
    smoothed_plus_dm = np.zeros(n, dtype=np.float64)
    smoothed_minus_dm = np.zeros(n, dtype=np.float64)

    smoothed_tr[period] = np.sum(tr[1: period + 1])
    smoothed_plus_dm[period] = np.sum(plus_dm[1: period + 1])
    smoothed_minus_dm[period] = np.sum(minus_dm[1: period + 1])

    for i in range(period + 1, n):
        smoothed_tr[i] = smoothed_tr[i - 1] - smoothed_tr[i - 1] / period + tr[i]
        smoothed_plus_dm[i] = smoothed_plus_dm[i - 1] - smoothed_plus_dm[i - 1] / period + plus_dm[i]
        smoothed_minus_dm[i] = smoothed_minus_dm[i - 1] - smoothed_minus_dm[i - 1] / period + minus_dm[i]

    # +DI and -DI
    for i in range(period, n):
        if smoothed_tr[i] == 0:
            plus_di[i] = 0.0
            minus_di[i] = 0.0
        else:
            plus_di[i] = 100.0 * smoothed_plus_dm[i] / smoothed_tr[i]
            minus_di[i] = 100.0 * smoothed_minus_dm[i] / smoothed_tr[i]

    # DX and ADX
    dx = np.full(n, np.nan, dtype=np.float64)
    for i in range(period, n):
        di_sum = plus_di[i] + minus_di[i]
        if di_sum == 0:
            dx[i] = 0.0
        else:
            dx[i] = 100.0 * abs(plus_di[i] - minus_di[i]) / di_sum

    # First ADX = average of first *period* DX values
    first_adx_idx = 2 * period - 1
    if first_adx_idx < n:
        valid_dx = dx[period: first_adx_idx + 1]
        valid_dx = valid_dx[~np.isnan(valid_dx)]
        if len(valid_dx) > 0:
            adx[first_adx_idx] = np.mean(valid_dx)

        for i in range(first_adx_idx + 1, n):
            if not np.isnan(adx[i - 1]) and not np.isnan(dx[i]):
                adx[i] = (adx[i - 1] * (period - 1) + dx[i]) / period

    return adx, plus_di, minus_di


# ---------------------------------------------------------------------------
# Ichimoku Cloud
# ---------------------------------------------------------------------------

def compute_ichimoku(
    high: np.ndarray,
    low: np.ndarray,
    close: np.ndarray,
    tenkan_period: int = 9,
    kijun_period: int = 26,
    senkou_b_period: int = 52,
    displacement: int = 26,
) -> dict[str, np.ndarray]:
    """Ichimoku Kinko Hyo.

    Returns a dict with keys:
    ``tenkan_sen``, ``kijun_sen``, ``senkou_a``, ``senkou_b``, ``chikou_span``.

    Senkou A and B are displaced forward by *displacement* periods.
    Chikou span is displaced backward by *displacement* periods.
    """
    n = len(close)

    def _midpoint(arr_high: np.ndarray, arr_low: np.ndarray, period: int) -> np.ndarray:
        result = np.full(n, np.nan, dtype=np.float64)
        for i in range(period - 1, n):
            result[i] = (np.max(arr_high[i - period + 1: i + 1]) + np.min(arr_low[i - period + 1: i + 1])) / 2.0
        return result

    tenkan_sen = _midpoint(high, low, tenkan_period)
    kijun_sen = _midpoint(high, low, kijun_period)

    # Senkou A = (Tenkan + Kijun) / 2, displaced forward
    senkou_a_raw = (tenkan_sen + kijun_sen) / 2.0
    senkou_a = np.full(n + displacement, np.nan, dtype=np.float64)
    senkou_a[displacement: displacement + n] = senkou_a_raw
    senkou_a = senkou_a[:n]  # trim to original length

    # Senkou B = midpoint of highest high and lowest low over senkou_b_period, displaced forward
    senkou_b_raw = _midpoint(high, low, senkou_b_period)
    senkou_b = np.full(n + displacement, np.nan, dtype=np.float64)
    senkou_b[displacement: displacement + n] = senkou_b_raw
    senkou_b = senkou_b[:n]

    # Chikou span = close displaced backward
    chikou_span = np.full(n, np.nan, dtype=np.float64)
    if n > displacement:
        chikou_span[: n - displacement] = close[displacement:]

    return {
        "tenkan_sen": tenkan_sen,
        "kijun_sen": kijun_sen,
        "senkou_a": senkou_a,
        "senkou_b": senkou_b,
        "chikou_span": chikou_span,
    }
