"""Supported domain-cache contracts, independent of handler implementation."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pandas as pd

from quant.data.cache.futures import FuturesDataCache
from quant.data.cache.options import OptionDataCache
from quant.data.cache.stock import StockDataCache


def _bars(start: datetime) -> pd.DataFrame:
    return pd.DataFrame(
        {"close": [100.0, 101.0]},
        index=pd.date_range(start, periods=2, freq="min", tz="UTC"),
    )


def test_stock_adjustment_keys_are_isolated(tmp_path) -> None:
    cache = StockDataCache(str(tmp_path))
    start = datetime(2025, 1, 2, 14, 30, tzinfo=UTC)
    end = start + timedelta(minutes=1)
    cache.put("spy", start, end, "1m", _bars(start))

    assert cache.get("SPY", start, end, "1m") is not None
    assert cache.get("SPY", start, end, "1m_raw") is None


def test_option_expiration_reference_and_query_coverage(tmp_path) -> None:
    cache = OptionDataCache(str(tmp_path))
    cache.save_expirations("spy", ["2025-01-17"])
    cache.record_expiration_query("SPY", "2025-01-01", "2025-01-03")

    assert cache.get_expirations("SPY") == ["2025-01-17"]
    assert cache.is_range_covered("SPY", "2025-01-01", "2025-01-03")


def test_futures_oof_raw_symbol_and_expirations(tmp_path) -> None:
    cache = FuturesDataCache(str(tmp_path))
    cache.set_oof_raw_symbol("es", 6000, "2025-01-17", "p", "EXAMPLE P6000")
    cache.save_expirations("ES", ["2025-01-17"])

    assert cache.get_oof_raw_symbol("ES", 6000, "2025-01-17", "P") == "EXAMPLE P6000"
    assert cache.get_expirations("ES") == ["2025-01-17"]
