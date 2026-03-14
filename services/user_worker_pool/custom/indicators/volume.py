"""
Volume indicators — VWAP, OBV, Accumulation/Distribution Line.
"""

from __future__ import annotations

import numpy as np
import structlog

log = structlog.get_logger(__name__)


# ---------------------------------------------------------------------------
# VWAP — Volume Weighted Average Price
# ---------------------------------------------------------------------------

def compute_vwap(
    high: np.ndarray,
    low: np.ndarray,
    close: np.ndarray,
    volume: np.ndarray,
) -> np.ndarray:
    """Cumulative intraday VWAP.

    ``VWAP = cumsum(TP * volume) / cumsum(volume)``
    where ``TP = (high + low + close) / 3``.

    This is a session-cumulative calculation.  The caller is responsible for
    resetting the buffer at the start of each session.
    """
    tp = (high + low + close) / 3.0
    cum_tp_vol = np.cumsum(tp * volume)
    cum_vol = np.cumsum(volume.astype(np.float64))

    vwap = np.where(cum_vol > 0, cum_tp_vol / cum_vol, np.nan)
    return vwap.astype(np.float64)


# ---------------------------------------------------------------------------
# OBV — On Balance Volume
# ---------------------------------------------------------------------------

def compute_obv(
    close: np.ndarray,
    volume: np.ndarray,
) -> np.ndarray:
    """On Balance Volume.

    Cumulative volume that adds volume on up-close bars and subtracts on
    down-close bars.
    """
    n = len(close)
    obv = np.zeros(n, dtype=np.float64)
    obv[0] = volume[0]

    for i in range(1, n):
        if close[i] > close[i - 1]:
            obv[i] = obv[i - 1] + volume[i]
        elif close[i] < close[i - 1]:
            obv[i] = obv[i - 1] - volume[i]
        else:
            obv[i] = obv[i - 1]

    return obv


# ---------------------------------------------------------------------------
# A/D Line — Accumulation/Distribution
# ---------------------------------------------------------------------------

def compute_ad_line(
    high: np.ndarray,
    low: np.ndarray,
    close: np.ndarray,
    volume: np.ndarray,
) -> np.ndarray:
    """Accumulation/Distribution Line.

    ``CLV = ((close - low) - (high - close)) / (high - low)``
    ``AD = cumsum(CLV * volume)``

    Where ``high == low`` the CLV is set to 0.
    """
    hl_range = high - low
    clv = np.where(
        hl_range > 0,
        ((close - low) - (high - close)) / hl_range,
        0.0,
    )
    ad = np.cumsum(clv * volume)
    return ad.astype(np.float64)
