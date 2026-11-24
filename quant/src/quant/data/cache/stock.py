"""Stock-bar cache facade over the dataset-agnostic Arrow cache."""

from __future__ import annotations

from datetime import datetime
from typing import TypedDict

import pandas as pd

from .._times import from_ns
from .core import ArrowIpcCache
from .frame_codec import frame_to_table, table_to_frame
from .models import CacheKey, SimpleDatasetSpec

_BARS = SimpleDatasetSpec("stock_bars")


class StockCacheStats(TypedDict):
    total_stocks: int
    total_rows: int
    total_size_mb: float
    by_ticker: dict[str, dict[str, int]]


class StockDataCache:
    """Supported stock cache API; handlers keep this facade private."""

    def __init__(self, cache_dir: str, verbose: bool = False) -> None:
        del verbose
        self._engine = ArrowIpcCache(cache_dir)

    @staticmethod
    def _key(ticker: str, interval: str) -> CacheKey:
        adjustment = "raw" if interval.endswith("_raw") else "all"
        return CacheKey.build(
            "stock_bars",
            "market",
            "stock",
            {"symbol": ticker.upper()},
            interval.removesuffix("_raw"),
            adjustment,
        )

    def get_coverage(
        self, ticker: str, interval: str
    ) -> tuple[pd.Timestamp | None, pd.Timestamp | None]:
        key = self._key(ticker, interval)
        with self._engine._connect() as conn:
            key_id = self._engine._existing_key_id(conn, key)
            if key_id is None:
                return None, None
            row = conn.execute(
                "SELECT MIN(start_ns) AS start_ns, MAX(end_ns) AS end_ns FROM coverage_segments WHERE key_id=?",
                (key_id,),
            ).fetchone()
        if row["start_ns"] is None:
            return None, None
        return from_ns(int(row["start_ns"])), from_ns(int(row["end_ns"]))

    def update_coverage(
        self, ticker: str, interval: str, start: datetime, end: datetime
    ) -> None:
        self._engine.commit_timeseries(
            self._key(ticker, interval), _BARS, None, [(start, end)]
        )

    def get(
        self, ticker: str, start: datetime, end: datetime, interval: str
    ) -> pd.DataFrame | None:
        result = self._engine.read_range(self._key(ticker, interval), _BARS, start, end)
        return table_to_frame(result.table) if result is not None else None

    def get_superset(
        self, ticker: str, start: datetime, end: datetime, interval: str
    ) -> pd.DataFrame | None:
        key = self._key(ticker, interval)
        return (
            self.get(ticker, start, end, interval)
            if self._engine.is_covered(key, start, end)
            else None
        )

    def missing_ranges(
        self, ticker: str, start: datetime, end: datetime, interval: str
    ) -> list[tuple[pd.Timestamp, pd.Timestamp]]:
        """Return true uncovered intervals; never collapse them to min/max."""
        return self._engine.missing_ranges(self._key(ticker, interval), start, end)

    def is_covered(
        self, ticker: str, start: datetime, end: datetime, interval: str
    ) -> bool:
        """Whether provider-success coverage spans the full requested range."""
        return self._engine.is_covered(self._key(ticker, interval), start, end)

    def put(
        self,
        ticker: str,
        start: datetime,
        end: datetime,
        interval: str,
        data: pd.DataFrame,
    ) -> None:
        self._engine.commit_timeseries(
            self._key(ticker, interval),
            _BARS,
            frame_to_table(data) if not data.empty else None,
            [(start, end)],
        )

    def get_cache_stats(self) -> StockCacheStats:
        stats = self._engine.dataset_stats("stock_bars")
        return {
            "total_stocks": int(stats["entries"]),
            "total_rows": int(stats["rows"]),
            "total_size_mb": float(stats["size_mb"]),
            "by_ticker": {},
        }

    def cleanup_old_entries(self, days_old: int = 30) -> None:
        self._engine.purge_before(
            pd.Timestamp.now(tz="UTC") - pd.Timedelta(days=days_old)
        )

    def clear_cache(self) -> None:
        self._engine.clear()
