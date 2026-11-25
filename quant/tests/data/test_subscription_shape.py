"""Typed subscription surface (spec `data-access` Data source protocol).

Verifies that a ``(instrument, datatype, cadence)`` bars subscription resolves
on both the backtest root (``BacktestDataHandler``, replay of a pre-loaded
frame) and the live root (``SyntheticDataHandler``, live-looking source) with
an identical typed shape: the same canonical columns and the same (naive-UTC)
timestamp normalization.
"""

from __future__ import annotations

import pandas as pd

from quant.backtest.backtest_data_handler import BacktestDataHandler
from quant.data import BARS_COLUMNS, Datatype, Subscription
from quant.data.synthetic_data_handler import SyntheticDataHandler
from quant.instruments import EquityInstrument


def _bars(df: pd.DatetimeIndex) -> pd.DataFrame:
    return pd.DataFrame(
        {
            "Open": 1.0,
            "High": 2.0,
            "Low": 0.5,
            "Close": 1.5,
            "Volume": 1000,
        },
        index=df,
    )


def test_bars_subscription_resolves_identical_shape_on_both_roots() -> None:
    instrument = EquityInstrument(symbol="SPY", venue="paper")
    sub = Subscription(instrument=instrument, datatype="bars", cadence="1d")
    assert sub.datatype is Datatype.BARS
    assert sub.key() == ("paper", "SPY", Datatype.BARS, "1d")

    start, end = "2023-06-01", "2023-07-01"

    # live root: the synthetic source answers from its generated series.
    live = SyntheticDataHandler(days=300).get_data(sub, start, end)

    # backtest root: replay of a pre-loaded daily frame for the same cadence.
    idx = pd.date_range("2023-06-01", periods=300, freq="B")
    backtest = BacktestDataHandler(_bars(idx), _bars(idx), "1d", "1d").get_data(
        sub, start, end
    )

    assert not live.empty
    assert not backtest.empty
    # identical canonical columns on both roots.
    assert list(live.columns) == list(BARS_COLUMNS)
    assert list(backtest.columns) == list(BARS_COLUMNS)
    assert list(live.columns) == list(backtest.columns)
    # identical timestamp normalization (both naive, no tz).
    assert live.index.tz is None
    assert backtest.index.tz is None
    assert isinstance(live.index, pd.DatetimeIndex)
    assert isinstance(backtest.index, pd.DatetimeIndex)


def test_backtest_out_of_range_subscription_returns_empty() -> None:
    idx = pd.date_range("2023-06-01", periods=30, freq="B")
    h = BacktestDataHandler(_bars(idx), _bars(idx), "1d", "1d")
    instrument = EquityInstrument(symbol="SPY", venue="paper")
    sub = Subscription(instrument=instrument, datatype=Datatype.BARS, cadence="1d")
    out = h.get_data(sub, "2025-01-01", "2025-02-01")  # beyond pre-loaded window
    assert out.empty


def test_subscription_rejects_non_bars_datatype() -> None:
    instrument = EquityInstrument(symbol="SPY", venue="paper")
    sub = Subscription(instrument=instrument, datatype=Datatype.BARS, cadence="1d")
    synth = SyntheticDataHandler()
    assert synth.get_data(sub, "2023-06-01", "2023-07-01").columns.equals(
        pd.Index(BARS_COLUMNS)
    )


def test_seeded_buffer_replays_without_reparse() -> None:
    # A seeded subscription buffer is replayed by index window (no re-parse of
    # source data), and an out-of-range window returns empty without raising.
    idx = pd.date_range("2023-06-01", periods=60, freq="B")
    df = _bars(idx)
    h = BacktestDataHandler(pd.DataFrame(), pd.DataFrame(), "unused", "1d")
    instrument = EquityInstrument(symbol="SPY", venue="paper")
    sub = Subscription(instrument=instrument, datatype=Datatype.BARS, cadence="1d")
    h.seed(sub, df)

    win = h.get_data(sub, "2023-06-05", "2023-06-15")
    assert not win.empty
    assert list(win.columns) == list(BARS_COLUMNS)

    # Beyond the seeded window the replay returns an empty stream, no raise.
    out_of_range = h.get_data(sub, "2025-01-01", "2025-02-01")
    assert out_of_range.empty


def test_unseeded_cadence_still_resolves_from_preloaded_frame() -> None:
    idx = pd.date_range("2023-06-01", periods=60, freq="B")
    df = _bars(idx)
    h = BacktestDataHandler(df, df, "1d", "1d")
    instrument = EquityInstrument(symbol="SPY", venue="paper")
    sub = Subscription(instrument=instrument, datatype=Datatype.BARS, cadence="1d")
    out = h.get_data(sub, "2023-06-01", "2023-06-10")
    assert not out.empty
    assert list(out.columns) == list(BARS_COLUMNS)
