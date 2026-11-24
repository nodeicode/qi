"""BacktestDataHandler trims incomplete intraday bars at end_dt."""

from __future__ import annotations

import pandas as pd

from quant.backtest.backtest_data_handler import BacktestDataHandler
from quant.utils.time import closed_bar_asof


def _ohlc(idx: pd.DatetimeIndex) -> pd.DataFrame:
    return pd.DataFrame(
        {
            "Open": 1.0,
            "High": 1.0,
            "Low": 1.0,
            "Close": 1.0,
            "Volume": 1.0,
        },
        index=idx,
    )


def test_manage_excludes_incomplete_bar_at_end_dt():
    # 15m bars; at end=10:00 the 10:00 bar is still open → last closed is 09:45.
    idx = pd.date_range("2024-06-03 13:30", periods=6, freq="15min")  # UTC = ET+4
    # 09:30, 09:45, 10:00, 10:15, 10:30, 10:45 ET
    df = _ohlc(idx)
    h = BacktestDataHandler(df, df, "15m", "15m")
    end = pd.Timestamp("2024-06-03 14:00:00")  # 10:00 ET
    out = h.get_historical_data("SPY", idx[0], end, "15m")
    last_closed = closed_bar_asof(end, "15m")
    assert out.index[-1] == last_closed
    assert pd.Timestamp("2024-06-03 14:00:00") not in out.index


def test_manage_mid_bar_excludes_open_stamp():
    idx = pd.date_range("2024-06-03 13:30", periods=6, freq="15min")
    df = _ohlc(idx)
    h = BacktestDataHandler(df, df, "15m", "15m")
    # 10:07 ET = 14:07 UTC — 10:00 bar still open
    end = pd.Timestamp("2024-06-03 14:07:00")
    out = h.get_historical_data("SPY", idx[0], end, "15m")
    assert out.index[-1] == closed_bar_asof(end, "15m")
    assert pd.Timestamp("2024-06-03 14:00:00") not in out.index
