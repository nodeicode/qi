"""
Tests for quant.data.options_data_handler.OptionDataHandler (Databento-only).

Covers:
  - Symbol formatting (Databento OCC with spaces)
  - Interval → schema mapping & resample detection
  - OHLCV resampling (1m → 5m/15m/30m)
  - DataBento fetch pipeline (column mapping, price_type=float, mid/bid/ask synthesis)
  - Expiration discovery via definition schema
  - CBBO cache helpers and quote extraction
  - Edge cases: empty data, weekend skipping, cache interactions
"""

from __future__ import annotations

import datetime
import os
import tempfile
from types import SimpleNamespace
from unittest.mock import MagicMock, PropertyMock, patch

import numpy as np
import pandas as pd
import pytest


# ---------------------------------------------------------------------------
# Helpers to avoid loading .env / real API keys during import
# ---------------------------------------------------------------------------
@pytest.fixture(autouse=True)
def _stub_env(monkeypatch):
    """Provide dummy API keys so the handler can be imported without real creds."""
    monkeypatch.setenv("DATABENTO_API_KEY", "test_databento_key")


# Patch real clients before importing the handler
@pytest.fixture()
def _patch_clients(monkeypatch):
    """Prevent real DataBento client from initialising."""
    monkeypatch.setattr("quant.data.options_data_handler.db.Historical", MagicMock)


def test_missing_databento_key_does_not_abort_option_handler_init(
    tmp_path, monkeypatch, caplog
):
    monkeypatch.setenv("DATABENTO_API_KEY", "")
    with patch("quant.data.options_data_handler.db.Historical") as historical:
        from quant.data.options_data_handler import OptionDataHandler

        handler = OptionDataHandler(cache_dir=str(tmp_path))

    assert handler._databento_client is None
    historical.assert_not_called()
    assert "option historical client was not initialized" in caplog.text


# ---------------------------------------------------------------------------
# Deterministic test data builders
# ---------------------------------------------------------------------------


def _make_1m_ohlcv_df(
    n_bars: int = 60,
    start: str = "2025-06-20 09:30",
    base_price: float = 5.0,
    tz: str = "UTC",
) -> pd.DataFrame:
    """Create a realistic 1-minute OHLCV DataFrame mimicking Databento to_df output."""
    idx = pd.date_range(start, periods=n_bars, freq="1min", tz=tz)
    rng = np.random.default_rng(42)
    closes = base_price + rng.standard_normal(n_bars).cumsum() * 0.05
    opens = np.roll(closes, 1)
    opens[0] = base_price
    highs = np.maximum(opens, closes) + rng.uniform(0, 0.03, n_bars)
    lows = np.minimum(opens, closes) - rng.uniform(0, 0.03, n_bars)
    vols = rng.integers(10, 500, n_bars).astype(float)
    return pd.DataFrame(
        {"open": opens, "high": highs, "low": lows, "close": closes, "volume": vols},
        index=idx,
    )


def _make_databento_raw_df(
    n_bars: int = 60, start: str = "2025-06-20 09:30"
) -> pd.DataFrame:
    """Simulate a raw DataFrame as returned by ``DBNStore.to_df(price_type="float")``."""
    idx = pd.date_range(start, periods=n_bars, freq="1min", tz="UTC")
    rng = np.random.default_rng(42)
    closes = 5.0 + rng.standard_normal(n_bars).cumsum() * 0.05
    opens = np.roll(closes, 1)
    opens[0] = 5.0
    highs = np.maximum(opens, closes) + rng.uniform(0, 0.03, n_bars)
    lows = np.minimum(opens, closes) - rng.uniform(0, 0.03, n_bars)
    vols = rng.integers(10, 500, n_bars).astype(float)
    df = pd.DataFrame(
        {
            "ts_event": idx,
            "open": opens,
            "high": highs,
            "low": lows,
            "close": closes,
            "volume": vols,
            "symbol": "SPY   250620C00590000",
        }
    )
    # to_df() returns ts_recv-indexed by default; we use ts_event as the column
    df.index = idx
    df.index.name = "ts_recv"
    return df


def _make_definition_df(
    expirations: list[str],
    ticker: str = "SPY",
) -> pd.DataFrame:
    """Simulate a ``definition`` schema DataFrame from Databento."""
    rows = [
        {
            "ts_recv": pd.Timestamp("2025-06-20", tz="UTC"),
            "ts_event": pd.Timestamp("2025-06-20", tz="UTC"),
            "raw_symbol": f"{ticker:<6}{exp_str[2:4]}{exp_str[5:7]}{exp_str[8:10]}{cp}{strike * 1000:08d}",
            "expiration": pd.Timestamp(exp_str, tz="UTC"),
            "instrument_class": cp,
            "strike_price": float(strike),
            "symbol": f"{ticker}.OPT",
        }
        for exp_str in expirations
        for cp in ["C", "P"]
        for strike in [580, 590, 600]
    ]
    df = pd.DataFrame(rows)
    if not df.empty:
        df.index = pd.RangeIndex(len(df))
    return df


# ---------------------------------------------------------------------------
# Fixture: handler instance with mocked Databento client
# ---------------------------------------------------------------------------


@pytest.fixture()
def tmp_cache(tmp_path):
    return str(tmp_path / "option_cache")


@pytest.fixture()
def databento_handler(tmp_cache, _patch_clients):
    from quant.data.options_data_handler import OptionDataHandler

    return OptionDataHandler(cache_dir=tmp_cache)


# ═══════════════════════════════════════════════════════════════════════════
# 1. Symbol formatting
# ═══════════════════════════════════════════════════════════════════════════


class TestSymbolFormatting:
    """Verify Databento symbol formatting."""

    def _handler_cls(self):
        from quant.data.options_data_handler import OptionDataHandler

        return OptionDataHandler

    def test_databento_symbol_call(self, _patch_clients):
        cls = self._handler_cls()
        sym = cls._format_databento_symbol("SPY", 590.0, "2025-12-19", "call")
        assert sym == "SPY   251219C00590000"
        assert len(sym.split(sym[-15:])[0]) == 6  # 6-char padded ticker

    def test_databento_symbol_put(self, _patch_clients):
        cls = self._handler_cls()
        sym = cls._format_databento_symbol("QQQ", 510.0, "2025-06-20", "put")
        assert sym == "QQQ   250620P00510000"

    def test_databento_symbol_long_ticker(self, _patch_clients):
        cls = self._handler_cls()
        sym = cls._format_databento_symbol("GOOGL", 180.0, "2025-03-21", "call")
        # GOOGL is 5 chars → padded to 6 with 1 space
        assert sym.startswith("GOOGL ")
        assert len(sym) == 21  # 6 + 6 + 1 + 8

    def test_databento_symbol_short_ticker(self, _patch_clients):
        cls = self._handler_cls()
        sym = cls._format_databento_symbol("F", 12.0, "2025-03-21", "put")
        assert sym.startswith("F     ")  # padded to 6


# ═══════════════════════════════════════════════════════════════════════════
# 2. Interval mapping
# ═══════════════════════════════════════════════════════════════════════════


class TestIntervalMapping:
    def _handler_cls(self):
        from quant.data.options_data_handler import OptionDataHandler

        return OptionDataHandler

    @pytest.mark.parametrize(
        "interval,expected_schema",
        [
            ("1m", "ohlcv-1m"),
            ("1", "ohlcv-1m"),
            ("5m", "ohlcv-1m"),
            ("15m", "ohlcv-1m"),
            ("30m", "ohlcv-1m"),
            ("1h", "ohlcv-1h"),
            ("60", "ohlcv-1h"),
            ("1d", "ohlcv-1d"),
            ("d", "ohlcv-1d"),
        ],
    )
    def test_interval_to_schema(self, interval, expected_schema, _patch_clients):
        assert self._handler_cls()._interval_to_schema(interval) == expected_schema

    @pytest.mark.parametrize(
        "interval,should_resample",
        [
            ("1m", False),
            ("5m", True),
            ("5", True),
            ("15m", True),
            ("30m", True),
            ("1h", False),
            ("1d", False),
        ],
    )
    def test_needs_resample(self, interval, should_resample, _patch_clients):
        assert self._handler_cls()._needs_resample(interval) == should_resample

    @pytest.mark.parametrize(
        "interval,expected_minutes",
        [
            ("5m", 5),
            ("15m", 15),
            ("30m", 30),
            ("1m", 0),
        ],
    )
    def test_resample_minutes(self, interval, expected_minutes, _patch_clients):
        assert self._handler_cls()._resample_minutes(interval) == expected_minutes


# ═══════════════════════════════════════════════════════════════════════════
# 3. OHLCV resampling
# ═══════════════════════════════════════════════════════════════════════════


class TestResampleOHLCV:
    def _handler_cls(self):
        from quant.data.options_data_handler import OptionDataHandler

        return OptionDataHandler

    def test_resample_5m_bar_count(self, _patch_clients):
        cls = self._handler_cls()
        df_1m = _make_1m_ohlcv_df(n_bars=60)  # 60 1m bars → 12 5m bars
        df_5m = cls._resample_ohlcv(df_1m, 5)
        assert len(df_5m) == 12

    def test_resample_15m_bar_count(self, _patch_clients):
        cls = self._handler_cls()
        df_1m = _make_1m_ohlcv_df(n_bars=60)
        df_15m = cls._resample_ohlcv(df_1m, 15)
        assert len(df_15m) == 4

    def test_resample_30m_bar_count(self, _patch_clients):
        cls = self._handler_cls()
        df_1m = _make_1m_ohlcv_df(n_bars=60)
        df_30m = cls._resample_ohlcv(df_1m, 30)
        assert len(df_30m) == 2

    def test_resample_preserves_ohlcv_semantics(self, _patch_clients):
        cls = self._handler_cls()
        df_1m = _make_1m_ohlcv_df(n_bars=10)
        df_5m = cls._resample_ohlcv(df_1m, 5)

        # First 5m bar should aggregate the first 5 1m bars
        first_5 = df_1m.iloc[:5]
        bar = df_5m.iloc[0]
        assert bar["open"] == pytest.approx(first_5["open"].iloc[0])
        assert bar["high"] == pytest.approx(first_5["high"].max())
        assert bar["low"] == pytest.approx(first_5["low"].min())
        assert bar["close"] == pytest.approx(first_5["close"].iloc[-1])
        assert bar["volume"] == pytest.approx(first_5["volume"].sum())

    def test_resample_adds_mid_bid_ask(self, _patch_clients):
        cls = self._handler_cls()
        df_1m = _make_1m_ohlcv_df(n_bars=10)
        df_5m = cls._resample_ohlcv(df_1m, 5)
        assert "mid" in df_5m.columns
        assert "bid" in df_5m.columns
        assert "ask" in df_5m.columns
        # mid = (high + low) / 2
        expected_mid = (df_5m["high"] + df_5m["low"]) / 2
        pd.testing.assert_series_equal(df_5m["mid"], expected_mid, check_names=False)

    def test_resample_empty_df(self, _patch_clients):
        cls = self._handler_cls()
        df_empty = pd.DataFrame(columns=["open", "high", "low", "close", "volume"])  # ty:ignore[invalid-argument-type]
        result = cls._resample_ohlcv(df_empty, 5)
        assert result.empty

    def test_resample_noop_for_1min(self, _patch_clients):
        cls = self._handler_cls()
        df_1m = _make_1m_ohlcv_df(n_bars=10)
        result = cls._resample_ohlcv(df_1m, 1)
        assert len(result) == 10  # unchanged


# ═══════════════════════════════════════════════════════════════════════════
# 4. Databento fetch pipeline
# ═══════════════════════════════════════════════════════════════════════════


class TestDatabentoFetch:
    """Test _fetch_from_databento returns correctly shaped data."""

    def test_fetch_1m_columns_and_index(self, databento_handler):
        raw_df = _make_databento_raw_df(n_bars=30)
        mock_store = MagicMock()
        mock_store.to_df.return_value = raw_df

        databento_handler.databento_client.timeseries.get_range.return_value = (
            mock_store
        )

        result = databento_handler._fetch_from_databento(
            "SPY",
            590.0,
            "2025-06-20",
            "call",
            datetime.datetime(2025, 6, 20, 9, 30),
            datetime.datetime(2025, 6, 20, 10, 0),
            "1m",
        )

        assert not result.empty
        assert result.index.name == "timestamp"
        for col in ["open", "high", "low", "close", "volume", "mid", "bid", "ask"]:
            assert col in result.columns, f"Missing column: {col}"
        # All prices should be float
        for col in ["open", "high", "low", "close", "mid", "bid", "ask"]:
            assert result[col].dtype == np.float64, (
                f"{col} dtype is {result[col].dtype}"
            )

    def test_fetch_5m_resamples(self, databento_handler):
        """Fetching with interval='5m' should resample 1m data to 5m bars."""
        raw_df = _make_databento_raw_df(n_bars=60)
        mock_store = MagicMock()
        mock_store.to_df.return_value = raw_df

        databento_handler.databento_client.timeseries.get_range.return_value = (
            mock_store
        )

        result = databento_handler._fetch_from_databento(
            "SPY",
            590.0,
            "2025-06-20",
            "call",
            datetime.datetime(2025, 6, 20, 9, 30),
            datetime.datetime(2025, 6, 20, 10, 30),
            "5m",
        )

        assert not result.empty
        assert len(result) == 12  # 60 / 5

    def test_fetch_passes_correct_schema(self, databento_handler):
        """Ensure the correct schema is passed to get_range for each interval."""
        raw_df = _make_databento_raw_df(n_bars=10)
        mock_store = MagicMock()
        mock_store.to_df.return_value = raw_df
        databento_handler.databento_client.timeseries.get_range.return_value = (
            mock_store
        )

        databento_handler._fetch_from_databento(
            "SPY",
            590.0,
            "2025-06-20",
            "call",
            datetime.datetime(2025, 6, 20, 9, 30),
            datetime.datetime(2025, 6, 20, 10, 0),
            "1h",
        )

        call_kwargs = databento_handler.databento_client.timeseries.get_range.call_args
        assert call_kwargs.kwargs["schema"] == "ohlcv-1h"

    def test_fetch_passes_raw_symbol_stype(self, databento_handler):
        raw_df = _make_databento_raw_df(n_bars=5)
        mock_store = MagicMock()
        mock_store.to_df.return_value = raw_df
        databento_handler.databento_client.timeseries.get_range.return_value = (
            mock_store
        )

        databento_handler._fetch_from_databento(
            "SPY",
            590.0,
            "2025-06-20",
            "call",
            datetime.datetime(2025, 6, 20, 9, 30),
            datetime.datetime(2025, 6, 20, 10, 0),
            "1m",
        )

        call_kwargs = databento_handler.databento_client.timeseries.get_range.call_args
        assert call_kwargs.kwargs["stype_in"] == "raw_symbol"
        assert call_kwargs.kwargs["dataset"] == "OPRA.PILLAR"

    def test_fetch_to_df_called_with_price_type_float(self, databento_handler):
        raw_df = _make_databento_raw_df(n_bars=5)
        mock_store = MagicMock()
        mock_store.to_df.return_value = raw_df
        databento_handler.databento_client.timeseries.get_range.return_value = (
            mock_store
        )

        databento_handler._fetch_from_databento(
            "SPY",
            590.0,
            "2025-06-20",
            "call",
            datetime.datetime(2025, 6, 20, 9, 30),
            datetime.datetime(2025, 6, 20, 10, 0),
            "1m",
        )

        mock_store.to_df.assert_called_once_with(price_type="float")

    def test_fetch_empty_returns_empty_df(self, databento_handler):
        mock_store = MagicMock()
        mock_store.to_df.return_value = pd.DataFrame()
        databento_handler.databento_client.timeseries.get_range.return_value = (
            mock_store
        )

        result = databento_handler._fetch_from_databento(
            "SPY",
            590.0,
            "2025-06-20",
            "call",
            datetime.datetime(2025, 6, 20, 9, 30),
            datetime.datetime(2025, 6, 20, 10, 0),
            "1m",
        )
        assert result.empty

    def test_fetch_api_error_returns_empty_df(self, databento_handler):
        databento_handler.databento_client.timeseries.get_range.side_effect = Exception(
            "API down"
        )

        result = databento_handler._fetch_from_databento(
            "SPY",
            590.0,
            "2025-06-20",
            "call",
            datetime.datetime(2025, 6, 20, 9, 30),
            datetime.datetime(2025, 6, 20, 10, 0),
            "1m",
        )
        assert result.empty

    def test_mid_bid_ask_calculation(self, databento_handler):
        raw_df = _make_databento_raw_df(n_bars=5)
        mock_store = MagicMock()
        mock_store.to_df.return_value = raw_df
        databento_handler.databento_client.timeseries.get_range.return_value = (
            mock_store
        )

        result = databento_handler._fetch_from_databento(
            "SPY",
            590.0,
            "2025-06-20",
            "call",
            datetime.datetime(2025, 6, 20, 9, 30),
            datetime.datetime(2025, 6, 20, 10, 0),
            "1m",
        )

        expected_mid = (result["high"] + result["low"]) / 2
        expected_spread = expected_mid * 0.05
        pd.testing.assert_series_equal(result["mid"], expected_mid, check_names=False)
        pd.testing.assert_series_equal(
            result["bid"], expected_mid - expected_spread / 2, check_names=False
        )
        pd.testing.assert_series_equal(
            result["ask"], expected_mid + expected_spread / 2, check_names=False
        )


# ═══════════════════════════════════════════════════════════════════════════
# 7. Expiration discovery — deterministic calendar
# ═══════════════════════════════════════════════════════════════════════════


class TestCalendarExpirationDiscovery:
    def test_spy_uses_daily_listing_calendar_without_provider_call(
        self, databento_handler
    ):
        result = databento_handler.prefetch_expirations(
            "SPY", ["2025-06-18", "2025-06-19"]
        )

        assert result == {"2025-06-18": "2025-06-18", "2025-06-19": "2025-06-19"}
        databento_handler.databento_client.timeseries.get_range.assert_not_called()

    def test_non_spy_uses_conservative_weekly_friday_schedule(self, databento_handler):
        assert databento_handler.resolve_expirations("QQQ", ["2025-06-18"]) == {
            "2025-06-18": "2025-06-20"
        }


# ═══════════════════════════════════════════════════════════════════════════
# 9. list_options_contracts routing & cache
# ═══════════════════════════════════════════════════════════════════════════


class TestListOptionsContractsRouting:
    def test_routes_to_calendar_without_provider_definition_call(
        self, databento_handler
    ):
        result = databento_handler.list_options_contracts(
            "SPY", "2025-06-20", "2025-07-31"
        )
        assert result == "2025-06-20"
        databento_handler.databento_client.timeseries.get_range.assert_not_called()


# ═══════════════════════════════════════════════════════════════════════════
# 10. fetch_option_data cache integration
# ═══════════════════════════════════════════════════════════════════════════


class TestFetchOptionDataCacheIntegration:
    def test_second_fetch_uses_cache_not_api(self, databento_handler):
        raw_df = _make_databento_raw_df(n_bars=10)
        mock_store = MagicMock()
        mock_store.to_df.return_value = raw_df
        databento_handler.databento_client.timeseries.get_range.return_value = (
            mock_store
        )

        start = datetime.datetime(2025, 6, 20, 9, 30, tzinfo=datetime.UTC)
        end = datetime.datetime(2025, 6, 20, 9, 40, tzinfo=datetime.UTC)

        # First call — hits API
        r1 = databento_handler.fetch_option_data(
            "SPY", 590.0, "2025-06-20", "call", start, end, "1m"
        )
        assert not r1.empty
        assert databento_handler.databento_client.timeseries.get_range.call_count == 1

        # Second call — should use cache
        r2 = databento_handler.fetch_option_data(
            "SPY", 590.0, "2025-06-20", "call", start, end, "1m"
        )
        assert not r2.empty
        # API should NOT have been called again
        assert databento_handler.databento_client.timeseries.get_range.call_count == 1


# ═══════════════════════════════════════════════════════════════════════════
# 11. Expiration format validation
# ═══════════════════════════════════════════════════════════════════════════


class TestExpirationFormatValidation:
    """Ensure calendar expiration dates are YYYY-MM-DD strings."""

    def test_calendar_returns_iso_date(self, databento_handler):
        result = databento_handler.resolve_expirations("SPY", ["2025-06-20"])[
            "2025-06-20"
        ]
        # Validate YYYY-MM-DD format
        assert isinstance(result, str)
        parsed = datetime.datetime.strptime(result, "%Y-%m-%d")
        assert parsed.year == 2025
        assert parsed.month == 6
        assert parsed.day == 20


# ═══════════════════════════════════════════════════════════════════════════
# 12. _process_databento_df shared helper
# ═══════════════════════════════════════════════════════════════════════════


class TestProcessDatabentoDF:
    """Verify the shared post-processing method used by both single and batch paths."""

    def test_empty_input_returns_empty(self):
        from quant.data.options_data_handler import OptionDataHandler

        result = OptionDataHandler._process_databento_df(pd.DataFrame(), "1m")
        assert result.empty

    def test_renames_ts_event_to_timestamp_index(self):
        from quant.data.options_data_handler import OptionDataHandler

        raw = _make_databento_raw_df(n_bars=10)
        result = OptionDataHandler._process_databento_df(raw.copy(), "1m")
        assert result.index.name == "timestamp"
        assert "ts_event" not in result.columns

    def test_ensures_required_columns(self):
        from quant.data.options_data_handler import OptionDataHandler

        raw = _make_databento_raw_df(n_bars=10)
        result = OptionDataHandler._process_databento_df(raw.copy(), "1m")
        for col in ["open", "high", "low", "close", "volume", "mid", "bid", "ask"]:
            assert col in result.columns, f"Missing column: {col}"

    def test_mid_bid_ask_synthesis(self):
        from quant.data.options_data_handler import OptionDataHandler

        raw = _make_databento_raw_df(n_bars=10)
        result = OptionDataHandler._process_databento_df(raw.copy(), "1m")
        expected_mid = (result["high"] + result["low"]) / 2.0
        pd.testing.assert_series_equal(result["mid"], expected_mid, check_names=False)
        assert (result["bid"] < result["mid"]).all()
        assert (result["ask"] > result["mid"]).all()

    def test_resamples_when_needed(self):
        from quant.data.options_data_handler import OptionDataHandler

        raw = _make_databento_raw_df(n_bars=60)
        result = OptionDataHandler._process_databento_df(raw.copy(), "5m")
        # 60 1-min bars → 12 5-min bars
        assert len(result) == 12

    def test_no_resample_for_native_interval(self):
        from quant.data.options_data_handler import OptionDataHandler

        raw = _make_databento_raw_df(n_bars=30)
        result = OptionDataHandler._process_databento_df(raw.copy(), "1m")
        assert len(result) == 30

    def test_naive_ts_event_index_stays_canonical_naive(self):
        from quant.data.options_data_handler import OptionDataHandler

        raw = _make_databento_raw_df(n_bars=5)
        raw = raw.copy()
        raw["ts_event"] = pd.DatetimeIndex(raw["ts_event"]).tz_localize(None)
        result = OptionDataHandler._process_databento_df(raw, "1m")
        # Engine-facing OHLCV is canonical tz-naive UTC (no aware re-wrap).
        assert pd.DatetimeIndex(result.index).tz is None


class TestPreloadDatabentoBatch:
    """Verify that the batch preload makes ONE get_range call per schema,
    not one per symbol, and correctly splits the response."""

    @staticmethod
    def _build_grouped(symbols_info):
        """Build a ``grouped`` dict matching the format expected by
        ``_preload_databento_batch``.

        ``symbols_info`` is a list of
        (ticker, strike, exp, opt_type, interval, start_dt, end_dt) tuples.
        """
        grouped = {}
        for tkr, strike, exp, opt_type, interval, start_dt, end_dt in symbols_info:
            gk = (tkr, strike, exp, opt_type, interval)
            grouped[gk] = {
                "min_start": start_dt,
                "max_end": end_dt,
                "members": [{"start_dt": start_dt, "end_dt": end_dt}],
            }
        return grouped

    def _make_multi_symbol_df(self, symbols, n_bars=10):
        """Create a multi-symbol DataFrame mimicking a batched DataBento response."""
        frames = []
        for sym in symbols:
            idx = pd.date_range(
                "2025-06-20 09:30", periods=n_bars, freq="1min", tz="UTC"
            )
            rng = np.random.default_rng(42)
            df = pd.DataFrame(
                {
                    "ts_event": idx,
                    "open": rng.uniform(4, 6, n_bars),
                    "high": rng.uniform(5, 7, n_bars),
                    "low": rng.uniform(3, 5, n_bars),
                    "close": rng.uniform(4, 6, n_bars),
                    "volume": rng.integers(10, 500, n_bars).astype(float),
                    "symbol": sym,
                }
            )
            df.index = idx
            df.index.name = "ts_recv"
            frames.append(df)
        return pd.concat(frames)

    def test_single_api_call_for_multiple_symbols(self, databento_handler):
        """4 symbols with the same schema should result in exactly 1 get_range call."""
        start = datetime.datetime(2025, 6, 20, 9, 30)
        end = datetime.datetime(2025, 6, 20, 16, 0)

        syms = [
            ("SPY", 580.0, "2025-06-20", "put", "1m", start, end),
            ("SPY", 590.0, "2025-06-20", "call", "1m", start, end),
            ("SPY", 600.0, "2025-06-20", "call", "1m", start, end),
            ("SPY", 560.0, "2025-06-20", "put", "1m", start, end),
        ]
        grouped = self._build_grouped(syms)

        # Build expected formatted symbols
        from quant.data.options_data_handler import OptionDataHandler

        formatted_syms = [
            OptionDataHandler._format_databento_symbol(t, s, e, ot)
            for t, s, e, ot, *_ in syms
        ]

        mock_store = MagicMock()
        mock_store.to_df.return_value = self._make_multi_symbol_df(
            formatted_syms, n_bars=10
        )
        databento_handler.databento_client.timeseries.get_range.return_value = (
            mock_store
        )

        result = databento_handler._preload_databento_batch(grouped)

        assert databento_handler.databento_client.timeseries.get_range.call_count == 1
        assert result["http_fetches"] == 1
        assert result["filled_windows"] == 4

        # Verify the call passed all 4 symbols in a single list
        call_kwargs = databento_handler.databento_client.timeseries.get_range.call_args
        passed_symbols = call_kwargs.kwargs.get("symbols") or call_kwargs[1].get(
            "symbols"
        )
        assert len(passed_symbols) == 4

    def test_cached_groups_skip_api(self, databento_handler):
        """Groups already in cache should not trigger any API call."""
        start = datetime.datetime(2025, 6, 20, 9, 30)
        end = datetime.datetime(2025, 6, 20, 16, 0)
        grouped = self._build_grouped(
            [
                ("SPY", 590.0, "2025-06-20", "call", "1m", start, end),
            ]
        )

        # Mock get_superset to indicate a warm cache hit.
        warm_df = _make_1m_ohlcv_df(n_bars=10)
        warm_df.index.name = "timestamp"
        databento_handler._cache.get_superset = MagicMock(return_value=warm_df)

        result = databento_handler._preload_databento_batch(grouped)
        assert result["http_fetches"] == 0
        assert databento_handler.databento_client.timeseries.get_range.call_count == 0

    def test_fallback_on_batch_error(self, databento_handler):
        """If the batch call raises, individual fetches should be attempted."""
        start = datetime.datetime(2025, 6, 20, 9, 30)
        end = datetime.datetime(2025, 6, 20, 16, 0)
        grouped = self._build_grouped(
            [
                ("SPY", 590.0, "2025-06-20", "call", "1m", start, end),
            ]
        )

        # First call (batch) raises, second call (fallback individual) succeeds
        raw = _make_databento_raw_df(n_bars=10)
        mock_store_ok = MagicMock()
        mock_store_ok.to_df.return_value = raw

        databento_handler.databento_client.timeseries.get_range.side_effect = [
            RuntimeError("batch exploded"),
            mock_store_ok,
        ]

        databento_handler._preload_databento_batch(grouped)
        # batch call + 1 individual fallback = 2 total calls
        assert databento_handler.databento_client.timeseries.get_range.call_count == 2


# ═══════════════════════════════════════════════════════════════════════════
# 15. prefetch_expirations uses only the local calendar
# ═══════════════════════════════════════════════════════════════════════════


class TestPrefetchExpirationsRouting:
    """Ensure no definition request is made."""

    def test_calendar_does_not_call_databento(self, databento_handler):
        result = databento_handler.prefetch_expirations("SPY", ["2025-06-10"])

        assert result == {"2025-06-10": "2025-06-10"}
        databento_handler.databento_client.timeseries.get_range.assert_not_called()


# ═══════════════════════════════════════════════════════════════════════════
# CBBO cache helpers
# ═══════════════════════════════════════════════════════════════════════════


def _make_raw_cbbo_df(
    n: int = 5,
    start: str = "2025-06-20 14:00",
    bid: float = 1.0,
    ask: float = 1.20,
    symbol: str | None = None,
) -> pd.DataFrame:
    """Simulate raw Databento CBBO df with bid_px_00 / ask_px_00 columns."""
    idx = pd.date_range(start, periods=n, freq="1min", tz="UTC")
    data: dict = {
        "bid_px_00": [bid + i * 0.01 for i in range(n)],
        "ask_px_00": [ask + i * 0.01 for i in range(n)],
    }
    if symbol is not None:
        data["symbol"] = symbol
    return pd.DataFrame(data, index=idx)


def _make_cache_cbbo_df(
    n: int = 5,
    start: str = "2025-06-20 14:00",
    bid: float = 1.0,
    ask: float = 1.20,
) -> pd.DataFrame:
    """Create a normalised cache CBBO DataFrame (bid, ask, mid)."""
    idx = pd.date_range(start, periods=n, freq="1min", tz="UTC")
    bids = [bid + i * 0.01 for i in range(n)]
    asks = [ask + i * 0.01 for i in range(n)]
    mids = [(b + a) / 2.0 for b, a in zip(bids, asks)]
    return pd.DataFrame({"bid": bids, "ask": asks, "mid": mids}, index=idx)


class TestCbboDfToCacheDf:
    """Tests for OptionDataHandler._cbbo_df_to_cache_df."""

    def _cls(self):
        from quant.data.options_data_handler import OptionDataHandler

        return OptionDataHandler

    def test_basic_conversion(self, _patch_clients):
        cls = self._cls()
        raw = _make_raw_cbbo_df(n=3, bid=2.0, ask=2.50)
        out = cls._cbbo_df_to_cache_df(raw)
        assert list(out.columns) == ["bid", "ask", "mid"]
        assert len(out) == 3
        assert out["mid"].iloc[0] == pytest.approx((2.0 + 2.50) / 2.0)

    def test_filters_by_symbol(self, _patch_clients):
        cls = self._cls()
        idx = pd.date_range("2025-06-20 14:00", periods=4, freq="1min", tz="UTC")
        raw = pd.DataFrame(
            {
                "bid_px_00": [1.0, 1.1, 2.0, 2.1],
                "ask_px_00": [1.5, 1.6, 2.5, 2.6],
                "symbol": ["SYM_A", "SYM_A", "SYM_B", "SYM_B"],
            },
            index=idx,
        )
        out = cls._cbbo_df_to_cache_df(raw, symbol="SYM_A")
        assert len(out) == 2
        assert out["bid"].iloc[0] == pytest.approx(1.0)

    def test_empty_df_returns_empty(self, _patch_clients):
        cls = self._cls()
        out = cls._cbbo_df_to_cache_df(pd.DataFrame())
        assert out.empty

    def test_missing_columns_returns_empty(self, _patch_clients):
        cls = self._cls()
        idx = pd.date_range("2025-06-20 14:00", periods=2, freq="1min", tz="UTC")
        raw = pd.DataFrame({"price": [1.0, 2.0]}, index=idx)
        out = cls._cbbo_df_to_cache_df(raw)
        assert out.empty

    def test_drops_negative_bid(self, _patch_clients):
        cls = self._cls()
        idx = pd.date_range("2025-06-20 14:00", periods=3, freq="1min", tz="UTC")
        raw = pd.DataFrame(
            {
                "bid_px_00": [-1.0, 0.0, 1.0],
                "ask_px_00": [1.5, 1.5, 1.5],
            },
            index=idx,
        )
        out = cls._cbbo_df_to_cache_df(raw)
        assert len(out) == 2

    def test_drops_zero_ask(self, _patch_clients):
        cls = self._cls()
        idx = pd.date_range("2025-06-20 14:00", periods=2, freq="1min", tz="UTC")
        raw = pd.DataFrame(
            {
                "bid_px_00": [1.0, 1.0],
                "ask_px_00": [0.0, 1.5],
            },
            index=idx,
        )
        out = cls._cbbo_df_to_cache_df(raw)
        assert len(out) == 1

    def test_drops_nan_prices(self, _patch_clients):
        cls = self._cls()
        idx = pd.date_range("2025-06-20 14:00", periods=3, freq="1min", tz="UTC")
        raw = pd.DataFrame(
            {
                "bid_px_00": [float("nan"), 1.0, 1.0],
                "ask_px_00": [1.5, float("nan"), 1.5],
            },
            index=idx,
        )
        out = cls._cbbo_df_to_cache_df(raw)
        assert len(out) == 1

    def test_rejects_one_sided_rows(self, _patch_clients):
        cls = self._cls()
        raw = pd.DataFrame(
            {"bid_px_00": [1.0, float("nan")], "ask_px_00": [float("nan"), 1.5]},
            index=pd.date_range("2025-06-20 14:00", periods=2, freq="1min", tz="UTC"),
        )
        assert cls._cbbo_df_to_cache_df(raw).empty

    def test_tz_naive_index_becomes_utc(self, _patch_clients):
        cls = self._cls()
        idx = pd.date_range("2025-06-20 14:00", periods=2, freq="1min")  # no tz
        raw = pd.DataFrame(
            {
                "bid_px_00": [1.0, 1.0],
                "ask_px_00": [1.5, 1.5],
            },
            index=idx,
        )
        out = cls._cbbo_df_to_cache_df(raw)
        out_idx = pd.DatetimeIndex(out.index)
        assert out_idx.tz is not None
        assert str(out_idx.tz) == "UTC"

    def test_symbol_filter_all_filtered_returns_empty(self, _patch_clients):
        cls = self._cls()
        raw = _make_raw_cbbo_df(n=3, symbol="SYM_A")
        out = cls._cbbo_df_to_cache_df(raw, symbol="NONEXISTENT")
        assert out.empty


class TestExtractCbboMidFromCache:
    """Tests for OptionDataHandler._extract_cbbo_mid_from_cache."""

    def _cls(self):
        from quant.data.options_data_handler import OptionDataHandler

        return OptionDataHandler

    def test_returns_mid_at_exact_time(self, _patch_clients):
        cls = self._cls()
        cache = _make_cache_cbbo_df(n=5, start="2025-06-20 14:00", bid=2.0, ask=2.50)
        at = datetime.datetime(2025, 6, 20, 14, 2, tzinfo=datetime.UTC)
        mid = cls._extract_cbbo_mid_from_cache(cache, at)
        expected_bid = 2.0 + 2 * 0.01
        expected_ask = 2.50 + 2 * 0.01
        assert mid == pytest.approx((expected_bid + expected_ask) / 2.0)

    def test_uses_latest_causal_bar_before_target(self, _patch_clients):
        """The canonical lookup is latest-at-or-before, never future."""
        cls = self._cls()
        cache = _make_cache_cbbo_df(n=5, start="2025-06-20 14:00", bid=1.0, ask=1.50)
        # 14:02:30 — between bar 2 (14:02) and bar 3 (14:03)
        at = datetime.datetime(2025, 6, 20, 14, 2, 30, tzinfo=datetime.UTC)
        assert cls._extract_cbbo_mid_from_cache(cache, at) == pytest.approx(1.27)

    def test_no_lookahead_all_bars_after_target(self, _patch_clients):
        """Returns None when all bars are after the target (no-lookahead)."""
        cls = self._cls()
        cache = _make_cache_cbbo_df(n=3, start="2025-06-20 15:00")
        at = datetime.datetime(2025, 6, 20, 14, 59, tzinfo=datetime.UTC)
        assert cls._extract_cbbo_mid_from_cache(cache, at) is None

    def test_none_on_empty_df(self, _patch_clients):
        cls = self._cls()
        assert (
            cls._extract_cbbo_mid_from_cache(
                pd.DataFrame(), datetime.datetime.now(datetime.UTC)
            )
            is None
        )

    def test_none_on_none(self, _patch_clients):
        cls = self._cls()
        assert (
            cls._extract_cbbo_mid_from_cache(None, datetime.datetime.now(datetime.UTC))
            is None
        )

    def test_rejects_negative_bid(self, _patch_clients):
        cls = self._cls()
        idx = pd.date_range("2025-06-20 14:00", periods=1, freq="1min", tz="UTC")
        cache = pd.DataFrame({"bid": [-0.5], "ask": [1.5], "mid": [0.5]}, index=idx)
        at = datetime.datetime(2025, 6, 20, 14, 0, tzinfo=datetime.UTC)
        assert cls._extract_cbbo_mid_from_cache(cache, at) is None

    def test_rejects_zero_ask(self, _patch_clients):
        cls = self._cls()
        idx = pd.date_range("2025-06-20 14:00", periods=1, freq="1min", tz="UTC")
        cache = pd.DataFrame({"bid": [1.0], "ask": [0.0], "mid": [0.5]}, index=idx)
        at = datetime.datetime(2025, 6, 20, 14, 0, tzinfo=datetime.UTC)
        assert cls._extract_cbbo_mid_from_cache(cache, at) is None

    def test_allows_bid_zero(self, _patch_clients):
        """bid=0 is valid for deep-OTM legs with no bid."""
        cls = self._cls()
        idx = pd.date_range("2025-06-20 14:00", periods=1, freq="1min", tz="UTC")
        cache = pd.DataFrame({"bid": [0.0], "ask": [0.05], "mid": [0.025]}, index=idx)
        at = datetime.datetime(2025, 6, 20, 14, 0, tzinfo=datetime.UTC)
        mid = cls._extract_cbbo_mid_from_cache(cache, at)
        assert mid == pytest.approx(0.025)

    def test_rejects_nan_bid(self, _patch_clients):
        cls = self._cls()
        idx = pd.date_range("2025-06-20 14:00", periods=1, freq="1min", tz="UTC")
        cache = pd.DataFrame(
            {"bid": [float("nan")], "ask": [1.5], "mid": [0.75]}, index=idx
        )
        at = datetime.datetime(2025, 6, 20, 14, 0, tzinfo=datetime.UTC)
        assert cls._extract_cbbo_mid_from_cache(cache, at) is None

    def test_tz_naive_cache_with_tz_aware_target(self, _patch_clients):
        """Should handle tz-naive cache index with tz-aware target."""
        cls = self._cls()
        idx = pd.date_range("2025-06-20 14:00", periods=3, freq="1min")  # no tz
        cache = pd.DataFrame(
            {
                "bid": [1.0, 1.01, 1.02],
                "ask": [1.5, 1.51, 1.52],
                "mid": [1.25, 1.26, 1.27],
            },
            index=idx,
        )
        at = datetime.datetime(2025, 6, 20, 14, 1, tzinfo=datetime.UTC)
        mid = cls._extract_cbbo_mid_from_cache(cache, at)
        assert mid == pytest.approx((1.01 + 1.51) / 2.0)
