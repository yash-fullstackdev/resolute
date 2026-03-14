"""
Newton-Raphson Implied Volatility solver (vectorised).

Given observed market price, spot, strike, T, r  ->  solve for sigma.
Convergence tolerance: 1e-6, max 100 iterations.
"""

from __future__ import annotations

import math
import numpy as np
import structlog

try:
    from numba import njit

    _HAS_NUMBA = True
except ImportError:
    _HAS_NUMBA = False

    def njit(*args, **kwargs):
        if args and callable(args[0]):
            return args[0]
        return lambda fn: fn

from .greeks import _norm_cdf_scalar, _INV_SQRT_2PI

logger = structlog.get_logger(service="signal_engine", module="iv_calculator")

_MIN_T = 1.0 / (365.0 * 24.0 * 60.0)
_TOL = 1e-6
_MAX_ITER = 100
_INITIAL_SIGMA = 0.25
_SIGMA_MIN = 0.001
_SIGMA_MAX = 5.0


# ---------------------------------------------------------------------------
# Scalar BS price + vega for the NR inner loop
# ---------------------------------------------------------------------------

@njit(cache=True)
def _bs_call_price_and_vega(s: float, k: float, t: float, r: float, sig: float) -> tuple:
    """Return (call_price, vega) for a single option."""
    sqrt_t = math.sqrt(t)
    sig_sqrt_t = sig * sqrt_t
    d1 = (math.log(s / k) + (r + 0.5 * sig * sig) * t) / sig_sqrt_t
    d2 = d1 - sig_sqrt_t

    nd1 = _norm_cdf_scalar(d1)
    nd2 = _norm_cdf_scalar(d2)
    nprime_d1 = _INV_SQRT_2PI * math.exp(-0.5 * d1 * d1)

    discount = math.exp(-r * t)
    call_price = s * nd1 - k * discount * nd2
    vega = s * nprime_d1 * sqrt_t  # raw vega (not per-1%)

    return call_price, vega


@njit(cache=True)
def _bs_put_price_and_vega(s: float, k: float, t: float, r: float, sig: float) -> tuple:
    """Return (put_price, vega) for a single option."""
    sqrt_t = math.sqrt(t)
    sig_sqrt_t = sig * sqrt_t
    d1 = (math.log(s / k) + (r + 0.5 * sig * sig) * t) / sig_sqrt_t
    d2 = d1 - sig_sqrt_t

    n_neg_d1 = _norm_cdf_scalar(-d1)
    n_neg_d2 = _norm_cdf_scalar(-d2)
    nprime_d1 = _INV_SQRT_2PI * math.exp(-0.5 * d1 * d1)

    discount = math.exp(-r * t)
    put_price = k * discount * n_neg_d2 - s * n_neg_d1
    vega = s * nprime_d1 * sqrt_t

    return put_price, vega


# ---------------------------------------------------------------------------
# Newton-Raphson solver — scalar
# ---------------------------------------------------------------------------

@njit(cache=True)
def _nr_iv_scalar(
    market_price: float,
    s: float,
    k: float,
    t: float,
    r: float,
    is_call: bool,
) -> float:
    """Newton-Raphson IV solve for one option. Returns NaN on failure."""
    if market_price <= 0.0 or t <= 0.0 or s <= 0.0 or k <= 0.0:
        return math.nan

    sig = _INITIAL_SIGMA

    for _ in range(_MAX_ITER):
        if is_call:
            theo, vega = _bs_call_price_and_vega(s, k, t, r, sig)
        else:
            theo, vega = _bs_put_price_and_vega(s, k, t, r, sig)

        diff = theo - market_price

        if abs(diff) < _TOL:
            return sig

        if abs(vega) < 1e-12:
            return math.nan

        sig = sig - diff / vega

        # Clamp
        if sig < _SIGMA_MIN:
            sig = _SIGMA_MIN
        elif sig > _SIGMA_MAX:
            sig = _SIGMA_MAX

    return math.nan  # did not converge


# ---------------------------------------------------------------------------
# Vectorised wrapper
# ---------------------------------------------------------------------------

@njit(cache=True)
def newton_raphson_iv(
    market_prices: np.ndarray,
    S: np.ndarray,
    K: np.ndarray,
    T: np.ndarray,
    r: float,
    is_call: np.ndarray,
) -> np.ndarray:
    """Solve IV for an array of options via Newton-Raphson.

    Parameters
    ----------
    market_prices : array  — observed option prices
    S : array  — spot prices (one per option, typically all identical for same underlying)
    K : array  — strike prices
    T : array  — time to expiry in years
    r : float  — risk-free rate
    is_call : boolean array  — True for calls, False for puts

    Returns
    -------
    sigma : array  — implied volatilities (NaN where solver failed)
    """
    n = market_prices.shape[0]
    sigma = np.empty(n)

    for i in range(n):
        t = T[i] if T[i] > _MIN_T else _MIN_T
        sigma[i] = _nr_iv_scalar(
            market_prices[i],
            S[i],
            K[i],
            t,
            r,
            is_call[i],
        )

    return sigma


# ---------------------------------------------------------------------------
# IV Rank — requires DB access (non-numba)
# ---------------------------------------------------------------------------

async def calculate_iv_rank(symbol: str, current_atm_iv: float, db) -> tuple[float, float]:
    """Compute IV Rank and IV Percentile for *symbol*.

    IV Rank  = (current - 52w_low) / (52w_high - 52w_low) * 100
    IV %ile  = (# days IV was below current) / total_days * 100

    Parameters
    ----------
    symbol : str — underlying symbol, e.g. "NIFTY"
    current_atm_iv : float — current ATM IV
    db : signal_engine.db.SignalEngineDB instance

    Returns
    -------
    (iv_rank, iv_percentile) — both 0-100 floats.
    """
    iv_history = await db.get_52_week_iv_history(symbol)

    if not iv_history:
        return 0.0, 0.0

    iv_values = [row["atm_iv"] for row in iv_history]
    high_iv = max(iv_values)
    low_iv = min(iv_values)

    # IV Rank
    if high_iv == low_iv:
        iv_rank = 50.0
    else:
        iv_rank = (current_atm_iv - low_iv) / (high_iv - low_iv) * 100.0
        iv_rank = max(0.0, min(100.0, iv_rank))

    # IV Percentile
    days_below = sum(1 for v in iv_values if v < current_atm_iv)
    iv_percentile = (days_below / len(iv_values)) * 100.0

    return iv_rank, iv_percentile
