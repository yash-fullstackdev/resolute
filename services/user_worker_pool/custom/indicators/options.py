"""
Options-specific indicators — IV Rank, IV Percentile, PCR, Max Pain,
OI Change, IV Skew.

These indicators pull data from option chain snapshots rather than OHLCV
price bars.
"""

from __future__ import annotations

from typing import Any

import numpy as np
import structlog

log = structlog.get_logger(__name__)


# ---------------------------------------------------------------------------
# IV Rank
# ---------------------------------------------------------------------------

def compute_iv_rank(
    current_iv: float,
    iv_history: np.ndarray,
) -> float:
    """IV Rank — where current IV sits relative to its 52-week range.

    ``IV_Rank = (current - min) / (max - min) * 100``

    Returns 0-100.  If the history is empty or max == min, returns 50.
    """
    if len(iv_history) == 0:
        return 50.0

    iv_min = float(np.min(iv_history))
    iv_max = float(np.max(iv_history))

    if iv_max == iv_min:
        return 50.0

    rank = (current_iv - iv_min) / (iv_max - iv_min) * 100.0
    return float(np.clip(rank, 0.0, 100.0))


# ---------------------------------------------------------------------------
# IV Percentile
# ---------------------------------------------------------------------------

def compute_iv_percentile(
    current_iv: float,
    iv_history: np.ndarray,
) -> float:
    """IV Percentile — percentage of days IV was lower than current.

    Returns 0-100.
    """
    if len(iv_history) == 0:
        return 50.0

    below = np.sum(iv_history < current_iv)
    return float(below / len(iv_history) * 100.0)


# ---------------------------------------------------------------------------
# PCR — Put-Call Ratio (by OI or Volume)
# ---------------------------------------------------------------------------

def compute_pcr_oi(
    chain_snapshot: dict[str, Any],
) -> float:
    """Put-Call Ratio by Open Interest.

    ``PCR_OI = total_put_oi / total_call_oi``

    Expects *chain_snapshot* to have a ``strikes`` list, each with
    ``call_oi`` and ``put_oi`` fields (or equivalent dict keys).
    """
    strikes = chain_snapshot.get("strikes", [])
    total_call_oi = 0.0
    total_put_oi = 0.0

    for strike in strikes:
        if isinstance(strike, dict):
            total_call_oi += strike.get("call_oi", 0)
            total_put_oi += strike.get("put_oi", 0)
        else:
            total_call_oi += getattr(strike, "call_oi", 0)
            total_put_oi += getattr(strike, "put_oi", 0)

    if total_call_oi == 0:
        return 0.0

    return total_put_oi / total_call_oi


def compute_pcr_volume(
    chain_snapshot: dict[str, Any],
) -> float:
    """Put-Call Ratio by Volume.

    ``PCR_VOL = total_put_volume / total_call_volume``
    """
    strikes = chain_snapshot.get("strikes", [])
    total_call_vol = 0.0
    total_put_vol = 0.0

    for strike in strikes:
        if isinstance(strike, dict):
            total_call_vol += strike.get("call_volume", 0)
            total_put_vol += strike.get("put_volume", 0)
        else:
            total_call_vol += getattr(strike, "call_volume", 0)
            total_put_vol += getattr(strike, "put_volume", 0)

    if total_call_vol == 0:
        return 0.0

    return total_put_vol / total_call_vol


# ---------------------------------------------------------------------------
# Max Pain
# ---------------------------------------------------------------------------

def compute_max_pain(
    chain_snapshot: dict[str, Any],
) -> float:
    """Max Pain — the strike price at which total option buyer loss is maximised
    (equivalently, the strike at which total ITM option value is minimised).

    For each candidate strike, compute the total intrinsic value of all calls
    and puts if the underlying expires at that strike.  The strike with the
    lowest aggregate intrinsic value is max pain.
    """
    strikes = chain_snapshot.get("strikes", [])
    if not strikes:
        return 0.0

    strike_data: list[tuple[float, float, float]] = []
    for s in strikes:
        if isinstance(s, dict):
            sp = s.get("strike", 0.0)
            c_oi = s.get("call_oi", 0)
            p_oi = s.get("put_oi", 0)
        else:
            sp = getattr(s, "strike", 0.0)
            c_oi = getattr(s, "call_oi", 0)
            p_oi = getattr(s, "put_oi", 0)
        strike_data.append((sp, c_oi, p_oi))

    if not strike_data:
        return 0.0

    strike_prices = [sd[0] for sd in strike_data]
    min_pain = float("inf")
    max_pain_strike = strike_prices[0]

    for candidate in strike_prices:
        total_pain = 0.0
        for sp, c_oi, p_oi in strike_data:
            # Call holders lose when candidate < strike (calls expire worthless → no pain)
            # Call holders have pain when candidate > strike (calls are ITM for holders)
            call_itv = max(candidate - sp, 0.0) * c_oi
            put_itv = max(sp - candidate, 0.0) * p_oi
            total_pain += call_itv + put_itv

        if total_pain < min_pain:
            min_pain = total_pain
            max_pain_strike = candidate

    return float(max_pain_strike)


# ---------------------------------------------------------------------------
# OI Change
# ---------------------------------------------------------------------------

def compute_oi_change(
    chain_snapshot: dict[str, Any],
    previous_snapshot: dict[str, Any] | None,
) -> dict[str, float]:
    """OI Change — change in total call and put OI between snapshots.

    Returns ``{"call_oi_change": float, "put_oi_change": float, "net_oi_change": float}``.
    """
    if previous_snapshot is None:
        return {"call_oi_change": 0.0, "put_oi_change": 0.0, "net_oi_change": 0.0}

    def _total_oi(snap: dict[str, Any]) -> tuple[float, float]:
        strikes = snap.get("strikes", [])
        call_oi = 0.0
        put_oi = 0.0
        for s in strikes:
            if isinstance(s, dict):
                call_oi += s.get("call_oi", 0)
                put_oi += s.get("put_oi", 0)
            else:
                call_oi += getattr(s, "call_oi", 0)
                put_oi += getattr(s, "put_oi", 0)
        return call_oi, put_oi

    curr_call, curr_put = _total_oi(chain_snapshot)
    prev_call, prev_put = _total_oi(previous_snapshot)

    call_change = curr_call - prev_call
    put_change = curr_put - prev_put

    return {
        "call_oi_change": call_change,
        "put_oi_change": put_change,
        "net_oi_change": call_change + put_change,
    }


# ---------------------------------------------------------------------------
# IV Skew
# ---------------------------------------------------------------------------

def compute_iv_skew(
    chain_snapshot: dict[str, Any],
    underlying_price: float,
    distance_pct: float = 5.0,
) -> float:
    """IV Skew — difference between put IV and call IV at equidistant OTM
    strikes.

    ``IV_Skew = put_iv(distance OTM) - call_iv(distance OTM)``

    A positive skew means puts are more expensive (bearish demand).
    """
    strikes = chain_snapshot.get("strikes", [])
    if not strikes or underlying_price <= 0:
        return 0.0

    otm_distance = underlying_price * (distance_pct / 100.0)
    target_call_strike = underlying_price + otm_distance
    target_put_strike = underlying_price - otm_distance

    # Find closest call strike above spot
    best_call_iv: float | None = None
    best_call_dist = float("inf")

    best_put_iv: float | None = None
    best_put_dist = float("inf")

    for s in strikes:
        if isinstance(s, dict):
            sp = s.get("strike", 0.0)
            c_iv = s.get("call_iv", None)
            p_iv = s.get("put_iv", None)
        else:
            sp = getattr(s, "strike", 0.0)
            c_iv = getattr(s, "call_iv", None)
            p_iv = getattr(s, "put_iv", None)

        call_dist = abs(sp - target_call_strike)
        if c_iv is not None and call_dist < best_call_dist:
            best_call_dist = call_dist
            best_call_iv = c_iv

        put_dist = abs(sp - target_put_strike)
        if p_iv is not None and put_dist < best_put_dist:
            best_put_dist = put_dist
            best_put_iv = p_iv

    if best_call_iv is None or best_put_iv is None:
        return 0.0

    return best_put_iv - best_call_iv
