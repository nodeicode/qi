"""Direct tests for the generic Arrow IPC data plane and SQLite catalog."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pandas as pd
import pyarrow as pa

from quant.data.cache.arrow_ipc import ArrowIpcCache, CacheKey, SimpleDatasetSpec


def _key(symbol: str = "SPY") -> CacheKey:
    return CacheKey.build("bars", "alpaca", "stock", {"symbol": symbol}, "1m")


def _table(start: str = "2025-01-02 14:30", periods: int = 3) -> pa.Table:
    timestamps = pd.date_range(start, periods=periods, freq="min", tz="UTC")
    return pa.table(
        {"timestamp": timestamps, "close": [float(index) for index in range(periods)]}
    )


def test_roundtrip_and_exact_coverage(tmp_path) -> None:
    cache = ArrowIpcCache(tmp_path)
    spec = SimpleDatasetSpec("bars")
    start = datetime(2025, 1, 2, 14, 30, tzinfo=UTC)
    end = start + timedelta(minutes=2)
    cache.commit_timeseries(_key(), spec, _table(), [(start, end)])

    result = cache.read_range(_key(), spec, start, end)
    assert result is not None
    assert result.covered is True
    assert result.table.num_rows == 3
    assert cache.missing_ranges(_key(), start, end) == []
    assert cache.missing_ranges(_key(), start - timedelta(minutes=1), end)


def test_later_generation_replaces_partition_and_deduplicates(tmp_path) -> None:
    cache = ArrowIpcCache(tmp_path)
    spec = SimpleDatasetSpec("bars")
    start = datetime(2025, 1, 2, 14, 30, tzinfo=UTC)
    first = _table(periods=2)
    second = pa.table(
        {
            "timestamp": pd.date_range(
                "2025-01-02 14:31", periods=2, freq="min", tz="UTC"
            ),
            "close": [99.0, 2.0],
        }
    )
    cache.commit_timeseries(
        _key(), spec, first, [(start, start + timedelta(minutes=1))]
    )
    cache.commit_timeseries(
        _key(),
        spec,
        second,
        [(start + timedelta(minutes=1), start + timedelta(minutes=2))],
    )

    result = cache.read_range(_key(), spec, start, start + timedelta(minutes=2))
    assert result is not None
    frame = result.table.to_pandas()
    assert len(frame) == 3
    assert (
        frame.loc[
            frame["timestamp"] == pd.Timestamp("2025-01-02 14:31", tz="UTC"), "close"
        ].item()
        == 99.0
    )


def test_empty_coverage_suppresses_refetch_without_creating_data_file(tmp_path) -> None:
    cache = ArrowIpcCache(tmp_path)
    spec = SimpleDatasetSpec("bars")
    start = datetime(2025, 1, 2, tzinfo=UTC)
    end = start + timedelta(days=1)
    cache.commit_timeseries(_key(), spec, None, [(start, end)])

    result = cache.read_range(_key(), spec, start, end)
    assert result is not None and result.covered
    assert result.table.num_rows == 0
    assert cache.stats()["entries"] == 0


def test_validation_deactivates_corrupted_active_file(tmp_path) -> None:
    cache = ArrowIpcCache(tmp_path)
    spec = SimpleDatasetSpec("bars")
    start = datetime(2025, 1, 2, 14, 30, tzinfo=UTC)
    cache.commit_timeseries(
        _key(), spec, _table(), [(start, start + timedelta(minutes=2))]
    )
    path = next(cache.data_dir.rglob("*.arrow"))
    path.write_bytes(b"broken")

    assert str(path) in cache.validate()
    res = cache.read_range(_key(), spec, start, start + timedelta(minutes=2))
    assert res is not None
    assert res.table.num_rows == 0
