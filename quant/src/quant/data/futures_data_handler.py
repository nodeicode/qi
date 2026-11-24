"""Futures and options-on-futures market-data handler (Databento-only).

Responsibilities
----------------
- Fetch futures OHLCV from Databento ``GLBX.MDP3`` (outright ``raw_symbol`` or
  ``continuous`` symbology).
- Fetch options-on-futures OHLCV and CBBO mids via CME ``raw_symbol``
  (built from weekly product code + expiration date — no definition pull).
- Discover OOF expirations via tiny ``definition`` snapshots on the **one**
  weekly parent expected for each date (``EX1``-``EX4`` / ``X1A``-``X5D`` / ``EX``).
- Persist everything in :class:`FuturesDataCache` (parquet + SQLite).
"""

from __future__ import annotations

import bisect
import datetime
import logging
import math
import os
import threading
import warnings
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Any, TypedDict

import databento as db
import numpy as np
import pandas as pd
from dotenv import load_dotenv

from ._times import assert_canonical_index, parse_naive_utc, to_naive_index
from .cache.futures import FuturesCacheStats, FuturesDataCache

CacheStats = FuturesCacheStats

_log = logging.getLogger(__name__)

_UNLISTED_BID = -1.0
_CBBO_1S_START = datetime.datetime(2025, 2, 20, tzinfo=datetime.UTC)
_DATASET = "GLBX.MDP3"
_MAX_SYM_PER_REQ = 2000
_PROBE_WINDOW_DAYS = 2
_CME_MONTH_CODES = "FGHJKMNQUVXZ"
_EXPIRATION_DEF_LIMIT_STEPS = (500, 2000, 5000)
_EXPIRATION_DIRECT_PROBE_THRESHOLD = 30
_EXPIRATION_HEAVY_FALLBACK_MAX_REMAINING = 5
_DEFAULT_NET_CONCURRENCY = 50

# CME GLBX weekly OOF parents (see micro E-mini FAQ).  Quarterlies (``MES.OPT`` /
# ``ES.OPT``) are intentionally omitted — backtests target same-day weeklies only.
_MES_OOF_PARENTS: tuple[str, ...] = (
    "EX.OPT",
    *(f"EX{i}.OPT" for i in range(1, 5)),
    *(f"X{w}{d}.OPT" for w in range(1, 6) for d in "ABCD"),
)
_ES_OOF_PARENTS: tuple[str, ...] = (
    "EW.OPT",
    *(f"EW{i}.OPT" for i in range(1, 5)),
    # Daily (Mon-Thu) weekly ES parents — E1A-E5D.OPT
    *(f"E{w}{d}.OPT" for w in range(1, 6) for d in "ABCD"),
)
OOF_PARENTS_BY_ROOT: dict[str, tuple[str, ...]] = {
    "MES": _MES_OOF_PARENTS,
    "ES": _ES_OOF_PARENTS,
}

_dotenv_path = Path(__file__).resolve().parents[1] / ".env"
if _dotenv_path.exists():
    load_dotenv(dotenv_path=_dotenv_path)

_LegKey = tuple[str, float, str, str]
_GroupKey = tuple[str, float, str, str, str]
_PrefetchJob = tuple[
    str,
    list[str],
    list[dict[str, Any]],
    datetime.datetime,
    datetime.datetime,
]


class _CbboResult(TypedDict):
    bid: float
    ask: float
    mid: float
    ts_used: str


def _valid_mid(bid: float | None, ask: float | None) -> float | None:
    if bid is None or ask is None:
        return None
    try:
        b, a = float(bid), float(ask)
    except (TypeError, ValueError):
        return None
    if not (math.isfinite(b) and math.isfinite(a)):
        return None
    if b < 0 or a <= 0:
        return None
    return (b + a) / 2.0


def _ensure_utc(dt: datetime.datetime) -> datetime.datetime:
    return dt if dt.tzinfo is not None else dt.replace(tzinfo=datetime.UTC)


def _definition_snapshot_window(
    at: datetime.datetime | datetime.date | str,
    *,
    span_days: int = 2,
) -> tuple[datetime.datetime, datetime.datetime]:
    """UTC-midnight window for Databento instrument-definition snapshots."""
    if isinstance(at, str):
        day = datetime.datetime.strptime(at, "%Y-%m-%d").date()
    elif isinstance(at, datetime.date) and not isinstance(at, datetime.datetime):
        day = at
    else:
        day = _ensure_utc(at).date()
    start = datetime.datetime.combine(day, datetime.time.min, tzinfo=datetime.UTC)
    end = start + datetime.timedelta(days=max(1, span_days))
    return start, end


def _cbbo_schema_for(at_utc: datetime.datetime) -> str:
    """Top-of-book quote schema for GLBX.MDP3 (CME).

    GLBX does **not** support OPRA-style ``cbbo-*`` schemas.  Use ``bbo-1m`` /
    ``bbo-1s`` (same ``bid_px_00`` / ``ask_px_00`` fields, venue-native BBO).
    """
    return "bbo-1s" if _ensure_utc(at_utc) >= _CBBO_1S_START else "bbo-1m"


def _is_symbology_error(exc: BaseException) -> bool:
    msg = str(exc).lower()
    return any(
        tok in msg
        for tok in (
            "symbology",
            "resolution",
            "unknown symbol",
            "symbol not found",
            "not listed",
            "contract not listed",
        )
    )


def _normalize_opt_type(opt_type: str) -> str:
    return "C" if opt_type.lower() in ("c", "call") else "P"


def _week_of_month(day: datetime.date) -> int:
    return (day.day - 1) // 7 + 1


def _is_last_friday_of_month(day: datetime.date) -> bool:
    if day.weekday() != 4:
        return False
    return (day + datetime.timedelta(days=7)).month != day.month


def _friday_index_in_month(day: datetime.date) -> int:
    count = 0
    cur = day.replace(day=1)
    while cur <= day:
        if cur.weekday() == 4:
            count += 1
        cur += datetime.timedelta(days=1)
    return count


def _weekly_product_stem(root: str, exp: datetime.date) -> str:
    """CME Globex weekly OOF product stem (e.g. ``X1C``, ``EX3``, ``EW``, ``E1A``).

    ES daily (Mon-Thu) weeklies use the ``E{week}{letter}`` convention
    (e.g. ``E1A`` for week-1 Monday, ``E2C`` for week-2 Wednesday), mirroring
    the MES ``X{week}{letter}`` scheme.  Friday/EOM ES uses ``EW{n}`` / ``EW``.
    """
    root_u = root.upper()
    fri_prefix = "EW" if root_u == "ES" else "EX"
    # Friday — same logic for ES and MES
    if exp.weekday() == 4:
        if _is_last_friday_of_month(exp):
            return fri_prefix
        return f"{fri_prefix}{_friday_index_in_month(exp)}"
    # Mon-Thu — ES uses E{week}{letter}, MES uses X{week}{letter}
    day_letters = {0: "A", 1: "B", 2: "C", 3: "D"}
    letter = day_letters.get(exp.weekday())
    if letter is None:
        raise ValueError(f"No weekly OOF product for {exp} (weekday={exp.weekday()})")
    prefix = "E" if root_u == "ES" else "X"
    return f"{prefix}{_week_of_month(exp)}{letter}"


def parent_symbol_for_expiration(root: str, exp: datetime.date) -> str:
    """GLBX parent symbol (``EX3.OPT``) for a listed weekly expiration."""
    return f"{_weekly_product_stem(root, exp)}.OPT"


def format_oof_raw_symbol(
    root: str,
    strike: float,
    expiration: str,
    opt_type: str,
) -> str:
    """Build CME OOF ``raw_symbol`` (e.g. ``X1CF6 P6000``) without definitions."""
    exp = datetime.datetime.strptime(expiration, "%Y-%m-%d").date()
    stem = _weekly_product_stem(root, exp)
    month_year = _CME_MONTH_CODES[exp.month - 1] + str(exp.year % 10)
    cp = _normalize_opt_type(opt_type)
    return f"{stem}{month_year} {cp}{round(strike)}"


class FuturesDataHandler:
    """High-throughput futures + OOF data + cache front-end backed by Databento."""

    _net_sema = threading.Semaphore(_DEFAULT_NET_CONCURRENCY)
    _cache_write_lock = threading.RLock()

    # Tiny definition probes for expiration-only lookups (mirrors options handler).
    EXPIRATION_PROBE_MAX_ROWS = 2

    def __init__(
        self,
        cache_dir: str | None = None,
        verbose: bool = False,
        default_stype_in: str = "continuous",
    ):
        if cache_dir is None:
            cache_dir = str(Path(__file__).resolve().parent / "futures_cache")
        self._cache = FuturesDataCache(cache_dir=cache_dir, verbose=verbose)
        self.databento_client: db.Historical = db.Historical(
            key=os.getenv("DATABENTO_API_KEY")
        )
        self.default_stype_in = default_stype_in

        self._missing_symbols: set[str] = set()
        self._expiration_lookup_cache: dict[tuple[str, str], str | None] = {}
        self._last_cbbo_prefetch_stats: dict = {}

    # ── Schema / processing helpers ───────────────────────────────────
    @staticmethod
    def _interval_to_schema(interval: str) -> str:
        if interval in ("1h", "60"):
            return "ohlcv-1h"
        if interval in ("1d", "d"):
            return "ohlcv-1d"
        return "ohlcv-1m"

    @staticmethod
    def _needs_resample(interval: str) -> bool:
        return interval in ("5m", "5", "15m", "15", "30m", "30")

    @staticmethod
    def _resample_minutes(interval: str) -> int:
        return {"5m": 5, "5": 5, "15m": 15, "15": 15, "30m": 30, "30": 30}.get(
            interval, 0
        )

    @staticmethod
    def _resample_ohlcv(df: pd.DataFrame, minutes: int) -> pd.DataFrame:
        if df.empty or minutes <= 1:
            return df
        resampler = df.resample(f"{minutes}min")
        parts: dict[str, pd.Series] = {}
        if "open" in df.columns:
            parts["open"] = resampler["open"].first()
        if "high" in df.columns:
            parts["high"] = resampler["high"].max()
        if "low" in df.columns:
            parts["low"] = resampler["low"].min()
        if "close" in df.columns:
            parts["close"] = resampler["close"].last()
        if "volume" in df.columns:
            parts["volume"] = resampler["volume"].sum()
        out = pd.DataFrame(parts).dropna(subset=["close"])
        if "high" in out.columns and "low" in out.columns:
            out["mid"] = (out["high"] + out["low"]) / 2.0
            spread = out["mid"] * 0.05
            out["bid"] = out["mid"] - spread / 2.0
            out["ask"] = out["mid"] + spread / 2.0
        return out

    @staticmethod
    def _process_databento_df(df: pd.DataFrame, interval: str) -> pd.DataFrame:
        if df.empty:
            return pd.DataFrame()
        df = df.rename(columns={"ts_event": "timestamp"})
        if "timestamp" in df.columns:
            df = df.set_index("timestamp")
        df = df.sort_index()
        for col in ("open", "high", "low", "close", "volume"):
            if col not in df.columns:
                df[col] = 0.0
        df["mid"] = (df["high"] + df["low"]) / 2.0
        spread = df["mid"] * 0.05
        df["bid"] = df["mid"] - spread / 2.0
        df["ask"] = df["mid"] + spread / 2.0
        if FuturesDataHandler._needs_resample(interval):
            df = FuturesDataHandler._resample_ohlcv(
                df, FuturesDataHandler._resample_minutes(interval)
            )
        return df

    @staticmethod
    def _to_title_case(df: pd.DataFrame, symbol: str) -> pd.DataFrame:
        if df.empty:
            return df
        out = df.rename(
            columns={
                "open": "Open",
                "high": "High",
                "low": "Low",
                "close": "Close",
                "volume": "Volume",
            }
        )
        out["symbol"] = symbol
        return out

    _INTRADAY_INTERVALS = frozenset(
        {"1m", "1", "5m", "5", "15m", "15", "30m", "30", "1h", "60", "60m"}
    )

    @staticmethod
    def _finalize_futures_index(df: pd.DataFrame, interval: str) -> pd.DataFrame:
        """Return intraday indexes as UTC-naive (parity with ``AlpacaDataHandler``).

        Cache may store UTC-aware timestamps; ``get_historical_data`` consumers
        (TradingEngine / strategies) expect naive == UTC wall clock.  Daily
        bars keep vendor timezone.  CBBO / OOF quote paths are unchanged.
        """
        out = to_naive_index(df, interval=interval)
        assert_canonical_index(out, interval=interval)
        return out

    # ── CBBO helpers ──────────────────────────────────────────────────
    @staticmethod
    def _cbbo_df_to_cache_df(
        df: pd.DataFrame, symbol: str | None = None
    ) -> pd.DataFrame:
        if df.empty:
            return pd.DataFrame()
        if symbol is not None and "symbol" in df.columns:
            df = df[df["symbol"] == symbol]
        if df.empty or "bid_px_00" not in df.columns or "ask_px_00" not in df.columns:
            return pd.DataFrame()

        bid = pd.to_numeric(df["bid_px_00"], errors="coerce")
        ask = pd.to_numeric(df["ask_px_00"], errors="coerce")
        mask = (
            bid.notna()
            & np.isfinite(bid)
            & (bid >= 0)
            & ask.notna()
            & np.isfinite(ask)
            & (ask > 0)
        )
        out = pd.DataFrame({"bid": bid[mask], "ask": ask[mask]})
        if out.empty:
            return out
        out["mid"] = (out["bid"] + out["ask"]) / 2.0
        if isinstance(out.index, pd.DatetimeIndex):
            out.index = (
                out.index.tz_localize("UTC")
                if out.index.tz is None
                else out.index.tz_convert("UTC")
            )
        return out[["bid", "ask", "mid"]]

    @staticmethod
    def _extract_cbbo_mid_from_cache(
        cache_df: pd.DataFrame, at_utc: datetime.datetime
    ) -> float | None:
        row = FuturesDataHandler._extract_cbbo_row(cache_df, at_utc)
        if row is None:
            return None
        return _valid_mid(row["bid"], row["ask"])

    @staticmethod
    def _extract_cbbo_row(
        cache_df: pd.DataFrame | None, at_utc: datetime.datetime
    ) -> pd.Series | None:
        if cache_df is None or cache_df.empty:
            return None
        if not isinstance(cache_df.index, pd.DatetimeIndex):
            return None
        idx: pd.DatetimeIndex = cache_df.index
        ts = pd.Timestamp(at_utc)
        if idx.tz is not None:
            target: pd.Timestamp = (
                ts.tz_localize("UTC") if ts.tzinfo is None else ts.tz_convert(idx.tz)
            )
        else:
            target = ts.tz_localize(None) if ts.tzinfo is not None else ts
        try:
            past = idx[idx <= target]
            if len(past) == 0:
                return None
            best = past[-1]
            row = cache_df.loc[best].copy()
            row.name = best
            return row
        except Exception:
            return None

    @staticmethod
    def _is_unlisted_cache(df: pd.DataFrame | None) -> bool:
        if df is None or df.empty:
            return False
        return float(df["bid"].iloc[0]) == _UNLISTED_BID

    def _store_unlisted_cbbo(
        self,
        root: str,
        strike: float,
        expiration: str,
        opt_type: str,
        schema: str,
        start_dt: datetime.datetime,
        end_dt: datetime.datetime,
    ) -> None:
        s = _ensure_utc(pd.Timestamp(start_dt).to_pydatetime())
        e = _ensure_utc(pd.Timestamp(end_dt).to_pydatetime())
        sentinel = pd.DataFrame(
            {
                "bid": [_UNLISTED_BID, _UNLISTED_BID],
                "ask": [_UNLISTED_BID, _UNLISTED_BID],
                "mid": [_UNLISTED_BID, _UNLISTED_BID],
            },
            index=pd.DatetimeIndex([pd.Timestamp(s), pd.Timestamp(e)]).tz_convert(
                "UTC"
            ),
        )
        with FuturesDataHandler._cache_write_lock:
            self._cache.put(root, strike, expiration, opt_type, s, e, schema, sentinel)

    # ── Futures bars ──────────────────────────────────────────────────
    def get_historical_data(
        self,
        ticker: str,
        start,
        end,
        interval: str = "1m",
        use_cache: bool = True,
        *,
        adjustment: str | None = None,
        **kwargs: Any,
    ) -> pd.DataFrame:
        """DataHandlerProtocol-compatible alias for futures bar fetches.

        *adjustment* is accepted for Alpaca API parity and ignored (futures
        bars are always unadjusted index points).
        """
        _ = adjustment, kwargs
        return self.get_futures_data(
            ticker,
            start,
            end,
            interval,
            stype_in=self.default_stype_in,
            use_cache=use_cache,
        )

    def get_futures_data(
        self,
        symbol: str,
        start,
        end,
        interval: str = "1m",
        *,
        stype_in: str = "raw_symbol",
        use_cache: bool = True,
        adjustment: str | None = None,
        **kwargs: Any,
    ) -> pd.DataFrame:
        _ = adjustment, kwargs
        start_dt = pd.to_datetime(start).to_pydatetime()
        end_dt = pd.to_datetime(end).to_pydatetime()

        def _return(df: pd.DataFrame) -> pd.DataFrame:
            return self._finalize_futures_index(
                self._to_title_case(df, symbol), interval
            )

        if not use_cache:
            df = self._fetch_futures_from_databento(
                symbol, start_dt, end_dt, interval, stype_in=stype_in
            )
            return _return(df)

        cov_min, cov_max = self._cache.get_coverage(symbol, interval)
        req_start, req_end = parse_naive_utc(start_dt, end_dt)

        if cov_min and cov_max:
            needs_head = req_start < cov_min
            needs_tail = req_end > cov_max

            if not needs_head and not needs_tail:
                cached = self._cache.get_futures(symbol, start_dt, end_dt, interval)
                return _return(cached if cached is not None else pd.DataFrame())

            if needs_head:
                self._fetch_and_cache_futures(
                    symbol,
                    start_dt,
                    cov_min.to_pydatetime(),
                    interval,
                    stype_in=stype_in,
                )
            if needs_tail:
                self._fetch_and_cache_futures(
                    symbol,
                    cov_max.to_pydatetime(),
                    end_dt,
                    interval,
                    stype_in=stype_in,
                )

            cached = self._cache.get_futures(symbol, start_dt, end_dt, interval)
            return _return(cached if cached is not None else pd.DataFrame())

        resampled = self._try_resample_from_cached_1m(
            symbol, start_dt, end_dt, interval
        )
        if resampled is not None and not resampled.empty:
            return _return(resampled)

        df = self._fetch_and_cache_futures(
            symbol, start_dt, end_dt, interval, stype_in=stype_in
        )
        return _return(df)

    def _try_resample_from_cached_1m(
        self,
        symbol: str,
        start_dt: datetime.datetime,
        end_dt: datetime.datetime,
        interval: str,
    ) -> pd.DataFrame | None:
        """Resample cached 1m futures bars when a coarser interval is requested."""
        if not self._needs_resample(interval):
            return None
        cov_min, cov_max = self._cache.get_coverage(symbol, "1m")
        if not cov_min or not cov_max:
            return None
        req_start, req_end = parse_naive_utc(start_dt, end_dt)
        if req_start < cov_min or req_end > cov_max:
            return None
        cached_1m = self._cache.get_futures(symbol, start_dt, end_dt, "1m")
        if cached_1m is None or cached_1m.empty:
            return None
        minutes = self._resample_minutes(interval)
        if minutes <= 0:
            return None
        work = cached_1m.copy()
        if "close" not in work.columns and "Close" in work.columns:
            work = work.rename(
                columns={
                    "Open": "open",
                    "High": "high",
                    "Low": "low",
                    "Close": "close",
                    "Volume": "volume",
                }
            )
        out = self._resample_ohlcv(work, minutes)
        if out.empty:
            return None
        with FuturesDataHandler._cache_write_lock:
            self._cache.put_futures(symbol, start_dt, end_dt, interval, out)
        return out

    def _fetch_and_cache_futures(
        self,
        symbol: str,
        start_dt: datetime.datetime,
        end_dt: datetime.datetime,
        interval: str,
        *,
        stype_in: str,
    ) -> pd.DataFrame:
        df = self._fetch_futures_from_databento(
            symbol, start_dt, end_dt, interval, stype_in=stype_in
        )
        with FuturesDataHandler._cache_write_lock:
            self._cache.put_futures(symbol, start_dt, end_dt, interval, df)
        return df

    def _fetch_futures_from_databento(
        self,
        symbol: str,
        start_dt: datetime.datetime,
        end_dt: datetime.datetime,
        interval: str,
        *,
        stype_in: str,
    ) -> pd.DataFrame:
        schema = self._interval_to_schema(interval)
        try:
            with FuturesDataHandler._net_sema, warnings.catch_warnings():
                warnings.simplefilter("ignore")
                store = self.databento_client.timeseries.get_range(
                    dataset=_DATASET,
                    symbols=symbol,
                    schema=schema,
                    start=start_dt,
                    end=end_dt,
                    stype_in=stype_in,
                )
            return self._process_databento_df(store.to_df(price_type="float"), interval)
        except Exception as exc:
            _log.error(
                "[Databento] futures OHLCV fetch failed for %s: %s",
                symbol,
                exc,
                exc_info=True,
            )
            return pd.DataFrame()

    # ── OOF symbol resolution ─────────────────────────────────────────
    def _parent_opt_symbol(self, root: str) -> str:
        return f"{root.upper()}.OPT"

    def _oof_parent_symbols(self, root: str) -> list[str]:
        """Weekly GLBX parent symbols for *root* (override via ``FUTURES_OOF_PARENTS``)."""
        override = os.getenv("FUTURES_OOF_PARENTS", "").strip()
        if override:
            return [p.strip() for p in override.split(",") if p.strip()]
        parents = OOF_PARENTS_BY_ROOT.get(root.upper())
        if parents:
            return list(parents)
        return [self._parent_opt_symbol(root)]

    def _resolve_oof_raw_symbol(
        self,
        root: str,
        strike: float,
        expiration: str,
        opt_type: str,
        at_date: datetime.datetime | None = None,
    ) -> str | None:
        del at_date  # symbology is fully determined by expiration + strike
        cached = self._cache.get_oof_raw_symbol(root, strike, expiration, opt_type)
        if cached:
            return cached
        try:
            sym = format_oof_raw_symbol(root, strike, expiration, opt_type)
        except ValueError:
            return None
        self._cache.set_oof_raw_symbol(root, strike, expiration, opt_type, sym)
        return sym

    @staticmethod
    def snap_strike_to_increment(strike: float, increment: float) -> float:
        """Round *strike* to the nearest CME OOF strike grid (e.g. $10 for MES)."""
        if increment <= 0:
            return round(strike, 2)
        return round(round(strike / increment) * increment, 2)

    def _fetch_expirations_for_parent(
        self,
        parent: str,
        start_dt: datetime.datetime,
        end_dt: datetime.datetime,
        *,
        limit: int | None = None,
    ) -> list[str]:
        """One tiny ``definition`` snapshot → unique expiration dates for *parent*."""
        try:
            with FuturesDataHandler._net_sema, warnings.catch_warnings():
                warnings.simplefilter("ignore")
                kwargs: dict[str, Any] = {
                    "dataset": _DATASET,
                    "symbols": [parent],
                    "schema": "definition",
                    "stype_in": "parent",
                    "start": start_dt,
                    "end": end_dt,
                }
                if limit is not None:
                    kwargs["limit"] = max(1, int(limit))
                store = self.databento_client.timeseries.get_range(**kwargs)
            df = store.to_df(price_type="float")
        except Exception as exc:
            level = logging.DEBUG
            msg = str(exc).lower()
            if "insufficient_funds" in msg or "402" in msg:
                level = logging.WARNING
            _log.log(
                level,
                "[Databento] OOF expiration snapshot failed for %s [%s -> %s]: %s",
                parent,
                start_dt,
                end_dt,
                exc,
            )
            return []
        if df is None or df.empty or "expiration" not in df.columns:
            return []
        return sorted(
            {
                d.strftime("%Y-%m-%d")
                for d in pd.to_datetime(df["expiration"], utc=True).dt.date
            }
        )

    # ── OOF OHLCV ─────────────────────────────────────────────────────
    def fetch_futures_option_data(
        self,
        root: str,
        strike: float,
        expiration: str,
        opt_type: str,
        start_dt: datetime.datetime,
        end_dt: datetime.datetime,
        interval: str = "1m",
    ) -> pd.DataFrame:
        superset = self._cache.get_superset(
            root, strike, expiration, opt_type, start_dt, end_dt, interval
        )
        if superset is not None and not superset.empty:
            if isinstance(superset.index, pd.DatetimeIndex):
                s_ts = pd.Timestamp(start_dt)
                e_ts = pd.Timestamp(end_dt)
                mask = (superset.index >= s_ts) & (superset.index <= e_ts)
                sliced = superset.loc[mask]
                with FuturesDataHandler._cache_write_lock:
                    self._cache.put(
                        root,
                        strike,
                        expiration,
                        opt_type,
                        start_dt,
                        end_dt,
                        interval,
                        sliced,
                    )
                return sliced
            return superset

        cached = self._cache.get(
            root, strike, expiration, opt_type, start_dt, end_dt, interval
        )
        if cached is not None:
            return cached

        df = self._fetch_oof_from_databento(
            root, strike, expiration, opt_type, start_dt, end_dt, interval
        )
        raw = self._cache.get_oof_raw_symbol(root, strike, expiration, opt_type)
        with FuturesDataHandler._cache_write_lock:
            self._cache.put(
                root,
                strike,
                expiration,
                opt_type,
                start_dt,
                end_dt,
                interval,
                df,
                raw_symbol=raw,
            )
        return df

    def _fetch_oof_from_databento(
        self,
        root: str,
        strike: float,
        expiration: str,
        opt_type: str,
        start_dt: datetime.datetime,
        end_dt: datetime.datetime,
        interval: str,
    ) -> pd.DataFrame:
        symbol = self._resolve_oof_raw_symbol(
            root, strike, expiration, opt_type, at_date=start_dt
        )
        if not symbol:
            return pd.DataFrame()

        schema = self._interval_to_schema(interval)
        try:
            with FuturesDataHandler._net_sema, warnings.catch_warnings():
                warnings.simplefilter("ignore")
                store = self.databento_client.timeseries.get_range(
                    dataset=_DATASET,
                    symbols=symbol,
                    schema=schema,
                    start=start_dt,
                    end=end_dt,
                    stype_in="raw_symbol",
                )
            return self._process_databento_df(store.to_df(price_type="float"), interval)
        except Exception as exc:
            _log.error(
                "[Databento] OOF OHLCV fetch failed for %s: %s",
                symbol,
                exc,
                exc_info=True,
            )
            return pd.DataFrame()

    # ── OOF CBBO ──────────────────────────────────────────────────────
    def fetch_futures_option_quote(
        self,
        root: str,
        strike: float,
        expiration: str,
        opt_type: str,
        at_time: datetime.datetime,
        window_min: int = 15,
    ) -> float | None:
        symbol = self._resolve_oof_raw_symbol(
            root, strike, expiration, opt_type, at_date=at_time
        )
        if not symbol or symbol in self._missing_symbols:
            return None

        at_utc = _ensure_utc(at_time)
        start = at_utc - datetime.timedelta(minutes=window_min)
        end = at_utc + datetime.timedelta(minutes=window_min)
        schema = _cbbo_schema_for(at_utc)

        cached = self._cache.get(root, strike, expiration, opt_type, start, end, schema)
        if cached is not None and not cached.empty:
            if self._is_unlisted_cache(cached):
                self._missing_symbols.add(symbol)
                return None
            return self._extract_cbbo_mid_from_cache(cached, at_utc)

        try:
            with FuturesDataHandler._net_sema, warnings.catch_warnings():
                warnings.simplefilter("ignore")
                store = self.databento_client.timeseries.get_range(
                    dataset=_DATASET,
                    symbols=symbol,
                    schema=schema,
                    start=start,
                    end=end,
                    stype_in="raw_symbol",
                )
            df = store.to_df(price_type="float")
        except Exception as exc:
            if _is_symbology_error(exc):
                self._missing_symbols.add(symbol)
                self._store_unlisted_cbbo(
                    root, strike, expiration, opt_type, schema, start, end
                )
            return None

        if df.empty:
            self._store_unlisted_cbbo(
                root, strike, expiration, opt_type, schema, start, end
            )
            return None

        cache_df = self._cbbo_df_to_cache_df(df, symbol=symbol)
        if not cache_df.empty:
            with FuturesDataHandler._cache_write_lock:
                self._cache.put(
                    root,
                    strike,
                    expiration,
                    opt_type,
                    start,
                    end,
                    schema,
                    cache_df,
                    raw_symbol=symbol,
                )
        else:
            self._store_unlisted_cbbo(
                root, strike, expiration, opt_type, schema, start, end
            )

        return self._extract_cbbo_mid_from_cache(cache_df, at_utc)

    def fetch_futures_option_quotes_batch(
        self, requests: list[dict[str, Any]], window_min: int = 15
    ) -> dict[_LegKey, float | None]:
        if not requests:
            return {}

        results: dict[_LegKey, float | None] = {}
        misses: list[dict[str, Any]] = []

        for r in requests:
            at_utc = _ensure_utc(r["at_time"])
            schema = _cbbo_schema_for(at_utc)
            start = at_utc - datetime.timedelta(minutes=window_min)
            end = at_utc + datetime.timedelta(minutes=window_min)
            rk = (r["root"], r["strike"], r["expiration"], r["opt_type"])
            sym = self._resolve_oof_raw_symbol(
                r["root"],
                r["strike"],
                r["expiration"],
                r["opt_type"],
                at_date=at_utc,
            )

            if not sym or sym in self._missing_symbols:
                results[rk] = None
                continue

            cached = self._cache.get(
                r["root"],
                r["strike"],
                r["expiration"],
                r["opt_type"],
                start,
                end,
                schema,
            )
            if (
                cached is not None
                and not cached.empty
                and not self._is_unlisted_cache(cached)
            ):
                results[rk] = self._extract_cbbo_mid_from_cache(cached, at_utc)
                continue

            misses.append(
                {
                    "rk": rk,
                    "sym": sym,
                    "schema": schema,
                    "at_utc": at_utc,
                    "start": start,
                    "end": end,
                    "root": r["root"],
                    "strike": r["strike"],
                    "expiration": r["expiration"],
                    "opt_type": r["opt_type"],
                }
            )

        if not misses:
            return results

        groups: dict[
            tuple[str, datetime.datetime, datetime.datetime], list[dict[str, Any]]
        ] = {}
        for m in misses:
            groups.setdefault((m["schema"], m["start"], m["end"]), []).append(m)

        for (schema, start, end), items in groups.items():
            symbols = sorted({m["sym"] for m in items})
            df = self._batched_get_range_with_recovery(
                schema=schema,
                symbols=symbols,
                start=start,
                end=end,
                items=items,
                results=results,
            )
            if df is None:
                continue

            sym_groups: dict[str, pd.DataFrame] = (
                {str(k): v for k, v in df.groupby("symbol")}
                if "symbol" in df.columns
                else {}
            )
            for m in items:
                sub = sym_groups.get(m["sym"], pd.DataFrame()) if sym_groups else df
                if sub is None or sub.empty:
                    self._store_unlisted_cbbo(
                        m["root"],
                        m["strike"],
                        m["expiration"],
                        m["opt_type"],
                        schema,
                        m["start"],
                        m["end"],
                    )
                    results[m["rk"]] = None
                    continue
                cache_df = self._cbbo_df_to_cache_df(sub, symbol=m["sym"])
                if cache_df.empty:
                    self._store_unlisted_cbbo(
                        m["root"],
                        m["strike"],
                        m["expiration"],
                        m["opt_type"],
                        schema,
                        m["start"],
                        m["end"],
                    )
                    results[m["rk"]] = None
                    continue
                with FuturesDataHandler._cache_write_lock:
                    self._cache.put(
                        m["root"],
                        m["strike"],
                        m["expiration"],
                        m["opt_type"],
                        m["start"],
                        m["end"],
                        schema,
                        cache_df,
                        raw_symbol=m["sym"],
                    )
                results[m["rk"]] = self._extract_cbbo_mid_from_cache(
                    cache_df, m["at_utc"]
                )

        return results

    def _batched_get_range_with_recovery(
        self,
        *,
        schema: str,
        symbols: list[str],
        start: datetime.datetime,
        end: datetime.datetime,
        items: list[dict[str, Any]],
        results: dict[tuple[Any, ...], Any],
    ) -> pd.DataFrame | None:
        try:
            with FuturesDataHandler._net_sema, warnings.catch_warnings():
                warnings.simplefilter("ignore")
                store = self.databento_client.timeseries.get_range(
                    dataset=_DATASET,
                    symbols=symbols,
                    schema=schema,
                    start=start,
                    end=end,
                    stype_in="raw_symbol",
                )
            return store.to_df(price_type="float")
        except Exception as exc:
            if not _is_symbology_error(exc):
                _log.warning("CBBO batch failed (%d syms): %s", len(symbols), exc)
                for m in items:
                    results.setdefault(m["rk"], None)
                return None

            sym_to_items: dict[str, list[dict]] = {}
            for m in items:
                sym_to_items.setdefault(m["sym"], []).append(m)

            recovered: list[pd.DataFrame] = []
            for sym in symbols:
                try:
                    with FuturesDataHandler._net_sema, warnings.catch_warnings():
                        warnings.simplefilter("ignore")
                        s = self.databento_client.timeseries.get_range(
                            dataset=_DATASET,
                            symbols=[sym],
                            schema=schema,
                            start=start,
                            end=end,
                            stype_in="raw_symbol",
                        )
                    sdf = s.to_df(price_type="float")
                    if not sdf.empty:
                        if "symbol" not in sdf.columns:
                            sdf = sdf.assign(symbol=sym)
                        recovered.append(sdf)
                    else:
                        for m in sym_to_items.get(sym, []):
                            self._store_unlisted_cbbo(
                                m["root"],
                                m["strike"],
                                m["expiration"],
                                m["opt_type"],
                                schema,
                                m["start"],
                                m["end"],
                            )
                            results[m["rk"]] = None
                except Exception as inner:
                    if _is_symbology_error(inner):
                        self._missing_symbols.add(sym)
                        for m in sym_to_items.get(sym, []):
                            self._store_unlisted_cbbo(
                                m["root"],
                                m["strike"],
                                m["expiration"],
                                m["opt_type"],
                                schema,
                                m["start"],
                                m["end"],
                            )
                            results[m["rk"]] = None

            if not recovered:
                return None
            return pd.concat(recovered, ignore_index=False)

    def prefetch_ic_cbbo(
        self,
        requests: list[dict[str, Any]],
        window_min: int = 2,
        retry_attempts: int = 0,
        retry_offset_min: int = 1,
    ) -> dict[Any, _CbboResult | None]:
        """Prefetch OOF CBBO bid/ask/mid/ts_used for IC legs.

        Each request: ``{root, strike, expiration, opt_type, at_time, leg_key}``.
        Returns ``{leg_key: {bid, ask, mid, ts_used} | None}``.
        """
        if not requests:
            return {}

        results: dict[Any, _CbboResult | None] = {}
        legs: list[dict[str, Any]] = []
        for r in requests:
            at_utc = _ensure_utc(r["at_time"])
            sym = self._resolve_oof_raw_symbol(
                r["root"],
                r["strike"],
                r["expiration"],
                r["opt_type"],
                at_date=at_utc,
            )
            legs.append(
                {
                    **r,
                    "at_utc": at_utc,
                    "schema": _cbbo_schema_for(at_utc),
                    "symbol": sym,
                    "start": at_utc - datetime.timedelta(minutes=window_min),
                    "end": at_utc + datetime.timedelta(minutes=window_min),
                }
            )

        misses: list[dict[str, Any]] = []
        n_cache_hit = n_unlisted = n_no_data = n_pretarget = 0
        for leg in legs:
            sym = leg["symbol"]
            if not sym or sym in self._missing_symbols:
                results[leg["leg_key"]] = None
                n_unlisted += 1
                continue

            cached = self._cache.get(
                leg["root"],
                leg["strike"],
                leg["expiration"],
                leg["opt_type"],
                leg["start"],
                leg["end"],
                leg["schema"],
            )

            if cached is None:
                misses.append(leg)
                continue

            if self._is_unlisted_cache(cached):
                self._missing_symbols.add(sym)
                results[leg["leg_key"]] = None
                n_unlisted += 1
                continue

            if cached.empty:
                results[leg["leg_key"]] = None
                n_no_data += 1
                continue

            row = self._extract_cbbo_row(cached, leg["at_utc"])
            if row is None:
                target_ts = pd.Timestamp(leg["at_utc"])
                _cached_idx = pd.DatetimeIndex(cached.index)
                if _cached_idx.tz is not None:
                    target_ts = (
                        target_ts.tz_localize("UTC")
                        if target_ts.tzinfo is None
                        else target_ts.tz_convert(_cached_idx.tz)
                    )
                future_rows = cached[cached.index > target_ts]
                if not future_rows.empty:
                    row = future_rows.iloc[0]
                    n_pretarget += 1
                else:
                    results[leg["leg_key"]] = None
                    n_no_data += 1
                    continue

            bid, ask = float(row["bid"]), float(row["ask"])
            if bid < 0 or ask <= 0:
                results[leg["leg_key"]] = None
                n_no_data += 1
                continue

            n_cache_hit += 1
            results[leg["leg_key"]] = {
                "bid": bid,
                "ask": ask,
                "mid": (bid + ask) / 2.0,
                "ts_used": str(row.name),
            }

        n_to_fetch = len(misses)
        _log.info(
            "OOF CBBO prefetch: %d/%d cache hits (incl. %d pretarget-fwd) | "
            "%d API fetches | skipped: %d unlisted, %d no-data",
            n_cache_hit,
            len(legs),
            n_pretarget,
            n_to_fetch,
            n_unlisted,
            n_no_data,
        )

        if misses:
            groups: dict[tuple[str, str, int], list[dict[str, Any]]] = {}
            for leg in misses:
                if not leg["symbol"]:
                    results[leg["leg_key"]] = None
                    continue
                gk = (
                    leg["schema"],
                    leg["at_utc"].strftime("%Y-%m-%d"),
                    leg["at_utc"].hour,
                )
                groups.setdefault(gk, []).append(leg)

            jobs: list[_PrefetchJob] = []
            for (_schema, _day, _hr), items in groups.items():
                schema = _schema
                min_start = min(it["start"] for it in items)
                max_end = max(it["end"] for it in items)
                uniq = list({it["symbol"] for it in items if it["symbol"]})
                for off in range(0, len(uniq), _MAX_SYM_PER_REQ):
                    chunk_syms = uniq[off : off + _MAX_SYM_PER_REQ]
                    chunk_set = set(chunk_syms)
                    chunk_items = [it for it in items if it["symbol"] in chunk_set]
                    jobs.append((schema, chunk_syms, chunk_items, min_start, max_end))

            def _run(job: _PrefetchJob) -> dict[Any, _CbboResult | None]:
                schema, chunk_syms, chunk_items, min_start, max_end = job
                local: dict[Any, _CbboResult | None] = {}
                df = self._batched_get_range_with_recovery(
                    schema=schema,
                    symbols=chunk_syms,
                    start=min_start,
                    end=max_end,
                    items=[
                        {**it, "rk": it["leg_key"], "sym": it["symbol"]}
                        for it in chunk_items
                    ],
                    results=local,
                )
                if df is None:
                    for it in chunk_items:
                        local.setdefault(it["leg_key"], None)
                    return local

                sym_groups: dict[str, pd.DataFrame] = (
                    {str(k): v for k, v in df.groupby("symbol")}
                    if "symbol" in df.columns
                    else {}
                )
                written: set[str] = set()
                valid: dict[str, pd.DataFrame] = {}
                for it in chunk_items:
                    sym = it["symbol"]
                    if sym not in written:
                        sub = sym_groups.get(sym, pd.DataFrame()) if sym_groups else df
                        if sub is None or sub.empty:
                            self._store_unlisted_cbbo(
                                it["root"],
                                it["strike"],
                                it["expiration"],
                                it["opt_type"],
                                schema,
                                min_start,
                                max_end,
                            )
                        else:
                            cache_df = self._cbbo_df_to_cache_df(sub, symbol=sym)
                            if cache_df.empty:
                                self._store_unlisted_cbbo(
                                    it["root"],
                                    it["strike"],
                                    it["expiration"],
                                    it["opt_type"],
                                    schema,
                                    min_start,
                                    max_end,
                                )
                            else:
                                valid[sym] = cache_df
                                with FuturesDataHandler._cache_write_lock:
                                    self._cache.put(
                                        it["root"],
                                        it["strike"],
                                        it["expiration"],
                                        it["opt_type"],
                                        min_start,
                                        max_end,
                                        schema,
                                        cache_df,
                                        raw_symbol=sym,
                                    )
                        written.add(sym)

                    vdf = valid.get(sym)
                    if vdf is None or vdf.empty:
                        local[it["leg_key"]] = None
                        continue
                    row = self._extract_cbbo_row(vdf, it["at_utc"])
                    if row is None:
                        local[it["leg_key"]] = None
                        continue
                    bid, ask = float(row["bid"]), float(row["ask"])
                    mid = _valid_mid(bid, ask)
                    if mid is None:
                        local[it["leg_key"]] = None
                        continue
                    local[it["leg_key"]] = {
                        "bid": bid,
                        "ask": ask,
                        "mid": mid,
                        "ts_used": str(row.name),
                    }
                return local

            if len(jobs) <= 1:
                for job in jobs:
                    results.update(_run(job))
            else:
                _log.info("OOF CBBO prefetch: %d parallel API calls", len(jobs))
                with ThreadPoolExecutor(max_workers=min(20, len(jobs))) as pool:
                    for fut in as_completed({pool.submit(_run, j): j for j in jobs}):
                        results.update(fut.result())

        if retry_attempts > 0:
            _req_by_key: dict[Any, dict[str, Any]] = {r["leg_key"]: r for r in requests}
            for attempt in range(1, retry_attempts + 1):
                missing_keys = [k for k in _req_by_key if results.get(k) is None]
                if not missing_keys:
                    break
                retry_reqs = [
                    {
                        **_req_by_key[k],
                        "at_time": _req_by_key[k]["at_time"]
                        + datetime.timedelta(minutes=retry_offset_min * attempt),
                    }
                    for k in missing_keys
                ]
                _log.info(
                    "OOF CBBO retry %d/%d: %d missing legs (offset +%d min)",
                    attempt,
                    retry_attempts,
                    len(retry_reqs),
                    retry_offset_min * attempt,
                )
                retry_out = self.prefetch_ic_cbbo(retry_reqs, window_min=window_min)
                results.update({k: v for k, v in retry_out.items() if v is not None})

        n_found = sum(1 for v in results.values() if v is not None)
        self._last_cbbo_prefetch_stats = {
            "total_legs": len(legs),
            "cache_hits": n_cache_hit,
            "pretarget_forwarded": n_pretarget,
            "api_fetches": n_to_fetch,
            "skipped_unlisted": n_unlisted,
            "skipped_no_data": n_no_data,
            "legs_with_quote": n_found,
            "cache_hit_rate_pct": round(n_cache_hit / max(len(legs), 1) * 100, 1),
        }
        return results

    def fetch_futures_option_quotes_batch_cbbo(
        self, requests: list[dict[str, Any]], window_min: int = 15
    ) -> dict[_LegKey, _CbboResult | None]:
        """Batch OOF CBBO quotes returning full bid/ask/mid/ts_used per leg."""
        if not requests:
            return {}

        leg_requests = [
            {
                **r,
                "leg_key": (
                    r["root"],
                    r["strike"],
                    r["expiration"],
                    r["opt_type"],
                ),
            }
            for r in requests
        ]
        raw = self.prefetch_ic_cbbo(leg_requests, window_min=window_min)
        return {k: v for k, v in raw.items() if isinstance(k, tuple)}

    def fetch_cbbo_range(
        self,
        root: str,
        strike: float,
        expiration: str,
        opt_type: str,
        start_dt: datetime.datetime,
        end_dt: datetime.datetime,
    ) -> pd.DataFrame:
        """Return OOF ``cbbo-1m`` time-series with columns ``[bid, ask, mid]``."""
        symbol = self._resolve_oof_raw_symbol(
            root, strike, expiration, opt_type, at_date=start_dt
        )
        if not symbol or symbol in self._missing_symbols:
            return pd.DataFrame()

        schema = "bbo-1m"
        start_utc = _ensure_utc(
            start_dt
            if isinstance(start_dt, datetime.datetime)
            else start_dt.to_pydatetime()
        )
        end_utc = _ensure_utc(
            end_dt if isinstance(end_dt, datetime.datetime) else end_dt.to_pydatetime()
        )

        superset = self._cache.get_superset(
            root, strike, expiration, opt_type, start_utc, end_utc, schema
        )
        if superset is not None and not superset.empty:
            if self._is_unlisted_cache(superset):
                self._missing_symbols.add(symbol)
                return pd.DataFrame()
            s_ts = pd.Timestamp(start_utc)
            e_ts = pd.Timestamp(end_utc)
            dti = superset.index
            if isinstance(dti, pd.DatetimeIndex) and dti.tz is not None:
                s_ts = (
                    s_ts.tz_localize(dti.tz)
                    if s_ts.tzinfo is None
                    else s_ts.tz_convert(dti.tz)
                )
                e_ts = (
                    e_ts.tz_localize(dti.tz)
                    if e_ts.tzinfo is None
                    else e_ts.tz_convert(dti.tz)
                )
            sliced = superset.loc[(superset.index >= s_ts) & (superset.index <= e_ts)]
            with FuturesDataHandler._cache_write_lock:
                self._cache.put(
                    root,
                    strike,
                    expiration,
                    opt_type,
                    start_utc,
                    end_utc,
                    schema,
                    sliced,
                    raw_symbol=symbol,
                )
            return sliced

        cached = self._cache.get(
            root, strike, expiration, opt_type, start_utc, end_utc, schema
        )
        if cached is not None:
            if self._is_unlisted_cache(cached):
                self._missing_symbols.add(symbol)
                return pd.DataFrame()
            return cached

        try:
            with FuturesDataHandler._net_sema, warnings.catch_warnings():
                warnings.simplefilter("ignore")
                store = self.databento_client.timeseries.get_range(
                    dataset=_DATASET,
                    symbols=symbol,
                    schema=schema,
                    start=start_utc,
                    end=end_utc,
                    stype_in="raw_symbol",
                )
            raw_df = store.to_df(price_type="float")
        except Exception as exc:
            if _is_symbology_error(exc):
                self._missing_symbols.add(symbol)
                self._store_unlisted_cbbo(
                    root, strike, expiration, opt_type, schema, start_utc, end_utc
                )
            _log.warning("OOF CBBO range fetch failed for %s: %s", symbol, exc)
            return pd.DataFrame()

        if raw_df.empty:
            self._store_unlisted_cbbo(
                root, strike, expiration, opt_type, schema, start_utc, end_utc
            )
            return pd.DataFrame()

        out = self._cbbo_df_to_cache_df(raw_df, symbol)
        if not out.empty:
            with FuturesDataHandler._cache_write_lock:
                self._cache.put(
                    root,
                    strike,
                    expiration,
                    opt_type,
                    start_utc,
                    end_utc,
                    schema,
                    out,
                    raw_symbol=symbol,
                )
        else:
            self._store_unlisted_cbbo(
                root, strike, expiration, opt_type, schema, start_utc, end_utc
            )
        return out

    def fetch_cbbo_range_batch(
        self,
        requests: list[dict[str, Any]],
        *,
        max_workers: int = 20,
    ) -> dict[tuple, pd.DataFrame]:
        """Batch-fetch OOF ``cbbo-1m`` time-series for multiple contracts.

        Each request: ``{root, expiration, strike, opt_type, start_dt, end_dt}``.
        Returns ``{(root, expiration, strike, opt_type): DataFrame}``.
        """
        if not requests:
            return {}

        merged: dict[tuple, dict[str, Any]] = {}
        for r in requests:
            key = (r["root"], r["expiration"], r["strike"], r["opt_type"])
            if key not in merged:
                merged[key] = dict(r)
            else:
                merged[key]["start_dt"] = min(merged[key]["start_dt"], r["start_dt"])
                merged[key]["end_dt"] = max(merged[key]["end_dt"], r["end_dt"])

        unique_reqs = list(merged.values())

        def _fetch(req: dict[str, Any]) -> tuple[tuple, pd.DataFrame | None]:
            key = (req["root"], req["expiration"], req["strike"], req["opt_type"])
            try:
                df = self.fetch_cbbo_range(
                    root=req["root"],
                    expiration=req["expiration"],
                    strike=req["strike"],
                    opt_type=req["opt_type"],
                    start_dt=req["start_dt"],
                    end_dt=req["end_dt"],
                )
                return key, df
            except Exception as exc:
                _log.warning("fetch_cbbo_range_batch: failed for %s: %s", key, exc)
                return key, None

        with ThreadPoolExecutor(max_workers=max_workers) as executor:
            results = list(executor.map(_fetch, unique_reqs))

        cache: dict[tuple, pd.DataFrame] = {
            key: df for key, df in results if df is not None and not df.empty
        }
        _log.info(
            "fetch_cbbo_range_batch: loaded %d/%d OOF contracts",
            len(cache),
            len(unique_reqs),
        )
        return cache

    # ── OOF expirations ───────────────────────────────────────────────
    def prefetch_futures_expirations(
        self, root: str, lookup_dates: list[str]
    ) -> dict[str, str | None]:
        return self.resolve_futures_expirations(root, lookup_dates, force_refresh=False)

    def refetch_futures_expirations(
        self, root: str, dates: list[str]
    ) -> dict[str, str | None]:
        return self.resolve_futures_expirations(root, dates, force_refresh=True)

    def resolve_futures_expirations(
        self,
        root: str,
        lookup_dates: list[str],
        *,
        force_refresh: bool = False,
        max_gap_days: int = 90,
    ) -> dict[str, str | None]:
        if not lookup_dates:
            return {}

        root_u = root.upper()
        lookup_dates = sorted(set(lookup_dates))
        out: dict[str, str | None] = {}

        if not force_refresh:
            for d in lookup_dates:
                key = (root_u, d)
                if key in self._expiration_lookup_cache:
                    out[d] = self._expiration_lookup_cache[key]

        cached = self._cache.get_expirations(root)
        unresolved: list[str] = []

        for d in lookup_dates:
            if d in out:
                continue
            idx = bisect.bisect_left(cached, d)
            if force_refresh:
                unresolved.append(d)
                continue
            if idx < len(cached):
                gap = (
                    datetime.datetime.strptime(cached[idx], "%Y-%m-%d")
                    - datetime.datetime.strptime(d, "%Y-%m-%d")
                ).days
                if max_gap_days == 0:
                    if cached[idx] == d:
                        out[d] = cached[idx]
                    else:
                        unresolved.append(d)
                elif gap <= max_gap_days:
                    out[d] = cached[idx]
                else:
                    unresolved.append(d)
            else:
                if self._cache.is_range_covered(root, d, d):
                    out[d] = None
                else:
                    unresolved.append(d)

        if unresolved:
            direct = self._batch_probe_futures_expirations(root, unresolved)
            found_direct = {exp for exp in direct.values() if exp is not None}
            if found_direct:
                self._cache.save_expirations(root, sorted(found_direct))
            out.update(direct)
            for d in direct:
                self._cache.record_expiration_query(root, d, d)

            remaining = [d for d in unresolved if out.get(d) is None]
            if remaining:
                if (
                    len(unresolved) >= _EXPIRATION_DIRECT_PROBE_THRESHOLD
                    and len(remaining) > _EXPIRATION_HEAVY_FALLBACK_MAX_REMAINING
                ):
                    _log.warning(
                        "Skipping heavy OOF expiration fallback for %d dates",
                        len(remaining),
                    )
                else:
                    recovered: set[str] = set()
                    for d in remaining:
                        day_start, day_end = _definition_snapshot_window(d, span_days=1)
                        for cap in _EXPIRATION_DEF_LIMIT_STEPS:
                            exps = self._fetch_futures_expirations_databento(
                                root,
                                day_start,
                                day_end,
                                limit=cap,
                                lookup=d,
                            )
                            if not exps:
                                continue
                            recovered.update(exps)
                            cand = next((exp for exp in exps if exp >= d), None)
                            if cand is not None:
                                out[d] = cand
                                break
                    if recovered:
                        self._cache.save_expirations(root, sorted(recovered))

        cached = self._cache.get_expirations(root)
        for d in lookup_dates:
            if d in out and out[d] is not None:
                continue
            idx = bisect.bisect_left(cached, d)
            if idx < len(cached):
                if max_gap_days == 0:
                    if cached[idx] == d:
                        out[d] = cached[idx]
                    else:
                        out.setdefault(d, None)
                else:
                    out[d] = cached[idx]
            else:
                out.setdefault(d, None)

        for d, exp in out.items():
            self._expiration_lookup_cache[(root_u, d)] = exp
        return out

    def _batch_probe_futures_expirations(
        self, root: str, lookup_dates: list[str]
    ) -> dict[str, str | None]:
        if not lookup_dates:
            return {}

        def _probe(date_str: str) -> tuple[str, str | None]:
            start, end = _definition_snapshot_window(
                date_str, span_days=_PROBE_WINDOW_DAYS
            )
            exp = self._probe_single_futures_expiration(
                root, start, end, exact_day=date_str
            )
            if exp is not None and exp < date_str:
                return date_str, None
            return date_str, exp

        if len(lookup_dates) == 1:
            d, exp = _probe(lookup_dates[0])
            return {d: exp}

        workers = min(20, len(lookup_dates))
        with ThreadPoolExecutor(max_workers=workers) as pool:
            return dict(pool.map(_probe, lookup_dates))

    def _parent_for_lookup(self, root: str, lookup: str) -> str | None:
        try:
            exp = datetime.datetime.strptime(lookup, "%Y-%m-%d").date()
            return parent_symbol_for_expiration(root, exp)
        except ValueError:
            return None

    def _fetch_futures_expirations_databento(
        self,
        root: str,
        start_dt: datetime.datetime,
        end_dt: datetime.datetime,
        *,
        limit: int | None = None,
        lookup: str | None = None,
    ) -> list[str]:
        """Expiration snapshot on the single weekly parent expected for *lookup*."""
        lookup_s = lookup or _ensure_utc(start_dt).strftime("%Y-%m-%d")
        parent = self._parent_for_lookup(root, lookup_s)
        if parent is None:
            return []
        return self._fetch_expirations_for_parent(parent, start_dt, end_dt, limit=limit)

    def _probe_single_futures_expiration(
        self,
        root: str,
        start_dt: datetime.datetime,
        end_dt: datetime.datetime,
        *,
        exact_day: str | None = None,
    ) -> str | None:
        lookup = exact_day or _ensure_utc(start_dt).strftime("%Y-%m-%d")
        parent = self._parent_for_lookup(root, lookup)
        if parent is None:
            return None

        limits = (
            (self.EXPIRATION_PROBE_MAX_ROWS,)
            if exact_day is None
            else _EXPIRATION_DEF_LIMIT_STEPS
        )
        for cap in limits:
            exps = self._fetch_expirations_for_parent(
                parent, start_dt, end_dt, limit=cap
            )
            if not exps:
                continue
            if exact_day is not None:
                return lookup if lookup in exps else None
            cand = next((exp for exp in exps if exp >= lookup), None)
            if cand is not None:
                return cand
        return None

    def _build_zero_dte_expiration_cache(
        self,
        root: str,
        trading_days: list,
    ) -> dict[str, str]:
        """Map each entry day to itself when a same-day weekly OOF exists.

        Uses ``resolve_futures_expirations`` (local ``oof_expirations`` cache
        first) so backtests keep working when Databento budget is exhausted.
        """
        if not trading_days:
            return {}

        day_strs = sorted({pd.Timestamp(d).strftime("%Y-%m-%d") for d in trading_days})
        resolved = self.resolve_futures_expirations(
            root, day_strs, force_refresh=False, max_gap_days=0
        )
        cache = {d: d for d, exp in resolved.items() if exp == d}
        missing = [d for d in day_strs if d not in cache]
        if missing:
            _log.warning(
                "No listed OOF expiration for %d entry days (skipped): %s",
                len(missing),
                missing[:10],
            )
        return cache

    def build_futures_expiration_cache(
        self,
        root: str,
        trading_days: list,
        min_dte: int,
        max_dte: int,
        repair_max_days: int = 30,
    ) -> dict[str, str]:
        if min_dte == 0 and max_dte == 0:
            return self._build_zero_dte_expiration_cache(root, trading_days)

        expiration_cache: dict[str, str] = {}
        missing_days: list[str] = []
        no_exp_days: list[str] = []
        too_far_days: list[str] = []
        try:
            day_to_lookup: dict[str, str] = {}
            lookup_dates: list[str] = []
            for day in trading_days:
                if min_dte > 0:
                    target_min = (pd.Timestamp(day) + pd.offsets.BDay(min_dte)).date()
                else:
                    target_min = day
                lookup_str = target_min.strftime("%Y-%m-%d")
                day_str = day.strftime("%Y-%m-%d")
                day_to_lookup[day_str] = lookup_str
                lookup_dates.append(lookup_str)

            _max_gap = 0 if max_dte <= 1 else 90
            prefetched = self.resolve_futures_expirations(
                root, lookup_dates, force_refresh=False, max_gap_days=_max_gap
            )

            for day in trading_days:
                day_str = day.strftime("%Y-%m-%d")
                exp = prefetched.get(day_to_lookup[day_str])
                if exp:
                    exp_date = datetime.datetime.strptime(exp, "%Y-%m-%d").date()
                    if max_dte > 0:
                        max_exp_allowed = (
                            pd.Timestamp(day) + pd.offsets.BDay(max_dte)
                        ).date()
                    else:
                        max_exp_allowed = day
                    if exp_date <= max_exp_allowed:
                        expiration_cache[day_str] = exp
                    else:
                        missing_days.append(day_str)
                        too_far_days.append(day_str)
                else:
                    missing_days.append(day_str)
                    no_exp_days.append(day_str)

            if max_dte <= 1:
                repair_candidates = list(set(no_exp_days) | set(too_far_days))
            else:
                repair_candidates = list(no_exp_days)
            if (
                repair_max_days > 0
                and len(repair_candidates) > repair_max_days
                and max_dte > 1
            ):
                repair_candidates = sorted(repair_candidates)[-repair_max_days:]

            if repair_candidates and max_dte <= 1 and repair_max_days != 0:
                _repair_lookup_to_entry = {
                    day_to_lookup[d]: d for d in repair_candidates
                }
                repaired = self.resolve_futures_expirations(
                    root,
                    list(_repair_lookup_to_entry.keys()),
                    force_refresh=True,
                    max_gap_days=0,
                )
                for lookup_str, exp in repaired.items():
                    day_str = _repair_lookup_to_entry.get(lookup_str)
                    if day_str and exp:
                        exp_date = datetime.datetime.strptime(exp, "%Y-%m-%d").date()
                        trade_date = datetime.datetime.strptime(
                            day_str, "%Y-%m-%d"
                        ).date()
                        _max_exp_repair = (
                            (pd.Timestamp(trade_date) + pd.offsets.BDay(max_dte)).date()
                            if max_dte > 0
                            else trade_date
                        )
                        if exp_date <= _max_exp_repair:
                            expiration_cache[day_str] = exp
                still_missing = [d for d in missing_days if d not in expiration_cache]
                missing_days = still_missing if still_missing else []

            # Do NOT calendar-fallback to lookup_str for OOF — unlike equity
            # 0-DTE, CME options only exist on listed expiration dates.
            # Assigning entry_day as expiration causes symbol-resolution churn.

        except Exception as e:
            _log.warning("Failed to build futures expiration cache: %s", e)

        if missing_days:
            _log.warning(
                "No listed OOF expiration for %d entry days (skipped): %s",
                len(missing_days),
                missing_days[:10],
            )
        return expiration_cache

    def get_cache_stats(self) -> CacheStats:
        return self._cache.get_cache_stats()


def _to_utc_ts(dt: datetime.datetime | pd.Timestamp) -> pd.Timestamp:
    ts = pd.Timestamp(dt)
    return ts.tz_localize("UTC") if ts.tzinfo is None else ts.tz_convert("UTC")
