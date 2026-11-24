"""Unit tests for the closed-form BS greeks + IV primitives.

These lock in the numerics used by ``OptionDataHandler.get_greeks_at``:
  * IV round-trips (bs_price -> iv_from_price -> 0.20)
  * greeks match finite-difference derivatives of bs_price
  * degenerate inputs produce 0.0, never NaN
  * put/call symmetry of gamma/vega/vanna/charm
  * dollar gamma (GEX) helper
"""

from __future__ import annotations

import numpy as np
import pytest

from quant.utils.bs_greeks import dollar_gamma, greeks
from quant.utils.bs_iv import bs_price, iv_from_price

_S, _K, _T, _SIG = 585.0, 585.0, 5.0 / 24.0 / 365.0, 0.20


def test_iv_roundtrip():
    for call in (True, False):
        for k in (578.0, 585.0, 592.0):
            px = bs_price(_S, k, _T, 0.20, call)
            iv = iv_from_price(_S, k, _T, px, call)
            assert iv == pytest.approx(0.20, abs=1e-6), (call, k, iv)


def test_greeks_match_finite_differences():
    h = 2e-5
    hv = 1e-4
    ht = 1e-6
    for call in (True, False):
        g = greeks(_S, _K, _T, _SIG, call)
        # delta = dV/dS
        est_delta = (
            bs_price(_S + h, _K, _T, _SIG, call) - bs_price(_S - h, _K, _T, _SIG, call)
        ) / (2 * h)
        assert g["delta"] == pytest.approx(est_delta, abs=1e-5)
        # gamma = d2V/dS2
        est_gamma = (
            bs_price(_S + h, _K, _T, _SIG, call)
            - 2 * bs_price(_S, _K, _T, _SIG, call)
            + bs_price(_S - h, _K, _T, _SIG, call)
        ) / (h * h)
        assert g["gamma"] == pytest.approx(est_gamma, abs=1e-3)
        # vega = dV/dvol
        est_vega = (
            bs_price(_S, _K, _T, _SIG + hv, call)
            - bs_price(_S, _K, _T, _SIG - hv, call)
        ) / (2 * hv)
        assert g["vega"] == pytest.approx(est_vega, abs=1e-6)
        # vanna = dVega/dS
        est_vanna = (
            greeks(_S + h, _K, _T, _SIG, call)["vega"]
            - greeks(_S - h, _K, _T, _SIG, call)["vega"]
        ) / (2 * h)
        assert g["vanna"] == pytest.approx(est_vanna, abs=1e-6)
        # charm = dVega/dT
        est_charm = (
            greeks(_S, _K, _T + ht, _SIG, call)["vega"]
            - greeks(_S, _K, _T - ht, _SIG, call)["vega"]
        ) / (2 * ht)
        assert g["charm"] == pytest.approx(est_charm, abs=2e-2)


def test_put_call_symmetry_second_order():
    """gamma/vega/vanna/charm are moneyness-symmetric per unit."""
    c = greeks(_S, _K, _T, _SIG, True)
    p = greeks(_S, _K, _T, _SIG, False)
    for k in ("gamma", "vega", "vanna", "charm"):
        assert c[k] == pytest.approx(p[k], abs=1e-12)
    # delta differs by 1 (call delta - put delta = 1)
    assert c["delta"] - p["delta"] == pytest.approx(1.0, abs=1e-9)


def test_degenerate_inputs_return_zero():
    z = {
        "delta": 0.0,
        "gamma": 0.0,
        "vega": 0.0,
        "theta": 0.0,
        "rho": 0.0,
        "vanna": 0.0,
        "charm": 0.0,
    }
    assert greeks(0.0, _K, _T, _SIG, True) == z
    assert greeks(_S, 0.0, _T, _SIG, True) == z
    assert greeks(_S, _K, 0.0, _SIG, True) == z
    assert greeks(_S, _K, _T, 0.0, True) == z
    assert bs_price(_S, _K, 0.0, _SIG, True) == 0.0
    assert bs_price(0.0, _K, _T, _SIG, True) == 0.0
    assert iv_from_price(_S, _K, 0.0, 1.0, True) == 0.0
    # Arbitrage-high price (above intrinsic+max-vol) => no positive solution
    assert iv_from_price(_S, _K, _T, 1e6, True) == 0.0
    # Intrinsic-floor price has no positive vol
    assert iv_from_price(_S, _K, _T, _S - _K if _S > _K else 0.0, True) == 0.0


def test_dollar_gamma():
    g = greeks(_S, _K, _T, _SIG, True)
    assert dollar_gamma(_S, g["gamma"]) == pytest.approx(_S * g["gamma"])
    # Short book => negative GEX
    assert dollar_gamma(_S, -g["gamma"]) < 0.0


def test_theta_sign():
    """Convention: theta = -dV/dT, so a LONG option has theta < 0.

    Time value grows as T increases (dV/dT > 0), hence theta is negative
    for a long.  A SHORT book (position = -1) collects positive theta —
    this matches ``OptionDataHandler.get_greeks_at`` (short IC book
    ``book["theta"] > 0``).
    """
    g = greeks(_S, _K, _T, _SIG, True)
    assert g["theta"] < 0.0
    assert g["theta"] * -1.0 > 0.0  # short position collects theta
