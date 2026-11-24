"""Supported domain-cache contracts, independent of handler implementation."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pandas as pd

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
