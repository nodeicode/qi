"""Provider-agnostic in-memory bar store with two adjustment-keyed frames.

``MemBarsAggregator`` buckets live 1-minute bars from any ``BarSource`` into the
configured timeframe and stores one output-shaped row per settled bucket — for BOTH
adjustment keys (``"all"`` and ``"raw"``). Historical correctness comes from REST
seeds (``seed``); live appends are as-traded, which equals ``all`` at the live edge
(Alpaca adjustment is backward-only — it rewrites bars *before* a corporate-action
ex-date, never the tail), so the same appended row is correct for both frames.
Two keys are kept deliberately: warmup seeds them from distinct REST fetches
(adjusted vs raw), so their history may legitimately differ; the live tail is the
same row appended to both.

Storage: preallocated numpy column arrays + int write index per key — append is an
O(1) array store, not a DataFrame concat — materialised into a DataFrame ONLY at
the read boundary (``window``/``get``).  The write side is called either by
``BarWorker``'s thread (live) or synchronously (tests); reads snapshot ``n`` then
slice the arrays — single-writer and lock-free by contract.

The aggregator is pure (no provider imports); the optional ``notify`` event lets a
writer signal read-side waiters without polling.
"""

from __future__ import annotations

import threading
from typing import Any

import numpy as np
import pandas as pd

from quant.data.subscription import Datatype, Subscription, normalize_bars_frame
from quant.data.ws.protocols import MinuteBar

# Column order matches get_historical_data output (Title-case) plus the symbol column.
_MEM_COLS = ("Open", "High", "Low", "Close", "Volume")

_RTH_START_MIN = 570  # 09:30 ET
_RTH_END_MIN = 960  # 16:00 ET

_CAP = 1 << 16  # per-key preallocation; grown exponentially when exceeded.


class MemBarsAggregator:
    """Buckets live minute bars and stores settled rows in two keyed frames.

    Settled-bar invariant: a bucket is appended only when all of its minutes have
    arrived (finalize-on-last-minute), so no partially closed bar is ever served.
    Missing minutes drop the whole bucket instead of fabricating a bar. The RTH
    mask and UTC-naive microsecond normalization are applied at append time, so
    the read fast path is a bare slice with no re-finalization.
    """

    _MEM_COLS = _MEM_COLS

    def __init__(self, notify: threading.Event | None = None) -> None:
        # key = (ticker, interval, adjustment) — the same triple get_historical_data
        # resolves, so the fast path needs no mapping.
        self._n: dict[tuple[str, str, str], int] = {}
        self._cap: dict[tuple[str, str, str], int] = {}
        self._ts: dict[tuple[str, str, str], np.ndarray] = {}
        self._cols: dict[tuple[str, str, str], dict[str, np.ndarray]] = {}
        # coverage[key] = (first stamp, last appended stamp) as int64 ns, UTC-naive.
        self._coverage: dict[tuple[str, str, str], tuple[int | None, int | None]] = {}
        # in-flight bucket per (symbol, interval): {ts, open, high, low, close,
        # volume, count} — never exposed to readers.
        self._buckets: dict[tuple[str, str], dict[str, Any]] = {}
        # raised on every write so read-side waiters need not poll.
        self.notify = notify
        # synced-frame cache: key -> (n_synced, DataFrame | None).  The read
        # boundary slices this instead of rebuilding the full frame from arrays
        # every time; appends (arrays only) invalidate it lazily by count.
        self._built: dict[tuple[str, str, str], tuple[int, pd.DataFrame | None]] = {}

    # ── read side ─────────────────────────────────────────────────────────────
    def has(self, key: tuple[str, str, str]) -> bool:
        return self._n.get(key, 0) > 0

    def coverage(
        self, key: tuple[str, str, str]
    ) -> tuple[pd.Timestamp | None, pd.Timestamp | None]:
        """Return the coverage envelope for *key* (or (None, None))."""
        lo, hi = self._coverage.get(key, (None, None))
        if lo is None or hi is None:
            return None, None
        return pd.Timestamp(lo, unit="ns"), pd.Timestamp(hi, unit="ns")

    def window(self, key: tuple[str, str, str], start, end) -> pd.DataFrame:
        """Materialise the [start, end] window of *key*'s store (clamped at the
        stored head/tail, exactly like a loc slice).  Always returns the output
        shape (Title cols + symbol, UTC-naive µs); empty when no overlap."""
        n = self._n.get(key, 0)
        if n == 0:
            return self._empty()
        frame = self._synced_frame(key)
        idx = frame.index
        a = int(idx.searchsorted(start, side="left"))
        b = int(idx.searchsorted(end, side="right"))
        if a >= b:
            return self._empty()
        return frame.iloc[a:b].copy()

    def _synced_frame(self, key: tuple[str, str, str]) -> pd.DataFrame:
        """Return the stored rows as a DataFrame.  The frame is synced at write
        time (one tiny concat per settled bucket), so the read boundary is a
        pure slice — never a full-materialise or rebuild."""
        n = self._n[key]
        built_n, built = self._built.get(key, (0, None))
        if built is None or built_n != n:
            built = self._materialize(key, 0, n)
            self._built[key] = (n, built)
        return built

    def _extend_built(
        self, key: tuple[str, str, str], ts_ns: int, cur: dict[str, Any]
    ) -> None:
        """Append one settled row to the synced frame (write-side sync)."""
        built_n, built = self._built.get(key, (0, None))
        if built is None or built_n != self._n[key] - 1:
            self._built[key] = (self._n[key], None)  # force rebuild next read
            return
        one = pd.DataFrame(
            {
                "Open": [cur["open"]],
                "High": [cur["high"]],
                "Low": [cur["low"]],
                "Close": [cur["close"]],
                "Volume": [cur["volume"]],
                "symbol": [str(key[0]).upper()],
            },
            index=pd.DatetimeIndex([pd.Timestamp(ts_ns, unit="ns")]).as_unit("us"),
        )
        self._built[key] = (self._n[key], pd.concat([built, one]))

    def _materialize(self, key: tuple[str, str, str], lo: int, hi: int) -> pd.DataFrame:
        if lo >= hi:
            return self._empty()
        ts = self._ts[key][lo:hi]
        cols = self._cols[key]
        symbol = str(key[0]).upper()
        return pd.DataFrame(
            {
                "Open": cols["Open"][lo:hi],
                "High": cols["High"][lo:hi],
                "Low": cols["Low"][lo:hi],
                "Close": cols["Close"][lo:hi],
                "Volume": cols["Volume"][lo:hi],
                "symbol": np.full(hi - lo, symbol, dtype=object),
            },
            index=pd.DatetimeIndex(ts).as_unit("us"),
        )

    def get(
        self, key: tuple[str, str, str]
    ) -> tuple[pd.DataFrame | None, tuple[pd.Timestamp | None, pd.Timestamp | None]]:
        """Backward-compatible accessor: (full materialised frame, coverage)."""
        n = self._n.get(key, 0)
        cov = self.coverage(key)
        if n == 0:
            return None, cov
        frame = self.window(key, self._ts[key][0], self._ts[key][n - 1])
        return frame, cov

    # ── subscription surface (spec `data-access` Live streaming to buffers) ──

    def _sub_store_key(self, subscription: Subscription) -> tuple[str, str, str]:
        """Map a bars subscription to the underlying (symbol, cadence, adj) store.

        The live edge equals ``all`` (adjustment rewrites history, never the
        tail), so a subscription buffer maps to the ``"all"`` key.
        """
        if subscription.datatype is not Datatype.BARS:
            raise ValueError(f"MemBars streams bars only, got {subscription.datatype}")
        return (subscription.instrument.symbol.upper(), subscription.cadence, "all")

    def subscribe(self, subscription: Subscription) -> None:
        """Declare a per-subscription buffer (register its store key).

        Idempotent: the store array is created lazily on the first seed/append,
        so calling this before a stream starts just reserves the key.
        """
        key = self._sub_store_key(subscription)
        self._ensure_cap(key, self._n.get(key, 0))

    def seed_subscription(
        self, subscription: Subscription, frame: pd.DataFrame, seed_end=None
    ) -> None:
        """Seed a subscription buffer from history (REST/replay), per-subscription.

        Delegates to the authoritative :meth:`seed` span logic so a pre-open
        warmup tags the buffer through the next decision bar while the live
        stream supplies the edge.
        """
        key = self._sub_store_key(subscription)
        self.seed(key[0], key[1], key[2], frame, seed_end)

    def append_settled(
        self,
        subscription: Subscription,
        *,
        timestamp,
        open: float,
        high: float,
        low: float,
        close: float,
        volume: float,
    ) -> None:
        """Append one settled bar to a subscription buffer (stream write path).

        The row must already be a fully-formed bar for the subscription's
        cadence; appending is an O(1) array store — never a per-bar re-fetch or
        re-parse of source data. Reads therefore never see a partial bar: only
        settled bars are appended, atomically. Stale/duplicate stamps are
        ignored so re-connects stay idempotent.
        """
        key = self._sub_store_key(subscription)
        ts = pd.Timestamp(timestamp)
        if ts.tzinfo is not None:
            ts = ts.tz_convert("UTC").tz_localize(None)
        ts_ns = ts.value
        n = self._n.get(key, 0)
        if n and self._ts[key][n - 1] >= np.datetime64(ts_ns, "ns"):
            return  # stale or duplicate — do not regress the buffer tail
        self._ensure_cap(key, n + 1)
        i = self._n[key]
        self._ts[key][i] = ts_ns
        cols = self._cols[key]
        cols["Open"][i] = open
        cols["High"][i] = high
        cols["Low"][i] = low
        cols["Close"][i] = close
        cols["Volume"][i] = volume
        self._n[key] = i + 1
        self._bump_cov(key, ts_ns)
        self._extend_built(
            key,
            ts_ns,
            {"open": open, "high": high, "low": low, "close": close, "volume": volume},
        )

    def subscription_window(
        self, subscription: Subscription, start, end
    ) -> pd.DataFrame:
        """Read a subscription buffer's ``[start, end]`` window (canonical shape).

        Returns exactly the canonical bars columns with naive-UTC timestamps,
        matching the backtest replay shape for the same subscription.
        """
        key = self._sub_store_key(subscription)
        frame = self.window(key, start, end)
        return normalize_bars_frame(frame) if not frame.empty else frame

    # ── write side (single writer enforced by callers) ───────────────────────
    def seed(self, ticker, interval, adjustment, frame, seed_end=None) -> None:
        """Insert a REST frame that is authoritative for its span.

        Live-appended rows stamped <= ``seed_end`` are dropped on overlap so the
        REST span is exactly what get_historical_data returns today, and the ws
        tail owns everything newer. Idempotent: re-seeding replaces the frame.
        """
        merged = frame.copy()
        existing, _ = self.get((ticker, interval, adjustment))
        if existing is not None and not existing.empty and seed_end is not None:
            live_tail = existing[existing.index > pd.Timestamp(seed_end)]
            if not live_tail.empty:
                merged = pd.concat([merged, live_tail])
        merged = merged[~merged.index.duplicated(keep="last")].sort_index()
        self._from_frame((ticker, interval, adjustment), merged)

    def merge(self, ticker, interval, adjustment, frame: pd.DataFrame) -> None:
        """Extend one keyed frame with rows (e.g. disk head rows that the memory
        store lacks after a repair) and widen the coverage envelope.  Unlike
        ``seed`` this never truncates existing rows; caller enforces the
        single-writer contract (BarWorker when a stream is running)."""
        if frame is None or frame.empty:
            return
        existing, _ = self.get((ticker, interval, adjustment))
        if existing is not None and not existing.empty:
            frame = pd.concat([existing, frame.copy()])
        merged = frame[~frame.index.duplicated(keep="last")].sort_index()
        self._from_frame((ticker, interval, adjustment), merged)

    def on_minute(self, bar: MinuteBar, interval: str = "15m") -> None:
        """Consume one settled 1-minute bar from a live stream.

        Buckets are keyed by the floored minute stamp (integer ns math — no
        pandas floor in the per-minute path); a bucket is finalized as soon as
        its minutes are all present (finalize-on-last-minute).  Stale minutes
        (older than the in-flight bucket) are ignored.
        """
        ts = pd.Timestamp(bar.timestamp)
        if ts.tzinfo is not None:
            ts = ts.tz_convert("UTC").tz_localize(None)
        ts_ns = ts.value
        step_ns = 60_000_000_000 * self._interval_minutes(interval)
        bucket_ns = (ts_ns // step_ns) * step_ns
        key = (bar.symbol, interval)
        cur = self._buckets.get(key)
        if cur is None:
            self._buckets[key] = self._new_bucket(bucket_ns, bar)
            return
        if bucket_ns < cur["ts"]:
            return  # stale / duplicate minute — ignore
        if bucket_ns == cur["ts"]:
            cur["high"] = max(cur["high"], bar.high)
            cur["low"] = min(cur["low"], bar.low)
            cur["close"] = bar.close
            cur["volume"] += bar.volume
            cur["count"] += 1
            # Finalize as soon as the bucket's minutes are all present (its last
            # minute arrives at close + pipeline-delay): settle == REST's
            # publication boundary, so the engine's closed-bar request is banked
            # the moment DATA_DELAY expires.
            if cur["count"] == self._interval_minutes(interval):
                self._finalize(bar.symbol, interval, cur)
                self._buckets.pop(key, None)
            return
        # Rollover: an unfinalized (partial / missing-minute) bucket is dropped.
        self._finalize(bar.symbol, interval, cur)
        self._buckets[key] = self._new_bucket(bucket_ns, bar)

    # ── internals ────────────────────────────────────────────────────────────
    @staticmethod
    def _interval_minutes(interval: str) -> int:
        return {"1m": 1, "5m": 5, "15m": 15, "30m": 30, "1h": 60}.get(interval, 15)

    @staticmethod
    def _new_bucket(bucket_ts_ns: int, bar: MinuteBar) -> dict[str, Any]:
        return {
            "ts": bucket_ts_ns,
            "open": bar.open,
            "high": bar.high,
            "low": bar.low,
            "close": bar.close,
            "volume": float(bar.volume),
            "count": 1,
        }

    def _finalize(self, symbol: str, interval: str, cur: dict[str, Any]) -> None:
        n = self._interval_minutes(interval)
        if cur["count"] != n:
            # Missing minutes → drop the bucket, never fabricate a bar.
            return
        ts = pd.Timestamp(cur["ts"], unit="ns")
        if not self._is_rth(ts):
            # Extended-hours buckets are dropped (get_historical_data output is
            # RTH-only; parity is preserved by dropping here as well).
            return
        ts_ns = ts.value
        for adjustment in ("all", "raw"):
            key = (symbol, interval, adjustment)
            self._ensure_cap(key, self._n.get(key, 0) + 1)
            i = self._n[key]
            self._ts[key][i] = ts_ns
            cols = self._cols[key]
            cols["Open"][i] = cur["open"]
            cols["High"][i] = cur["high"]
            cols["Low"][i] = cur["low"]
            cols["Close"][i] = cur["close"]
            cols["Volume"][i] = cur["volume"]
            self._n[key] = i + 1
            self._bump_cov(key, ts_ns)
            self._extend_built(key, ts_ns, cur)

    @staticmethod
    def _is_rth(ts: pd.Timestamp) -> bool:
        et = ts.tz_localize("UTC").tz_convert("America/New_York")
        return _RTH_START_MIN <= et.hour * 60 + et.minute < _RTH_END_MIN

    def _ensure_cap(self, key: tuple[str, str, str], need: int) -> None:
        cap = self._cap.get(key, 0)
        if cap == 0:
            cap = max(_CAP, need + 64)
            self._ts[key] = np.empty(cap, dtype="datetime64[ns]")
            self._cols[key] = {c: np.empty(cap, dtype=np.float64) for c in _MEM_COLS}
            self._cap[key] = cap
            self._n[key] = self._n.get(key, 0)
        elif need > cap:
            cap = need * 2
            self._ts[key] = np.resize(self._ts[key], cap)
            for c in _MEM_COLS:
                self._cols[key][c] = np.resize(self._cols[key][c], cap)
            self._cap[key] = cap

    def _bump_cov(self, key: tuple[str, str, str], ts_ns: int) -> None:
        cov = self._coverage.get(key, (None, None))
        self._coverage[key] = (
            ts_ns if cov[0] is None else min(cov[0], ts_ns),
            ts_ns,
        )
        if self.notify is not None:
            self.notify.set()

    def _from_frame(self, key: tuple[str, str, str], merged: pd.DataFrame) -> None:
        """Rebuild a keyed array store from an output-shaped DataFrame (naive UTC
        index; units normalised to ns for storage)."""
        if merged is None or merged.empty:
            return
        self._built[key] = (0, None)  # invalidate the read-side cache
        idx = pd.DatetimeIndex(merged.index)
        if idx.tz is not None:
            idx = idx.tz_convert("UTC").tz_localize(None)
        n = len(idx)
        self._ensure_cap(key, n)
        self._ts[key][:n] = idx.as_unit("ns").asi8  # int64 ns regardless of frame unit
        cols = self._cols[key]
        for col in _MEM_COLS:
            cols[col][:n] = merged[col].to_numpy(dtype=np.float64)
        self._n[key] = n
        self._coverage[key] = (int(idx[0].value), int(idx[-1].value))
        if self.notify is not None:
            self.notify.set()

    def _empty(self) -> pd.DataFrame:
        return pd.DataFrame(
            {c: np.array([], dtype=np.float64) for c in _MEM_COLS}
            | {"symbol": np.array([], dtype=object)},
            index=pd.DatetimeIndex([], dtype="datetime64[us]"),
        )

    @staticmethod
    def _to_ns(value) -> np.datetime64:
        """Convert a (possibly tz-aware) timestamp to datetime64[ns] naive UTC."""
        ts = pd.Timestamp(value)
        if ts.tzinfo is not None:
            ts = ts.tz_convert("UTC").tz_localize(None)
        return np.datetime64(ts.value, "ns")
