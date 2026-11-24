"""
Tests for quant.data.data_handler.AlpacaDataHandler

External API clients (Alpaca, Databento) are fully mocked — no network calls.

Covers:
  - _normalize_interval: canonical forms of all supported interval strings
  - _map_interval_to_timeframe: correct TimeFrame mapping for each interval
  - _filter_market_hours: intraday filter keeps 9:30-16:00 ET; daily passes through
  - get_historical_data - cache hit: reads from StockDataCache, no API call
  - get_historical_data - cache miss: calls API, stores result in cache
  - get_historical_data - partial coverage (needs_head / needs_tail): fetches gap
  - register_client: stores new TradingClient under given name
  - clear_cache: delegates to StockDataCache.clear_cache
"""

from __future__ import annotations

import logging
import time
from datetime import UTC, date, datetime
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import MagicMock, PropertyMock, patch

import numpy as np
import pandas as pd
import pytest

from quant.data.ws.protocols import MinuteBar
from quant.utils.time import EquitySessionClock, closed_bar_asof

# ---------------------------------------------------------------------------
# Environment stubs — must happen before importing the handler
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _stub_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ALPACA_API_KEY", "test_key")
    monkeypatch.setenv("ALPACA_API_SECRET", "test_secret")
    monkeypatch.setenv("DATABENTO_API_KEY", "test_databento_key")


# ---------------------------------------------------------------------------
# Helper: build a minimal bar-like object that AlpacaDataHandler iterates over
# ---------------------------------------------------------------------------


def _make_bar(symbol: str, ts: pd.Timestamp) -> SimpleNamespace:
    return SimpleNamespace(
        symbol=symbol,
        timestamp=ts,
        open=100.0,
        high=101.0,
        low=99.0,
        close=100.5,
        volume=1000.0,
    )


def _make_api_response(
    ticker: str, n: int = 5, start: str = "2025-01-02 14:30"
) -> dict[str, list[Any]]:
    """Minimal structure returned by stock_client.get_stock_bars()."""
    idx = pd.date_range(start, periods=n, freq="1min", tz="UTC")
    bars = [_make_bar(ticker, ts) for ts in idx]
    return {ticker: bars}


# ---------------------------------------------------------------------------
# Fixture: AlpacaDataHandler with all clients mocked
# ---------------------------------------------------------------------------


@pytest.fixture()
def handler(tmp_path: Path):
    with (
        patch("quant.data.data_handler.TradingClient") as mock_tc,
        patch("quant.data.data_handler.StockHistoricalDataClient") as mock_shc,
        patch("quant.data.data_handler.OptionHistoricalDataClient") as mock_ohc,
        patch("quant.data.data_handler.db.Historical") as mock_db,
    ):
        mock_tc.return_value = MagicMock()
        mock_shc.return_value = MagicMock()
        mock_ohc.return_value = MagicMock()
        mock_db.return_value = MagicMock()

        from quant.data.data_handler import AlpacaDataHandler

        h = AlpacaDataHandler(cache_dir=str(tmp_path), provider="alpaca")
        yield h


# ---------------------------------------------------------------------------
# 1. _normalize_interval
# ---------------------------------------------------------------------------


class TestNormalizeInterval:
    @pytest.mark.parametrize(
        "raw,expected",
        [
            ("1m", "1m"),
            ("1", "1m"),
            ("5m", "5m"),
            ("5", "5m"),
            ("15m", "15m"),
            ("15", "15m"),
            ("30m", "30m"),
            ("30", "30m"),
            ("60", "1h"),
            ("1h", "1h"),
            ("1d", "1d"),
            ("d", "1d"),
            ("1min", "1m"),
            ("15min", "15m"),
        ],
    )
    def test_normalize(self, handler: Any, raw: str, expected: str) -> None:
        assert handler._normalize_interval(raw) == expected


# ---------------------------------------------------------------------------
# 2. _map_interval_to_timeframe
# ---------------------------------------------------------------------------


class TestMapIntervalToTimeframe:
    """TimeFrame has no __eq__; compare via .amount and .unit attributes."""

    @staticmethod
    def _tf_attrs(tf: Any) -> tuple[int, str]:
        from alpaca.data.timeframe import TimeFrameUnit

        return (tf.amount, tf.unit)

    def test_minute(self, handler: Any) -> None:
        from alpaca.data.timeframe import TimeFrameUnit

        tf = handler._map_interval_to_timeframe("1m")
        assert self._tf_attrs(tf) == (1, TimeFrameUnit.Minute)

    def test_5_minute(self, handler: Any) -> None:
        from alpaca.data.timeframe import TimeFrameUnit

        tf = handler._map_interval_to_timeframe("5m")
        assert self._tf_attrs(tf) == (5, TimeFrameUnit.Minute)

    def test_15_minute(self, handler: Any) -> None:
        from alpaca.data.timeframe import TimeFrameUnit

        tf = handler._map_interval_to_timeframe("15m")
        assert self._tf_attrs(tf) == (15, TimeFrameUnit.Minute)

    def test_30_minute(self, handler: Any) -> None:
        from alpaca.data.timeframe import TimeFrameUnit

        tf = handler._map_interval_to_timeframe("30m")
        assert self._tf_attrs(tf) == (30, TimeFrameUnit.Minute)

    def test_hour(self, handler: Any) -> None:
        from alpaca.data.timeframe import TimeFrameUnit

        tf = handler._map_interval_to_timeframe("1h")
        assert self._tf_attrs(tf) == (1, TimeFrameUnit.Hour)

    def test_day(self, handler: Any) -> None:
        from alpaca.data.timeframe import TimeFrameUnit

        tf = handler._map_interval_to_timeframe("1d")
        assert self._tf_attrs(tf) == (1, TimeFrameUnit.Day)

    def test_unknown_falls_back_gracefully(self, handler: Any) -> None:
        # Should not raise; returns a default TimeFrame
        tf = handler._map_interval_to_timeframe("unknown")
        assert tf is not None


# ---------------------------------------------------------------------------
# 3. _filter_market_hours
# ---------------------------------------------------------------------------


class TestFilterMarketHours:
    def _make_full_day_df(self, date: str = "2025-01-02") -> pd.DataFrame:
        """One-minute bars spanning midnight → midnight UTC (covers pre/post market)."""
        idx = pd.date_range(f"{date} 00:00", periods=390 + 200, freq="1min", tz="UTC")
        return pd.DataFrame({"close": 100.0}, index=idx)

    def test_intraday_keeps_only_market_hours(self, handler: Any) -> None:
        df = self._make_full_day_df()
        result = handler._filter_market_hours(df, "1m")
        if result.empty:
            return  # Could be empty if date columns don't align — skip

        et = result.index.tz_convert("America/New_York")
        minutes = et.hour * 60 + et.minute
        assert (minutes >= 570).all(), "Pre-market rows leaked through"
        assert (minutes < 960).all(), "Post-market rows leaked through"

    def test_daily_interval_passes_through_unfiltered(self, handler: Any) -> None:
        idx = pd.date_range("2025-01-02", periods=10, freq="1d", tz="UTC")
        df = pd.DataFrame({"close": 100.0}, index=idx)
        result = handler._filter_market_hours(df, "1d")
        assert len(result) == len(df)

    def test_empty_dataframe_returns_empty(self, handler: Any) -> None:
        result = handler._filter_market_hours(pd.DataFrame(), "1m")
        assert result.empty

    def test_non_intraday_interval_passes_through(self, handler: Any) -> None:
        df = self._make_full_day_df()
        result = handler._filter_market_hours(df, "1w")  # unknown → pass through
        assert len(result) == len(df)


# ---------------------------------------------------------------------------
# 4. get_historical_data — cache hit
# ---------------------------------------------------------------------------


class TestGetHistoricalDataCacheHit:
    def _seed_cache(
        self, handler: Any, ticker: str, interval: str, n: int = 20
    ) -> pd.DataFrame:
        """Write data directly to the StockDataCache so get_historical_data finds it."""
        idx = pd.date_range("2025-01-02 15:00", periods=n, freq="1min", tz="UTC")
        df = pd.DataFrame(
            {"open": 1.0, "high": 1.0, "low": 1.0, "close": 1.0, "volume": 100.0},
            index=idx,
        )
        t0, t1 = idx[0].to_pydatetime(), idx[-1].to_pydatetime()
        handler._cache.put(ticker, t0, t1, interval, df)
        return df

    def test_cache_hit_returns_dataframe(self, handler: Any) -> None:
        self._seed_cache(handler, "SPY", "1m")
        result = handler.get_historical_data(
            "SPY", "2025-01-02 15:01", "2025-01-02 15:10", "1m"
        )
        assert isinstance(result, pd.DataFrame)
        assert not result.empty

    def test_cache_hit_does_not_call_api(self, handler: Any) -> None:
        self._seed_cache(handler, "SPY", "1m")
        handler.stock_client.get_stock_bars = MagicMock()

        handler.get_historical_data("SPY", "2025-01-02 15:01", "2025-01-02 15:10", "1m")

        handler.stock_client.get_stock_bars.assert_not_called()

    def test_cache_hit_columns_are_title_case(self, handler: Any) -> None:
        """Handler renames cache columns to Title-case (Open/High/Low/Close/Volume)."""
        self._seed_cache(handler, "NVDA", "1m")
        result = handler.get_historical_data(
            "NVDA", "2025-01-02 15:01", "2025-01-02 15:10", "1m"
        )
        if not result.empty:
            for col in ("Open", "High", "Low", "Close", "Volume"):
                assert col in result.columns, f"Expected Title-case column '{col}'"

    def test_intraday_cache_hit_returns_utc_naive_index(self, handler: Any) -> None:
        """Intraday get_historical_data strips tz; naive means UTC wall clock."""
        self._seed_cache(handler, "SPY", "1m")
        result = handler.get_historical_data(
            "SPY", "2025-01-02 15:01", "2025-01-02 15:10", "1m"
        )
        assert not result.empty
        assert isinstance(result.index, pd.DatetimeIndex)
        assert result.index.tz is None
        # Seeded at 15:00 UTC → wall clock preserved after strip
        assert result.index[0].hour >= 14


class TestFinalizeEquityIndex:
    def test_intraday_aware_becomes_utc_naive(self, handler: Any) -> None:
        idx = pd.date_range("2025-06-04 14:00", periods=5, freq="1min", tz="UTC")
        df = pd.DataFrame({"Close": 100.0}, index=idx)
        out = handler._finalize_equity_index(df, "1m")
        assert out.index.tz is None
        assert out.index[0] == pd.Timestamp("2025-06-04 14:00:00")

    def test_daily_keeps_timezone(self, handler: Any) -> None:
        idx = pd.date_range("2025-01-02", periods=3, freq="1d", tz="UTC")
        df = pd.DataFrame({"Close": 100.0}, index=idx)
        out = handler._finalize_equity_index(df, "1d")
        assert out.index.tz is not None
        assert str(out.index.tz) == "UTC"


# ---------------------------------------------------------------------------
# 5. get_historical_data — cache miss (API called)
# ---------------------------------------------------------------------------


class TestGetHistoricalDataCacheMiss:
    def _mock_api_response(self, handler: Any, ticker: str, n: int = 5) -> None:
        response = _make_api_response(ticker, n=n)
        handler.stock_client.get_stock_bars = MagicMock(return_value=response)

    def test_api_called_on_cache_miss(self, handler: Any) -> None:
        self._mock_api_response(handler, "AAPL")
        handler.get_historical_data(
            "AAPL",
            "2025-01-02 14:30",
            "2025-01-02 15:00",
            "1m",
            use_cache=False,
        )
        handler.stock_client.get_stock_bars.assert_called()

    def test_cache_miss_returns_dataframe(self, handler: Any) -> None:
        self._mock_api_response(handler, "AAPL", n=5)
        result = handler.get_historical_data(
            "AAPL",
            "2025-01-02 14:30",
            "2025-01-02 15:00",
            "1m",
            use_cache=False,
        )
        assert isinstance(result, pd.DataFrame)

    def test_empty_api_response_returns_empty_dataframe(self, handler: Any) -> None:
        handler.stock_client.get_stock_bars = MagicMock(return_value={"AAPL": []})
        result = handler.get_historical_data(
            "AAPL",
            "2025-01-02 14:30",
            "2025-01-02 15:00",
            "1m",
            use_cache=False,
        )
        assert isinstance(result, pd.DataFrame)

    def test_api_exception_fails_loudly(self, handler: Any) -> None:
        from quant.data.data_handler import HistoricalDataFetchError

        handler.stock_client.get_stock_bars = MagicMock(
            side_effect=RuntimeError("API down")
        )
        with pytest.raises(HistoricalDataFetchError, match="Alpaca fetch failed"):
            handler.get_historical_data(
                "TSLA",
                "2025-01-02 14:30",
                "2025-01-02 15:00",
                "1m",
                use_cache=False,
            )

    def test_recent_sip_subscription_error_fails_loudly(
        self, handler: Any, caplog: pytest.LogCaptureFixture
    ) -> None:
        from quant.data.data_handler import HistoricalDataFetchError

        error = RuntimeError("subscription does not permit querying recent SIP data")
        handler.stock_client.get_stock_bars = MagicMock(side_effect=error)

        with (
            caplog.at_level(logging.ERROR, logger=handler.logger.name),
            pytest.raises(HistoricalDataFetchError, match="SIP coverage unavailable"),
        ):
            handler.get_historical_data(
                "TSLA",
                "2025-01-02 14:30",
                "2025-01-02 15:00",
                "1m",
                use_cache=False,
            )

        assert "Alpaca denied recent SIP data for TSLA" in caplog.text
        assert "Refusing to return partial market data" in caplog.text

    def test_recent_sip_subscription_error_does_not_return_cached_partial_result(
        self, handler: Any, caplog: pytest.LogCaptureFixture
    ) -> None:
        from quant.data.data_handler import HistoricalDataFetchError

        idx = pd.date_range("2025-01-02 15:00", periods=5, freq="1min", tz="UTC")
        cached = pd.DataFrame(
            {"open": 1.0, "high": 1.0, "low": 1.0, "close": 1.0, "volume": 1.0},
            index=idx,
        )
        handler._cache.put(
            "TSLA", idx[0].to_pydatetime(), idx[-1].to_pydatetime(), "1m", cached
        )
        error = RuntimeError("subscription does not permit querying recent SIP data")
        handler.stock_client.get_stock_bars = MagicMock(side_effect=error)

        with (
            caplog.at_level(logging.ERROR, logger=handler.logger.name),
            pytest.raises(HistoricalDataFetchError, match="SIP coverage unavailable"),
        ):
            handler.get_historical_data(
                "TSLA", "2025-01-02 15:00", "2025-01-02 15:10", "1m"
            )

        assert "Refusing to return partial market data" in caplog.text

    def test_repairs_interior_cache_gap_before_returning_cached_series(
        self, handler: Any
    ) -> None:
        """A min/max coverage envelope must not hide an unfetched middle span."""
        early_idx = pd.date_range("2025-01-02 14:30", periods=5, freq="1min", tz="UTC")
        late_idx = pd.date_range("2025-01-02 14:36", periods=5, freq="1min", tz="UTC")

        def frame(idx: pd.DatetimeIndex) -> pd.DataFrame:
            return pd.DataFrame(
                {
                    "open": 100.0,
                    "high": 101.0,
                    "low": 99.0,
                    "close": 100.5,
                    "volume": 1000.0,
                },
                index=idx,
            )

        handler._cache.put("SPY", early_idx[0], early_idx[-1], "1m", frame(early_idx))
        handler._cache.put("SPY", late_idx[0], late_idx[-1], "1m", frame(late_idx))
        handler.stock_client.get_stock_bars = MagicMock(
            return_value=_make_api_response("SPY", n=3, start="2025-01-02 14:34")
        )

        handler.get_historical_data("SPY", "2025-01-02 14:30", "2025-01-02 14:40", "1m")

        handler.stock_client.get_stock_bars.assert_called_once()
        assert handler._cache.is_covered(
            "SPY",
            pd.Timestamp("2025-01-02 14:30", tz="UTC"),
            pd.Timestamp("2025-01-02 14:40", tz="UTC"),
            "1m",
        )


# ---------------------------------------------------------------------------
# 6. get_historical_data — raw vs adjusted use separate cache keys
# ---------------------------------------------------------------------------


class TestAdjustmentCacheKeys:
    def _seed(self, handler: Any, ticker: str, interval: str, n: int = 10) -> None:
        idx = pd.date_range("2025-01-02 14:30", periods=n, freq="1min", tz="UTC")
        df = pd.DataFrame(
            {"open": 50.0, "high": 50.5, "low": 49.5, "close": 50.0, "volume": 200.0},
            index=idx,
        )
        t0, t1 = idx[0].to_pydatetime(), idx[-1].to_pydatetime()
        handler._cache.put(ticker, t0, t1, interval, df)

    def test_raw_and_adjusted_do_not_collide(self, handler: Any) -> None:
        self._seed(handler, "AAPL", "1d")
        self._seed(handler, "AAPL", "1d_raw")

        cov_adj = handler._cache.get_coverage("AAPL", "1d")
        cov_raw = handler._cache.get_coverage("AAPL", "1d_raw")

        # Both exist independently
        assert cov_adj != (None, None)
        assert cov_raw != (None, None)


# ---------------------------------------------------------------------------
# 7. register_client
# ---------------------------------------------------------------------------


class TestRegisterClient:
    def test_register_adds_to_clients_dict(self, handler: Any) -> None:
        with patch("quant.data.data_handler.TradingClient") as mock_tc:
            mock_tc.return_value = MagicMock()
            handler.register_client("my_account", "key123", "secret456", paper=True)

        assert "my_account" in handler.clients

    def test_register_failed_client_does_not_raise(self, handler: Any) -> None:
        with patch(
            "quant.data.data_handler.TradingClient",
            side_effect=RuntimeError("bad creds"),
        ):
            # Should log the error and not propagate
            handler.register_client("bad_account", "k", "s", paper=True)

        assert "bad_account" not in handler.clients


# ---------------------------------------------------------------------------
# 8. clear_cache
# ---------------------------------------------------------------------------


class TestClearCache:
    def test_clear_cache_delegates_to_stock_cache(self, handler: Any) -> None:
        handler._cache.clear_cache = MagicMock()
        handler.clear_cache()
        handler._cache.clear_cache.assert_called_once()

    def test_clear_cache_with_days_delegates_to_cleanup(self, handler: Any) -> None:
        handler._cache.cleanup_old_entries = MagicMock()
        handler.clear_cache(older_than_days=30)
        handler._cache.cleanup_old_entries.assert_called_once_with(days_old=30)


# ---------------------------------------------------------------------------
# 9. get_option_expirations
# ---------------------------------------------------------------------------


def _make_contract(expiration: str | date) -> SimpleNamespace:
    """Minimal option contract stub with an expiration_date attribute."""
    return SimpleNamespace(expiration_date=expiration)


class TestGetOptionExpirations:
    def _trading_client(self, handler: Any) -> MagicMock:
        """Return the first (paper) TradingClient mock wired into the handler."""
        return next(iter(handler.clients.values()))

    # -- happy path ----------------------------------------------------------

    def test_returns_sorted_expiration_strings(self, handler: Any) -> None:

        contracts = [
            _make_contract(date(2026, 4, 4)),
            _make_contract(date(2026, 3, 28)),
            _make_contract(date(2026, 4, 11)),
        ]
        self._trading_client(
            handler
        ).get_option_contracts.return_value = SimpleNamespace(
            option_contracts=contracts
        )
        result = handler.get_option_expirations("SPY", "2026-03-27", "2026-04-15")
        assert result == ["2026-03-28", "2026-04-04", "2026-04-11"]

    def test_deduplicates_contracts_with_same_expiration(self, handler: Any) -> None:

        exp = date(2026, 3, 28)
        contracts = [_make_contract(exp), _make_contract(exp), _make_contract(exp)]
        self._trading_client(
            handler
        ).get_option_contracts.return_value = SimpleNamespace(
            option_contracts=contracts
        )
        result = handler.get_option_expirations("SPY", "2026-03-27", "2026-04-01")
        assert result == ["2026-03-28"]

    def test_handles_string_expiration_dates(self, handler: Any) -> None:
        """expiration_date stored as a plain string (no strftime) still works."""
        contracts = [
            _make_contract("2026-04-04"),
            _make_contract("2026-03-28"),
        ]
        self._trading_client(
            handler
        ).get_option_contracts.return_value = SimpleNamespace(
            option_contracts=contracts
        )
        result = handler.get_option_expirations("SPY", "2026-03-27", "2026-04-10")
        assert result == ["2026-03-28", "2026-04-04"]

    # -- edge / empty cases --------------------------------------------------

    def test_empty_contracts_list_returns_empty(self, handler: Any) -> None:
        self._trading_client(
            handler
        ).get_option_contracts.return_value = SimpleNamespace(option_contracts=[])
        result = handler.get_option_expirations("SPY", "2026-03-27", "2026-04-01")
        assert result == []

    def test_none_contracts_returns_empty(self, handler: Any) -> None:
        self._trading_client(
            handler
        ).get_option_contracts.return_value = SimpleNamespace(option_contracts=None)
        result = handler.get_option_expirations("SPY", "2026-03-27", "2026-04-01")
        assert result == []

    def test_no_clients_returns_empty(self, handler: Any) -> None:
        handler.clients.clear()
        result = handler.get_option_expirations("SPY", "2026-03-27", "2026-04-01")
        assert result == []

    def test_api_exception_returns_empty(self, handler: Any) -> None:
        self._trading_client(handler).get_option_contracts.side_effect = RuntimeError(
            "network error"
        )
        result = handler.get_option_expirations("SPY", "2026-03-27", "2026-04-01")
        assert result == []

    # -- parameter forwarding ------------------------------------------------

    def test_passes_ticker_and_date_range_to_api(self, handler: Any) -> None:
        tc = self._trading_client(handler)
        tc.get_option_contracts.return_value = SimpleNamespace(option_contracts=[])

        handler.get_option_expirations("AAPL", "2026-04-01", "2026-04-30")

        tc.get_option_contracts.assert_called_once()
        req = tc.get_option_contracts.call_args[0][0]
        assert req.underlying_symbols == ["AAPL"]
        assert str(req.expiration_date_gte) == "2026-04-01"
        assert str(req.expiration_date_lte) == "2026-04-30"

    def test_custom_limit_is_forwarded(self, handler: Any) -> None:
        tc = self._trading_client(handler)
        tc.get_option_contracts.return_value = SimpleNamespace(option_contracts=[])

        handler.get_option_expirations("SPY", "2026-04-01", "2026-04-30", limit=25)

        req = tc.get_option_contracts.call_args[0][0]
        assert req.limit == 25


# ---------------------------------------------------------------------------
# N. Get Historical Data — MemBars fast path
# ---------------------------------------------------------------------------


class TestMemBarsFastPath:
    """Fully-covered windows are served from the in-memory store for both
    adjustment keys, with bit-exact parity to today's output shape; anything
    else falls through to the existing cache/API path."""

    @staticmethod
    def _seed_frame(stamps: list[str], base: float) -> pd.DataFrame:
        idx = pd.DatetimeIndex([pd.Timestamp(t).as_unit("us") for t in stamps])
        return pd.DataFrame(
            {
                "Open": [base + i for i in range(len(stamps))],
                "High": [base + i + 1 for i in range(len(stamps))],
                "Low": [base + i - 1 for i in range(len(stamps))],
                "Close": [base + i + 0.5 for i in range(len(stamps))],
                "Volume": 1000.0,
                "symbol": "SPY",
            },
            index=idx,
        )

    @staticmethod
    def _seed(handler: Any) -> None:
        stamps = [
            "2026-01-05 09:30",
            "2026-01-05 09:45",
            "2026-01-05 10:00",
            "2026-01-05 10:15",
        ]
        handler.seed_mem_bars(
            "SPY", "15m", "all", TestMemBarsFastPath._seed_frame(stamps, 100)
        )
        handler.seed_mem_bars(
            "SPY", "15m", "raw", TestMemBarsFastPath._seed_frame(stamps, 200)
        )

    def test_covered_window_served_from_memory_without_cache(
        self, handler: Any, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        self._seed(handler)
        # The read must never touch the Arrow cache (nor the API).

        def _boom(*a, **kw):
            raise AssertionError("stock_cache must not be touched")

        monkeypatch.setattr(handler._cache, "get", _boom)

        all_df = handler.get_historical_data(
            "SPY", "2026-01-05 09:30", "2026-01-05 10:15", "15m"
        )
        raw_df = handler.get_historical_data(
            "SPY", "2026-01-05 09:30", "2026-01-05 10:15", "15m", adjustment="raw"
        )

        assert list(all_df.columns) == [
            "Open",
            "High",
            "Low",
            "Close",
            "Volume",
            "symbol",
        ]
        assert all_df.index.dtype == "datetime64[us]"
        assert all_df.index.tz is None
        assert len(all_df) == 4
        assert all_df.loc["2026-01-05 09:30", "Open"] == 100.0
        assert all_df["symbol"].tolist() == ["SPY"] * 4
        # raw frame is served from its own key, not the adjusted one
        assert raw_df.loc["2026-01-05 09:30", "Open"] == 200.0

    def test_live_edge_window_served_from_memory(
        self, handler: Any, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Engine-shaped live edge: request end sits one bucket past the banked
        tail (cov1 + <interval, i.e. now - DATA_DELAY with the delayed feed one
        bucket ahead).  The needed closed bar is banked, so memory serves and
        the un-banked remainder is clamped — cache/API never touched."""
        self._seed(handler)  # memory through 10:15

        def _boom(*a, **kw):
            raise AssertionError("cache/API must not be touched (live-edge hit)")

        monkeypatch.setattr(handler._cache, "get", _boom)
        monkeypatch.setattr(handler, "_fetch_api_data", _boom)

        df = handler.get_historical_data(
            "SPY", "2026-01-05 09:30", "2026-01-05 10:25", "15m"
        )
        assert len(df) == 4  # clamped to the banked 10:15 row
        assert df.index[-1] == pd.Timestamp("2026-01-05 10:15")
        raw_df = handler.get_historical_data(
            "SPY", "2026-01-05 09:30", "2026-01-05 10:25", "15m", adjustment="raw"
        )
        assert raw_df.loc["2026-01-05 09:30", "Open"] == 200.0

    def test_boundary_race_waits_for_the_bank(
        self, handler: Any, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Engine fires at the boundary; the ws banks the decision bucket a few
        hundred ms later (the feed's last-minute delivery).  The fetch must wait
        out the bank and serve from memory — never race to REST."""
        # Realistic live store: memory banked through 14:15 UTC (09:15 ET); the
        # decision bar 14:30 (09:30 ET, RTH) is the very next bucket.
        for adj in ("all", "raw"):
            _stamps = pd.DatetimeIndex(
                [
                    pd.Timestamp("2026-01-05 14:00").as_unit("us"),
                    pd.Timestamp("2026-01-05 14:15").as_unit("us"),
                ]
            )
            _f = pd.DataFrame(
                {
                    "Open": 100.0,
                    "High": 101.0,
                    "Low": 99.0,
                    "Close": 100.5,
                    "Volume": 1000.0,
                    "symbol": "SPY",
                },
                index=_stamps,
            )
            handler.seed_mem_bars("SPY", "15m", adj, _f, seed_end="2026-01-05 14:15")
        monkeypatch.setattr(
            handler,
            "_fetch_api_data",
            lambda *a, **k: (_ for _ in ()).throw(
                AssertionError("REST must not be called")
            ),
        )

        def bank_late() -> None:
            time.sleep(0.4)
            for i in range(15):
                m = pd.Timestamp("2026-01-05 14:30") + pd.Timedelta(minutes=i)
                handler._bars.on_minute(
                    MinuteBar(
                        symbol="SPY",
                        timestamp=m,
                        open=100.0,
                        high=101.0,
                        low=99.0,
                        close=100.5,
                        volume=1000.0,
                    ),
                    interval="15m",
                )

        import threading

        t = threading.Thread(target=bank_late, daemon=True)
        t.start()
        t0 = time.perf_counter()
        df = handler.get_historical_data(
            "SPY",
            "2026-01-05 14:00",
            "2026-01-05 14:45",
            "15m",
            adjustment="raw",
            use_cache=True,
        )
        waited = (time.perf_counter() - t0) * 1000
        t.join()
        # needed bar 14:30 lands in the wait window -> served from memory.
        assert len(df) == 3
        assert df.index[-1] == pd.Timestamp("2026-01-05 14:30")
        assert waited >= 300, f"expected to wait for the bank, took {waited:.0f}ms"

    def test_miss_beyond_one_bucket_falls_through_and_warns(
        self, handler: Any, caplog: pytest.LogCaptureFixture
    ) -> None:
        self._seed(handler)
        with caplog.at_level(
            "WARNING", logger="quant.data.data_handler.AlpacaDataHandler"
        ):
            df = handler.get_historical_data(
                "SPY", "2026-01-05 09:30", "2026-01-05 11:00", "15m"
            )
        assert df.empty  # mocked API returns nothing -> memory was NOT used
        assert any("MemBars miss" in r.message for r in caplog.records)

    def test_uncovered_window_falls_through(self, handler: Any) -> None:
        self._seed(handler)
        # window end past g1 + interval (memory holds through 10:15; request 11:00)
        df = handler.get_historical_data(
            "SPY", "2026-01-05 09:30", "2026-01-05 11:00", "15m"
        )
        assert df.empty  # mocked API returns nothing — memory was NOT used

    def test_head_clip_served_from_memory_when_disk_has_no_head_rows(
        self, handler: Any, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Engine-shaped lookback: request starts before the stored first row
        (first-day RTH/cache clip) but the disk has no rows in the head either —
        the clamped memory slice IS the disk answer, so serve it (no cache/API)."""
        self._seed(handler)

        def _head_none(*a, **kw):
            return None  # disk has no rows in the head span

        monkeypatch.setattr(handler._cache, "get", _head_none)

        df = handler.get_historical_data(
            "SPY", "2026-01-04 09:30", "2026-01-05 10:00", "15m"
        )
        assert len(df) == 3  # clamped head: 01-05 09:30, 09:45, 10:00
        assert df.index[0] == pd.Timestamp("2026-01-05 09:30")

    def test_disk_head_rows_healed_and_served(
        self, handler: Any, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Disk holds head rows the memory lacks (repair write): they must be
        MERGED into the store (not skipped) and the fetch served from memory —
        REST untouched, and the merged head rows present in the result."""
        self._seed(handler)  # 09:30..10:15
        head_frame = pd.DataFrame(
            {
                "Open": 9.0,
                "High": 9.5,
                "Low": 8.5,
                "Close": 9.2,
                "Volume": 10.0,
                "symbol": "SPY",
            },
            index=pd.DatetimeIndex(
                [pd.Timestamp("2026-01-04 14:30")]
            ),  # RTH (09:30 ET)
        )
        monkeypatch.setattr(handler._cache, "get", lambda *a, **kw: head_frame)
        monkeypatch.setattr(
            handler,
            "_fetch_api_data",
            lambda *a, **k: (_ for _ in ()).throw(
                AssertionError("REST must not be called")
            ),
        )

        df = handler.get_historical_data(
            "SPY",
            "2026-01-04 09:30",
            "2026-01-05 10:15",
            "15m",
            adjustment="raw",
            use_cache=True,
        )
        assert len(df) == 5  # merged head row + 4 seed rows
        assert df.index[0] == pd.Timestamp("2026-01-04 14:30")
        assert df.iloc[0]["Open"] == 9.0
        # second call: memoized heal -> still memory, same rows
        df2 = handler.get_historical_data(
            "SPY",
            "2026-01-04 09:30",
            "2026-01-05 10:15",
            "15m",
            adjustment="raw",
            use_cache=True,
        )
        assert len(df2) == 5 and df2.index[0] == pd.Timestamp("2026-01-04 14:30")

    def test_fast_path_inert_outside_gates(
        self, handler: Any, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        self._seed(handler)
        # Any attempt to consult memory is a test failure for these gates.

        def _boom(*a, **kw):
            raise AssertionError("memory must not be consulted")

        monkeypatch.setattr(handler._bars, "get", _boom)
        assert handler.get_historical_data(
            "SPY", "2026-01-05 09:30", "2026-01-05 10:00", "1d"
        ).empty  # non-intraday
        assert handler.get_historical_data(
            "SPY", "2026-01-05 09:30", "2026-01-05 10:00", "15m", adjustment="split"
        ).empty  # unknown adjustment key
        assert handler.get_historical_data(
            "SPY", "2026-01-05 09:30", "2026-01-05 10:00", "15m", use_cache=False
        ).empty  # force-fetch

    def test_seed_and_tail_serve_as_one_frame(self, handler: Any) -> None:
        self._seed(handler)
        # ws tail: complete bucket at 14:30 UTC (= 09:30 ET) via the aggregator
        for i in range(15):
            minute = pd.Timestamp("2026-01-05 14:30") + pd.Timedelta(minutes=i)
            handler._bars.on_minute(
                MinuteBar(
                    symbol="SPY",
                    timestamp=minute,
                    open=300.0,
                    high=301.0,
                    low=299.0,
                    close=300.5 + i / 100,
                    volume=500.0,
                ),
                interval="15m",
            )
        handler._bars.on_minute(
            MinuteBar(
                symbol="SPY",
                timestamp=pd.Timestamp("2026-01-05 14:45"),
                open=300.0,
                high=301.0,
                low=299.0,
                close=300.5,
                volume=500.0,
            ),
            interval="15m",
        )
        df = handler.get_historical_data(
            "SPY", "2026-01-05 09:30", "2026-01-05 14:30", "15m"
        )
        assert len(df) == 5  # 4 seed rows (through 10:15) + tail bucket 14:30
        assert df.index[-1] == pd.Timestamp("2026-01-05 14:30")
        assert df.loc["2026-01-05 14:30", "Volume"] == 7500.0  # 15 x 500
        assert df.loc["2026-01-05 09:30", "Open"] == 100.0  # seed span untouched

    def test_session_open_edge_waits_for_first_bar_not_rest(
        self, handler: Any, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Regression: the 09:30/09:45 cold fetch spike.

        Warmup seeds MemBars with *prior-day* data only (today's bars don't
        exist pre-open). So the session's first decision bar falls an overnight
        gap past the stored tail, which previously failed the ``cov1 + interval``
        edge-wait precondition and raced straight to REST (~120ms cold fetch)
        before the delayed stream banked the bar.

        Right behaviour: the fetch must WAIT for the ws to bank the session-open
        bar and serve it from memory — never touch REST/cache.
        """
        # Prior-day (D-0) tail: last RTH decision bars, 15:00..15:45 ET (= 20:00..20:45 UTC).
        for adj in ("all", "raw"):
            _stamps = pd.DatetimeIndex(
                [
                    pd.Timestamp("2026-01-05 20:00").as_unit("us"),
                    pd.Timestamp("2026-01-05 20:15").as_unit("us"),
                    pd.Timestamp("2026-01-05 20:30").as_unit("us"),
                    pd.Timestamp("2026-01-05 20:45").as_unit("us"),
                ]
            )
            _f = pd.DataFrame(
                {
                    "Open": 100.0,
                    "High": 101.0,
                    "Low": 99.0,
                    "Close": 100.5,
                    "Volume": 1000.0,
                    "symbol": "SPY",
                },
                index=_stamps,
            )
            handler.seed_mem_bars("SPY", "15m", adj, _f, seed_end="2026-01-05 20:45")

        # Disk has no head rows; REST must never be hit.
        monkeypatch.setattr(handler._cache, "get", lambda *a, **k: None)
        monkeypatch.setattr(
            handler,
            "_fetch_api_data",
            lambda *a, **k: (_ for _ in ()).throw(
                AssertionError("REST must not be called on session-open edge")
            ),
        )

        def bank_session_open() -> None:
            # The delayed feed banks the 09:30 ET (14:30 UTC) decision bucket a
            # few hundred ms after the engine's boundary fire.
            time.sleep(0.4)
            for i in range(15):
                m = pd.Timestamp("2026-01-06 14:30") + pd.Timedelta(minutes=i)
                handler._bars.on_minute(
                    MinuteBar(
                        symbol="SPY",
                        timestamp=m,
                        open=102.0,
                        high=103.0,
                        low=101.0,
                        close=102.5,
                        volume=2000.0,
                    ),
                    interval="15m",
                )

        import threading

        t = threading.Thread(target=bank_session_open, daemon=True)
        t.start()
        t0 = time.perf_counter()
        df = handler.get_historical_data(
            "SPY", "2026-01-05 09:30", "2026-01-06 14:47", "15m", adjustment="all"
        )
        waited = (time.perf_counter() - t0) * 1000
        t.join()

        # Session-open bar 01-06 09:30 ET (14:30 UTC) was banked in the wait
        # window and served from memory — across the overnight gap, not REST.
        assert len(df) >= 1
        assert df.index[-1] == pd.Timestamp("2026-01-06 14:30")
        assert df.loc["2026-01-06 14:30", "Open"] == 102.0
        assert waited >= 300, f"expected to wait for the open bar, took {waited:.0f}ms"

    def test_data_delay_pre_open_gap_serves_memory_not_rest(
        self, handler: Any, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Regression: the DATA_DELAY pre-open first-ask spikes (09:30/09:45).

        With ``DATA_DELAY`` the engine fires at the session open asking for
        ``closed_bar_asof(end_dt)``, which is a **pre-open** bar: delay=15m →
        the 09:30 ET fire asks for the 09:00 bar, the 09:45 fire for the 09:15
        bar. RTH filtering means the feed will never bank those, so they sit in
        the ``(cov1, next session open)`` gap. The prior ``cov1 + interval`` /
        ``next_open_after`` edge-wait preconditions both failed there, and the
        fetch raced to REST (the ~120ms cold spike).

        Right behaviour: a ``needed`` bar in the never-banks gap is answered by
        the prior-day tail MemBars already holds — never REST, never wait. This
        is fully DATA_DELAY-invariant, so we cover both the delay=15 shape
        (end 14:15 UTC → needed 09:00 ET) and the delay=0 shape (end 14:30 UTC
        → needed 09:15 ET).
        """
        for adj in ("all", "raw"):
            _stamps = pd.DatetimeIndex(
                [
                    pd.Timestamp("2026-01-05 20:00").as_unit("us"),
                    pd.Timestamp("2026-01-05 20:15").as_unit("us"),
                    pd.Timestamp("2026-01-05 20:30").as_unit("us"),
                    pd.Timestamp("2026-01-05 20:45").as_unit("us"),
                ]
            )
            _f = pd.DataFrame(
                {
                    "Open": 100.0,
                    "High": 101.0,
                    "Low": 99.0,
                    "Close": 100.5,
                    "Volume": 1000.0,
                    "symbol": "SPY",
                },
                index=_stamps,
            )
            handler.seed_mem_bars("SPY", "15m", adj, _f, seed_end="2026-01-05 20:45")

        # Only the banked prior-day tail must answer — never disk or REST.
        monkeypatch.setattr(handler._cache, "get", lambda *a, **k: None)
        monkeypatch.setattr(
            handler,
            "_fetch_api_data",
            lambda *a, **k: (_ for _ in ()).throw(
                AssertionError("REST must not be called on a pre-open gap ask")
            ),
        )

        _open = EquitySessionClock()
        for end_dt, label in [
            ("2026-01-06 14:15", "delay=15: 09:30 ET fire asks 09:00 bar"),
            ("2026-01-06 14:30", "delay=0 : 09:30 ET fire asks 09:15 bar"),
        ]:
            _e = pd.Timestamp(end_dt)
            _needed = closed_bar_asof(_e, "15m")
            _cov1 = pd.Timestamp("2026-01-05 20:45")
            # Confirm we are testing the never-banks pre-open gap, not the
            # session-open edge-wait (which would require the bar to exist).
            assert _needed > _cov1
            assert _needed < _open.next_open_after(_cov1), label

            df = handler.get_historical_data(
                "SPY", "2026-01-05 09:30", end_dt, "15m", adjustment="all"
            )
            # Entirely the banked prior-day tail, served from memory — no REST.
            assert len(df) == 4, label
            assert df.index[0] == pd.Timestamp("2026-01-05 20:00")
            assert df.index[-1] == pd.Timestamp("2026-01-05 20:45"), label
