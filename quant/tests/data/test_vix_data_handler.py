"""
Tests for quant.data.vix_data_handler.VixDailyDataHandler.

No network: the live FRED fetch is replaced by a canned series (and one
failure-mode subclass).  The strict-before-``end`` availability semantics are
the contract under test — a decision on date D must never see D's own (or any
future) VIXCLS close.
"""

from __future__ import annotations

import pandas as pd
import pytest

from quant.data.vix_data_handler import VixDailyDataHandler, VixDataView


class StubVix(VixDailyDataHandler):
    """Handler variant whose fetch returns a deterministic canned series."""

    n_fetches = 0

    def __init__(self, closes: dict[str, float] | None = None):
        super().__init__()
        if closes is None:
            closes = {f"2026-01-{d:02d}": float(d) for d in range(1, 11)}
        self._canned = pd.Series(
            list(closes.values()),
            index=pd.to_datetime(list(closes.keys())),
            name="vix",
        ).sort_index()

    def _from_fred(self) -> pd.Series:
        type(self).n_fetches += 1
        return self._canned.copy()


class EmptyVix(VixDailyDataHandler):
    def _from_fred(self) -> pd.Series:
        return pd.Series(dtype=float)


@pytest.fixture(autouse=True)
def _reset_fetch_counter():
    StubVix.n_fetches = 0
    yield


def test_protocol_conformance():
    assert isinstance(StubVix(), VixDataView)
    assert isinstance(VixDailyDataHandler(), VixDataView)


def test_ticker_validation():
    h = StubVix()
    for tick in ("VIX", "VIXCLS"):
        assert not h.get_historical_data(tick).empty
    for bad in ("SPY", "vix", ""):
        with pytest.raises(ValueError):
            h.get_historical_data(bad)
    with pytest.raises(ValueError):
        h.get_historical_data(None)  # type: ignore[arg-type]


def test_strict_before_end_excludes_end_date():
    """end=D must not serve D's own close (no lookahead)."""
    h = StubVix()
    df = h.get_historical_data("VIX", end="2026-01-05")
    assert (
        list(df.index)
        == pd.to_datetime(
            ["2026-01-01", "2026-01-02", "2026-01-03", "2026-01-04"]
        ).tolist()
    )


def test_midday_end_excludes_same_day_close():
    """A mid-day decision on D must not see D's close (published after D)."""
    h = StubVix()
    df = h.get_historical_data("VIX", end="2026-01-05 10:00:00")
    assert "2026-01-05" not in df.index.astype(str).tolist()
    assert df.index.max().strftime("%Y-%m-%d") == "2026-01-04"


def test_start_filter_inclusive():
    h = StubVix()
    df = h.get_historical_data("VIX", start="2026-01-03", end="2026-01-06")
    assert (
        list(df.index)
        == pd.to_datetime(["2026-01-03", "2026-01-04", "2026-01-05"]).tolist()
    )


def test_end_none_returns_full_history():
    h = StubVix()
    df = h.get_historical_data("VIX")
    assert len(df) == 10
    assert df.index.min().strftime("%Y-%m-%d") == "2026-01-01"
    assert df.index.max().strftime("%Y-%m-%d") == "2026-01-10"


def test_values_and_column_name():
    h = StubVix()
    df = h.get_historical_data("VIX", start="2026-01-02", end="2026-01-04")
    assert list(df.columns) == ["vix"]
    assert float(df.loc["2026-01-02", "vix"]) == 2.0
    assert float(df.loc["2026-01-03", "vix"]) == 3.0


def test_in_memory_caching_single_fetch():
    h = StubVix()
    h.get_historical_data("VIX", end="2026-01-03")
    h.get_historical_data("VIX", end="2026-01-02")
    h.get_historical_data("VIX", end="2026-01-10")
    assert StubVix.n_fetches == 1


def test_compat_params_ignored():
    h = StubVix()
    df = h.get_historical_data(
        "VIX",
        start="2026-01-02",
        end="2026-01-05",
        interval="5m",
        use_cache=False,
        adjustment="all",
    )
    assert len(df) == 3


def test_safe_fail_empty_series():
    h = EmptyVix()
    df = h.get_historical_data("VIX", end="2026-01-05")
    assert df.empty
    assert "vix" in df.columns


def test_safe_fail_fetch_error_never_raises(monkeypatch):
    h = VixDailyDataHandler()
    monkeypatch.setattr(
        "quant.data.vix_data_handler.urllib.request.urlopen",
        lambda *a, **k: (_ for _ in ()).throw(RuntimeError("network down")),
    )
    df = h.get_historical_data("VIX", end="2026-01-05")
    assert df.empty


def test_empty_result_is_valid_for_gates():
    h = EmptyVix()
    df = h.get_historical_data("VIX")
    assert df.empty  # strategy gates reject instead of trading on missing VIX


def test_aware_end_timestamp_accepted():
    h = StubVix()
    aware = pd.Timestamp("2026-01-05 10:00:00", tz="UTC")
    df = h.get_historical_data("VIX", end=aware)
    assert df.index.max().strftime("%Y-%m-%d") in ("2026-01-03", "2026-01-04")
    assert "2026-01-05" not in df.index.astype(str).tolist()
