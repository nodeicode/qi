"""Black-Scholes price and implied-volatility (IV) inversion utilities.

The module is self-contained: it implements forward Black-Scholes pricing
(``bs_price``) and an implied-volatility solver (``iv_from_price``) with no
inputs beyond scalars.  It is the counterpart of ``quant.utils.bs_greeks``
(closed-form first/second-order greeks evaluated at the inverted IV).

Conventions
-----------
- Time ``T`` is in years.
- ``sigma`` (volatility) and ``price`` are decimals, not percent.
- ``is_call`` selects a call (``True``) or put (``False``).
- Dividend yield ``q`` and risk-free rate ``r`` default to 0.0, the
  convention the options-data handler's greeks gate uses.  They are
  parameters so the same primitive can serve other books without a second
  implementation.
- ``iv_from_price`` returns ``0.0`` (never raises) when the price is outside
  the range any finite positive vol can produce, or when Newton-Raphson
  fails to converge; callers treat ``iv <= 0`` as "no usable IV".
"""

from __future__ import annotations

import math

from scipy.stats import norm

# Numerical configuration for the IV solver.
_IV_SIGMA0 = 0.20  # Newton-Raphson starting vol (decimal)
_IV_TOL = 1e-10  # convergence tolerance on |price - target|
_IV_MAX_ITER = 200
# Vol search bounds; a price unreachable with vol in this band returns 0.0.
_IV_SIGMA_MIN = 1e-4
_IV_SIGMA_MAX = 20.0


def _d1d2(
    S: float, K: float, T: float, sigma: float, r: float, q: float
) -> tuple[float, float]:
    """Return ``(d1, d2)`` for the Black-Scholes probability arguments."""
    sigma_t = sigma * math.sqrt(T)
    d1 = (math.log(S / K) + (r - q + 0.5 * sigma * sigma) * T) / sigma_t
    d2 = d1 - sigma_t
    return d1, d2


def bs_price(
    S: float,
    K: float,
    T: float,
    sigma: float,
    is_call: bool,
    r: float = 0.0,
    q: float = 0.0,
) -> float:
    """Black-Scholes price of a European option.

    Parameters
    ----------
    S:
        Spot price of the underlying.
    K:
        Strike.
    T:
        Time to maturity in years.
    sigma:
        Volatility (decimal, e.g. 0.20 for 20 %).
    is_call:
        ``True`` for a call, ``False`` for a put.
    r, q:
        Risk-free rate / dividend yield, both defaulting to 0.0.

    Returns ``0.0`` for degenerate inputs (``T <= 0``, ``S <= 0``,
    ``K <= 0``, ``sigma <= 0``) so a vectorised caller never sees NaN.
    """
    if T <= 0 or S <= 0 or K <= 0 or sigma <= 0:
        return 0.0
    d1, d2 = _d1d2(S, K, T, sigma, r, q)
    df_s = math.exp(-q * T)
    df_r = math.exp(-r * T)
    if is_call:
        return S * df_s * norm.cdf(d1) - K * df_r * norm.cdf(d2)
    return K * df_r * norm.cdf(-d2) - S * df_s * norm.cdf(-d1)


def iv_from_price(
    S: float,
    K: float,
    T: float,
    price: float,
    is_call: bool,
    r: float = 0.0,
    q: float = 0.0,
) -> float:
    """Invert ``bs_price`` for implied volatility via Newton-Raphson.

    Returns the vol (decimal) whose Black-Scholes price equals ``price``,
    or ``0.0`` when no positive vol in :data:`_IV_SIGMA_MIN` →
    :data:`_IV_SIGMA_MAX` matches (e.g. an arbitrage price outside the
    intrinsic band, or a degenerate ``T``).  This never raises.

    The solver mixes Newton-Raphson with a bisection fallback: Newton's
    step is taken only when it stays inside the current bracket, which
    keeps the method stable for deep-OTM wings whose vega is tiny.
    """
    if T <= 0 or S <= 0 or K <= 0 or price <= 0:
        return 0.0
    # Intrinsic bounds: with sigma -> 0 the payoff is the intrinsic value;
    # a price beyond the intrinsic valuation has no positive-vol solution.
    intrinsic = max(S - K, 0.0) if is_call else max(K - S, 0.0)
    if price <= intrinsic:
        return 0.0

    lo, hi = _IV_SIGMA_MIN, _IV_SIGMA_MAX
    f_lo = bs_price(S, K, T, lo, is_call, r, q) - price
    if f_lo >= 0:  # target below the smallest achievable price
        return 0.0
    f_hi = bs_price(S, K, T, hi, is_call, r, q) - price
    if f_hi <= 0:  # target above the largest achievable price
        return 0.0

    sig = _IV_SIGMA0
    for _ in range(_IV_MAX_ITER):
        f = bs_price(S, K, T, sig, is_call, r, q) - price
        if abs(f) < _IV_TOL:
            return sig
        # Newton step (vega = dPrice/dSigma), fall back to bisection when
        # the step escapes the bracket or vega is ~0.
        vega = S * math.sqrt(T) * norm.pdf(_d1d2(S, K, T, sig, r, q)[0])
        if vega > 1e-12:
            step = f / vega
            cand = sig - step
        else:
            cand = float("inf")
        if lo < cand < hi:
            sig = cand
        else:
            mid = 0.5 * (lo + hi)
            f_mid = bs_price(S, K, T, mid, is_call, r, q) - price
            if f_mid > 0:
                hi = mid
            else:
                lo = mid
            sig = mid
    return sig
