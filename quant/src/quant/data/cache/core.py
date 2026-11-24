"""A small, transactional Arrow IPC cache for immutable historical data.

Arrow IPC is the payload format.  SQLite only indexes files and exact fetched
coverage; market-data rows are never stored in the database.
"""

from __future__ import annotations

import contextlib
import hashlib
import os
import sqlite3
import threading
import uuid
from collections.abc import Iterable
from datetime import UTC, datetime
from pathlib import Path

import pandas as pd
import pyarrow as pa
import pyarrow.ipc as ipc

from .._times import from_ns
from .models import CacheKey, CacheRead, DatasetSpec


class ArrowIpcCache:
    """Single-writer Arrow IPC cache with a SQLite metadata catalog."""

    _CATALOG_VERSION = 1

    def __init__(self, cache_dir: str | Path, *, busy_timeout_ms: int = 5_000):
        self.root = Path(cache_dir) / "arrow_ipc_v1"
        self.data_dir = self.root / "data"
        self.db_path = self.root / "catalog.sqlite"
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._busy_timeout_ms = busy_timeout_ms
        self._init_catalog()
        self.cleanup_temporary_files()

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db_path, timeout=self._busy_timeout_ms / 1000)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys=ON")
        conn.execute(f"PRAGMA busy_timeout={self._busy_timeout_ms}")
        return conn

    def _init_catalog(self) -> None:
        with self._connect() as conn:
            conn.executescript(
                """
                PRAGMA journal_mode=WAL;
                PRAGMA synchronous=FULL;
                CREATE TABLE IF NOT EXISTS schema_migrations (
                    version INTEGER PRIMARY KEY, applied_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS cache_keys (
                    id INTEGER PRIMARY KEY,
                    key_hash TEXT NOT NULL UNIQUE,
                    canonical_key TEXT NOT NULL UNIQUE,
                    dataset TEXT NOT NULL,
                    schema_version INTEGER NOT NULL,
                    created_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS data_files (
                    id INTEGER PRIMARY KEY,
                    key_id INTEGER NOT NULL REFERENCES cache_keys(id) ON DELETE CASCADE,
                    partition_date TEXT NOT NULL,
                    generation TEXT NOT NULL,
                    path TEXT NOT NULL UNIQUE,
                    min_ts_ns INTEGER NOT NULL,
                    max_ts_ns INTEGER NOT NULL,
                    rows INTEGER NOT NULL,
                    bytes INTEGER NOT NULL,
                    checksum TEXT NOT NULL,
                    schema_fingerprint TEXT NOT NULL,
                    active INTEGER NOT NULL DEFAULT 1,
                    committed_at TEXT NOT NULL
                );
                CREATE UNIQUE INDEX IF NOT EXISTS active_partition
                    ON data_files(key_id, partition_date) WHERE active=1;
                CREATE INDEX IF NOT EXISTS data_file_range
                    ON data_files(key_id, active, min_ts_ns, max_ts_ns);
                CREATE TABLE IF NOT EXISTS coverage_segments (
                    id INTEGER PRIMARY KEY,
                    key_id INTEGER NOT NULL REFERENCES cache_keys(id) ON DELETE CASCADE,
                    start_ns INTEGER NOT NULL,
                    end_ns INTEGER NOT NULL,
                    fresh_until_ns INTEGER,
                    outcome TEXT NOT NULL CHECK(outcome IN ('data', 'empty')),
                    CHECK(start_ns <= end_ns)
                );
                CREATE INDEX IF NOT EXISTS coverage_lookup
                    ON coverage_segments(key_id, start_ns, end_ns);
                CREATE TABLE IF NOT EXISTS reference_files (
                    id INTEGER PRIMARY KEY,
                    key_id INTEGER NOT NULL REFERENCES cache_keys(id) ON DELETE CASCADE,
                    path TEXT NOT NULL UNIQUE,
                    checksum TEXT NOT NULL,
                    schema_fingerprint TEXT NOT NULL,
                    fresh_until_ns INTEGER,
                    active INTEGER NOT NULL DEFAULT 1,
                    committed_at TEXT NOT NULL
                );
                """
            )
            conn.execute(
                "INSERT OR IGNORE INTO schema_migrations(version, applied_at) VALUES (?, ?)",
                (self._CATALOG_VERSION, datetime.now(UTC).isoformat()),
            )

    @staticmethod
    @staticmethod
    def _ns(value) -> int:
        """Canonical int64 ns from any accepted bound — never exposes aware."""
        ts = pd.Timestamp(value)
        if ts.tzinfo is None:
            return int(ts.as_unit("ns").value)
        return int(ts.tz_convert("UTC").tz_localize(None).as_unit("ns").value)

    def _key_id(self, conn: sqlite3.Connection, key: CacheKey) -> int:
        row = conn.execute(
            "SELECT id FROM cache_keys WHERE key_hash=?", (key.digest,)
        ).fetchone()
        if row:
            return int(row["id"])
        conn.execute(
            """INSERT INTO cache_keys(key_hash, canonical_key, dataset, schema_version, created_at)
               VALUES (?, ?, ?, ?, ?)""",
            (
                key.digest,
                key.canonical_json,
                key.dataset,
                key.schema_version,
                datetime.now(UTC).isoformat(),
            ),
        )
        return int(conn.execute("SELECT last_insert_rowid()").fetchone()[0])

    def _existing_key_id(self, conn: sqlite3.Connection, key: CacheKey) -> int | None:
        row = conn.execute(
            "SELECT id FROM cache_keys WHERE key_hash=?", (key.digest,)
        ).fetchone()
        return int(row["id"]) if row else None

    @staticmethod
    def _schema_fingerprint(table: pa.Table) -> str:
        return hashlib.sha256(str(table.schema).encode()).hexdigest()

    @staticmethod
    def _checksum(path: Path) -> str:
        digest = hashlib.sha256()
        with path.open("rb") as fh:
            for block in iter(lambda: fh.read(1024 * 1024), b""):
                digest.update(block)
        return digest.hexdigest()

    @staticmethod
    def _empty_like(table: pa.Table | None = None) -> pa.Table:
        return table.slice(0, 0) if table is not None else pa.table({})

    def _partition_path(self, key: CacheKey, date: str, generation: str) -> Path:
        return self.data_dir / key.digest / f"date={date}" / f"{generation}.arrow"

    def _write_table(self, path: Path, table: pa.Table) -> tuple[int, str, str]:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(path.suffix + ".tmp")
        with (
            pa.OSFile(str(tmp), "wb") as sink,
            ipc.new_file(sink, table.schema) as writer,
        ):
            writer.write_table(table)
        with tmp.open("rb") as fh:
            os.fsync(fh.fileno())
        with pa.memory_map(str(tmp), "r") as source:
            ipc.open_file(source).read_all()
        tmp.replace(path)
        return (
            path.stat().st_size,
            self._checksum(path),
            self._schema_fingerprint(table),
        )

    @staticmethod
    def _table_to_frame(table: pa.Table) -> pd.DataFrame:
        return table.to_pandas(date_as_object=False)

    def _read_file(self, path: Path) -> pa.Table:
        with pa.memory_map(str(path), "r") as source:
            return ipc.open_file(source).read_all()

    def _normalise_table(self, table: pa.Table, spec: DatasetSpec) -> pa.Table:
        spec.validate(table)
        frame = self._table_to_frame(table)
        ts = pd.to_datetime(frame[spec.timestamp_column], utc=True)
        frame[spec.timestamp_column] = ts
        frame = frame.sort_values(spec.timestamp_column)
        frame = frame.drop_duplicates(list(spec.identity_columns), keep="last")
        return pa.Table.from_pandas(frame, preserve_index=False)

    def _read_active_partition(
        self, conn: sqlite3.Connection, key_id: int, date: str
    ) -> tuple[sqlite3.Row, pa.Table] | None:
        row = conn.execute(
            "SELECT * FROM data_files WHERE key_id=? AND partition_date=? AND active=1",
            (key_id, date),
        ).fetchone()
        if not row:
            return None
        path = self.root / row["path"]
        if not path.exists():
            return None
        return row, self._read_file(path)

    def _coalesce_coverage(self, conn: sqlite3.Connection, key_id: int) -> None:
        rows = conn.execute(
            """SELECT start_ns, end_ns, fresh_until_ns, outcome FROM coverage_segments
               WHERE key_id=? ORDER BY outcome, fresh_until_ns, start_ns""",
            (key_id,),
        ).fetchall()
        merged: list[tuple[int, int, int | None, str]] = []
        for row in rows:
            current = (
                int(row["start_ns"]),
                int(row["end_ns"]),
                row["fresh_until_ns"],
                row["outcome"],
            )
            if (
                merged
                and merged[-1][3] == current[3]
                and merged[-1][2] == current[2]
                and current[0] <= merged[-1][1] + 1
            ):
                prior = merged[-1]
                merged[-1] = (prior[0], max(prior[1], current[1]), prior[2], prior[3])
            else:
                merged.append(current)
        conn.execute("DELETE FROM coverage_segments WHERE key_id=?", (key_id,))
        conn.executemany(
            "INSERT INTO coverage_segments(key_id, start_ns, end_ns, fresh_until_ns, outcome) VALUES (?, ?, ?, ?, ?)",
            [(key_id, *item) for item in merged],
        )

    def missing_ranges(
        self,
        key: CacheKey,
        start: datetime | pd.Timestamp | str,
        end: datetime | pd.Timestamp | str,
    ) -> list[tuple[pd.Timestamp, pd.Timestamp]]:
        start_ns, end_ns = self._ns(start), self._ns(end)
        with self._connect() as conn:
            key_id = self._existing_key_id(conn, key)
            if key_id is None:
                return [(from_ns(start_ns), from_ns(end_ns))]
            now_ns = self._ns(pd.Timestamp.now(tz="UTC"))
            rows = conn.execute(
                """SELECT start_ns, end_ns FROM coverage_segments
                   WHERE key_id=? AND (fresh_until_ns IS NULL OR fresh_until_ns >= ?)
                   ORDER BY start_ns""",
                (key_id, now_ns),
            ).fetchall()
        cursor = start_ns
        missing: list[tuple[pd.Timestamp, pd.Timestamp]] = []
        for row in rows:
            seg_start, seg_end = int(row["start_ns"]), int(row["end_ns"])
            if seg_end < cursor:
                continue
            if seg_start > end_ns:
                break
            if seg_start > cursor:
                missing.append((from_ns(cursor), from_ns(min(end_ns, seg_start - 1))))
            cursor = max(cursor, seg_end + 1)
            if cursor > end_ns:
                break
        if cursor <= end_ns:
            missing.append((from_ns(cursor), from_ns(end_ns)))
        return missing

    def is_covered(
        self,
        key: CacheKey,
        start: datetime | pd.Timestamp | str,
        end: datetime | pd.Timestamp | str,
    ) -> bool:
        return not self.missing_ranges(key, start, end)

    def commit_timeseries(
        self,
        key: CacheKey,
        spec: DatasetSpec,
        table: pa.Table | None,
        covered_ranges: Iterable[
            tuple[datetime | pd.Timestamp | str, datetime | pd.Timestamp | str]
        ],
        *,
        fresh_until: datetime | pd.Timestamp | str | None = None,
    ) -> None:
        """Atomically index day files and exact provider-success coverage."""
        ranges = list(covered_ranges)
        normal = (
            self._normalise_table(table, spec)
            if table is not None and table.num_rows
            else None
        )
        with self._lock, self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            key_id = self._key_id(conn, key)
            if normal is not None:
                frame = self._table_to_frame(normal)
                frame[spec.timestamp_column] = pd.to_datetime(
                    frame[spec.timestamp_column], utc=True
                )
                for date, group in frame.groupby(
                    frame[spec.timestamp_column].dt.strftime("%Y-%m-%d"), sort=False
                ):
                    new_table = pa.Table.from_pandas(group, preserve_index=False)
                    existing = self._read_active_partition(conn, key_id, str(date))
                    if existing:
                        _, existing_table = existing
                        merged = self._normalise_table(
                            pa.concat_tables(
                                [existing_table, new_table], promote_options="default"
                            ),
                            spec,
                        )
                    else:
                        merged = new_table
                    generation = uuid.uuid4().hex
                    path = self._partition_path(key, str(date), generation)
                    byte_count, checksum, fingerprint = self._write_table(path, merged)
                    ts = pd.to_datetime(
                        self._table_to_frame(merged)[spec.timestamp_column], utc=True
                    )
                    conn.execute(
                        "UPDATE data_files SET active=0 WHERE key_id=? AND partition_date=? AND active=1",
                        (key_id, str(date)),
                    )
                    conn.execute(
                        """INSERT INTO data_files(key_id, partition_date, generation, path, min_ts_ns, max_ts_ns,
                           rows, bytes, checksum, schema_fingerprint, active, committed_at)
                           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 1, ?)""",
                        (
                            key_id,
                            str(date),
                            generation,
                            str(path.relative_to(self.root)),
                            int(ts.min().value),
                            int(ts.max().value),
                            merged.num_rows,
                            byte_count,
                            checksum,
                            fingerprint,
                            datetime.now(UTC).isoformat(),
                        ),
                    )
            expiry = self._ns(fresh_until) if fresh_until is not None else None
            outcome = "data" if normal is not None else "empty"
            conn.executemany(
                "INSERT INTO coverage_segments(key_id, start_ns, end_ns, fresh_until_ns, outcome) VALUES (?, ?, ?, ?, ?)",
                [
                    (key_id, self._ns(start), self._ns(end), expiry, outcome)
                    for start, end in ranges
                ],
            )
            self._coalesce_coverage(conn, key_id)
            conn.commit()

    def read_range(
        self,
        key: CacheKey,
        spec: DatasetSpec,
        start: datetime | pd.Timestamp | str,
        end: datetime | pd.Timestamp | str,
        columns: Iterable[str] | None = None,
    ) -> CacheRead | None:
        start_ns, end_ns = self._ns(start), self._ns(end)
        with self._connect() as conn:
            key_id = self._existing_key_id(conn, key)
            if key_id is None:
                return None
            rows = conn.execute(
                """SELECT path FROM data_files WHERE key_id=? AND active=1
                   AND max_ts_ns >= ? AND min_ts_ns <= ? ORDER BY partition_date""",
                (key_id, start_ns, end_ns),
            ).fetchall()
        tables: list[pa.Table] = []
        for row in rows:
            path = self.root / row["path"]
            if path.exists():
                tables.append(self._read_file(path))
        if not tables:
            return CacheRead(self._empty_like(), self.is_covered(key, start, end))
        table = pa.concat_tables(tables, promote_options="default")
        frame = self._table_to_frame(table)
        ts = pd.to_datetime(frame[spec.timestamp_column], utc=True)
        # tz-aware storage may be at us precision; ``to_numpy(dtype="int64")``
        # would then emit microseconds. Cast to ns first (lossless) for exact
        # UTC-ns comparison against coverage bounds.
        ts_ns = ts.to_numpy(dtype="datetime64[ns]").astype("int64")
        frame = frame[(ts_ns >= start_ns) & (ts_ns <= end_ns)]
        frame = frame.sort_values(spec.timestamp_column).drop_duplicates(
            list(spec.identity_columns), keep="last"
        )
        if columns is not None:
            selected = [column for column in columns if column in frame.columns]
            frame = frame[selected]
        return CacheRead(
            pa.Table.from_pandas(frame, preserve_index=False),
            self.is_covered(key, start, end),
        )

    def put_reference(
        self,
        key: CacheKey,
        table: pa.Table,
        *,
        fresh_until: datetime | pd.Timestamp | str | None = None,
    ) -> None:
        if table.num_rows == 0:
            return
        with self._lock, self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            key_id = self._key_id(conn, key)
            generation = uuid.uuid4().hex
            path = self.data_dir / key.digest / "reference" / f"{generation}.arrow"
            _, checksum, fingerprint = self._write_table(path, table)
            conn.execute(
                "UPDATE reference_files SET active=0 WHERE key_id=? AND active=1",
                (key_id,),
            )
            conn.execute(
                """INSERT INTO reference_files(key_id, path, checksum, schema_fingerprint, fresh_until_ns, active, committed_at)
                   VALUES (?, ?, ?, ?, ?, 1, ?)""",
                (
                    key_id,
                    str(path.relative_to(self.root)),
                    checksum,
                    fingerprint,
                    self._ns(fresh_until) if fresh_until else None,
                    datetime.now(UTC).isoformat(),
                ),
            )
            conn.commit()

    def get_reference(self, key: CacheKey) -> pa.Table | None:
        with self._connect() as conn:
            key_id = self._existing_key_id(conn, key)
            if key_id is None:
                return None
            row = conn.execute(
                "SELECT path FROM reference_files WHERE key_id=? AND active=1",
                (key_id,),
            ).fetchone()
        if not row:
            return None
        path = self.root / row["path"]
        return self._read_file(path) if path.exists() else None

    def clear(self) -> None:
        with self._lock, self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            conn.execute("DELETE FROM cache_keys")
            conn.commit()
        for path in self.data_dir.rglob("*.arrow"):
            with contextlib.suppress(OSError):
                path.unlink()

    def cleanup_temporary_files(self) -> int:
        removed = 0
        for path in self.root.rglob("*.tmp"):
            with contextlib.suppress(OSError):
                path.unlink()
                removed += 1
        return removed

    def validate(self) -> list[str]:
        """Return invalid active file paths and deactivate them in the catalog."""
        bad: list[str] = []
        with self._lock, self._connect() as conn:
            rows = conn.execute(
                "SELECT id, path, checksum FROM data_files WHERE active=1"
            ).fetchall()
            for row in rows:
                path = self.root / row["path"]
                try:
                    if not path.exists() or self._checksum(path) != row["checksum"]:
                        raise OSError("missing or checksum mismatch")
                    self._read_file(path)
                except Exception:
                    bad.append(str(path))
                    conn.execute(
                        "UPDATE data_files SET active=0 WHERE id=?", (row["id"],)
                    )
            conn.commit()
        return bad

    def stats(self) -> dict[str, int | float]:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT COUNT(*) entries, COALESCE(SUM(rows), 0) rows, COALESCE(SUM(bytes), 0) bytes FROM data_files WHERE active=1"
            ).fetchone()
        return {
            "entries": int(row["entries"]),
            "rows": int(row["rows"]),
            "size_mb": int(row["bytes"]) / (1024 * 1024),
        }

    def dataset_stats(self, dataset: str) -> dict[str, int | float]:
        """Return active-file statistics scoped to one dataset specification."""
        with self._connect() as conn:
            row = conn.execute(
                """SELECT COUNT(DISTINCT f.key_id) entries, COALESCE(SUM(f.rows), 0) rows,
                          COALESCE(SUM(f.bytes), 0) bytes
                   FROM data_files f JOIN cache_keys k ON k.id=f.key_id
                   WHERE f.active=1 AND k.dataset=?""",
                (dataset,),
            ).fetchone()
        return {
            "entries": int(row["entries"]),
            "rows": int(row["rows"]),
            "size_mb": int(row["bytes"]) / (1024 * 1024),
        }

    def purge_before(self, cutoff: datetime | pd.Timestamp | str) -> int:
        """Drop keys whose latest active time-series file predates *cutoff*."""
        cutoff_ns = self._ns(cutoff)
        with self._lock, self._connect() as conn:
            doomed = conn.execute(
                """SELECT k.id FROM cache_keys k
                   JOIN data_files f ON f.key_id=k.id AND f.active=1
                   GROUP BY k.id HAVING MAX(f.max_ts_ns) < ?""",
                (cutoff_ns,),
            ).fetchall()
            if not doomed:
                return 0
            ids = [int(row["id"]) for row in doomed]
            doomed_ids = set(ids)
            paths = [
                self.root / row["path"]
                for row in conn.execute(
                    "SELECT key_id, path FROM data_files"
                ).fetchall()
                if int(row["key_id"]) in doomed_ids
            ]
            conn.execute("BEGIN IMMEDIATE")
            conn.executemany(
                "DELETE FROM cache_keys WHERE id=?", [(key_id,) for key_id in ids]
            )
            conn.commit()
        for path in paths:
            with contextlib.suppress(OSError):
                path.unlink()
        return len(doomed)
