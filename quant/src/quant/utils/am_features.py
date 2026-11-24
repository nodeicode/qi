"""Shared AM-feature helpers (strategy-agnostic).

Used by ``example_equity`` and (via deprecated re-exports) ``quant_range``.

Canonical builder uses Parkinson+CC blend RV (via ``quant.utils.indicators``)
and NumPy session-boundary aggregation for the live hot path.
"""

from __future__ import annotations

import zoneinfo

import numpy as np
import pandas as pd

from quant.utils.indicators import parkinson_rv, rolling_mean, wilder_atr

_ET = zoneinfo.ZoneInfo("America/New_York")

AM_FEATURE_COLUMNS = pd.Index(
    [
        "am_ret",
        "am_range_pct",
        "am_rv",
        "am_vwap_dev",
        "am_up_frac",
        "am_dir",
        "am_close_loc",
        "am_vol_ratio",
    ]
)

# Extra columns produced by the canonical builder (not in AM_FEATURE_COLUMNS).
_AM_PUBLIC_EXTRA_COLS = ("am_last_close", "am_last_bar_ts")
_AM_EXTRA_COLS = (*_AM_PUBLIC_EXTRA_COLS, "am_vol")
_AM_PUBLIC_COLUMNS = pd.Index(list(AM_FEATURE_COLUMNS) + list(_AM_PUBLIC_EXTRA_COLS))
_AM_RAW_COLUMNS = pd.Index(list(AM_FEATURE_COLUMNS) + list(_AM_EXTRA_COLS))


def empty_am_features() -> pd.DataFrame:
    """Return an empty AM-feature frame with the core public feature columns."""
    return pd.DataFrame(columns=AM_FEATURE_COLUMNS)


def _empty_public_am_features() -> pd.DataFrame:
    """Empty public builder frame (core features + last-close extras, no ``am_vol``)."""
    return pd.DataFrame(columns=_AM_PUBLIC_COLUMNS)


def _empty_raw_am_features() -> pd.DataFrame:
    """Empty frame including internal ``am_vol`` + last-close extras."""
    return pd.DataFrame(columns=_AM_RAW_COLUMNS)


def _dt_components(dti: pd.DatetimeIndex):
    """Datetime accessor via Series — ty-safe vs incomplete DatetimeIndex stubs."""
    return pd.Series(pd.DatetimeIndex(dti)).dt


def to_et_index(index) -> pd.DatetimeIndex:
    """Map UTC-naive (or tz-aware) bar timestamps to America/New_York."""
    dti = pd.DatetimeIndex(index)
    if len(dti) == 0:
        return dti
    if dti.tz is None:
        dti = dti.tz_localize("UTC")
    return dti.tz_convert(_ET)


def to_et_session_midnight(index) -> pd.DatetimeIndex:
    """Map timestamps to naive midnight keyed by America/New_York session date.

    Daily OHLCV bars and AM feature frames must share this contract so
    ``reindex`` never silently NaNs ``AM_Last_Close`` / ``am_*`` columns
    due to UTC vs ET calendar mismatches (e.g. ``2026-07-20 00:00 UTC``
    is still the prior evening in ET).

    * Tz-aware input → convert to ET, take calendar date.
    * Naive input → treat the timestamp's calendar date as the session
      label (already date-normalized by callers).

    Note: avoids ``DatetimeIndex.date`` (removed / stub-missing in some
    pandas builds); uses vectorized year/month/day instead.
    """
    dti = pd.DatetimeIndex(index)
    if len(dti) == 0:
        return dti
    if dti.tz is not None:
        dti = dti.tz_convert(_ET)
    dt = _dt_components(dti)
    return pd.DatetimeIndex(
        pd.to_datetime({"year": dt.year, "month": dt.month, "day": dt.day})
    )


def et_hhmm_mask(index, lo_hhmm: int, hi_exclusive: int) -> np.ndarray:
    """Boolean mask for ET bars with ``lo_hhmm <= HHMM < hi_exclusive``."""
    et = to_et_index(index)
    if len(et) == 0:
        return np.zeros(0, dtype=bool)
    dt = _dt_components(et)
    hhmm = dt.hour.to_numpy(dtype=np.int64) * 100 + dt.minute.to_numpy(dtype=np.int64)
    return (hhmm >= int(lo_hhmm)) & (hhmm < int(hi_exclusive))


def entry_bar_mask(index, hour: int, minute: int) -> np.ndarray:
    """Boolean mask for ET bars matching ``(hour, minute)``."""
    et = to_et_index(index)
    if len(et) == 0:
        return np.zeros(0, dtype=bool)
    dt = _dt_components(et)
    return (dt.hour.to_numpy() == int(hour)) & (dt.minute.to_numpy() == int(minute))


def format_am_strike_center(s: float, bar_ts=None) -> str:
    """Format strike-center log fragment: ``S=751.90(AM - 2026-07-17 09:45 ET)``."""
    label = "AM"
    if bar_ts is not None and not (isinstance(bar_ts, float) and np.isnan(bar_ts)):
        try:
            if pd.notna(bar_ts):
                t = pd.Timestamp(bar_ts)
                if t.tzinfo is not None:
                    t = t.tz_convert(_ET).tz_localize(None)
                else:
                    # Naive values are UTC wall clock from the equity handler.
                    t = t.tz_localize("UTC").tz_convert(_ET).tz_localize(None)
                label = f"AM - {t.strftime('%Y-%m-%d %H:%M')} ET"
        except (TypeError, ValueError, OverflowError):
            pass
    return f"S={float(s):.2f}({label})"


def align_timestamp_to_index(ts: pd.Timestamp, index: pd.DatetimeIndex) -> pd.Timestamp:
    """Make *ts* comparable with *index* (filter, elapsed-time math).

    Aware index → localize or convert entry into the index timezone.
    Naive index → keep naive entry; convert aware entry to UTC-naive wall time
    (matches inject_indicators / ``_to_utc_naive_us`` manage bars).
    """
    idx_tz = index.tz
    if idx_tz is not None:
        if ts.tzinfo is None:
            return ts.tz_localize(idx_tz)
        if ts.tz != idx_tz:
            return ts.tz_convert(idx_tz)
        return ts
    if ts.tzinfo is None:
        return ts
    return ts.tz_convert("UTC").tz_localize(None)


def atr_fast(
    high: np.ndarray, low: np.ndarray, close: np.ndarray, period: int = 14
) -> np.ndarray:
    """Wilder-smoothed Average True Range (shared TA-Lib implementation)."""
    return wilder_atr(high, low, close, period)


def _session_keys_from_et(idx_et: pd.DatetimeIndex) -> pd.DatetimeIndex:
    """Naive midnight session keys from an ET DatetimeIndex (vectorized)."""
    if len(idx_et) == 0:
        return pd.DatetimeIndex([])
    dt = _dt_components(idx_et)
    return pd.DatetimeIndex(
        pd.to_datetime({"year": dt.year, "month": dt.month, "day": dt.day})
    )


def _am_sessions_in_frame(df_15m: pd.DataFrame, entry_hhmm: int) -> pd.DatetimeIndex:
    """Unique ET session midnights that have AM bars in *df_15m*."""
    if df_15m is None or df_15m.empty:
        return pd.DatetimeIndex([])
    mask = et_hhmm_mask(df_15m.index, 930, int(entry_hhmm))
    if not np.any(mask):
        return pd.DatetimeIndex([])
    keys = _session_keys_from_et(to_et_index(df_15m.index)[mask])
    # Unique preserving sorted order
    return pd.DatetimeIndex(keys.unique()).sort_values()


def _apply_vol_ratio(daily: pd.DataFrame) -> pd.DataFrame:
    """Recompute ``am_vol_ratio`` from stored ``am_vol`` (history-dependent)."""
    out = daily
    if out.empty or "am_vol" not in out.columns:
        return out
    vol = out["am_vol"].to_numpy(dtype=np.float64)
    mean = rolling_mean(vol, 20, min_periods=5)
    with np.errstate(divide="ignore", invalid="ignore"):
        ratio = vol / mean
    out = out.copy()
    out["am_vol_ratio"] = ratio
    return out


def _public_daily(daily: pd.DataFrame) -> pd.DataFrame:
    """Drop internal ``am_vol``; keep public AM columns + last-close extras."""
    if daily.empty:
        return _empty_public_am_features()
    cols = [c for c in daily.columns if c != "am_vol"]
    return daily.loc[:, cols].copy()


def _build_am_features_raw(
    df_15m: pd.DataFrame,
    entry_hhmm: int = 1000,
) -> pd.DataFrame:
    """Session-keyed AM features including internal ``am_vol``.

    Window: ``[09:30, entry_hhmm)`` ET. NumPy session-boundary aggregation.
    """
    if df_15m is None or df_15m.empty:
        return _empty_raw_am_features()

    idx_et = to_et_index(df_15m.index)
    dt = _dt_components(idx_et)
    hhmm = dt.hour.to_numpy(dtype=np.int64) * 100 + dt.minute.to_numpy(dtype=np.int64)
    mask = (hhmm >= 930) & (hhmm < int(entry_hhmm))
    if not np.any(mask):
        return _empty_raw_am_features()

    o = df_15m["Open"].to_numpy(dtype=np.float64)[mask]
    h = df_15m["High"].to_numpy(dtype=np.float64)[mask]
    lo = df_15m["Low"].to_numpy(dtype=np.float64)[mask]
    c = df_15m["Close"].to_numpy(dtype=np.float64)[mask]
    v = df_15m["Volume"].to_numpy(dtype=np.float64)[mask]
    et = idx_et[mask]
    et_dt = _dt_components(et)

    # Integer YYYYMMDD session ids for boundary detection (sorted by time).
    sess = (
        et_dt.year.to_numpy(dtype=np.int64) * 10000
        + et_dt.month.to_numpy(dtype=np.int64) * 100
        + et_dt.day.to_numpy(dtype=np.int64)
    )
    change = np.empty(len(sess), dtype=bool)
    change[0] = True
    change[1:] = sess[1:] != sess[:-1]
    starts = np.flatnonzero(change)
    ends = np.empty_like(starts)
    ends[:-1] = starts[1:]
    ends[-1] = len(sess)
    n_groups = len(starts)

    am_ret = np.empty(n_groups, dtype=np.float64)
    am_range = np.empty(n_groups, dtype=np.float64)
    am_rv = np.empty(n_groups, dtype=np.float64)
    am_vwap_dev = np.empty(n_groups, dtype=np.float64)
    am_up = np.empty(n_groups, dtype=np.float64)
    am_dir = np.empty(n_groups, dtype=np.float64)
    am_loc = np.empty(n_groups, dtype=np.float64)
    am_vol = np.empty(n_groups, dtype=np.float64)
    am_close = np.empty(n_groups, dtype=np.float64)
    last_ts = np.empty(n_groups, dtype="datetime64[ns]")
    dates: list[object] = []

    for gi in range(n_groups):
        s = int(starts[gi])
        e = int(ends[gi])
        n = e - s
        o0 = float(o[s])
        c_last = float(c[e - 1])
        hi = float(np.max(h[s:e]))
        lo_v = float(np.min(lo[s:e]))
        vol_sum = float(np.sum(v[s:e]))
        am_close[gi] = c_last
        am_vol[gi] = vol_sum
        # ET last bar → UTC-naive wall clock (matches prior groupby path).
        last_bar = pd.Timestamp(et[e - 1])
        last_ts[gi] = np.datetime64(
            last_bar.tz_convert("UTC").tz_localize(None).to_datetime64()
        )
        dates.append(pd.Timestamp(et[s]).date())

        am_ret[gi] = (c_last - o0) / o0 if o0 > 0 else np.nan
        am_range[gi] = (hi - lo_v) / o0 if o0 > 0 else np.nan

        if n >= 3:
            log_c = np.log(c[s:e])
            rets = np.diff(log_c)
            # Require >=2 returns (n>=3 closes) — matches prior ``len(rets) >= 2``.
            cc_rv = float(np.std(rets, ddof=0) * np.sqrt(n))
        else:
            cc_rv = float("nan")
        pk = parkinson_rv(h[s:e], lo[s:e], n)
        if np.isfinite(cc_rv) and np.isfinite(pk):
            am_rv[gi] = 0.5 * (cc_rv + pk)
        elif np.isfinite(pk):
            am_rv[gi] = pk
        else:
            am_rv[gi] = cc_rv

        if vol_sum > 0:
            vwap = float(np.sum(c[s:e] * v[s:e]) / vol_sum)
        else:
            vwap = c_last
        am_vwap_dev[gi] = (c_last - vwap) / vwap if vwap > 0 else np.nan
        am_up[gi] = float(np.sum(c[s:e] > o[s:e])) / n
        if c_last > o0:
            am_dir[gi] = 1.0
        elif c_last < o0:
            am_dir[gi] = -1.0
        else:
            am_dir[gi] = 0.0
        am_loc[gi] = (c_last - lo_v) / (hi - lo_v) if (hi - lo_v) > 0 else 0.5

    out = pd.DataFrame(
        {
            "am_ret": am_ret,
            "am_range_pct": am_range,
            "am_rv": am_rv,
            "am_vwap_dev": am_vwap_dev,
            "am_up_frac": am_up,
            "am_dir": am_dir,
            "am_close_loc": am_loc,
            "am_vol": am_vol,
            "am_last_close": am_close,
            "am_last_bar_ts": last_ts,
        },
        index=to_et_session_midnight(pd.to_datetime(dates)),
    ).sort_index()
    return _apply_vol_ratio(out)


def build_am_features(
    df_15m: pd.DataFrame,
    entry_hhmm: int = 1000,
) -> pd.DataFrame:
    """Session-keyed AM features from a 15m OHLCV frame.

    Window: ``[09:30, entry_hhmm)`` ET.  Also attaches ``am_last_close`` /
    ``am_last_bar_ts`` (UTC-naive) for strike centering — one pass over AM bars.

    Uses Parkinson+CC blend RV (``parkinson_rv``) so a short 15m morning
    window still yields a non-degenerate ``am_rv``.
    """
    raw = _build_am_features_raw(df_15m, entry_hhmm=entry_hhmm)
    return _public_daily(raw)


def scatter_daily_on_mask(
    daily: pd.DataFrame,
    session_keys: pd.DatetimeIndex,
    entry_mask: np.ndarray,
    columns: list[str] | tuple[str, ...],
) -> dict[str, np.ndarray]:
    """Map session-keyed daily features onto entry-bar positions.

    Returns arrays of length ``len(entry_mask)`` — values only where
    ``entry_mask`` is True (and the mapped value is finite / non-NaT);
    elsewhere NaN / NaT.
    """
    n = len(entry_mask)
    entry = np.asarray(entry_mask, dtype=bool)
    if daily is None or daily.empty or n == 0 or not np.any(entry):
        result: dict[str, np.ndarray] = {}
        for c in columns:
            if c in ("am_last_bar_ts",) or str(c).endswith("_ts"):
                result[c] = np.full(n, np.datetime64("NaT"), dtype="datetime64[ns]")
            else:
                result[c] = np.full(n, np.nan, dtype=np.float64)
        return result

    mapped = daily.reindex(session_keys)
    result = {}
    for c in columns:
        if c not in mapped.columns:
            continue
        vals = mapped[c]
        if c == "am_last_bar_ts" or str(c).endswith("_ts"):
            out_ts = np.full(n, np.datetime64("NaT"), dtype="datetime64[ns]")
            # Fast path: an already-datetime64 column (the common am case) needs
            # no expensive pd.to_datetime coercion over the full 31k frame.
            if getattr(vals.dtype, "kind", None) == "M":
                raw = vals.to_numpy(copy=False)
                if raw.dtype != np.dtype("datetime64[ns]"):
                    raw = raw.astype("datetime64[ns]")
                ok = entry & ~np.isnat(raw)
                out_ts[ok] = raw[ok]
            else:
                raw = pd.to_datetime(vals, errors="coerce").to_numpy(
                    dtype="datetime64[ns]"
                )
                ok = entry & ~pd.isna(vals).to_numpy()
                out_ts[ok] = raw[ok]
            result[c] = out_ts
        else:
            arr = vals.to_numpy(dtype=np.float64, copy=True)
            out = np.full(n, np.nan, dtype=np.float64)
            ok = entry & np.isfinite(arr)
            out[ok] = arr[ok]
            result[c] = out
    return result


class AmFeatureStore:
    """Incremental daily AM feature cache keyed by ET session midnight.

    Cold start / ``entry_hhmm`` change → full rebuild.
    Warm ticks → refresh from the last cached session onward (AM window may
    still be forming) and drop sessions outside the current lookback.
    """

    def __init__(self) -> None:
        self._daily: pd.DataFrame = _empty_raw_am_features()
        self._entry_hhmm: int | None = None
        self._n_bars: int = 0
        self._first_ts = None
        self._last_ts = None

    @property
    def daily(self) -> pd.DataFrame:
        """Public daily frame (no internal ``am_vol``)."""
        return _public_daily(self._daily)

    def clear(self) -> None:
        self._daily = _empty_raw_am_features()
        self._entry_hhmm = None
        self._n_bars = 0
        self._first_ts = None
        self._last_ts = None

    def _remember_frame(self, df_15m: pd.DataFrame, entry_hhmm: int) -> None:
        self._entry_hhmm = entry_hhmm
        if df_15m is None or df_15m.empty:
            self._n_bars = 0
            self._first_ts = None
            self._last_ts = None
            return
        self._n_bars = len(df_15m)
        self._first_ts = df_15m.index[0]
        self._last_ts = df_15m.index[-1]

    def _frame_unchanged(self, df_15m: pd.DataFrame, entry_hhmm: int) -> bool:
        if self._entry_hhmm != entry_hhmm or df_15m is None or df_15m.empty:
            return False
        return (
            self._n_bars == len(df_15m)
            and self._first_ts == df_15m.index[0]
            and self._last_ts == df_15m.index[-1]
        )

    def _full_rebuild(self, df_15m: pd.DataFrame, entry_hhmm: int) -> pd.DataFrame:
        self._daily = _build_am_features_raw(df_15m, entry_hhmm=entry_hhmm)
        self._remember_frame(df_15m, entry_hhmm)
        return self.daily

    def update(self, df_15m: pd.DataFrame, entry_hhmm: int) -> pd.DataFrame:
        """Return full daily AM features; recompute only dirty tail sessions."""
        entry_hhmm = int(entry_hhmm)

        if (
            self._daily.empty
            or self._entry_hhmm != entry_hhmm
            or df_15m is None
            or df_15m.empty
        ):
            return self._full_rebuild(df_15m, entry_hhmm)

        # Identical frame fingerprint → return cached daily (live warm tick).
        if self._frame_unchanged(df_15m, entry_hhmm):
            return self.daily

        lookback_slid = self._first_ts is not None and df_15m.index[0] > self._first_ts
        rebuild_from = self._daily.index[-1]

        # Slice bars from just before rebuild_from (ET midnight → UTC-naive).
        # Avoids scanning the full PRE_LOAD window on every tick.
        start_et = pd.Timestamp(rebuild_from).tz_localize(_ET) - pd.Timedelta(days=1)
        start_utc = start_et.tz_convert("UTC").tz_localize(None)
        idx = pd.DatetimeIndex(df_15m.index)
        if idx.tz is not None:
            start_bound = start_et.tz_convert(idx.tz)
            tail = df_15m.loc[idx >= start_bound]
        else:
            tail = df_15m.loc[idx >= start_utc]

        if tail.empty:
            return self._full_rebuild(df_15m, entry_hhmm)

        tail_et = to_et_index(tail.index)
        tail_sess = _session_keys_from_et(tail_et)
        tail_mask = tail_sess >= rebuild_from
        if not np.any(tail_mask):
            return self._full_rebuild(df_15m, entry_hhmm)

        new_rows = _build_am_features_raw(tail.loc[tail_mask], entry_hhmm=entry_hhmm)
        head = self._daily.loc[self._daily.index < rebuild_from]
        if head.empty:
            merged = new_rows
        elif new_rows.empty:
            merged = head
        else:
            merged = pd.concat([head, new_rows]).sort_index()
            merged = merged[~merged.index.duplicated(keep="last")]

        if lookback_slid:
            idx_ends = to_et_index(df_15m.index[[0, -1]])
            ends = _session_keys_from_et(idx_ends)
            lo, hi = ends[0], ends[-1]
            merged = merged.loc[(merged.index >= lo) & (merged.index <= hi)]

        self._daily = _apply_vol_ratio(merged)
        self._remember_frame(df_15m, entry_hhmm)
        return self.daily
