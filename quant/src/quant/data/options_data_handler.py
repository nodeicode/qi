"""Options market-data handler (Databento-only).

Responsibilities
----------------
- Fetch option OHLCV from Databento (``ohlcv-1m``/``-1h``/``-1d``,
  resampling locally for 5/15/30-minute bars).
- Fetch consolidated BBO rows for historical ranges and causal IC-leg fills.
- Resolve option expirations from a local listing calendar.
- Persist everything in :class:`OptionDataCache` (parquet + SQLite).

Design
------
- One Databento ``Historical`` client; a single ``threading.Semaphore`` caps
  concurrent HTTP calls; one ``RLock`` serialises cache writes.
- Symbol format: ``"{TICKER:<6}{YY}{MM}{DD}{C|P}{strike*1000:08d}"``.
- A successful request with no usable two-sided quote is recorded as an empty
  covered cache range. Provider failures are never cached as illiquidity.
- CBBO requests are grouped by their exact causal window, so legs at different
  fill times never widen one another's provider request.
"""

from __future__ import annotations

import datetime
import logging
import math
import os
import threading
import warnings
import zoneinfo
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Any, TypedDict, cast

import databento as db
import numpy as np
import pandas as pd
from dotenv import load_dotenv

from quant.utils.bs_greeks import greeks as _bs_greeks
from quant.utils.bs_iv import iv_from_price

from ._times import assert_canonical_index, to_naive_index, to_naive_utc
from .cache.options import OptionDataCache
from .options_calendar import scheduled_expiration_on_or_after

_log = logging.getLogger(__name__)

_CBBO_1S_START = datetime.datetime(2025, 2, 20, tzinfo=datetime.UTC)
_DATASET = "OPRA.PILLAR"
_MAX_SYM_PER_REQ = 2000  # Databento batch ceiling
_DEFAULT_NET_CONCURRENCY = 50


# Load .env from project root if present.
_dotenv_path = Path(__file__).resolve().parents[1] / ".env"
if _dotenv_path.exists():
    load_dotenv(dotenv_path=_dotenv_path)


# ── Type aliases / TypedDicts ─────────────────────────────────────────
# (ticker, strike, expiration, opt_type, interval) — cache grouping key.
_GroupKey = tuple[str, float, str, str, str]
# Per-job tuple for prefetch_ic_cbbo parallel execution.
_PrefetchJob = tuple[
    str,  # schema
    list[str],  # chunk_syms
    list[dict[str, Any]],  # chunk_items
    datetime.datetime,  # min_start
    datetime.datetime,  # max_end
]


class _CbboResult(TypedDict, total=False):
    """Hydrated CBBO quote for a single leg (bid/ask/mid + timestamp used).

    Quote is the newest two-sided row at or before ``at_time``.
    """

    bid: float
    ask: float
    mid: float
    ts_used: str
    quote_age_seconds: float
    source: str


def _valid_mid(bid: float | None, ask: float | None) -> float | None:
    """Return ``(bid + ask) / 2`` when both are finite, ``bid >= 0``, ``ask > 0``."""
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


def _cbbo_schema_for(at_utc: datetime.datetime) -> str:
    """``cbbo-1s`` once Databento has it (``>= 2025-02-20``), else ``cbbo-1m``."""
    return "cbbo-1s" if _ensure_utc(at_utc) >= _CBBO_1S_START else "cbbo-1m"


def _cbbo_causal_range(
    at_utc: datetime.datetime, max_age_seconds: int
) -> tuple[datetime.datetime, datetime.datetime]:
    """Causal provider range: one bounded lookback interval through fill time."""
    at = _ensure_utc(at_utc)
    return (
        at - datetime.timedelta(seconds=max(int(max_age_seconds), 0)),
        at + datetime.timedelta(milliseconds=1),
    )


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


def _is_budget_exhausted_error(exc: BaseException) -> bool:
    """Whether Databento rejected a request because the account has no budget."""
    message = str(exc).lower()
    return (
        "account_insufficient_funds" in message
        or "insufficient budget" in message
        or "sufficient budget to create the request" in message
    )


class OptionDataHandler:
    """High-throughput options data + cache front-end backed by Databento."""

    # Class-level locks/semaphores (shared across instances by design).
    _net_sema = threading.Semaphore(_DEFAULT_NET_CONCURRENCY)
    _cache_write_lock = threading.RLock()

    def __init__(
        self,
        cache_dir: str | None = None,
        verbose: bool = False,
        **legacy_kwargs: Any,  # silently ignore old kwargs (e.g. provider="databento")
    ):
        del legacy_kwargs
        if cache_dir is None:
            cache_dir = str(Path(__file__).resolve().parent / "option_cache")
        self._cache = OptionDataCache(cache_dir=cache_dir, verbose=verbose)
        self._databento_client: db.Historical | None = None
        api_key = os.getenv("DATABENTO_API_KEY")
        if not api_key:
            _log.warning(
                "DATABENTO_API_KEY is missing; option historical client was not initialized."
            )
        else:
            try:
                self._databento_client = db.Historical(key=api_key)
            except Exception as e:
                _log.warning(
                    "Failed to initialize Databento option historical client: %s", e
                )

        self._expiration_lookup_cache: dict[tuple[str, str], str | None] = {}
        # A 402 must halt the parallel CBBO queue. Without this shared event,
        # already-submitted workers keep retrying requests that cannot succeed.
        self._cbbo_budget_exhausted = threading.Event()
        self._cbbo_budget_lock = threading.Lock()
        # Populated by prefetch_ic_cbbo; readable from notebooks/backtester.
        self._last_cbbo_prefetch_stats: dict = {}

    @property
    def databento_client(self) -> db.Historical:
        """Return the initialized client or fail within the caller's error boundary."""
        if self._databento_client is None:
            raise RuntimeError("Databento option historical client is unavailable")
        return self._databento_client

    # ── Symbol / schema helpers ────────────────────────────────────────
    @staticmethod
    def _format_databento_symbol(
        ticker: str, strike: float, expiration: str, opt_type: str
    ) -> str:
        """Return a Databento OPRA raw symbol (6-char padded ticker).

        Single source of truth for the backtest's OPRA raw contract key.
        ``expiration`` may be an ISO date string, a ``date``/``datetime``, or
        ``YYYYMMDD``; it is normalised here so callers never format a symbol
        themselves.
        """
        cp = "C" if opt_type.lower() in ("c", "call") else "P"
        exp = str(expiration).replace("-", "")[:8]  # YYYYMMDD
        return (
            f"{ticker.upper():<6}"
            f"{exp[2:4]}{exp[4:6]}{exp[6:8]}"
            f"{cp}{round(strike * 1000):08d}"
        )

    @staticmethod
    def _interval_to_schema(interval: str) -> str:
        """Map an interval string to a Databento OHLCV schema (resampled locally for 5/15/30m)."""
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

    # ── DataFrame post-processing ─────────────────────────────────────
    @staticmethod
    def _resample_ohlcv(df: pd.DataFrame, minutes: int) -> pd.DataFrame:
        """Aggregate 1-minute bars to ``minutes``-minute bars and re-derive synthetic bid/ask/mid."""
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
        """Normalise an OHLCV response: rename ``ts_event`` → ``timestamp`` index,
        ensure required columns, synthesise bid/ask/mid, optionally resample.

        Index is canonical tz-naive UTC (naive assumed UTC) at the
        engine-facing boundary.
        """
        if df.empty:
            return pd.DataFrame()
        df = df.rename(columns={"ts_event": "timestamp"})
        if "timestamp" in df.columns:
            df = df.set_index("timestamp")
        df = df.sort_index()
        df = to_naive_index(df)  # engine-facing OHLCV is canonical naive-UTC
        for col in ("open", "high", "low", "close", "volume"):
            if col not in df.columns:
                df[col] = 0.0
        df["mid"] = (df["high"] + df["low"]) / 2.0
        spread = df["mid"] * 0.05
        df["bid"] = df["mid"] - spread / 2.0
        df["ask"] = df["mid"] + spread / 2.0
        if OptionDataHandler._needs_resample(interval):
            df = OptionDataHandler._resample_ohlcv(
                df, OptionDataHandler._resample_minutes(interval)
            )
        return df

    # ── CBBO helpers ──────────────────────────────────────────────────
    @staticmethod
    def _cbbo_df_to_cache_df(
        df: pd.DataFrame, symbol: str | None = None
    ) -> pd.DataFrame:
        """Normalize provider CBBO rows to the handler's two-sided contract."""
        if df.empty:
            return pd.DataFrame()
        if symbol is not None and "symbol" in df.columns:
            df = df[df["symbol"] == symbol]
        if df.empty or "bid_px_00" not in df.columns or "ask_px_00" not in df.columns:
            return pd.DataFrame()

        bid = pd.to_numeric(df["bid_px_00"], errors="coerce")
        ask = pd.to_numeric(df["ask_px_00"], errors="coerce")
        bid_valid = bid.notna() & np.isfinite(bid) & (bid >= 0)
        ask_valid = ask.notna() & np.isfinite(ask) & (ask > 0)
        mask = bid_valid & ask_valid
        out = pd.DataFrame(
            {
                "bid": bid.where(bid_valid)[mask],
                "ask": ask.where(ask_valid)[mask],
            }
        )
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
    def _usable_cached_cbbo_rows(df: pd.DataFrame) -> pd.DataFrame:
        """Apply the frozen two-sided contract to pre-existing cache rows."""
        if df.empty or not {"bid", "ask"}.issubset(df.columns):
            return pd.DataFrame()
        bid = pd.to_numeric(df["bid"], errors="coerce")
        ask = pd.to_numeric(df["ask"], errors="coerce")
        mask = (
            bid.notna()
            & np.isfinite(bid)
            & (bid >= 0)
            & ask.notna()
            & np.isfinite(ask)
            & (ask > 0)
        )
        if not mask.any():
            return pd.DataFrame()
        out = pd.DataFrame({"bid": bid[mask], "ask": ask[mask]}, index=df.index[mask])
        out["mid"] = (out["bid"] + out["ask"]) / 2.0
        return out

    @staticmethod
    def _extract_cbbo_mid_from_cache(
        cache_df: pd.DataFrame, at_utc: datetime.datetime
    ) -> float | None:
        """Causal two-sided CBBO mid from cache; ``None`` if invalid or absent."""
        row = OptionDataHandler._extract_cbbo_row(cache_df, at_utc)
        if row is None:
            return None
        return _valid_mid(row["bid"], row["ask"])

    @staticmethod
    def _extract_cbbo_row(
        cache_df: pd.DataFrame | None, at_utc: datetime.datetime
    ) -> pd.Series | None:
        """Return the closest at-or-before row + its timestamp from a CBBO frame."""
        if cache_df is None or cache_df.empty:
            return None
        if not isinstance(cache_df.index, pd.DatetimeIndex):
            return None
        idx: pd.DatetimeIndex = cache_df.index
        target_ns = to_naive_utc(at_utc).value
        try:
            pos = int(np.searchsorted(idx.asi8, target_ns, side="right")) - 1
            if pos < 0:
                return None
            best = idx[pos]
            row = cache_df.loc[best].copy()
            row.name = best
            return row
        except Exception:
            return None

    def _store_empty_cbbo(
        self,
        ticker: str,
        strike: float,
        expiration: str,
        opt_type: str,
        schema: str,
        start_dt: datetime.datetime,
        end_dt: datetime.datetime,
    ) -> None:
        """Record a successful provider response with no usable quote rows."""
        with OptionDataHandler._cache_write_lock:
            self._cache.put(
                ticker,
                strike,
                expiration,
                opt_type,
                _ensure_utc(start_dt),
                _ensure_utc(end_dt),
                schema,
                pd.DataFrame(),
            )

    # ── OHLCV: single fetch with cache ────────────────────────────────
    def fetch_option_data(
        self,
        ticker: str,
        strike: float,
        expiration: str,
        opt_type: str,
        start_dt: datetime.datetime,
        end_dt: datetime.datetime,
        interval: str = "1m",
    ) -> pd.DataFrame:
        """Return option OHLCV bars, hitting cache when possible, fetching from Databento otherwise."""
        # 1) superset cache: cached window contains the request fully.
        superset = self._cache.get_superset(
            ticker, strike, expiration, opt_type, start_dt, end_dt, interval
        )
        if superset is not None and not superset.empty:
            if not isinstance(superset.index, pd.DatetimeIndex):
                return superset
            dti: pd.DatetimeIndex = superset.index
            s_ts = pd.Timestamp(start_dt)
            e_ts = pd.Timestamp(end_dt)
            if dti.tz is not None:
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
            mask = (superset.index >= s_ts) & (superset.index <= e_ts)
            sliced = superset.loc[mask]
            with OptionDataHandler._cache_write_lock:
                self._cache.put(
                    ticker,
                    strike,
                    expiration,
                    opt_type,
                    start_dt,
                    end_dt,
                    interval,
                    sliced,
                )
            sliced = to_naive_index(sliced)
            assert_canonical_index(sliced, allow_aware_daily=False)
            return sliced

        # 2) exact cache row.
        cached = self._cache.get(
            ticker, strike, expiration, opt_type, start_dt, end_dt, interval
        )
        if cached is not None:
            cached = to_naive_index(cached)
            assert_canonical_index(cached, allow_aware_daily=False)
            return cached

        # 3) miss → API.
        df = self._fetch_from_databento(
            ticker, strike, expiration, opt_type, start_dt, end_dt, interval
        )
        with OptionDataHandler._cache_write_lock:
            self._cache.put(
                ticker, strike, expiration, opt_type, start_dt, end_dt, interval, df
            )
        return df

    def _fetch_from_databento(
        self,
        ticker: str,
        strike: float,
        expiration: str,
        opt_type: str,
        start_dt: datetime.datetime,
        end_dt: datetime.datetime,
        interval: str,
    ) -> pd.DataFrame:
        symbol = self._format_databento_symbol(ticker, strike, expiration, opt_type)
        schema = self._interval_to_schema(interval)
        try:
            with OptionDataHandler._net_sema, warnings.catch_warnings():
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
                "[Databento] OHLCV fetch failed for %s: %s", symbol, exc, exc_info=True
            )
            return pd.DataFrame()

    # ── OHLCV: batch preload ──────────────────────────────────────────
    def preload_requests(self, requests: list[dict[str, Any]]) -> dict[str, int]:
        """Group requests by contract+interval, pack into Databento batches.

        Each request: ``{ticker, strike, expiration, opt_type, start_dt, end_dt, interval}``.
        Returns ``{"http_fetches": n_api_calls, "filled_windows": n_request_windows}``.
        """
        if not requests:
            return {"http_fetches": 0, "filled_windows": 0}

        grouped: dict[_GroupKey, dict[str, Any]] = {}
        for r in requests:
            gk = (
                r["ticker"],
                r["strike"],
                r["expiration"],
                r["opt_type"],
                r["interval"],
            )
            if gk not in grouped:
                grouped[gk] = {
                    "min_start": r["start_dt"],
                    "max_end": r["end_dt"],
                    "members": [r],
                }
            else:
                g = grouped[gk]
                g["min_start"] = min(g["min_start"], r["start_dt"])
                g["max_end"] = max(g["max_end"], r["end_dt"])
                g["members"].append(r)

        return self._preload_databento_batch(grouped)

    def _preload_databento_batch(
        self, grouped: dict[_GroupKey, dict[str, Any]]
    ) -> dict[str, int]:
        api_calls, filled = 0, 0

        # Phase 1: drain superset cache hits.
        to_fetch: dict[_GroupKey, tuple[str, dict[str, Any]]] = {}
        for gk, info in grouped.items():
            t, s, e, o, interval = gk
            superset = self._cache.get_superset(
                t, s, e, o, info["min_start"], info["max_end"], interval
            )
            if superset is not None and not superset.empty:
                with OptionDataHandler._cache_write_lock:
                    self._cache.put(
                        t,
                        s,
                        e,
                        o,
                        info["min_start"],
                        info["max_end"],
                        interval,
                        superset,
                    )
                filled += len(info["members"])
            else:
                to_fetch[gk] = (
                    self._format_databento_symbol(t, s, e, o),
                    info,
                )

        if not to_fetch:
            return {"http_fetches": 0, "filled_windows": filled}

        # Phase 2: bucket misses by schema → one or more batched calls.
        by_schema: dict[str, list[tuple[_GroupKey, str, dict[str, Any]]]] = {}
        for gk, (sym, info) in to_fetch.items():
            schema = self._interval_to_schema(gk[4])
            by_schema.setdefault(schema, []).append((gk, sym, info))

        for schema, entries in by_schema.items():
            min_start = min(e[2]["min_start"] for e in entries)
            max_end = max(e[2]["max_end"] for e in entries)

            for chunk_off in range(0, len(entries), _MAX_SYM_PER_REQ):
                chunk = entries[chunk_off : chunk_off + _MAX_SYM_PER_REQ]
                symbols = [c[1] for c in chunk]
                try:
                    with OptionDataHandler._net_sema, warnings.catch_warnings():
                        warnings.simplefilter("ignore")
                        store = self.databento_client.timeseries.get_range(
                            dataset=_DATASET,
                            symbols=symbols,
                            schema=schema,
                            start=min_start,
                            end=max_end,
                            stype_in="raw_symbol",
                        )
                    df_all = store.to_df(price_type="float")
                    api_calls += 1
                except Exception as exc:
                    _log.error(
                        "[Databento] batch preload failed (%d symbols): %s",
                        len(symbols),
                        exc,
                    )
                    # Fallback: one call per symbol.
                    for gk, _sym, info in chunk:
                        t, s, e, o, interval = gk
                        try:
                            df = self._fetch_from_databento(
                                t, s, e, o, info["min_start"], info["max_end"], interval
                            )
                            api_calls += 1
                            if df is not None and not df.empty:
                                with OptionDataHandler._cache_write_lock:
                                    self._cache.put(
                                        t,
                                        s,
                                        e,
                                        o,
                                        info["min_start"],
                                        info["max_end"],
                                        interval,
                                        df,
                                    )
                        except Exception:
                            _log.debug(
                                "Individual fallback failed for %s", gk, exc_info=True
                            )
                        filled += len(info["members"])
                    continue

                if df_all.empty:
                    filled += sum(len(info["members"]) for _, _, info in chunk)
                    continue

                groups_df: dict[str, pd.DataFrame] = (
                    {str(k): v for k, v in df_all.groupby("symbol")}
                    if "symbol" in df_all.columns
                    else {}
                )
                for gk, sym, info in chunk:
                    t, s, e, o, interval = gk
                    sym_df = (
                        groups_df.get(sym, pd.DataFrame()).copy()
                        if groups_df
                        else df_all.copy()
                    )
                    processed = self._process_databento_df(sym_df, interval)
                    if processed is not None and not processed.empty:
                        with OptionDataHandler._cache_write_lock:
                            self._cache.put(
                                t,
                                s,
                                e,
                                o,
                                info["min_start"],
                                info["max_end"],
                                interval,
                                processed,
                            )
                    filled += len(info["members"])

        return {"http_fetches": api_calls, "filled_windows": filled}

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
        """Run a single batched ``get_range`` and recover from symbology errors.

        On a symbology failure the batch is retried per-symbol and the
        *successful* per-symbol DataFrames are merged into the returned frame.
        Failed symbols deliberately remain uncovered so a transient provider
        failure can be retried on a later run.
        """
        if self._cbbo_budget_exhausted.is_set():
            for m in items:
                results.setdefault(m["rk"], None)
            return None
        try:
            with OptionDataHandler._net_sema, warnings.catch_warnings():
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
            if _is_budget_exhausted_error(exc):
                with self._cbbo_budget_lock:
                    first_budget_error = not self._cbbo_budget_exhausted.is_set()
                    self._cbbo_budget_exhausted.set()
                if first_budget_error:
                    _log.error(
                        "Databento CBBO budget exhausted; cancelling pending CBBO "
                        "requests for this backtest. Subsequent requests will "
                        "return missing quotes without hitting the provider."
                    )
                for m in items:
                    results.setdefault(m["rk"], None)
                return None
            if not _is_symbology_error(exc):
                _log.warning("CBBO batch failed (%d syms): %s", len(symbols), exc)
                for m in items:
                    results.setdefault(m["rk"], None)
                return None

            _log.info(
                "CBBO symbology error in batch (%d syms) — retrying per-symbol",
                len(symbols),
            )
            sym_to_items: dict[str, list[dict]] = {}
            for m in items:
                sym_to_items.setdefault(m["sym"], []).append(m)

            recovered: list[pd.DataFrame] = []
            for sym in symbols:
                try:
                    with OptionDataHandler._net_sema, warnings.catch_warnings():
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
                            self._store_empty_cbbo(
                                m["ticker"],
                                m["strike"],
                                m["expiration"],
                                m["opt_type"],
                                m["schema"],
                                m["start"],
                                m["end"],
                            )
                            results[m["rk"]] = None
                except Exception as inner:
                    _log.warning("CBBO recovery failed for %s: %s", sym, inner)
                    for m in sym_to_items.get(sym, []):
                        results[m["rk"]] = None

            if not recovered:
                return None
            return pd.concat(recovered)

    # ── IC-leg prefetch (returns full row + ts_used) ──────────────────
    def prefetch_ic_cbbo(
        self,
        requests: list[dict[str, Any]],
        *,
        allow_network: bool = True,
        max_quote_age_seconds: dict[str, int] | None = None,
        window_min: int | None = None,
    ) -> dict[Any, _CbboResult | None]:
        """Prefetch CBBO bid/ask/mid/ts_used for a set of IC legs.

        Each request: ``{ticker, strike, expiration, opt_type, at_time, leg_key}``.
        Returns ``{leg_key: {bid, ask, mid, ts_used} | None}``.

        Cache-hit legs are extracted in-process; misses sharing the same
        bounded causal window are fetched together, then jobs run in parallel.

        Quotes are selected at-or-before ``at_time`` and must be no older than
        one native CBBO interval (60 seconds for cbbo-1m, 1 second for
        cbbo-1s by default).  Future quotes and synthetic prices are never
        used. ``allow_network=False`` validates an already materialized cache.

        ``window_min`` is retained as a backward-compatibility no-op: the
        legacy ±N-minute window is superseded by the bounded causal selection
        above, so the value is accepted (the executors on the
        ``feat-data-cache-consolidation`` branch still pass it) but never used.
        Setting it to ``0`` has the same effect as leaving it ``None``.
        """
        if not requests:
            return {}

        results: dict[Any, _CbboResult | None] = {}

        # 1) Build per-leg metadata.
        legs: list[dict[str, Any]] = []
        for r in requests:
            at_utc = _ensure_utc(r["at_time"])
            # OPRA raw key is always derived here — the handler is the single
            # source of truth for the Databento contract symbol.
            sym = self._format_databento_symbol(
                r["ticker"], r["strike"], r["expiration"], r["opt_type"]
            )
            schema = _cbbo_schema_for(at_utc)
            age_limit = (max_quote_age_seconds or {}).get(
                schema, 1 if schema == "cbbo-1s" else 60
            )
            start, end = _cbbo_causal_range(at_utc, age_limit)
            legs.append(
                {
                    **r,
                    "at_utc": at_utc,
                    "schema": schema,
                    "symbol": sym,
                    "cache_start": start,
                    "cache_end": end,
                    "max_age_seconds": age_limit,
                    "fetch_start": start,
                    "fetch_end": end,
                }
            )

        # 2) Cache pre-pass.  Empty covered ranges are successful no-quote
        # responses; only absent coverage may request the provider.
        misses: list[dict[str, Any]] = []
        n_cache_hit = n_no_data = 0
        for leg in legs:
            cached = self._cache.get_superset(
                leg["ticker"],
                leg["strike"],
                leg["expiration"],
                leg["opt_type"],
                leg["cache_start"],
                leg["cache_end"],
                leg["schema"],
            )

            # A partial Arrow read is not a cache hit.  Coverage must include
            # this bounded causal provider request before it can be used.
            if cached is None:
                misses.append(leg)
                continue

            if cached.empty:
                results[leg["leg_key"]] = None
                n_no_data += 1
                continue

            usable = self._usable_cached_cbbo_rows(cached)
            if usable.empty:
                results[leg["leg_key"]] = None
                n_no_data += 1
                continue

            row = self._extract_cbbo_row(usable, leg["at_utc"])
            age = (
                (to_naive_utc(leg["at_utc"]) - to_naive_utc(row.name)).total_seconds()
                if row is not None
                else float("inf")
            )
            if row is None or age < 0 or age > leg["max_age_seconds"]:
                misses.append(leg)
                continue

            mid = (
                _valid_mid(row.get("bid"), row.get("ask")) if row is not None else None
            )
            if mid is None:
                misses.append(leg)
                continue

            n_cache_hit += 1
            results[leg["leg_key"]] = {
                "bid": float(row["bid"]),
                "ask": float(row["ask"]),
                "mid": mid,
                "ts_used": to_naive_utc(row.name).isoformat(),
                "quote_age_seconds": age,
                "source": "cache",
            }

        n_to_fetch = len(misses)
        _log.info(
            "CBBO cache scan: %d/%d causal hits | %d cache misses | no-data: %d",
            n_cache_hit,
            len(legs),
            n_to_fetch,
            n_no_data,
        )

        if not misses:
            self._last_cbbo_prefetch_stats = {
                "total_legs": len(legs),
                "cache_hits": n_cache_hit,
                "cache_miss_legs": 0,
                "api_fetches": 0,
                "api_symbols": 0,
                "skipped_unlisted": 0,
                "skipped_no_data": n_no_data,
                "missing_quote_legs": 0,
                "missing_exact_legs": 0,
                "legs_with_quote": sum(1 for v in results.values() if v is not None),
                "cache_hit_rate_pct": round(n_cache_hit / max(len(legs), 1) * 100, 1),
            }
            return results

        if not allow_network:
            for leg in misses:
                results[leg["leg_key"]] = None
            missing_exact = misses
            self._last_cbbo_prefetch_stats = {
                "total_legs": len(legs),
                "cache_hits": n_cache_hit,
                "cache_miss_legs": n_to_fetch,
                "api_fetches": 0,
                "api_symbols": 0,
                "skipped_unlisted": 0,
                "skipped_no_data": n_no_data,
                "missing_quote_legs": len(missing_exact),
                "missing_exact_legs": len(missing_exact),
                "legs_with_quote": sum(1 for v in results.values() if v is not None),
                "cache_hit_rate_pct": round(n_cache_hit / max(len(legs), 1) * 100, 1),
            }
            _log.warning(
                "CBBO cache-only mode: %d bounded-age quote(s) unavailable; no API request made",
                len(missing_exact),
            )
            return results

        # 3) Group legs that share the same bounded causal provider window.
        groups: dict[
            tuple[str, datetime.datetime, datetime.datetime], list[dict[str, Any]]
        ] = {}
        for leg in misses:
            gk = (leg["schema"], leg["fetch_start"], leg["fetch_end"])
            groups.setdefault(gk, []).append(leg)

        jobs: list[_PrefetchJob] = []
        for (schema, fetch_start, fetch_end), items in groups.items():
            uniq = list({it["symbol"] for it in items})
            for off in range(0, len(uniq), _MAX_SYM_PER_REQ):
                chunk_syms = uniq[off : off + _MAX_SYM_PER_REQ]
                chunk_set = set(chunk_syms)
                chunk_items = [it for it in items if it["symbol"] in chunk_set]
                jobs.append((schema, chunk_syms, chunk_items, fetch_start, fetch_end))

        _log.info(
            "CBBO network plan: %d causal request(s), %d cache-miss leg(s), %d symbol(s)",
            len(jobs),
            n_to_fetch,
            sum(len(job[1]) for job in jobs),
        )

        def _run(job: _PrefetchJob) -> dict[Any, _CbboResult | None]:
            schema, chunk_syms, chunk_items, min_start, max_end = job
            local: dict[Any, _CbboResult | None] = {}
            df = self._batched_get_range_with_recovery(
                schema=schema,
                symbols=chunk_syms,
                start=min_start,
                end=max_end,
                items=[
                    {
                        **it,
                        "rk": it["leg_key"],
                        "sym": it["symbol"],
                        "start": it["cache_start"],
                        "end": it["cache_end"],
                    }
                    for it in chunk_items
                ],
                results=local,
            )
            if df is None:
                for it in chunk_items:
                    local.setdefault(it["leg_key"], None)
                return local

            if df.empty:
                for it in chunk_items:
                    self._store_empty_cbbo(
                        it["ticker"],
                        it["strike"],
                        it["expiration"],
                        it["opt_type"],
                        it["schema"],
                        it["cache_start"],
                        it["cache_end"],
                    )
                    local[it["leg_key"]] = None
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
                    if sub is not None and not sub.empty:
                        cache_df = self._cbbo_df_to_cache_df(sub)
                        if not cache_df.empty:
                            valid[sym] = cache_df
                    written.add(sym)

                vdf = valid.get(sym)
                if vdf is None or vdf.empty:
                    self._store_empty_cbbo(
                        it["ticker"],
                        it["strike"],
                        it["expiration"],
                        it["opt_type"],
                        it["schema"],
                        it["cache_start"],
                        it["cache_end"],
                    )
                    local[it["leg_key"]] = None
                    continue
                row = self._extract_cbbo_row(vdf, it["at_utc"])
                age = (
                    (
                        pd.Timestamp(it["at_utc"]) - pd.Timestamp(row.name)  # ty: ignore[invalid-argument-type]
                    ).total_seconds()
                    if row is not None
                    else float("inf")
                )
                if row is None or age < 0 or age > it["max_age_seconds"]:
                    self._store_empty_cbbo(
                        it["ticker"],
                        it["strike"],
                        it["expiration"],
                        it["opt_type"],
                        it["schema"],
                        it["cache_start"],
                        it["cache_end"],
                    )
                    local[it["leg_key"]] = None
                    continue
                mid = _valid_mid(row.get("bid"), row.get("ask"))
                if mid is None:
                    self._store_empty_cbbo(
                        it["ticker"],
                        it["strike"],
                        it["expiration"],
                        it["opt_type"],
                        it["schema"],
                        it["cache_start"],
                        it["cache_end"],
                    )
                    local[it["leg_key"]] = None
                    continue
                with OptionDataHandler._cache_write_lock:
                    self._cache.put(
                        it["ticker"],
                        it["strike"],
                        it["expiration"],
                        it["opt_type"],
                        it["cache_start"],
                        it["cache_end"],
                        it["schema"],
                        vdf,
                    )
                local[it["leg_key"]] = {
                    "bid": float(row["bid"]),
                    "ask": float(row["ask"]),
                    "mid": mid,
                    "ts_used": to_naive_utc(row.name).isoformat(),
                    "quote_age_seconds": age,
                    "source": "api",
                }
            return local

        # 4) Run jobs (parallel when more than one).
        if len(jobs) <= 1:
            for job in jobs:
                results.update(_run(job))
        else:
            _log.info("CBBO prefetch: %d parallel API calls", len(jobs))
            with ThreadPoolExecutor(max_workers=min(20, len(jobs))) as pool:
                for fut in as_completed({pool.submit(_run, j): j for j in jobs}):
                    results.update(fut.result())

        missing_exact = [leg for leg in legs if results.get(leg["leg_key"]) is None]
        n_found = sum(1 for v in results.values() if v is not None)
        self._last_cbbo_prefetch_stats = {
            "total_legs": len(legs),
            "cache_hits": n_cache_hit,
            "cache_miss_legs": n_to_fetch,
            "api_fetches": len(jobs),
            "api_symbols": sum(len(job[1]) for job in jobs),
            "skipped_unlisted": 0,
            "skipped_no_data": n_no_data,
            "missing_quote_legs": len(missing_exact),
            "missing_exact_legs": len(missing_exact),
            "legs_with_quote": n_found,
            "cache_hit_rate_pct": round(n_cache_hit / max(len(legs), 1) * 100, 1),
        }
        return results

    # ── Expirations ───────────────────────────────────────────────────
    def prefetch_expirations(
        self, ticker: str, lookup_dates: list[str]
    ) -> dict[str, str | None]:
        """Resolve expiration dates from the local listing calendar only."""
        return self.resolve_expirations(ticker, lookup_dates)

    def refetch_expirations(
        self, ticker: str, dates: list[str]
    ) -> dict[str, str | None]:
        """Compatibility alias; calendar resolution has no provider refresh."""
        return self.resolve_expirations(ticker, dates)

    def resolve_expirations(
        self,
        ticker: str,
        lookup_dates: list[str],
    ) -> dict[str, str | None]:
        """Map each lookup date to its calendar-scheduled expiration.

        This intentionally performs no Databento ``definition`` request.
        Missing or exceptional listings are rejected later when the required
        CBBO legs are fetched.
        """
        ticker_u = ticker.upper()
        out: dict[str, str | None] = {}
        for date_str in sorted(set(lookup_dates)):
            key = (ticker_u, date_str)
            exp = self._expiration_lookup_cache.get(key)
            if exp is None:
                exp = scheduled_expiration_on_or_after(
                    ticker_u, datetime.date.fromisoformat(date_str)
                )
                self._expiration_lookup_cache[key] = exp
            out[date_str] = exp
        return out

    def build_expiration_cache(
        self,
        ticker: str,
        trading_days: list,  # list[datetime.date]
        min_dte: int,
        max_dte: int,
        repair_max_days: int = 30,
    ) -> dict[str, str]:
        """Build ``{entry_day_str: expiration_str}`` for all *trading_days*.

        Uses only the local expiration calendar.  The later CBBO request is
        the listing/tradeability check; candidates with missing legs are
        skipped by the prefetch/execution path.

        Parameters
        ----------
        ticker:
            Underlying ticker (e.g. ``"SPY"``).
        trading_days:
            Ordered list of ``datetime.date`` entry dates.
        min_dte:
            Minimum days-to-expiration (business days) from entry.
        max_dte:
            Maximum days-to-expiration (business days) from entry.
        repair_max_days:
            Retained for API compatibility; no provider repair is performed.
        """
        del repair_max_days
        expiration_cache: dict[str, str] = {}
        missing_days: list[str] = []
        too_far_days: list[str] = []
        try:
            for day in trading_days:
                day_str = day.strftime("%Y-%m-%d")
                target_min = (
                    (pd.Timestamp(day) + pd.offsets.BDay(min_dte)).date()
                    if min_dte > 0
                    else day
                )
                exp = scheduled_expiration_on_or_after(ticker, target_min)
                exp_date = datetime.date.fromisoformat(exp)
                max_exp_allowed = (
                    (pd.Timestamp(day) + pd.offsets.BDay(max_dte)).date()
                    if max_dte > 0
                    else day
                )
                if exp_date <= max_exp_allowed:
                    expiration_cache[day_str] = exp
                else:
                    missing_days.append(day_str)
                    too_far_days.append(day_str)

        except Exception as e:
            _log.warning("Failed to build expiration cache: %s", e)

        if missing_days:
            _log.warning(
                "No expiration found for %d days: %s", len(missing_days), missing_days
            )
            if too_far_days:
                _log.info(
                    "Expiration misses with only too-far expirations: %d day(s)",
                    len(too_far_days),
                )
        _log.info(
            "Expiration cache: %d/%d days.", len(expiration_cache), len(trading_days)
        )
        return expiration_cache

    def list_options_contracts(
        self,
        underlying_ticker: str,
        expiration_date_gte: str,
        expiration_date_lte: str,
        expired: bool = False,  # kept for back-compat (unused)
        order: str = "asc",  # kept for back-compat (unused)
        limit: int = 1,  # kept for back-compat (unused)
    ) -> str | None:
        """Earliest calendar expiration in ``[gte, lte]``."""
        del expired, order, limit  # legacy params
        try:
            out = self.resolve_expirations(
                underlying_ticker,
                [expiration_date_gte],
            )
            exp = out.get(expiration_date_gte)
            if exp is None:
                raise ValueError(
                    f"No calendar expiration found for {underlying_ticker} "
                    f"between {expiration_date_gte} and {expiration_date_lte}"
                )
            if exp > expiration_date_lte:
                raise ValueError(
                    f"No valid option expirations found for {underlying_ticker} "
                    f"between {expiration_date_gte} and {expiration_date_lte}"
                )
            return exp
        except ValueError:
            raise
        except Exception as exc:
            raise RuntimeError(
                f"Failed to list options contracts for {underlying_ticker}: {exc}"
            ) from exc

    # ── CBBO range fetch (replaces OHLCV for OPTION/SPREAD route) ─────
    def fetch_cbbo_range(
        self,
        ticker: str,
        strike: float,
        expiration: str,
        opt_type: str,
        start_dt: datetime.datetime,
        end_dt: datetime.datetime,
        schema: str = "cbbo-1m",
    ) -> pd.DataFrame:
        """Return a ``cbbo-1m`` time-series DataFrame over *start_dt*→*end_dt*.

        Columns: ``[bid, ask, mid]`` with a UTC DatetimeIndex.  Real OPRA
        consolidated best bid/offer — no synthetic spread approximation.
        Returns an empty DataFrame when the contract has no usable data.

        ``schema`` defaults to ``cbbo-1m``; pass ``cbbo-1s`` for dates on or
        after 2025-02-20 (the native 1-second NBBO interval).
        """
        symbol = self._format_databento_symbol(ticker, strike, expiration, opt_type)
        start_utc = _ensure_utc(
            start_dt
            if isinstance(start_dt, datetime.datetime)
            else start_dt.to_pydatetime()
        )
        end_utc = _ensure_utc(
            end_dt if isinstance(end_dt, datetime.datetime) else end_dt.to_pydatetime()
        )

        # 1) Superset cache hit.
        superset = self._cache.get_superset(
            ticker, strike, expiration, opt_type, start_utc, end_utc, schema
        )
        if superset is not None:
            if superset.empty:
                return superset
            superset = self._usable_cached_cbbo_rows(superset)
            if superset.empty:
                return superset
        if superset is not None:
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
            with OptionDataHandler._cache_write_lock:
                self._cache.put(
                    ticker,
                    strike,
                    expiration,
                    opt_type,
                    start_utc,
                    end_utc,
                    schema,
                    sliced,
                )
            sliced = to_naive_index(sliced)
            assert_canonical_index(sliced, allow_aware_daily=False)
            return sliced

        # 2) Exact cache hit.
        cached = self._cache.get(
            ticker, strike, expiration, opt_type, start_utc, end_utc, schema
        )
        if cached is not None:
            # A frame with no usable two-sided rows is a cached no-quote.
            out = to_naive_index(self._usable_cached_cbbo_rows(cached))
            assert_canonical_index(out, allow_aware_daily=False)
            return out

        # 3) Miss → API.
        try:
            with OptionDataHandler._net_sema, warnings.catch_warnings():
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
            _log.warning("CBBO range fetch failed for %s: %s", symbol, exc)
            return pd.DataFrame()

        if raw_df.empty:
            self._store_empty_cbbo(
                ticker, strike, expiration, opt_type, schema, start_utc, end_utc
            )
            return pd.DataFrame()

        out = self._cbbo_df_to_cache_df(raw_df, symbol)
        if not out.empty:
            with OptionDataHandler._cache_write_lock:
                self._cache.put(
                    ticker,
                    strike,
                    expiration,
                    opt_type,
                    start_utc,
                    end_utc,
                    schema,
                    out,
                )
        else:
            self._store_empty_cbbo(
                ticker, strike, expiration, opt_type, schema, start_utc, end_utc
            )
        out = to_naive_index(out)
        assert_canonical_index(out, allow_aware_daily=False)
        return out

    def fetch_cbbo_range_batch(
        self,
        requests: list[dict[str, Any]],
        *,
        max_workers: int = 20,
    ) -> dict[tuple, pd.DataFrame]:
        """Batch-fetch ``cbbo-1m`` time-series for multiple option contracts.

        Fetches real OPRA consolidated bid/ask instead of synthetic
        OHLCV-derived spreads.

        Each request: ``{ticker, expiration, strike, opt_type, start_dt, end_dt}``.
        The ``interval`` key is accepted but ignored (always ``cbbo-1m``).
        Returns ``{(ticker, expiration, strike, opt_type): DataFrame}``.
        """
        if not requests:
            return {}

        # Deduplicate: merge time windows for the same contract.
        merged: dict[tuple, dict[str, Any]] = {}
        for r in requests:
            key = (r["ticker"], r["expiration"], r["strike"], r["opt_type"])
            if key not in merged:
                merged[key] = {
                    "ticker": r["ticker"],
                    "expiration": r["expiration"],
                    "strike": r["strike"],
                    "opt_type": r["opt_type"],
                    "start_dt": r["start_dt"],
                    "end_dt": r["end_dt"],
                }
            else:
                merged[key]["start_dt"] = min(merged[key]["start_dt"], r["start_dt"])
                merged[key]["end_dt"] = max(merged[key]["end_dt"], r["end_dt"])

        unique_reqs = list(merged.values())

        def _fetch(req: dict[str, Any]) -> tuple[tuple, pd.DataFrame | None]:
            key = (req["ticker"], req["expiration"], req["strike"], req["opt_type"])
            try:
                df = self.fetch_cbbo_range(
                    ticker=req["ticker"],
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
            "fetch_cbbo_range_batch: loaded %d/%d contracts",
            len(cache),
            len(unique_reqs),
        )
        return cache

    # ── Point-in-time greeks (as-of pattern) ───────────────────────────
    #
    # A "greeks at a timestamp" service for strategy gates: given the SPY
    # spot, an as-of instant, and a set of option legs (strike / expiration /
    # type / signed position), return per-leg Black-Scholes greeks plus
    # book-level GEX / vanna / charm.  The quote surface is a causal CBBO
    # mid at-or-before the as-of instant (age-limited, cache-first).
    @staticmethod
    def _time_to_settlement_years(
        expiration: str,
        as_of_utc: datetime.datetime,
        settlement_hour: int = 16,
        settlement_minute: int = 0,
    ) -> float:
        """Years from ``as_of_utc`` to settlement on the expiration date.

        Default settlement is 16:00 ET (SPY 0-DTE expiry).  Clamped to a
        60-second floor so a degenerate ``T`` never produces NaN greeks.
        Returns ``0.0`` for unparseable expirations.
        """
        try:
            exp = pd.Timestamp(expiration)
        except Exception:
            return 0.0
        try:
            settle_ny = exp.replace(
                hour=settlement_hour,
                minute=settlement_minute,
                tzinfo=zoneinfo.ZoneInfo("America/New_York"),
            )
            settle_utc = settle_ny.tz_convert("UTC")
            secs = (settle_utc - pd.Timestamp(as_of_utc)).total_seconds()
        except Exception:
            return 0.0
        return max(secs, 60.0) / 31536000.0

    def _cbbo_mid_at(
        self,
        ticker: str,
        strike: float,
        expiration: str,
        opt_type: str,
        as_of: datetime.datetime,
        *,
        max_quote_age_seconds: int = 60,
        fetch_lookback_seconds: int | None = None,
        allow_network: bool = True,
    ) -> dict[str, Any] | None:
        """Causal CBBO mid at a single timestamp (as-of pattern).

        Returns ``{mid, bid, ask, ts_used, quote_age_seconds, source}`` or
        ``None`` when no usable two-sided quote exists at-or-before ``as_of``
        within the age limit.  Cache-first; on miss fetches one bounded
        causal window (the cheapest per-call shape) and caches it, so
        repeated as-of calls over an already-cached window are free.

        ``fetch_lookback_seconds`` widens the *fetch* window on a miss (the
        quote-selection age limit is unchanged) so a caller polling many
        intraday as-of timestamps can amortize one full-day fetch into
        ``fetch_lookback_seconds=23400`` (6.5h RTH) and pay $0 afterwards.
        """
        at_utc = _ensure_utc(as_of)
        schema = _cbbo_schema_for(at_utc)
        age_limit = 1 if schema == "cbbo-1s" else max(int(max_quote_age_seconds), 0)
        start, end = _cbbo_causal_range(at_utc, age_limit)
        fetch_start, fetch_end = start, end
        if fetch_lookback_seconds is not None:
            lb = max(int(fetch_lookback_seconds), age_limit)
            fetch_start = at_utc - datetime.timedelta(seconds=lb)
            fetch_end = at_utc + datetime.timedelta(milliseconds=1)

        # 1) Cache pre-pass: covered range (incl. empty no-quote) is final.
        cached = self._cache.get_superset(
            ticker, strike, expiration, opt_type, start, end, schema
        )
        if cached is not None:
            usable = self._usable_cached_cbbo_rows(cached)
            row = self._extract_cbbo_row(usable, at_utc)
            if row is not None:
                mid = _valid_mid(row["bid"], row["ask"])
                if mid is not None:
                    row_ts = cast("pd.Timestamp", row.name)
                    return {
                        "mid": mid,
                        "bid": float(row["bid"]),
                        "ask": float(row["ask"]),
                        "ts_used": to_naive_utc(row.name).isoformat(),
                        "quote_age_seconds": max(
                            (
                                to_naive_utc(at_utc) - to_naive_utc(row_ts)
                            ).total_seconds(),
                            0.0,
                        ),
                        "source": "cache",
                    }
            return None  # covered but no usable quote at-or-before as_of

        if not allow_network:
            return None

        # 2) Bounded fetch (fetch_cbbo_range caches the window it returns).
        df = self.fetch_cbbo_range(
            ticker,
            strike,
            expiration,
            opt_type,
            fetch_start,
            fetch_end,
            schema=schema,
        )
        row = self._extract_cbbo_row(df, at_utc)
        if row is None:
            return None
        mid = _valid_mid(row["bid"], row["ask"])
        if mid is None:
            return None
        row_ts = cast("pd.Timestamp", row.name)
        return {
            "mid": mid,
            "bid": float(row["bid"]),
            "ask": float(row["ask"]),
            "ts_used": to_naive_utc(row.name).isoformat(),
            "quote_age_seconds": max(
                (to_naive_utc(at_utc) - to_naive_utc(row_ts)).total_seconds(), 0.0
            ),
            "source": "provider",
        }

    def get_greeks_at(
        self,
        ticker: str,
        spot: float,
        as_of: datetime.datetime,
        legs: list[dict[str, Any]],
        *,
        max_quote_age_seconds: int = 60,
        fetch_lookback_seconds: int | None = None,
        settlement_hour: int = 16,
        settlement_minute: int = 0,
        allow_network: bool = True,
    ) -> dict[str, Any]:
        """Point-in-time option greeks for a set of legs (as-of pattern).

        Computes per-leg Black-Scholes greeks (delta, gamma, vega, theta,
        rho, vanna, charm) plus book-level GEX / vanna / charm for a set of
        option legs at a SINGLE timestamp ``as_of`` — no range query.

        Each leg: ``{"strike": float, "expiration": "YYYY-MM-DD",
        "opt_type": "C"|"P", "position": +1|-1, "name": str?}``.
        ``position`` is +1 long / -1 short; book sums are signed.

        Data path (cheap, cache-first):
          * CBBO mid at-or-before ``as_of`` (bounded causal window,
            age-limited) — the same quotes an execution fill would see.
          * IV inverted from the mid via ``quant.utils.bs_iv.iv_from_price``
            with r = q = 0.0 (the strategy gate convention).
          * Greeks are closed-form Black-Scholes from
            ``quant.utils.bs_greeks``.  Databento exposes no greeks fields
            in any OPRA.PILLAR schema, so IV and greeks are computed
            locally from the two-sided quote surface.

        Cost: one fresh 4-leg causal window ≈ 3,200 B ≈ $0.000006 at $2/GB
        (cbbo-1m); repeated as-of calls over an already-cached window are
        $0.00.  Pass ``fetch_lookback_seconds`` (e.g. 23400 for a 6.5h RTH
        day) to amortize many intraday as-of calls into one fetch.

        Returns ``{"as_of", "spot", "legs": {name: {...}}, "book": {...},
        "meta": {...}}``.  Legs without a usable quote or IV are present
        with ``iv_ok=False`` and zero greeks; the book only sums ``iv_ok``
        legs.
        """
        at_utc = _ensure_utc(as_of)
        spot = float(spot)

        out_legs: dict[str, dict[str, Any]] = {}
        n_quoted = n_cache_hits = n_provider_fetches = n_iv_ok = 0
        for i, leg in enumerate(legs):
            name = str(leg.get("name", i))
            strike = float(leg["strike"])
            opt_type_raw = str(leg["opt_type"]).upper()
            opt_type = "CALL" if opt_type_raw in ("C", "CALL") else "PUT"
            is_call = opt_type == "CALL"
            position = float(leg.get("position", 1.0))
            entry: dict[str, Any] = {
                "strike": strike,
                "opt_type": opt_type,
                "position": position,
                "bid": None,
                "ask": None,
                "mid": None,
                "ts_used": None,
                "quote_age_seconds": None,
                "source": None,
                "T_years": None,
                "iv": 0.0,
                "iv_ok": False,
                "delta": 0.0,
                "gamma": 0.0,
                "vega": 0.0,
                "theta": 0.0,
                "rho": 0.0,
                "vanna": 0.0,
                "charm": 0.0,
            }
            q = self._cbbo_mid_at(
                ticker,
                strike,
                leg["expiration"],
                opt_type,
                at_utc,
                max_quote_age_seconds=max_quote_age_seconds,
                fetch_lookback_seconds=fetch_lookback_seconds,
                allow_network=allow_network,
            )
            if q is not None:
                n_quoted += 1
                if q["source"] == "cache":
                    n_cache_hits += 1
                else:
                    n_provider_fetches += 1
                entry.update(
                    {
                        "bid": q["bid"],
                        "ask": q["ask"],
                        "mid": q["mid"],
                        "ts_used": q["ts_used"],
                        "quote_age_seconds": q["quote_age_seconds"],
                        "source": q["source"],
                    }
                )
                T = self._time_to_settlement_years(
                    leg["expiration"], at_utc, settlement_hour, settlement_minute
                )
                entry["T_years"] = T
                if T > 0:
                    iv = iv_from_price(spot, strike, T, q["mid"], is_call)
                    if iv > 0:
                        entry["iv"] = iv
                        entry["iv_ok"] = True
                        g = _bs_greeks(spot, strike, T, iv, is_call)
                        entry.update(g)
                        n_iv_ok += 1
            out_legs[name] = entry

        book = dict.fromkeys(
            ("delta", "gamma", "vega", "theta", "rho", "vanna", "charm"), 0.0
        )
        for entry in out_legs.values():
            if entry["iv_ok"]:
                for k in book:
                    book[k] += entry["position"] * entry[k]
        book["GEX"] = spot * book["gamma"]  # dollar gamma (Gamma Exposure)
        book["|GEX|"] = abs(book["GEX"])

        return {
            "as_of": at_utc,
            "spot": spot,
            "legs": out_legs,
            "book": book,
            "meta": {
                "n_legs": len(legs),
                "n_quoted": n_quoted,
                "n_iv_ok": n_iv_ok,
                "n_cache_hits": n_cache_hits,
                "n_provider_fetches": n_provider_fetches,
                "max_quote_age_seconds": max_quote_age_seconds,
                "fetch_lookback_seconds": fetch_lookback_seconds,
            },
        }
