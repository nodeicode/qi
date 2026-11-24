"""Futures and options-on-futures cache facade."""

from __future__ import annotations

from datetime import datetime
from typing import TypedDict

import pandas as pd
import pyarrow as pa

from .._times import from_ns
from .core import ArrowIpcCache
from .frame_codec import frame_to_table, table_to_frame
from .models import CacheKey, SimpleDatasetSpec

_BARS = SimpleDatasetSpec("futures_bars")
_OOF_BARS = SimpleDatasetSpec("oof_bars")


class FuturesCacheStats(TypedDict):
    total_futures_bars: int
    total_oof_contracts: int
    total_rows: int
    total_size_mb: float
    by_symbol: dict[str, dict[str, int]]
    by_root: dict[str, dict[str, int]]


class FuturesDataCache:
    def __init__(self, cache_dir: str, verbose: bool = False) -> None:
        del verbose
        self._engine = ArrowIpcCache(cache_dir)

    @staticmethod
    def _futures_key(symbol: str, interval: str) -> CacheKey:
        return CacheKey.build(
            "futures_bars", "market", "future", {"symbol": symbol}, interval
        )

    @staticmethod
    def _oof_key(
        root: str, strike: float, expiration: str, option_type: str, interval: str
    ) -> CacheKey:
        return CacheKey.build(
            "oof_bars",
            "market",
            "option_on_future",
            {
                "root": root.upper(),
                "strike": strike,
                "expiration": expiration,
                "option_type": option_type.upper(),
            },
            interval,
        )

    @staticmethod
    def _reference_key(root: str, kind: str, **extra: object) -> CacheKey:
        return CacheKey.build(
            kind, "market", "option_on_future", {"root": root.upper(), **extra}
        )

    def get_coverage(
        self, symbol: str, interval: str
    ) -> tuple[pd.Timestamp | None, pd.Timestamp | None]:
        key = self._futures_key(symbol, interval)
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
        self, symbol: str, interval: str, start: datetime, end: datetime
    ) -> None:
        self._engine.commit_timeseries(
            self._futures_key(symbol, interval), _BARS, None, [(start, end)]
        )

    def get_futures(
        self, symbol: str, start: datetime, end: datetime, interval: str
    ) -> pd.DataFrame | None:
        result = self._engine.read_range(
            self._futures_key(symbol, interval), _BARS, start, end
        )
        return table_to_frame(result.table) if result is not None else None

    def get_futures_superset(
        self, symbol: str, start: datetime, end: datetime, interval: str
    ) -> pd.DataFrame | None:
        key = self._futures_key(symbol, interval)
        return (
            self.get_futures(symbol, start, end, interval)
            if self._engine.is_covered(key, start, end)
            else None
        )

    def put_futures(
        self,
        symbol: str,
        start: datetime,
        end: datetime,
        interval: str,
        data: pd.DataFrame,
    ) -> None:
        self._engine.commit_timeseries(
            self._futures_key(symbol, interval),
            _BARS,
            frame_to_table(data) if not data.empty else None,
            [(start, end)],
        )

    def get(
        self,
        root: str,
        strike: float,
        expiration: str,
        option_type: str,
        start: datetime,
        end: datetime,
        interval: str,
    ) -> pd.DataFrame | None:
        result = self._engine.read_range(
            self._oof_key(root, strike, expiration, option_type, interval),
            _OOF_BARS,
            start,
            end,
        )
        return table_to_frame(result.table) if result is not None else None

    def get_superset(
        self,
        root: str,
        strike: float,
        expiration: str,
        option_type: str,
        start: datetime,
        end: datetime,
        interval: str,
    ) -> pd.DataFrame | None:
        key = self._oof_key(root, strike, expiration, option_type, interval)
        return (
            self.get(root, strike, expiration, option_type, start, end, interval)
            if self._engine.is_covered(key, start, end)
            else None
        )

    def put(
        self,
        root: str,
        strike: float,
        expiration: str,
        option_type: str,
        start: datetime,
        end: datetime,
        interval: str,
        data: pd.DataFrame,
        *,
        raw_symbol: str | None = None,
    ) -> None:
        self._engine.commit_timeseries(
            self._oof_key(root, strike, expiration, option_type, interval),
            _OOF_BARS,
            frame_to_table(data) if not data.empty else None,
            [(start, end)],
        )
        if raw_symbol:
            self.set_oof_raw_symbol(root, strike, expiration, option_type, raw_symbol)

    def set_oof_raw_symbol(
        self,
        root: str,
        strike: float,
        expiration: str,
        option_type: str,
        raw_symbol: str,
    ) -> None:
        self._engine.put_reference(
            self._reference_key(
                root,
                "oof_raw_symbol",
                strike=strike,
                expiration=expiration,
                option_type=option_type.upper(),
            ),
            pa.table({"raw_symbol": [raw_symbol]}),
        )

    def get_oof_raw_symbol(
        self, root: str, strike: float, expiration: str, option_type: str
    ) -> str | None:
        table = self._engine.get_reference(
            self._reference_key(
                root,
                "oof_raw_symbol",
                strike=strike,
                expiration=expiration,
                option_type=option_type.upper(),
            )
        )
        return (
            str(table["raw_symbol"][0].as_py())
            if table is not None and table.num_rows
            else None
        )

    def save_expirations(self, root: str, expirations: list[str]) -> None:
        if not expirations:
            return
        key = self._reference_key(root, "oof_expirations")
        old = self._engine.get_reference(key)
        values = set(expirations)
        if old is not None:
            values.update(str(value) for value in old["expiration"].to_pylist())
        self._engine.put_reference(key, pa.table({"expiration": sorted(values)}))

    def get_expirations(self, root: str) -> list[str]:
        table = self._engine.get_reference(self._reference_key(root, "oof_expirations"))
        return (
            sorted(str(value) for value in table["expiration"].to_pylist())
            if table is not None and table.num_rows
            else []
        )

    def record_expiration_query(self, root: str, start: str, end: str) -> None:
        self._engine.commit_timeseries(
            self._reference_key(root, "oof_expiration_queries"),
            _BARS,
            None,
            [(start, end)],
        )

    def is_range_covered(self, root: str, start: str, end: str) -> bool:
        return self._engine.is_covered(
            self._reference_key(root, "oof_expiration_queries"), start, end
        )

    def get_cache_stats(self) -> FuturesCacheStats:
        futures, oof = (
            self._engine.dataset_stats("futures_bars"),
            self._engine.dataset_stats("oof_bars"),
        )
        return {
            "total_futures_bars": int(futures["entries"]),
            "total_oof_contracts": int(oof["entries"]),
            "total_rows": int(futures["rows"] + oof["rows"]),
            "total_size_mb": float(futures["size_mb"] + oof["size_mb"]),
            "by_symbol": {},
            "by_root": {},
        }

    def cleanup_old_entries(self, days_old: int = 30) -> None:
        self._engine.purge_before(
            pd.Timestamp.now(tz="UTC") - pd.Timedelta(days=days_old)
        )

    def clear_cache(self) -> None:
        self._engine.clear()
