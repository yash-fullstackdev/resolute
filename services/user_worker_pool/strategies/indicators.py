"""Technical indicators for strategy evaluation.

Pure Python — no external dependencies.
All functions accept plain Python lists and return plain Python lists (or scalars).
"""

from __future__ import annotations
import math


# ─────────────────────────────────────────────────────────────────────────────
# EMA
# ─────────────────────────────────────────────────────────────────────────────

def ema(closes: list[float], period: int) -> list[float]:
    """Exponential Moving Average.

    Returns a list of length (len(closes) - period + 1).
    First value is seeded with a simple average of the first `period` bars.
    """
    n = len(closes)
    if n < period or period < 1:
        return []
    k = 2.0 / (period + 1)
    seed = sum(closes[:period]) / period
    result = [seed]
    for price in closes[period:]:
        result.append(price * k + result[-1] * (1.0 - k))
    return result


def ema_series(closes: list[float], period: int) -> list[float]:
    """Alias for ema()."""
    return ema(closes, period)


# ─────────────────────────────────────────────────────────────────────────────
# ATR (Wilder smoothing)
# ─────────────────────────────────────────────────────────────────────────────

def atr_wilder(highs: list[float], lows: list[float], closes: list[float],
               period: int) -> list[float]:
    """Wilder Average True Range.

    Returns a list of length (len(closes) - period).
    Seed = SMA of first `period` true ranges; then Wilder smoothing.
    """
    n = min(len(highs), len(lows), len(closes))
    if n < period + 1:
        return []

    # True ranges
    tr: list[float] = []
    for i in range(1, n):
        tr.append(max(
            highs[i] - lows[i],
            abs(highs[i] - closes[i - 1]),
            abs(lows[i] - closes[i - 1]),
        ))

    if len(tr) < period:
        return []

    seed = sum(tr[:period]) / period
    result = [seed]
    for val in tr[period:]:
        result.append((result[-1] * (period - 1) + val) / period)
    return result


# ─────────────────────────────────────────────────────────────────────────────
# Bollinger Bands
# ─────────────────────────────────────────────────────────────────────────────

def bollinger_bands(closes: list[float], period: int = 20,
                    std_mult: float = 2.0) -> dict:
    """Bollinger Bands.

    Returns dict with keys: upper, mid, lower, bandwidth (all lists of equal length).
    """
    n = len(closes)
    if n < period:
        return {"upper": [], "mid": [], "lower": [], "bandwidth": []}

    upper, mid, lower, bw = [], [], [], []
    for i in range(period - 1, n):
        window = closes[i - period + 1: i + 1]
        m = sum(window) / period
        variance = sum((x - m) ** 2 for x in window) / period
        std = math.sqrt(variance)
        u = m + std_mult * std
        l = m - std_mult * std
        upper.append(u)
        mid.append(m)
        lower.append(l)
        bw.append((u - l) / m if m != 0 else 0.0)

    return {"upper": upper, "mid": mid, "lower": lower, "bandwidth": bw}


# ─────────────────────────────────────────────────────────────────────────────
# Keltner Channels
# ─────────────────────────────────────────────────────────────────────────────

def keltner_channels(highs: list[float], lows: list[float], closes: list[float],
                     kc_period: int = 20, atr_period: int = 10,
                     mult: float = 1.5) -> dict:
    """Keltner Channels based on EMA midline ± mult × ATR.

    Returns dict: upper, mid, lower (lists, aligned to the shorter of ema/atr).
    """
    atr = atr_wilder(highs, lows, closes, atr_period)
    ema_c = ema(closes, kc_period)
    if not atr or not ema_c:
        return {"upper": [], "mid": [], "lower": []}

    length = min(len(atr), len(ema_c))
    atr_tail = atr[-length:]
    ema_tail = ema_c[-length:]
    upper = [e + mult * a for e, a in zip(ema_tail, atr_tail)]
    lower = [e - mult * a for e, a in zip(ema_tail, atr_tail)]
    return {"upper": upper, "mid": list(ema_tail), "lower": lower}


# ─────────────────────────────────────────────────────────────────────────────
# RSI (Wilder smoothing)
# ─────────────────────────────────────────────────────────────────────────────

def rsi_wilder(closes: list[float], period: int = 14) -> list[float]:
    """Wilder Relative Strength Index.

    Returns a list starting from index `period` (len = len(closes) - period).
    """
    n = len(closes)
    if n < period + 1:
        return []

    deltas = [closes[i] - closes[i - 1] for i in range(1, n)]
    gains = [max(d, 0.0) for d in deltas]
    losses = [max(-d, 0.0) for d in deltas]

    # Seed with first `period` bars
    avg_gain = sum(gains[:period]) / period
    avg_loss = sum(losses[:period]) / period

    def _rsi(ag: float, al: float) -> float:
        if al == 0:
            return 100.0
        return 100.0 - 100.0 / (1.0 + ag / al)

    result = [_rsi(avg_gain, avg_loss)]
    for i in range(period, len(deltas)):
        avg_gain = (avg_gain * (period - 1) + gains[i]) / period
        avg_loss = (avg_loss * (period - 1) + losses[i]) / period
        result.append(_rsi(avg_gain, avg_loss))

    return result


# ─────────────────────────────────────────────────────────────────────────────
# VWAP with bands
# ─────────────────────────────────────────────────────────────────────────────

def vwap_with_bands(highs: list[float], lows: list[float], closes: list[float],
                    volumes: list[float], std_mult: float = 1.0) -> dict:
    """Rolling (cumulative) VWAP with ±1 std deviation bands.

    Returns dict: vwap, upper_1, lower_1 (lists of same length as input).
    """
    n = min(len(highs), len(lows), len(closes), len(volumes))
    if n < 2:
        return {}

    vwap_list: list[float] = []
    upper_list: list[float] = []
    lower_list: list[float] = []

    cum_tp_vol = 0.0
    cum_vol = 0.0
    cum_tp2_vol = 0.0

    for i in range(n):
        tp = (highs[i] + lows[i] + closes[i]) / 3.0
        v = max(volumes[i], 0.0)
        cum_tp_vol += tp * v
        cum_vol += v
        cum_tp2_vol += tp * tp * v

        if cum_vol == 0:
            vwap_list.append(tp)
            upper_list.append(tp)
            lower_list.append(tp)
        else:
            vwap = cum_tp_vol / cum_vol
            variance = max((cum_tp2_vol / cum_vol) - vwap ** 2, 0.0)
            std = math.sqrt(variance)
            vwap_list.append(vwap)
            upper_list.append(vwap + std_mult * std)
            lower_list.append(vwap - std_mult * std)

    return {"vwap": vwap_list, "upper_1": upper_list, "lower_1": lower_list}


# ─────────────────────────────────────────────────────────────────────────────
# Volume ratio
# ─────────────────────────────────────────────────────────────────────────────

def volume_ratio(volumes: list[float], period: int = 20) -> float:
    """Current bar volume / average of previous `period` bars."""
    if not volumes or len(volumes) < 2:
        return 0.0
    curr = volumes[-1]
    hist = volumes[-(period + 1):-1]
    if not hist:
        return 0.0
    avg = sum(hist) / len(hist)
    return curr / avg if avg > 0 else 0.0
