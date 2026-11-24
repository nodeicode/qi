"""Tests for quant.core._utils standalone helper functions.

Covers:
  - fetch_data — basic fetch, None-handler guard, daily incomplete-bar trim
  - fetch_manage_data — same-interval reuse path, different-interval fetch path,
    no-signal_df path
  - inject_indicators — indicator injection, no-indicator fallback
  - call_manage_trade — successful call, exception → returns -1
"""

from __future__ import annotations

import datetime
from types import SimpleNamespace

import pandas as pd
import pytest

from quant.backtest.backtest_data_handler import BacktestDataHandler
from quant.core._utils import (
    _trim_incomplete_daily_bar,
    attach_raw_close,
    call_manage_trade,
    fetch_data,
    fetch_manage_data,
    inject_indicators,
    select_signal_row,
)
from quant.core.protocols import PositionDict
from quant.utils.time import (
    _interval_floor_freq,
    _interval_timedelta,
    bar_close_utc,
    closed_bar_asof,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_config(**overrides):
    defaults = {
        "INTERVAL": "1h",
        "MANAGE_INTERVAL": "1h",
    }
    defaults.update(overrides)
    return SimpleNamespace(**defaults)


def _make_ohlcv(n=5, tz="UTC"):
    idx = pd.date_range("2024-01-02 14:30", periods=n, freq="1h", tz=tz)
    return pd.DataFrame(
        {"Open": 100.0, "High": 101.0, "Low": 99.0, "Close": 100.5, "Volume": 1000},
        index=idx,
    )


def _mock_handler(df):
    h = SimpleNamespace()
    h.get_historical_data = lambda **kw: df
    return h


def _make_strategy():
    ns = SimpleNamespace()
    ns.calculate_indicators = lambda df: df
    ns.generate_signals = lambda df: df
    ns.manage_trade = lambda pos, df: -1
    return ns


# ========================================================================
# fetch_data
# ========================================================================


class TestFetchData:
    def test_basic_fetch_returns_df(self):
        df = _make_ohlcv()
        handler = _mock_handler(df)
        end = datetime.datetime(2024, 1, 10, tzinfo=datetime.UTC)
        result = fetch_data(handler, "SPY", end, 5, "1h")
        assert not result.empty

    def test_none_handler_returns_empty_dataframe(self):
        end = datetime.datetime(2024, 1, 10, tzinfo=datetime.UTC)
        result = fetch_data(None, "SPY", end, 5, "1h")
        assert isinstance(result, pd.DataFrame)
        assert result.empty

    def test_daily_fetch_trims_last_bar_before_market_close(self):
        idx = pd.DatetimeIndex(
            [
                "2026-06-16 04:00:00+00:00",
                "2026-06-17 04:00:00+00:00",
                "2026-06-18 04:00:00+00:00",
            ]
        )
        df = pd.DataFrame({"Close": [750.33, 740.96, 742.10]}, index=idx)
        handler = _mock_handler(df)
        end = datetime.datetime(2026, 6, 18, 14, 5, tzinfo=datetime.UTC)
        result = fetch_data(handler, "SPY", end, 30, "1d")
        assert len(result) == 2
        assert result.index[-1] == pd.Timestamp("2026-06-17 04:00:00+00:00")

    def test_daily_fetch_keeps_last_bar_after_market_close(self):
        idx = pd.DatetimeIndex(
            [
                "2026-06-16 04:00:00+00:00",
                "2026-06-17 04:00:00+00:00",
                "2026-06-18 04:00:00+00:00",
            ]
        )
        df = pd.DataFrame({"Close": [750.33, 740.96, 742.10]}, index=idx)
        handler = _mock_handler(df)
        end = datetime.datetime(2026, 6, 18, 21, 5, tzinfo=datetime.UTC)
        result = fetch_data(handler, "SPY", end, 30, "1d")
        assert len(result) == 3


# ========================================================================
# fetch_manage_data
# ========================================================================


class TestFetchManageData:
    def test_same_interval_reuses_signal_path(self):
        """MANAGE_INTERVAL == INTERVAL — reuses signal_df, no extra fetch."""
        df = _make_ohlcv()
        handler = _mock_handler(df)
        signal_df = pd.DataFrame(
            {"Signal": "BUY", "ATR": 1.0},
            index=pd.date_range("2024-01-02 14:30", periods=5, freq="1h", tz="UTC"),
        )
        end = datetime.datetime(2024, 1, 10, tzinfo=datetime.UTC)
        cfg = _make_config(INTERVAL="1h", MANAGE_INTERVAL="1h")
        result = fetch_manage_data(
            handler, cfg, "SPY", end, 5, "1h", signal_df=signal_df
        )
        assert not result.empty

    def test_different_interval_uses_manage_interval(self):
        """MANAGE_INTERVAL != INTERVAL → separate fetch at manage interval."""
        cfg = _make_config(INTERVAL="1d", MANAGE_INTERVAL="1h")
        df = _make_ohlcv()
        handler = _mock_handler(df)
        end = datetime.datetime(2024, 1, 10, tzinfo=datetime.UTC)
        result = fetch_manage_data(handler, cfg, "SPY", end, 5, "1d")
        assert not result.empty

    def test_no_signal_df_skips_injection(self):
        """When signal_df is None, no indicator injection occurs."""
        df = _make_ohlcv()
        handler = _mock_handler(df)
        end = datetime.datetime(2024, 1, 10, tzinfo=datetime.UTC)
        result = fetch_manage_data(handler, _make_config(), "SPY", end, 5, "1h")
        assert "ATR" not in result.columns


# ========================================================================
# inject_indicators  (used internally by prepare_manage_data logic)
# ========================================================================


class TestInjectIndicators:
    def test_with_signal_df_injects_indicators(self):
        manage_df = _make_ohlcv()
        signal_df = pd.DataFrame(
            {"ATR": 2.5},
            index=pd.date_range("2024-01-02 14:30", periods=5, freq="1h", tz="UTC"),
        )
        result = inject_indicators(
            manage_df, signal_df, _make_config(), indicator_columns=["ATR"]
        )
        assert "ATR" in result.columns

    def test_empty_default_skips_injection_adds_raw_close(self):
        """DEFAULT_INDICATOR_COLUMNS is [] → no inject, Raw_Close added."""
        manage_df = _make_ohlcv()
        signal_df = pd.DataFrame(
            {"ATR": 2.5},
            index=pd.date_range("2024-01-02 14:30", periods=5, freq="1h", tz="UTC"),
        )
        result = inject_indicators(manage_df, signal_df, _make_config())
        assert "ATR" not in result.columns
        assert "Raw_Close" in result.columns

    def test_no_valid_indicator_cols_returns_copy_with_raw_close(self):
        """Requested cols absent from signal_df → Raw_Close added."""
        manage_df = _make_ohlcv()
        signal_df = _make_ohlcv()  # only OHLCV cols
        result = inject_indicators(
            manage_df, signal_df, _make_config(), indicator_columns=["ATR"]
        )
        assert "Raw_Close" in result.columns


# ========================================================================
# call_manage_trade
# ========================================================================


class TestCallManageTrade:
    def test_successful_call_returns_exit_idx(self):
        """strategy.manage_trade returning a value is passed through."""
        strategy = _make_strategy()
        strategy.manage_trade = lambda pos, df: 3
        pos: PositionDict = {"Ticker": "SPY", "EntryPrice": 100.0}
        result = call_manage_trade(strategy, pos, _make_ohlcv())
        assert result == 3

    def test_exception_returns_minus_one(self):
        """strategy.manage_trade raising → call_manage_trade returns -1."""
        strategy = _make_strategy()

        def bad_manage(pos, df):
            raise RuntimeError("oops")

        strategy.manage_trade = bad_manage
        pos: PositionDict = {"Ticker": "SPY", "EntryPrice": 100.0}
        result = call_manage_trade(strategy, pos, _make_ohlcv())
        assert result == -1


# ========================================================================
# attach_raw_close
# ========================================================================


class TestAttachRawClose:
    def test_primary_raw_copies_close(self):
        df = _make_ohlcv()
        out = attach_raw_close(
            df, None, "SPY", "2024-01-01", "2024-01-10", "1h", primary_adjustment="raw"
        )
        assert (out["Raw_Close"] == out["Close"]).all()

    def test_adjusted_primary_uses_raw_fetch(self):
        adj = _make_ohlcv()
        adj["Close"] = 100.0
        raw = _make_ohlcv()
        raw["Close"] = 105.0  # tradable ≠ adjusted

        calls = []

        def _get(**kw):
            calls.append(kw)
            return raw if kw.get("adjustment") == "raw" else adj

        handler = SimpleNamespace(get_historical_data=_get)
        out = attach_raw_close(
            adj,
            handler,
            "SPY",
            "2024-01-01",
            "2024-01-10",
            "1h",
            primary_adjustment="all",
        )
        assert (out["Raw_Close"] == 105.0).all()
        assert any(c.get("adjustment") == "raw" for c in calls)


# ========================================================================
# Interval helpers — invalid / fallback inputs
# ========================================================================


class TestIntervalHelpers:
    def test_interval_timedelta_invalid_falls_back_to_60min(self):
        assert _interval_timedelta("") == datetime.timedelta(minutes=60)
        assert _interval_timedelta("15x") == datetime.timedelta(minutes=60)

    def test_interval_floor_freq_daily_and_unknown_unit(self):
        assert _interval_floor_freq("1d") == "1D"
        assert _interval_floor_freq("15s") == "60min"


# ========================================================================
# closed_bar_asof / bar_close_utc — naive timestamp paths
# ========================================================================


class TestBarAsofNaivePaths:
    def test_closed_bar_asof_daily_naive_before_close_uses_previous_day(self):
        # Naive input is treated as UTC wall time: 10:00 UTC = 05:00 ET
        # < 16:00, so the daily session is not yet closed.
        assert closed_bar_asof("2025-01-03 10:00", "1d") == pd.Timestamp("2025-01-02")

    def test_bar_close_utc_daily_naive_localizes_session_to_et(self):
        # Daily close = 16:00 ET on the session date, expressed in UTC.
        result = bar_close_utc("2025-01-03", "1d")
        assert result == pd.Timestamp("2025-01-03 21:00:00+00:00")


# ========================================================================
# select_signal_row — tz alignment and duplicate-index paths
# ========================================================================


class TestSelectSignalRow:
    def test_naive_index_resolves_naive_ts(self):
        df = pd.DataFrame(
            {"Signal": ["HOLD"], "Confidence": [0.5]},
            index=pd.DatetimeIndex(["2024-01-02 14:00"]),
        )
        resolved = select_signal_row(df, "2024-01-02 14:00")
        assert resolved is not None
        assert resolved[0] == "HOLD"

    def test_aware_ts_not_auto_aligned(self):
        # Cross-tz auto-alignment was removed (canonical naive-UTC contract):
        # callers resolve to canonical before selection.
        df = pd.DataFrame(
            {"Signal": ["HOLD"], "Confidence": [0.5]},
            index=pd.DatetimeIndex(["2024-01-02 14:00"]),
        )
        assert select_signal_row(df, "2024-01-02T14:00:00+00:00") is None

    def test_duplicate_index_uses_last_row(self):
        idx = pd.DatetimeIndex(["2024-01-02 14:00", "2024-01-02 14:00"])
        df = pd.DataFrame(
            {"Signal": ["HOLD", "BUY"], "Confidence": [0.1, 0.9]}, index=idx
        )
        selected = select_signal_row(df, "2024-01-02 14:00")
        assert selected is not None
        signal, confidence, _ = selected
        assert signal == "BUY"
        assert confidence == 0.9


# ========================================================================
# fetch_data / _trim_incomplete_daily_bar — error + naive paths
# ========================================================================


class TestFetchDataEdgePaths:
    def test_type_error_not_about_adjustment_propagates(self):
        def _bad(**kw):
            raise TypeError("unrelated signature error")

        handler = SimpleNamespace(get_historical_data=_bad)
        with pytest.raises(TypeError, match="unrelated signature error"):
            fetch_data(handler, "SPY", pd.Timestamp("2024-01-02 15:00"), 5, "1h")

    def test_trim_short_daily_frame_unchanged(self):
        df = _make_ohlcv(1)
        out = _trim_incomplete_daily_bar(df, "2025-01-02 10:00")
        assert len(out) == 1

    def test_trim_naive_daily_before_close_drops_last_bar(self):
        idx = pd.DatetimeIndex(["2025-01-02", "2025-01-03"])
        df = pd.DataFrame({"Open": 100.0, "Close": 101.0, "Volume": 1000}, index=idx)
        out = _trim_incomplete_daily_bar(df, "2025-01-03 10:00")
        assert list(out.index) == [pd.Timestamp("2025-01-02")]


# ========================================================================
# attach_raw_close — fallback and alignment edge paths
# ========================================================================


class TestAttachRawCloseFallbacks:
    def test_missing_handler_uses_close(self):
        df = _make_ohlcv()
        out = attach_raw_close(
            df, None, "SPY", "2024-01-01", "2024-01-10", "1h", primary_adjustment="all"
        )
        assert (out["Raw_Close"] == out["Close"]).all()

    def test_raw_fetch_generic_exception_uses_close(self, caplog):
        def _boom(**kw):
            raise RuntimeError("provider down")

        handler = SimpleNamespace(get_historical_data=_boom)
        df = _make_ohlcv()
        out = attach_raw_close(df, handler, "SPY", "2024-01-01", "2024-01-10", "1h")
        assert (out["Raw_Close"] == out["Close"]).all()

    def test_aligns_raw_naive_to_out_naive(self):
        df = _make_ohlcv(tz=None)
        raw = _make_ohlcv(tz=None)
        raw["Close"] = 105.0
        handler = SimpleNamespace(get_historical_data=lambda **kw: raw)
        out = attach_raw_close(df, handler, "SPY", "2024-01-02", "2024-01-02", "1h")
        assert (out["Raw_Close"] == 105.0).all()

    def test_mixed_tz_falls_back_to_close(self):
        # Cross-tz index alignment was removed; mixed tz is a contract
        # violation and falls back to ``Close`` rather than silently aligning.
        df = _make_ohlcv(tz=None)
        raw = _make_ohlcv(tz="UTC")
        raw["Close"] = 105.0
        handler = SimpleNamespace(get_historical_data=lambda **kw: raw)
        out = attach_raw_close(df, handler, "SPY", "2024-01-02", "2024-01-02", "1h")
        assert (out["Raw_Close"] == out["Close"]).all()

    def test_duplicate_raw_index_alignment_failure_uses_close(self, caplog):
        idx = pd.DatetimeIndex(["2024-01-02 14:00", "2024-01-02 14:00"])
        raw = pd.DataFrame(
            {"Close": [105.0, 106.0]},
            index=idx,
        )
        handler = SimpleNamespace(get_historical_data=lambda **kw: raw)
        df = _make_ohlcv(1, tz=None)
        out = attach_raw_close(df, handler, "SPY", "2024-01-02", "2024-01-02", "1h")
        assert (out["Raw_Close"] == df["Close"]).all()
