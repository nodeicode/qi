"""Closed-form Black-Scholes greeks, including second-order vanna & charm.

This module is self-contained: every formula below is the analytic partial
derivative of the Black-Scholes price (see :func:`quant.utils.bs_iv.bs_price`)
with respect to its inputs.  It pairs with ``quant.utils.bs_iv``: inside the
options-data handler, per-leg greeks are evaluated at the *implied* vol
inverted from a CBBO mid (r = q = 0).

Conventions
-----------
- ``T`` is time-to-maturity in years; ``sigma`` is a decimal vol.
- ``is_call`` selects call (``True``) / put (``False``).
- ``r`` (risk-free) and ``q`` (dividend yield) default to 0.0.
- All greeks are per-unit (per option contract):
  delta & gamma are per $1 of spot, vega per $1 of vol (a 0.01 vol move
  changes price by ``0.01 * vega``), theta per year, rho per $1 of rate,
  vanna per $1 of spot times $1 of vol, charm per year times $1 of vol.
- Position-scaling (long/short) and book aggregation live in the caller
  (``OptionDataHandler.get_greeks_at``); this module returns single-leg
  unsigned-per-contract values.

Definitions
-----------
- delta = dV/dS
- gamma = d²V/dS² = d(delta)/dS          (convexity, dollar gamma = S*gamma)
- vega  = dV/dsigma
- theta = -dV/dT                        (time decay; positive = value lost/day)
- rho   = dV/dr
- vanna = dVega/dS = dDelta/dsigma      (spot/vol cross-sensitivity; the two
          mixed partials are equal, so this is the volga of the skew)
- charm = dVega/dT                      (vega decay; how fast vol exposure
          erodes as time passes)

Vanna is the cross-sensitivity that sources the spot-vol correlation
("leverage effect"): a long-vanna book profits when spot and vol co-move.
Charm is the second-order time effect: short-dated options lose vega
quickly, so a long-charm book is favoured when vol is realised early.
"""

from __future__ import annotations

import math

from scipy.stats import norm


def greeks(
    S: float,
    K: float,
    T: float,
    sigma: float,
    is_call: bool,
    r: float = 0.0,
    q: float = 0.0,
) -> dict[str, float]:
    """Return first/second-order Black-Scholes greeks as a float dict.

    Keys: ``delta, gamma, vega, theta, rho, vanna, charm``.  All values are
    per-contract and unsigned (long-option convention); scale by position in
    the caller.  Degenerate inputs (``T <= 0``, non-positive spot/strike/vol)
    return a zero-filled dict rather than NaN.
    """
    zero = {
        "delta": 0.0,
        "gamma": 0.0,
        "vega": 0.0,
        "theta": 0.0,
        "rho": 0.0,
        "vanna": 0.0,
        "charm": 0.0,
    }
    if T <= 0 or S <= 0 or K <= 0 or sigma <= 0:
        return dict(zero)

    sigma_t = sigma * math.sqrt(T)
    d1 = (math.log(S / K) + (r - q + 0.5 * sigma * sigma) * T) / sigma_t
    d2 = d1 - sigma_t
    pdf = norm.pdf(d1)
    df_s = math.exp(-q * T)
    df_r = math.exp(-r * T)

    # First order.
    delta = norm.cdf(d1) if is_call else norm.cdf(d1) - 1.0
    gamma = pdf / (S * sigma_t)
    vega = S * df_s * math.sqrt(T) * pdf
    # theta = -dV/dT (r=q=0 term; second term = -r*K*df_r*N(d2) for a call).
    theta = -S * df_s * sigma * pdf / (2.0 * math.sqrt(T))
    if is_call:
        theta -= r * K * df_r * norm.cdf(d2)
    else:
        theta += r * K * df_r * norm.cdf(-d2)
    rho = K * T * df_r * norm.cdf(d2) if is_call else -K * T * df_r * norm.cdf(-d2)

    # Second order.
    # vanna = dVega/dS = dDelta/dsigma (mixed partials commute).
    vanna = df_s * pdf * (math.sqrt(T) - d1 / sigma)
    # charm = dVega/dT.  d1's T-partial here already folds r-q+sigma^2/2.
    d1_dT = (r - q + 0.5 * sigma * sigma) / sigma_t - (
        math.log(S / K) + (r - q + 0.5 * sigma * sigma) * T
    ) / (2.0 * sigma * T * math.sqrt(T))
    charm = (
        S
        * df_s
        * pdf
        * (1.0 / (2.0 * math.sqrt(T)) - q * math.sqrt(T) - math.sqrt(T) * d1 * d1_dT)
    )

    return {
        "delta": delta,
        "gamma": gamma,
        "vega": vega,
        "theta": theta,
        "rho": rho,
        "vanna": vanna,
        "charm": charm,
    }


def dollar_gamma(S: float, book_gamma: float) -> float:
    """Dollar gamma (Gamma Exposure of a position book) = ``S * gamma``.

    Provided as the canonical first-order GEX of an *actual* option book:
    the $-change in book value for a 1 % spot move is ``0.01 * S * gamma``.
    Market GEX (gamma-weighted open interest) is a different, chain-level
    construct and lives in the analytics layer.
    """
    return S * book_gamma


__all__ = ["dollar_gamma", "greeks"]
