"""Tests for closed_bar_asof / select_signal_row (L0/L2 parity helpers)."""

from __future__ import annotations

import datetime

import pandas as pd
import pytest

from quant.core._utils import select_signal_row
from quant.utils.time import closed_bar_asof


def _ohlcv_at(timestamps, signals=None, confidences=None) -> pd.DataFrame:
    n = len(timestamps)
    df = pd.DataFrame(
        {
            "Open": [100.0] * n,
            "High": [101.0] * n,
            "Low": [99.0] * n,
            "Close": [100.5] * n,
            "Volume": [1000] * n,
            "Signal": signals or ["HOLD"] * n,
            "Confidence": confidences or [0.0] * n,
        },
        index=pd.DatetimeIndex(timestamps, name="timestamp"),
    )
    return df


class TestClosedBarAsof:
    def test_15m_exactly_on_close(self):
        # Bar 10:00 covers [10:00, 10:15); closed at 10:15.
        end = pd.Timestamp("2026-04-27 14:15:00")  # 10:15 ET ≈ 14:15 UTC
        assert closed_bar_asof(end, "15m") == pd.Timestamp("2026-04-27 14:00:00")

    def test_15m_mid_bar(self):
        end = pd.Timestamp("2026-04-27 14:20:00")
        assert closed_bar_asof(end, "15m") == pd.Timestamp("2026-04-27 14:00:00")

    def test_15m_just_before_close(self):
        end = pd.Timestamp("2026-04-27 14:14:59")
        assert closed_bar_asof(end, "15m") == pd.Timestamp("2026-04-27 13:45:00")

    def test_5m(self):
        end = pd.Timestamp("2026-04-27 14:06:00")
        assert closed_bar_asof(end, "5m") == pd.Timestamp("2026-04-27 14:00:00")

    def test_1h(self):
        end = pd.Timestamp("2026-04-27 15:30:00")
        assert closed_bar_asof(end, "1h") == pd.Timestamp("2026-04-27 14:00:00")

    def test_daily_before_close_et_uses_prior_day(self):
        # 14:00 UTC = 10:00 ET — before 16:00 ET → prior calendar day
        end = pd.Timestamp("2026-04-27 14:00:00", tz="UTC")
        assert closed_bar_asof(end, "1d") == pd.Timestamp("2026-04-26")

    def test_daily_after_close_et_uses_today(self):
        # 20:30 UTC = 16:30 ET — after regular close → today's bar
        end = pd.Timestamp("2026-04-27 20:30:00", tz="UTC")
        assert closed_bar_asof(end, "1d") == pd.Timestamp("2026-04-27")


class TestSelectSignalRow:
    def test_exact_match(self):
        ts_1000 = pd.Timestamp("2026-04-27 14:00:00")
        ts_1015 = pd.Timestamp("2026-04-27 14:15:00")
        df = _ohlcv_at(
            [ts_1000, ts_1015],
            signals=["SELL_IC", "HOLD"],
            confidences=[0.85, 0.1],
        )
        result = select_signal_row(df, ts_1000)
        assert result is not None
        signal, conf, row = result
        assert signal == "SELL_IC"
        assert conf == pytest.approx(0.85)
        assert row["_bar_time"] == ts_1000

    def test_missing_bar_returns_none(self):
        df = _ohlcv_at([pd.Timestamp("2026-04-27 14:00:00")])
        assert select_signal_row(df, pd.Timestamp("2026-04-27 14:15:00")) is None

    def test_empty_frame_returns_none(self):
        assert select_signal_row(pd.DataFrame(), pd.Timestamp("2026-04-27")) is None

    def test_never_blind_iloc_last(self):
        """Later HOLD must not shadow an earlier SELL_IC at bar_ts."""
        ts_1000 = pd.Timestamp("2026-04-27 14:00:00")
        ts_1015 = pd.Timestamp("2026-04-27 14:15:00")
        ts_1030 = pd.Timestamp("2026-04-27 14:30:00")
        df = _ohlcv_at(
            [ts_1000, ts_1015, ts_1030],
            signals=["SELL_IC", "HOLD", "HOLD"],
            confidences=[0.9, 0.0, 0.0],
        )
        # Selecting 10:00 must get SELL_IC even though iloc[-1] is HOLD.
        result = select_signal_row(df, ts_1000)
        assert result is not None
        signal, _, _ = result
        assert signal == "SELL_IC"
        # And selecting a later bar must get HOLD (not scan back).
        result2 = select_signal_row(df, ts_1030)
        assert result2 is not None
        signal2, _, _ = result2
        assert signal2 == "HOLD"


class TestParitySelectionLiveVsBt:
    """Same bar_ts selection for live-style full frame and BT-style frame."""

    def test_10_00_sell_ic_selected_when_bar_first_closes(self):
        # 10:00 ET = 14:00 UTC (EDT). Bar closes at 14:15 UTC.
        ts_1000 = pd.Timestamp("2026-04-27 14:00:00")
        ts_1015 = pd.Timestamp("2026-04-27 14:15:00")
        ts_1030 = pd.Timestamp("2026-04-27 14:30:00")
        df = _ohlcv_at(
            [ts_1000, ts_1015, ts_1030],
            signals=["SELL_IC", "HOLD", "HOLD"],
            confidences=[0.88, 0.0, 0.0],
        )

        # First moment the 10:00 bar is closed: end_dt = 14:15
        end_dt = pd.Timestamp("2026-04-27 14:15:00")
        bar_ts = closed_bar_asof(end_dt, "15m")
        assert bar_ts == ts_1000

        live_mode = select_signal_row(df, bar_ts)  # full frame (live)
        bt_mode = select_signal_row(df.loc[[ts_1000]], bar_ts)  # 1-row (legacy BT)

        assert live_mode is not None and bt_mode is not None
        assert live_mode[0] == bt_mode[0] == "SELL_IC"
        assert live_mode[1] == pytest.approx(bt_mode[1])

        # Before close: end_dt = 14:14 → prior bar, not 10:00 SELL_IC
        early = closed_bar_asof(pd.Timestamp("2026-04-27 14:14:00"), "15m")
        assert early != ts_1000
