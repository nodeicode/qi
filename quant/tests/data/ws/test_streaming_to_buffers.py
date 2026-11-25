"""Live subscription streaming to buffers (spec `data-access` Live streaming).

Verifies that ``MemBarsAggregator`` fulfills a ``bars`` subscription the stream
way: seeded history from REST/replay, settled bars appended per-subscription
(no per-bar re-fetch/parse), partial bars never served, and the stream buffer
carries the identical typed shape as the backtest replay of the same rows.
"""

from __future__ import annotations

import pandas as pd

from quant.backtest.backtest_data_handler import BacktestDataHandler
from quant.data import BARS_COLUMNS, Datatype, Subscription
from quant.data.ws import MemBarsAggregator
from quant.instruments import EquityInstrument

_SPY = EquityInstrument(symbol="SPY", venue="paper")
SUB_1H = Subscription(instrument=_SPY, datatype=Datatype.BARS, cadence="1h")
SUB_1D = Subscription(instrument=_SPY, datatype=Datatype.BARS, cadence="1d")


def _bars(idx: pd.DatetimeIndex, close=100.0) -> pd.DataFrame:
    return pd.DataFrame(
        {
            "Open": close - 1.0,
            "High": close + 1.0,
            "Low": close - 2.0,
            "Close": [close + 0.5] * len(idx),
            "Volume": [1_000_000] * len(idx),
        },
        index=idx,
    )


def test_seed_then_stream_edge_fulfills_buffer() -> None:
    agg = MemBarsAggregator()
    idx = pd.date_range("2024-01-02 14:00", periods=3, freq="1h")  # UTC = ET+5
    agg.seed_subscription(SUB_1H, _bars(idx))

    seeded = agg.subscription_window(SUB_1H, idx[0], idx[-1])
    assert list(seeded.columns) == list(BARS_COLUMNS)
    assert seeded.index.tz is None
    assert list(seeded.index) == list(idx)

    # The live stream supplies the edge as a settled bar beyond the seed.
    edge = idx[-1] + pd.Timedelta(hours=1)
    agg.append_settled(
        SUB_1H, timestamp=edge, open=100, high=103, low=97, close=102, volume=2_000_000
    )
    after = agg.subscription_window(SUB_1H, idx[0], edge)
    assert after.index[-1] == edge
    assert len(after) == 4

    # Stale/duplicate appends never regress the buffer tail (idempotent).
    agg.append_settled(
        SUB_1H, timestamp=edge, open=999, high=999, low=999, close=999, volume=0
    )
    reread = agg.subscription_window(SUB_1H, idx[0], edge)
    assert len(reread) == 4
    assert reread["Close"].iloc[-1] == 102


def test_partial_bars_never_served() -> None:
    agg = MemBarsAggregator()
    idx = pd.date_range("2024-01-02 14:00", periods=2, freq="1h")
    agg.seed_subscription(SUB_1H, _bars(idx))
    # A not-yet-settled future hour must not appear in reads.
    window = agg.subscription_window(SUB_1H, idx[0], idx[-1] + pd.Timedelta(hours=1))
    assert len(window) == 2


def test_stream_and_replay_share_identical_shape() -> None:
    idx = pd.date_range("2024-01-02", periods=3, freq="D")  # daily naive UTC
    df = _bars(idx)

    # backtest root: replay of the pre-loaded daily frame. The handler trims the
    # still-incomplete trailing daily bar, returning the completed rows.
    backtest = BacktestDataHandler(df, df, "1d", "1d").get_data(SUB_1D, idx[0], idx[-1])

    # live root: seeded stream buffer holding the same completed rows, plus a
    # settled edge bar the backtest cannot yet see.
    agg = MemBarsAggregator()
    agg.seed_subscription(SUB_1D, df)
    edge = idx[-1] + pd.Timedelta(days=1)
    agg.append_settled(
        SUB_1D, timestamp=edge, open=100, high=103, low=97, close=102, volume=2
    )
    live = agg.subscription_window(SUB_1D, idx[0], idx[-1])

    # identical typed shape on the shared completed rows: same canonical columns,
    # both naive-UTC datetimes, and identical OHLCV history.
    assert list(live.columns) == list(backtest.columns) == list(BARS_COLUMNS)
    assert live.index.tz is None and backtest.index.tz is None
    assert isinstance(live.index, pd.DatetimeIndex)
    assert isinstance(backtest.index, pd.DatetimeIndex)
    shared = live.iloc[: len(backtest)]
    assert list(shared.index) == list(backtest.index)
    assert (shared.to_numpy(dtype=float) == backtest.to_numpy(dtype=float)).all()
