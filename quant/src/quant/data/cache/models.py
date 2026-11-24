"""Portable public models for the historical cache."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from typing import Protocol, TypedDict

import pyarrow as pa


@dataclass(frozen=True, slots=True)
class CacheKey:
    """Stable identity for one cached series, independent of its storage path."""

    dataset: str
    provider: str
    asset_class: str
    instrument: tuple[tuple[str, str], ...]
    interval: str = ""
    adjustment: str = ""
    schema_version: int = 1

    @classmethod
    def build(
        cls,
        dataset: str,
        provider: str,
        asset_class: str,
        instrument: dict[str, object],
        interval: str = "",
        adjustment: str = "",
        schema_version: int = 1,
    ) -> CacheKey:
        return cls(
            dataset=dataset,
            provider=provider,
            asset_class=asset_class,
            instrument=tuple(
                sorted((str(name), str(value)) for name, value in instrument.items())
            ),
            interval=interval,
            adjustment=adjustment,
            schema_version=schema_version,
        )

    @property
    def canonical_json(self) -> str:
        return json.dumps(
            {
                "adjustment": self.adjustment,
                "asset_class": self.asset_class,
                "dataset": self.dataset,
                "instrument": dict(self.instrument),
                "interval": self.interval,
                "provider": self.provider,
                "schema_version": self.schema_version,
            },
            separators=(",", ":"),
            sort_keys=True,
        )

    @property
    def digest(self) -> str:
        return hashlib.sha256(self.canonical_json.encode()).hexdigest()


class DatasetSpec(Protocol):
    """Arrow schema and row identity supplied by a dataset domain."""

    name: str
    timestamp_column: str
    identity_columns: tuple[str, ...]

    def validate(self, table: pa.Table) -> None: ...


@dataclass(frozen=True, slots=True)
class SimpleDatasetSpec:
    name: str
    timestamp_column: str = "timestamp"
    identity_columns: tuple[str, ...] = ("timestamp",)
    required_columns: tuple[str, ...] = ("timestamp",)

    def validate(self, table: pa.Table) -> None:
        missing = set(self.required_columns) - set(table.column_names)
        if missing:
            raise ValueError(
                f"{self.name} table missing required columns: {sorted(missing)}"
            )
        if self.timestamp_column not in table.column_names:
            raise ValueError(
                f"{self.name} table has no {self.timestamp_column!r} column"
            )


@dataclass(frozen=True, slots=True)
class CacheRead:
    table: pa.Table
    covered: bool


class CacheFileStats(TypedDict):
    entries: int
    rows: int
    size_mb: float
