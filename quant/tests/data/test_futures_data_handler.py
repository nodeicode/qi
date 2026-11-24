"""
Tests for quant.data.futures_data_handler.FuturesDataHandler (Databento-only).

Covers:
  - Interval → schema mapping and resampling
  - Futures bar fetch pipeline with cache gap-fill
  - OOF raw_symbol construction (CME weekly symbology)
  - CBBO quote extraction
  - Expiration discovery for OOF roots
"""

from __future__ import annotations

import datetime
from unittest.mock import MagicMock, patch

import numpy as np
import pandas as pd
import pytest


@pytest.fixture(autouse=True)
def _stub_env(monkeypatch):
    monkeypatch.setenv("DATABENTO_API_KEY", "test_databento_key")


@pytest.fixture()
def _patch_clients(monkeypatch):
    monkeypatch.setattr("quant.data.futures_data_handler.db.Historical", MagicMock)


def _make_1m_ohlcv_df(
    n_bars: int = 60,
    start: str = "2025-06-20 09:30",
    base_price: float = 5000.0,
) -> pd.DataFrame:
    idx = pd.date_range(start, periods=n_bars, freq="1min", tz="UTC")
    rng = np.random.default_rng(42)
    closes = base_price + rng.standard_normal(n_bars).cumsum() * 0.5
    opens = np.roll(closes, 1)
    opens[0] = base_price
    highs = np.maximum(opens, closes) + rng.uniform(0, 0.1, n_bars)
    lows = np.minimum(opens, closes) - rng.uniform(0, 0.1, n_bars)
    vols = rng.integers(10, 500, n_bars).astype(float)
    return pd.DataFrame(
        {"open": opens, "high": highs, "low": lows, "close": closes, "volume": vols},
        index=idx,
    )


def _make_databento_raw_df(
    n_bars: int = 60, start: str = "2025-06-20 09:30"
) -> pd.DataFrame:
    idx = pd.date_range(start, periods=n_bars, freq="1min", tz="UTC")
    rng = np.random.default_rng(42)
    closes = 5000.0 + rng.standard_normal(n_bars).cumsum() * 0.5
    opens = np.roll(closes, 1)
    opens[0] = 5000.0
    highs = np.maximum(opens, closes) + 0.1
    lows = np.minimum(opens, closes) - 0.1
    vols = rng.integers(10, 500, n_bars).astype(float)
    df = pd.DataFrame(
        {
            "ts_event": idx,
            "open": opens,
            "high": highs,
            "low": lows,
            "close": closes,
            "volume": vols,
            "symbol": "ESZ4",
        }
    )
    df.index = idx
    return df


def _make_oof_definition_df(
    root: str = "ES",
    strike: float = 5800.0,
    expiration: str = "2025-03-21",
    opt_type: str = "P",
    raw_symbol: str = "EW3H5 P5800",
) -> pd.DataFrame:
    return pd.DataFrame(
        [
            {
                "ts_event": pd.Timestamp("2025-03-01", tz="UTC"),
                "raw_symbol": raw_symbol,
                "expiration": pd.Timestamp(expiration, tz="UTC"),
                "instrument_class": opt_type,
                "strike_price": strike,
                "symbol": f"{root}.OPT",
            }
        ]
    )


@pytest.fixture()
def tmp_cache(tmp_path):
    return str(tmp_path / "futures_cache")


@pytest.fixture()
def futures_handler(tmp_cache, _patch_clients):
    from quant.data.futures_data_handler import FuturesDataHandler

    return FuturesDataHandler(cache_dir=tmp_cache)


class TestIntervalMapping:
    def _cls(self):
        from quant.data.futures_data_handler import FuturesDataHandler

        return FuturesDataHandler

    @pytest.mark.parametrize(
        "interval,expected",
        [
            ("1m", "ohlcv-1m"),
            ("5m", "ohlcv-1m"),
            ("1h", "ohlcv-1h"),
            ("1d", "ohlcv-1d"),
        ],
    )
    def test_interval_to_schema(self, interval, expected, _patch_clients):
        assert self._cls()._interval_to_schema(interval) == expected


class TestProcessDatabentoDf:
    def test_processes_and_synthesizes_bid_ask(self, _patch_clients):
        from quant.data.futures_data_handler import FuturesDataHandler

        raw = _make_databento_raw_df(n_bars=10)
        result = FuturesDataHandler._process_databento_df(raw.copy(), "1m")
        assert not result.empty
        for col in ("bid", "ask", "mid"):
            assert col in result.columns

    def test_resamples_5m(self, _patch_clients):
        from quant.data.futures_data_handler import FuturesDataHandler

        raw = _make_databento_raw_df(n_bars=60)
        result = FuturesDataHandler._process_databento_df(raw.copy(), "5m")
        assert len(result) < 60


class TestFuturesBars:
    def test_get_futures_data_cache_miss_fetches(
        self, futures_handler, _patch_clients
    ) -> None:
        raw = _make_databento_raw_df(n_bars=30)
        mock_store = MagicMock()
        mock_store.to_df.return_value = raw
        futures_handler.databento_client.timeseries.get_range.return_value = mock_store

        start = "2025-06-20 09:30"
        end = "2025-06-20 10:00"
        df = futures_handler.get_futures_data(
            "ESZ4", start, end, "1m", stype_in="raw_symbol"
        )
        assert not df.empty
        assert "Open" in df.columns
        assert df["symbol"].iloc[0] == "ESZ4"
        futures_handler.databento_client.timeseries.get_range.assert_called_once()

    def test_get_futures_data_cache_hit(self, futures_handler, _patch_clients) -> None:
        bars = _make_1m_ohlcv_df(n_bars=20)
        t0 = bars.index[0].to_pydatetime()
        t1 = bars.index[-1].to_pydatetime()
        futures_handler._cache.put_futures("ES.c.0", t0, t1, "1m", bars)

        df = futures_handler.get_futures_data(
            "ES.c.0",
            t0.isoformat(),
            t1.isoformat(),
            "1m",
            stype_in="continuous",
        )
        assert not df.empty
        # Handler boundary: UTC-naive (parity with AlpacaDataHandler intraday).
        assert isinstance(df.index, pd.DatetimeIndex)
        assert df.index.tz is None
        futures_handler.databento_client.timeseries.get_range.assert_not_called()

    def test_finalize_futures_index_strips_tz(self, _patch_clients) -> None:
        from quant.data.futures_data_handler import FuturesDataHandler

        df = _make_1m_ohlcv_df(n_bars=5)
        assert isinstance(df.index, pd.DatetimeIndex)
        assert df.index.tz is not None
        out = FuturesDataHandler._finalize_futures_index(df, "15m")
        assert isinstance(out.index, pd.DatetimeIndex)
        assert out.index.tz is None
        # Same UTC wall clock — first stamp unchanged after convert+strip.
        assert out.index[0] == df.index[0].tz_convert("UTC").tz_localize(None)

    def test_finalize_futures_index_daily_keeps_tz(self, _patch_clients) -> None:
        from quant.data.futures_data_handler import FuturesDataHandler

        df = _make_1m_ohlcv_df(n_bars=3)
        out = FuturesDataHandler._finalize_futures_index(df, "1d")
        assert isinstance(out.index, pd.DatetimeIndex)
        assert out.index.tz is not None

    def test_get_historical_data_alias(self, futures_handler, _patch_clients) -> None:
        raw = _make_databento_raw_df(n_bars=10)
        mock_store = MagicMock()
        mock_store.to_df.return_value = raw
        futures_handler.databento_client.timeseries.get_range.return_value = mock_store

        df = futures_handler.get_historical_data(
            "ES.c.0", "2025-06-20 09:30", "2025-06-20 09:40", "1m"
        )
        assert not df.empty
        call_kwargs = (
            futures_handler.databento_client.timeseries.get_range.call_args.kwargs
        )
        assert call_kwargs["stype_in"] == "continuous"


class TestOofSymbolResolution:
    def test_format_oof_raw_symbol_mes_wednesday(self, _patch_clients) -> None:
        from quant.data.futures_data_handler import format_oof_raw_symbol

        assert (
            format_oof_raw_symbol("MES", 6000.0, "2026-01-07", "put") == "X1CF6 P6000"
        )

    def test_format_oof_raw_symbol_es_friday(self, _patch_clients) -> None:
        from quant.data.futures_data_handler import format_oof_raw_symbol

        assert format_oof_raw_symbol("ES", 5800.0, "2025-03-21", "P") == "EW3H5 P5800"

    def test_resolve_oof_raw_symbol_uses_cache(
        self, futures_handler, _patch_clients
    ) -> None:
        futures_handler._cache.set_oof_raw_symbol(
            "ES", 5800.0, "2025-03-21", "P", "CACHED_SYM"
        )
        sym = futures_handler._resolve_oof_raw_symbol("ES", 5800.0, "2025-03-21", "P")
        assert sym == "CACHED_SYM"
        futures_handler.databento_client.timeseries.get_range.assert_not_called()

    def test_resolve_oof_raw_symbol_builds_symbology(
        self, futures_handler, _patch_clients
    ) -> None:
        sym = futures_handler._resolve_oof_raw_symbol(
            "MES",
            6900.0,
            "2026-01-16",
            "P",
        )
        assert sym == "EX3F6 P6900"
        futures_handler.databento_client.timeseries.get_range.assert_not_called()


class TestOofFetch:
    def test_fetch_futures_option_data(self, futures_handler, _patch_clients) -> None:
        futures_handler._cache.set_oof_raw_symbol(
            "ES", 5800.0, "2025-03-21", "P", "EW3H5 P5800"
        )
        raw = _make_databento_raw_df(n_bars=15)
        mock_store = MagicMock()
        mock_store.to_df.return_value = raw
        futures_handler.databento_client.timeseries.get_range.return_value = mock_store

        start = datetime.datetime(2025, 3, 1, 14, 30, tzinfo=datetime.UTC)
        end = datetime.datetime(2025, 3, 1, 15, 0, tzinfo=datetime.UTC)
        df = futures_handler.fetch_futures_option_data(
            "ES", 5800.0, "2025-03-21", "P", start, end, "1m"
        )
        assert not df.empty


class TestCbbo:
    def test_cbbo_df_to_cache_df(self, _patch_clients):
        from quant.data.futures_data_handler import FuturesDataHandler

        idx = pd.date_range("2025-03-01 14:30", periods=5, freq="1s", tz="UTC")
        raw = pd.DataFrame(
            {
                "bid_px_00": [10.0, 10.1, 10.2, 10.3, 10.4],
                "ask_px_00": [10.2, 10.3, 10.4, 10.5, 10.6],
                "symbol": ["EW3H5 P5800"] * 5,
            },
            index=idx,
        )
        out = FuturesDataHandler._cbbo_df_to_cache_df(raw, symbol="EW3H5 P5800")
        assert not out.empty
        assert "mid" in out.columns

    def test_fetch_futures_option_quote(self, futures_handler, _patch_clients) -> None:
        futures_handler._cache.set_oof_raw_symbol(
            "ES", 5800.0, "2025-03-21", "P", "EW3H5 P5800"
        )
        idx = pd.date_range("2025-03-01 14:29", periods=10, freq="1s", tz="UTC")
        raw = pd.DataFrame(
            {
                "bid_px_00": [10.0] * 10,
                "ask_px_00": [10.2] * 10,
                "symbol": ["EW3H5 P5800"] * 10,
            },
            index=idx,
        )
        mock_store = MagicMock()
        mock_store.to_df.return_value = raw
        futures_handler.databento_client.timeseries.get_range.return_value = mock_store

        at = datetime.datetime(2025, 3, 1, 14, 30, tzinfo=datetime.UTC)
        mid = futures_handler.fetch_futures_option_quote(
            "ES", 5800.0, "2025-03-21", "P", at
        )
        assert mid == pytest.approx(10.1)


class TestStrikeSnap:
    def test_snap_strike_to_increment(self, _patch_clients) -> None:
        from quant.data.futures_data_handler import FuturesDataHandler

        assert FuturesDataHandler.snap_strike_to_increment(6797.0, 10.0) == 6800.0
        assert FuturesDataHandler.snap_strike_to_increment(6793.0, 10.0) == 6790.0


class TestPrefetchIcCbbo:
    def test_prefetch_ic_cbbo_returns_full_cbbo(
        self, futures_handler, _patch_clients
    ) -> None:
        futures_handler._cache.set_oof_raw_symbol(
            "MES", 5100.0, "2025-06-20", "P", "MES_OPT_P5100"
        )
        idx = pd.date_range("2025-06-20 14:29", periods=10, freq="1s", tz="UTC")
        raw = pd.DataFrame(
            {
                "bid_px_00": [5.0] * 10,
                "ask_px_00": [5.4] * 10,
                "symbol": ["MES_OPT_P5100"] * 10,
            },
            index=idx,
        )
        mock_store = MagicMock()
        mock_store.to_df.return_value = raw
        futures_handler.databento_client.timeseries.get_range.return_value = mock_store

        at = datetime.datetime(2025, 6, 20, 14, 30, tzinfo=datetime.UTC)
        leg_key = (0, "sp")
        out = futures_handler.prefetch_ic_cbbo(
            [
                {
                    "root": "MES",
                    "strike": 5100.0,
                    "expiration": "2025-06-20",
                    "opt_type": "P",
                    "at_time": at,
                    "leg_key": leg_key,
                }
            ],
            window_min=2,
        )
        assert leg_key in out
        cbbo = out[leg_key]
        assert cbbo is not None
        assert cbbo["bid"] == pytest.approx(5.0)
        assert cbbo["ask"] == pytest.approx(5.4)
        assert cbbo["mid"] == pytest.approx(5.2)
        assert "ts_used" in cbbo
        assert futures_handler._last_cbbo_prefetch_stats["legs_with_quote"] == 1

    def test_fetch_cbbo_range_batch(self, futures_handler, _patch_clients) -> None:
        futures_handler._cache.set_oof_raw_symbol(
            "MES", 5100.0, "2025-06-20", "P", "MES_OPT_P5100"
        )
        idx = pd.date_range("2025-06-20 14:00", periods=30, freq="1min", tz="UTC")
        raw = pd.DataFrame(
            {
                "bid_px_00": [5.0] * 30,
                "ask_px_00": [5.2] * 30,
                "symbol": ["MES_OPT_P5100"] * 30,
            },
            index=idx,
        )
        mock_store = MagicMock()
        mock_store.to_df.return_value = raw
        futures_handler.databento_client.timeseries.get_range.return_value = mock_store

        start = datetime.datetime(2025, 6, 20, 14, 0, tzinfo=datetime.UTC)
        end = datetime.datetime(2025, 6, 20, 14, 30, tzinfo=datetime.UTC)
        cache = futures_handler.fetch_cbbo_range_batch(
            [
                {
                    "root": "MES",
                    "strike": 5100.0,
                    "expiration": "2025-06-20",
                    "opt_type": "P",
                    "start_dt": start,
                    "end_dt": end,
                }
            ]
        )
        key = ("MES", "2025-06-20", 5100.0, "P")
        assert key in cache
        assert not cache[key].empty
        assert "bid" in cache[key].columns


class TestExpirations:
    def test_parent_symbol_for_expiration_mes(self, _patch_clients) -> None:
        from quant.data.futures_data_handler import parent_symbol_for_expiration

        assert (
            parent_symbol_for_expiration("MES", datetime.date(2026, 1, 7)) == "X1C.OPT"
        )
        assert (
            parent_symbol_for_expiration("MES", datetime.date(2026, 1, 16)) == "EX3.OPT"
        )

    def test_fetch_futures_expirations_databento(
        self, futures_handler, _patch_clients
    ) -> None:
        def_df = pd.DataFrame(
            {
                "expiration": [
                    pd.Timestamp("2026-01-07", tz="UTC"),
                    pd.Timestamp("2026-01-14", tz="UTC"),
                ]
            }
        )
        mock_store = MagicMock()
        mock_store.to_df.return_value = def_df
        futures_handler.databento_client.timeseries.get_range.return_value = mock_store

        start = datetime.datetime(2026, 1, 7, tzinfo=datetime.UTC)
        end = datetime.datetime(2026, 1, 8, tzinfo=datetime.UTC)
        exps = futures_handler._fetch_futures_expirations_databento(
            "MES", start, end, lookup="2026-01-07"
        )
        assert exps == ["2026-01-07", "2026-01-14"]
        call_kwargs = (
            futures_handler.databento_client.timeseries.get_range.call_args.kwargs
        )
        assert call_kwargs["symbols"] == ["X1C.OPT"]
        assert call_kwargs["schema"] == "definition"

    def test_probe_single_futures_expiration_prefers_nearest_weekly(
        self, futures_handler, _patch_clients, monkeypatch
    ) -> None:
        monkeypatch.setattr(
            futures_handler,
            "_fetch_expirations_for_parent",
            lambda *a, **k: ["2025-03-07", "2025-06-20"],
        )
        start = datetime.datetime(2025, 3, 7, tzinfo=datetime.UTC)
        end = start + datetime.timedelta(days=2)
        exp = futures_handler._probe_single_futures_expiration("MES", start, end)
        assert exp == "2025-03-07"

    def test_probe_single_futures_expiration_exact_day(
        self, futures_handler, _patch_clients, monkeypatch
    ) -> None:
        monkeypatch.setattr(
            futures_handler,
            "_fetch_expirations_for_parent",
            lambda *a, **k: ["2025-03-07", "2025-06-20"],
        )
        start = datetime.datetime(2025, 3, 7, tzinfo=datetime.UTC)
        end = start + datetime.timedelta(days=2)
        assert (
            futures_handler._probe_single_futures_expiration(
                "MES", start, end, exact_day="2025-03-07"
            )
            == "2025-03-07"
        )
        assert (
            futures_handler._probe_single_futures_expiration(
                "MES", start, end, exact_day="2025-03-08"
            )
            is None
        )

    def test_build_zero_dte_expiration_cache(
        self, futures_handler, _patch_clients, monkeypatch
    ) -> None:
        listed = {
            "2026-01-05",
            "2026-01-06",
            "2026-01-07",
            "2026-01-12",
        }

        def _fake_parent(parent, start, end, *, limit=None):
            day = start.strftime("%Y-%m-%d")
            return [day] if day in listed else []

        monkeypatch.setattr(
            futures_handler, "_fetch_expirations_for_parent", _fake_parent
        )
        days = [
            datetime.date(2026, 1, 5),
            datetime.date(2026, 1, 6),
            datetime.date(2026, 1, 7),
            datetime.date(2026, 1, 8),
        ]
        cache = futures_handler._build_zero_dte_expiration_cache("MES", days)
        assert cache == {
            "2026-01-05": "2026-01-05",
            "2026-01-06": "2026-01-06",
            "2026-01-07": "2026-01-07",
        }

    def test_build_zero_dte_expiration_cache_uses_local_cache(
        self, futures_handler, _patch_clients, monkeypatch
    ) -> None:
        futures_handler._cache.save_expirations(
            "MES", ["2026-01-05", "2026-01-06", "2026-01-07"]
        )

        def _should_not_call_api(*_args, **_kwargs):
            raise AssertionError("API should not be called when cache has exact dates")

        monkeypatch.setattr(
            futures_handler, "_fetch_expirations_for_parent", _should_not_call_api
        )
        days = [
            datetime.date(2026, 1, 5),
            datetime.date(2026, 1, 6),
            datetime.date(2026, 1, 7),
        ]
        cache = futures_handler._build_zero_dte_expiration_cache("MES", days)
        assert cache == {
            "2026-01-05": "2026-01-05",
            "2026-01-06": "2026-01-06",
            "2026-01-07": "2026-01-07",
        }

    def test_resolve_futures_expirations_from_cache(
        self, futures_handler, _patch_clients
    ) -> None:
        futures_handler._cache.save_expirations("ES", ["2025-03-21", "2025-06-20"])
        out = futures_handler.resolve_futures_expirations("ES", ["2025-03-01"])
        assert out["2025-03-01"] == "2025-03-21"
