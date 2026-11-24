"""Option-bar and option-reference cache facade."""

from __future__ import annotations

from datetime import datetime
from typing import TypedDict

import pandas as pd
import pyarrow as pa

from .core import ArrowIpcCache
from .frame_codec import frame_to_table, table_to_frame
from .models import CacheKey, SimpleDatasetSpec

_BARS = SimpleDatasetSpec("option_bars")


class OptionCacheStats(TypedDict):
    total_contracts: int
    total_rows: int
    total_size_mb: float
    by_ticker: dict[str, dict[str, int]]


class OptionDataCache:
    def __init__(self, cache_dir: str, verbose: bool = False) -> None:
        del verbose
        self._engine = ArrowIpcCache(cache_dir)

    @staticmethod
    def _key(
        ticker: str, strike: float, expiration: str, option_type: str, interval: str
    ) -> CacheKey:
        return CacheKey.build(
            "option_bars",
            "market",
            "option",
            {
                "ticker": ticker.upper(),
                "strike": strike,
                "expiration": expiration,
                "option_type": option_type.upper(),
            },
            interval,
        )

    @staticmethod
    def _reference_key(ticker: str, kind: str) -> CacheKey:
        return CacheKey.build(kind, "market", "option", {"ticker": ticker.upper()})

    def get(
        self,
        ticker: str,
        strike: float,
        expiration: str,
        option_type: str,
        start: datetime,
        end: datetime,
        interval: str,
    ) -> pd.DataFrame | None:
        result = self._engine.read_range(
            self._key(ticker, strike, expiration, option_type, interval),
            _BARS,
            start,
            end,
        )
        return table_to_frame(result.table) if result is not None else None

    def get_superset(
        self,
        ticker: str,
        strike: float,
        expiration: str,
        option_type: str,
        start: datetime,
        end: datetime,
        interval: str,
    ) -> pd.DataFrame | None:
        key = self._key(ticker, strike, expiration, option_type, interval)
        return (
            self.get(ticker, strike, expiration, option_type, start, end, interval)
            if self._engine.is_covered(key, start, end)
            else None
        )

    def put(
        self,
        ticker: str,
        strike: float,
        expiration: str,
        option_type: str,
        start: datetime,
        end: datetime,
        interval: str,
        data: pd.DataFrame,
    ) -> None:
        self._engine.commit_timeseries(
            self._key(ticker, strike, expiration, option_type, interval),
            _BARS,
            frame_to_table(data) if not data.empty else None,
            [(start, end)],
        )

    def save_expirations(self, ticker: str, expirations: list[str]) -> None:
        if not expirations:
            return
        key = self._reference_key(ticker, "option_expirations")
        old = self._engine.get_reference(key)
        values = set(expirations)
        if old is not None and "expiration" in old.column_names:
            values.update(str(value) for value in old["expiration"].to_pylist())
        self._engine.put_reference(key, pa.table({"expiration": sorted(values)}))

    def get_expirations(self, ticker: str) -> list[str]:
        table = self._engine.get_reference(
            self._reference_key(ticker, "option_expirations")
        )
        return (
            sorted(str(value) for value in table["expiration"].to_pylist())
            if table is not None and "expiration" in table.column_names
            else []
        )

    def record_expiration_query(self, ticker: str, start: str, end: str) -> None:
        self._engine.commit_timeseries(
            self._reference_key(ticker, "option_expiration_queries"),
            _BARS,
            None,
            [(start, end)],
        )

    def is_range_covered(self, ticker: str, start: str, end: str) -> bool:
        return self._engine.is_covered(
            self._reference_key(ticker, "option_expiration_queries"), start, end
        )

    def get_cache_stats(self) -> OptionCacheStats:
        stats = self._engine.dataset_stats("option_bars")
        return {
            "total_contracts": int(stats["entries"]),
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
