"""Timestamp boundary contract for every data handler in ``quant/data``.

Canonical model
---------------
* Non-pandas (scalar) timestamps that cross the handler boundary are
  canonicalised to **tz-naive UTC** ``pd.Timestamp`` (unit ``us``); their
  monotonic integer form is ``int64`` nanoseconds since the Unix epoch (see
  :func:`ts_ns` / :func:`from_ns`), which is what numba kernels and hot-path
  scalar hand-offs use.
* Handlers must return frames through :func:`assert_canonical_index`, which
  guarantees a ``DatetimeIndex`` that is tz-naive-UTC for intraday bars. Daily
  bars follow the equity-session convention (see
  ``data_handler._finalize_equity_index``) and may pass through timezone-aware
  until that convention is unified.
* **Aware time may exist only inside a handler** (parsing its inputs). It never
  crosses the boundary; the engine, executors and monitor only *read* canonical
  stamps. The single sanctioned mutation of a timestamp is
  ``utils/fills.calculate_fill_time`` (the ``DATA_DELAY`` simulation wall,
  backtest-only).

These helpers exist so handlers stop re-deriving ``tz_.*``/``replace(tzinfo)``
per file; the engine path holds zero tz handling.
"""

from __future__ import annotations

import numpy as np
import pandas as pd

# Canonical scalar type: tz-naive UTC pd.Timestamp (natural unit).
type CanonicalTs = pd.Timestamp
# Monotonic integer nanoseconds since epoch; hot-path / persisted scalars.
type TsNs = int


def parse_naive_utc(start, end):
    """Coerce *start* / *end* (str, int, datetime, pd.Timestamp) to canonical
    tz-naive-UTC :class:`CanonicalTs` scalars (unit ``us``).

    Naive input is assumed to be UTC wall time. Aware input is converted to UTC
    then stripped. This is the **only** place aware time may be handled.

    Raises
    ------
    ValueError
        If an input cannot be parsed, or is an ``NaT``/out-of-bounds stamp.
    """
    return _to_canonical(start), _to_canonical(end)


def _to_canonical(value) -> CanonicalTs:
    ts = pd.Timestamp(value)
    if isinstance(value, np.datetime64) and np.isnat(value):
        raise ValueError("cannot canonicalize NaT timestamp")
    if ts is pd.NaT:
        raise ValueError("cannot canonicalize NaT timestamp")
    if ts.tzinfo is not None:
        ts = ts.tz_convert("UTC").tz_localize(None)
    # NOTE: do not coerce the unit here. The cache-coverage bookkeeping is
    # unit-sensitive; forcing ``us`` changes cache-visible bounds and breaks
    # coverage fusing. Unit pinning belongs at the frame *output* contract
    # (``data_handler._finalize_equity_index`` → ``as_unit("us")``).
    # ``to_pydatetime()`` is banned here because it truncates sub-microsecond
    # (ns) resolution at cache boundaries.
    return ts


def to_naive_utc(value) -> CanonicalTs:
    """Single-value canonicalizer; convenience for ``parse_naive_utc``."""
    return _to_canonical(value)


def _is_daily(interval):
    return interval in ("1d", "1wk", "1mo")


def to_naive_index(df: pd.DataFrame, interval: str | None = None) -> pd.DataFrame:
    """Return *df* with an index guaranteed tz-naive UTC for intraday frames.

    Mirrors each handler's existing intraday normalize semantics exactly (so the
    wiring never changes cache-visible units):

    * non-``DatetimeIndex`` / empty / already-naive input is returned unchanged;
    * daily intervals pass through (equity-session convention, may be aware);
    * tz-aware intraday indexes are converted to UTC then stripped and pinned
      to ``datetime64[us]``.
    """
    if df.empty or not isinstance(df.index, pd.DatetimeIndex):
        return df
    if interval is not None and _is_daily(interval):
        return df
    idx = pd.DatetimeIndex(df.index)
    if idx.tz is None:
        return df  # already naive; preserve natural unit (cache sensitivity)
    out = df.copy()
    out.index = pd.DatetimeIndex(idx.tz_convert("UTC").tz_localize(None)).as_unit("us")
    return out


def ts_ns(ts: CanonicalTs) -> TsNs:
    """Return the exact int64-nanoseconds-since-epoch value of *ts*.

    This is O(1) and lossless (uses the underlying int64, not float math).
    """
    return int(ts.value)


def from_ns(ns: TsNs) -> CanonicalTs:
    """Reconstruct a canonical tz-naive UTC :class:`CanonicalTs` from int64 *ns*.

    Preserves nanosecond unit: cache coverage boundaries are computed at ns
    precision and rounding to ``us`` here truncates the ``…999999`` tail,
    leaving a 1 ns hole that coverage fusing cannot bridge.
    """
    return pd.Timestamp(int(ns), unit="ns")


def assert_canonical_index(
    df: pd.DataFrame,
    *,
    allow_aware_daily: bool = True,
    interval: str | None = None,
) -> pd.DataFrame:
    """Assert *df* carries a canonical index; return *df* unchanged.

    Requires a ``DatetimeIndex``. Intraday frames (``interval`` not daily, or
    ``allow_aware_daily=False``) must be tz-naive UTC at unit ``us``. Daily
    frames may pass through per the equity-session convention until
    ``data_handler`` unifies it.

    Raises
    ------
    AssertionError
        If the index violates the canonical contract.
    """
    if df.empty:
        return df  # no-data frames carry no index contract
    idx = df.index
    assert isinstance(idx, pd.DatetimeIndex), (
        f"handler returned non-DatetimeIndex: {type(idx).__name__}"
    )
    if interval is not None and interval in ("1d", "1wk", "1mo"):
        return df  # equity-session daily convention (may be tz-aware for now)
    if allow_aware_daily:
        return df
    assert idx.tz is None, (
        "handler returned a tz-aware index where tz-naive UTC is required; "
        "normalize at the boundary, not in the engine"
    )
    return df


def aligned_at(df: pd.DataFrame, at) -> CanonicalTs | None:
    """Return the canonical stamp of the last index label at-or-before *at*.

    *at* is coerced to canonical; *df* index is asserted canonical.
    Returns ``None`` when nothing precedes *at*.
    """
    idx = df.index
    assert isinstance(idx, pd.DatetimeIndex), "aligned_at requires a DatetimeIndex"
    target = _to_canonical(at)
    if len(idx) == 0 or idx[0].value > target.value:
        return None
    pos = int(np.searchsorted(idx.asi8, target.value, side="right")) - 1
    return idx[pos] if pos >= 0 else None
