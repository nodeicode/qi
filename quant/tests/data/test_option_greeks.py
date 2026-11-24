"""Tests for OptionDataHandler.get_greeks_at (point-in-time, as-of pattern).

Covers:
  * cache-only as-of greeks: seeded CBBO cache → per-leg greeks + book
    GEX/vanna/charm at a single timestamp, zero provider fetches
  * short iron condor book signs (negative gamma/vega, GEX < 0)
  * IV round-trip: seeded mids priced at iv=0.20 invert back to ~0.20
  * missing quote → leg flagged iv_ok=False, book sums only valid legs
  * allow_network=False never touches the provider
"""

from __future__ import annotations

import datetime

import numpy as np
import pandas as pd
import pytest

from quant.utils.bs_iv import bs_price

_T = 5.75 / 24.0 / 365.0  # 10:15 ET → 16:00 ET settlement, 0-DTE


@pytest.fixture(autouse=True)
def _stub_env(monkeypatch):
    monkeypatch.setenv("DATABENTO_API_KEY", "test_databento_key")


@pytest.fixture()
def _patch_clients(monkeypatch):
    monkeypatch.setattr(
        "quant.data.options_data_handler.db.Historical", type("M", (), {})
    )


def _handler(tmp_path):
    from quant.data.options_data_handler import OptionDataHandler

    return OptionDataHandler(cache_dir=str(tmp_path / "cache"))


def _seed_cbbo(h, ticker, legs, as_of, spot, iv=0.20, spread=0.001):
    """Seed the CBBO cache with synthetic two-sided quotes priced at ``iv``.

    ``spread`` is small (0.001) so deep-OTM wings (tiny mids) keep a
    non-negative bid and stay valid under the handler's two-sided contract.
    """
    for leg in legs:
        is_call = str(leg["opt_type"]).upper() in ("C", "CALL")
        px = bs_price(spot, leg["strike"], _T, iv, is_call)
        df = pd.DataFrame(
            {"bid": [px - spread / 2], "ask": [px + spread / 2], "mid": [px]},
            index=pd.DatetimeIndex([as_of - datetime.timedelta(seconds=30)], name="ts"),
        )
        h._cache.put(
            ticker,
            leg["strike"],
            leg["expiration"],
            leg["opt_type"],
            as_of - datetime.timedelta(seconds=60),
            as_of + datetime.timedelta(milliseconds=1),
            "cbbo-1m",
            df,
        )


def _ic_legs(spot):
    return [
        {
            "name": "lp",
            "strike": spot - 7,
            "expiration": "2024-10-18",
            "opt_type": "PUT",
            "position": +1.0,
        },
        {
            "name": "sp",
            "strike": spot - 5,
            "expiration": "2024-10-18",
            "opt_type": "PUT",
            "position": -1.0,
        },
        {
            "name": "sc",
            "strike": spot + 5,
            "expiration": "2024-10-18",
            "opt_type": "CALL",
            "position": -1.0,
        },
        {
            "name": "lc",
            "strike": spot + 7,
            "expiration": "2024-10-18",
            "opt_type": "CALL",
            "position": +1.0,
        },
    ]


def test_get_greeks_at_cache_only(tmp_path, _patch_clients):
    h = _handler(tmp_path)
    as_of = pd.Timestamp("2024-10-18 14:15:00", tz="UTC")  # 10:15 ET
    spot = 585.0
    legs = _ic_legs(spot)
    _seed_cbbo(h, "SPY", legs, as_of, spot, iv=0.20)

    res = h.get_greeks_at("SPY", spot, as_of, legs, allow_network=False)

    assert res["as_of"] == as_of
    assert res["spot"] == spot
    assert res["meta"]["n_quoted"] == 4
    assert res["meta"]["n_iv_ok"] == 4
    assert res["meta"]["n_cache_hits"] == 4
    assert res["meta"]["n_provider_fetches"] == 0

    # IV round-trips to ~0.20 from the seeded mids.
    for name, leg in res["legs"].items():
        assert leg["iv_ok"] is True
        assert abs(leg["iv"] - 0.20) < 0.02, (name, leg["iv"])
        assert leg["source"] == "cache"
        assert leg["quote_age_seconds"] == pytest.approx(30.0, abs=1.0)

    # Short IC book: negative gamma/vega, GEX < 0; vanna/charm present.
    book = res["book"]
    assert book["gamma"] < 0.0
    assert book["vega"] < 0.0
    assert book["GEX"] < 0.0
    assert book["|GEX|"] == abs(book["GEX"])
    assert "vanna" in book and "charm" in book
    assert book["theta"] > 0.0  # short book collects theta


def test_get_greeks_at_missing_quote_flagged(tmp_path, _patch_clients):
    h = _handler(tmp_path)
    as_of = pd.Timestamp("2024-10-18 14:15:00", tz="UTC")
    spot = 585.0
    legs = _ic_legs(spot)
    # Seed only 3 of 4 legs; the 4th has no cache coverage.
    _seed_cbbo(h, "SPY", legs[:3], as_of, spot, iv=0.20)

    res = h.get_greeks_at("SPY", spot, as_of, legs, allow_network=False)

    assert res["meta"]["n_quoted"] == 3
    assert res["meta"]["n_iv_ok"] == 3
    assert res["legs"]["lc"]["iv_ok"] is False
    assert res["legs"]["lc"]["mid"] is None
    # Book still sums the 3 valid legs (no NaN).
    assert np.isfinite(res["book"]["gamma"])


def test_get_greeks_at_naive_utc_as_of(tmp_path, _patch_clients):
    """Naive UTC timestamps are treated as UTC (handler convention)."""
    h = _handler(tmp_path)
    as_of = pd.Timestamp("2024-10-18 14:15:00")  # naive
    spot = 585.0
    legs = _ic_legs(spot)
    _seed_cbbo(h, "SPY", legs, as_of, spot, iv=0.20)

    res = h.get_greeks_at("SPY", spot, as_of, legs, allow_network=False)
    assert res["meta"]["n_iv_ok"] == 4
    assert res["as_of"].tzinfo is not None  # normalized to UTC-aware
