"""
Full Black-Scholes Greeks implementation with NumPy vectorisation.

Performance target: 500 options in < 10 ms.

Attempts to use numba @njit for JIT compilation.  Falls back to pure NumPy
if numba is not available so the service still starts in CI/dev environments.
"""

from __future__ import annotations

import math
import numpy as np

try:
    from numba import njit

    _HAS_NUMBA = True
except ImportError:  # pragma: no cover
    _HAS_NUMBA = False

    def njit(*args, **kwargs):  # type: ignore[misc]
        """No-op decorator when numba is absent."""
        if args and callable(args[0]):
            return args[0]
        return lambda fn: fn


# ---------------------------------------------------------------------------
# Standard-normal helpers (needed inside @njit where scipy is unavailable)
# ---------------------------------------------------------------------------

_INV_SQRT_2PI = 1.0 / math.sqrt(2.0 * math.pi)


@njit(cache=True)
def _norm_pdf(x: np.ndarray) -> np.ndarray:
    """Standard-normal probability density function, element-wise."""
    return _INV_SQRT_2PI * np.exp(-0.5 * x * x)


@njit(cache=True)
def _norm_cdf_scalar(x: float) -> float:
    """Abramowitz-Stegun approximation of N(x), scalar version.

    Maximum absolute error ~ 7.5e-8 — sufficient for options pricing.
    """
    a1 = 0.254829592
    a2 = -0.284496736
    a3 = 1.421413741
    a4 = -1.453152027
    a5 = 1.061405429
    p = 0.3275911

    sign = 1.0
    if x < 0:
        sign = -1.0
    x = abs(x)

    t = 1.0 / (1.0 + p * x)
    y = 1.0 - (((((a5 * t + a4) * t) + a3) * t + a2) * t + a1) * t * math.exp(-x * x / 2.0)
    return 0.5 * (1.0 + sign * y)


@njit(cache=True)
def _norm_cdf(x: np.ndarray) -> np.ndarray:
    """Element-wise standard-normal CDF over an array."""
    out = np.empty_like(x)
    for i in range(x.shape[0]):
        out[i] = _norm_cdf_scalar(x[i])
    return out


# ---------------------------------------------------------------------------
# Core Black-Scholes (vectorised)
# ---------------------------------------------------------------------------

# Minimum time-to-expiry to avoid division by zero (~ 1 minute in years)
_MIN_T = 1.0 / (365.0 * 24.0 * 60.0)
_MIN_SIGMA = 1e-8


@njit(cache=True)
def black_scholes_vectorised(
    S: np.ndarray,
    K: np.ndarray,
    T: np.ndarray,
    r: float,
    sigma: np.ndarray,
) -> tuple:
    """Vectorised Black-Scholes pricing and first-order Greeks.

    Parameters
    ----------
    S : array  — Spot prices (one per option)
    K : array  — Strike prices
    T : array  — Time to expiry in years
    r : float  — Risk-free rate (annualised, e.g. 0.065 for 6.5 %)
    sigma : array  — Implied volatilities

    Returns
    -------
    (call_price, put_price, delta_call, delta_put, gamma, theta_call, theta_put, vega)
    All arrays of the same shape as the inputs.
    """
    n = S.shape[0]
    call_price = np.empty(n)
    put_price = np.empty(n)
    delta_call = np.empty(n)
    delta_put = np.empty(n)
    gamma = np.empty(n)
    theta_call = np.empty(n)
    theta_put = np.empty(n)
    vega = np.empty(n)

    for i in range(n):
        s = S[i]
        k = K[i]
        t = T[i] if T[i] > _MIN_T else _MIN_T
        sig = sigma[i] if sigma[i] > _MIN_SIGMA else _MIN_SIGMA

        sqrt_t = math.sqrt(t)
        sig_sqrt_t = sig * sqrt_t
        d1 = (math.log(s / k) + (r + 0.5 * sig * sig) * t) / sig_sqrt_t
        d2 = d1 - sig_sqrt_t

        nd1 = _norm_cdf_scalar(d1)
        nd2 = _norm_cdf_scalar(d2)
        n_neg_d1 = _norm_cdf_scalar(-d1)
        n_neg_d2 = _norm_cdf_scalar(-d2)

        nprime_d1 = _INV_SQRT_2PI * math.exp(-0.5 * d1 * d1)

        discount = math.exp(-r * t)

        # Prices
        call_price[i] = s * nd1 - k * discount * nd2
        put_price[i] = k * discount * n_neg_d2 - s * n_neg_d1

        # Deltas
        delta_call[i] = nd1
        delta_put[i] = nd1 - 1.0

        # Gamma (same for call and put)
        gamma[i] = nprime_d1 / (s * sig_sqrt_t)

        # Theta (per calendar day — divide annual theta by 365)
        theta_call[i] = (
            -(s * nprime_d1 * sig / (2.0 * sqrt_t)) - r * k * discount * nd2
        ) / 365.0
        theta_put[i] = (
            -(s * nprime_d1 * sig / (2.0 * sqrt_t)) + r * k * discount * n_neg_d2
        ) / 365.0

        # Vega (per 1 % move in IV)
        vega[i] = s * nprime_d1 * sqrt_t / 100.0

    return (call_price, put_price, delta_call, delta_put, gamma, theta_call, theta_put, vega)


# ---------------------------------------------------------------------------
# Moneyness filter — skip if > 15 % ITM
# ---------------------------------------------------------------------------

def moneyness_mask(
    S: np.ndarray,
    K: np.ndarray,
    option_type: np.ndarray,
    threshold: float = 0.15,
) -> np.ndarray:
    """Return a boolean mask where True = option is within moneyness threshold.

    Parameters
    ----------
    S : array  — Spot prices
    K : array  — Strikes
    option_type : array of int  — 1 for call, -1 for put
    threshold : float — max |moneyness| to keep (default 0.15 = 15 %)

    Calls are ITM when S > K.  Puts are ITM when K > S.
    We skip deep-ITM options because BS IV is poorly defined there.
    """
    moneyness = (S - K) / K  # positive ⇒ call ITM
    call_mask = option_type == 1
    put_mask = option_type == -1
    itm_pct = np.where(call_mask, moneyness, -moneyness)
    return itm_pct <= threshold
