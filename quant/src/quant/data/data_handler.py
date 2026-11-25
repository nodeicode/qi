# Alpaca-py imports
import logging
import os
import threading
import time
import warnings
from collections import deque
from datetime import UTC
from pathlib import Path
from typing import ClassVar

import databento as db
import pandas as pd
from alpaca.data.enums import Adjustment, DataFeed
from alpaca.data.historical import OptionHistoricalDataClient, StockHistoricalDataClient
from alpaca.data.requests import (
    OptionBarsRequest,
    StockBarsRequest,
    StockSnapshotRequest,
)
from alpaca.data.timeframe import TimeFrame, TimeFrameUnit
from alpaca.trading.client import TradingClient
from alpaca.trading.requests import GetOptionContractsRequest
from dotenv import load_dotenv

from quant.data.subscription import Datatype, Subscription
from quant.data.ws import BarSource, BarWorker, MemBarsAggregator, build_bar_source
from quant.utils.time import (
    _ET,
    EquitySessionClock,
    _interval_timedelta,
    closed_bar_asof,
)

from ._times import assert_canonical_index, parse_naive_utc
from .cache.stock import StockDataCache

logging.getLogger(__name__).addHandler(logging.NullHandler())
_log = logging.getLogger(__name__)

# US-equity session clock: resolves the first decision bar of the next trading
# day (09:30 ET) for the MemBars session-open edge-wait (see
# ``AlpacaDataHandler._mem_is_due_next_bar``). Futures' continuous 18:00 ET
# sessions differ, but a mismatch only widens the bounded wait, never serves
# wrong data (falls through to REST after the edge deadline).
_SESSION = EquitySessionClock()


class HistoricalDataCoverageError(RuntimeError):
    """Raised when requested market-data coverage cannot be completed exactly."""


class HistoricalDataFetchError(HistoricalDataCoverageError):
    """Raised when a provider failure would otherwise yield partial data."""


# Load environment variables from .env file located in the project root
_dotenv_path = Path(__file__).parent / ".." / ".env"
if _dotenv_path.exists():
    load_dotenv(dotenv_path=_dotenv_path)
else:
    _log.warning(
        "Warning: .env file not found at %s. Using default or environment-set credentials.",
        _dotenv_path,
    )

# =============================================================================
# Alpaca Data Handler
# =============================================================================


class AlpacaDataHandler:
    """
    Fetches underlying historical data from Alpaca or Databento with local caching support.
    """

    # Intervals that represent intraday (sub-daily) data
    # Bounded bank-wait for the engine's boundary fire (get_historical_data). The
    # single _INTRADAY_INTERVALS definition lives at class level (below).
    _MEM_EDGE_WAIT_S = 2.0  # max wait for the ws to bank the due decision bucket
    _MEM_HEAD_MERGE_WAIT_S = 0.5  # max wait for a worker-routed head merge to land
    _MEM_EDGE_POLL_S = 0.05  # poll cadence (50 ms); only runs on a due miss

    def __init__(
        self,
        cache_dir: str | None = None,
        provider: str = "alpaca",
        rate_limit_per_min: int = 200,
    ):
        self.logger = logging.getLogger(f"{__name__}.{self.__class__.__name__}")
        if cache_dir is None:
            # Default to quant/data/data_cache
            cache_dir = str(Path(__file__).resolve().parent / "data_cache")

        self.provider = provider

        if self.provider == "databento":
            api_key = os.getenv("DATABENTO_API_KEY")
            if not api_key:
                self.logger.warning(
                    "DATABENTO_API_KEY not found. Falling back to Alpaca."
                )
                self.provider = "alpaca"
            else:
                self.databento_client = db.Historical(key=api_key)

        # Initialize Trading Clients (Paper & Live)
        self.clients: dict[str, TradingClient] = {}

        # Paper Client
        paper_key = os.getenv("ALPACA_PAPER_API_KEY") or os.getenv("ALPACA_API_KEY")
        paper_secret = os.getenv("ALPACA_PAPER_API_SECRET") or os.getenv(
            "ALPACA_API_SECRET"
        )
        if paper_key and paper_secret:
            try:
                self.clients["paper"] = TradingClient(
                    api_key=paper_key, secret_key=paper_secret, paper=True
                )
            except Exception as e:
                self.logger.warning("Failed to init Paper Client: %s", e)

        # Paper Client 1 (account 1 — strategy testing)
        paper1_key = os.getenv("ALPACA_PAPER1_API_KEY")
        paper1_secret = os.getenv("ALPACA_PAPER1_API_SECRET")
        if paper1_key and paper1_secret:
            try:
                self.clients["paper1"] = TradingClient(
                    api_key=paper1_key, secret_key=paper1_secret, paper=True
                )
            except Exception as e:
                self.logger.warning("Failed to init Paper1 Client: %s", e)

        # Paper Client 2 (account 2 — strategy testing)
        paper2_key = os.getenv("ALPACA_PAPER2_API_KEY")
        paper2_secret = os.getenv("ALPACA_PAPER2_API_SECRET")
        if paper2_key and paper2_secret:
            try:
                self.clients["paper2"] = TradingClient(
                    api_key=paper2_key, secret_key=paper2_secret, paper=True
                )
            except Exception as e:
                self.logger.warning("Failed to init Paper2 Client: %s", e)

        # Live Client
        live_key = os.getenv("ALPACA_LIVE_API_KEY")
        live_secret = os.getenv("ALPACA_LIVE_API_SECRET")
        if live_key and live_secret:
            try:
                self.clients["prod"] = TradingClient(
                    api_key=live_key, secret_key=live_secret, paper=False
                )
            except Exception as e:
                self.logger.warning("Failed to init Live Client: %s", e)

        self.stock_client = StockHistoricalDataClient(
            api_key=os.getenv("ALPACA_API_KEY", ""),
            secret_key=os.getenv("ALPACA_API_SECRET", ""),
        )

        if not os.getenv("ALPACA_API_KEY") or not os.getenv("ALPACA_API_SECRET"):
            self.logger.warning(
                "ALPACA_API_KEY or ALPACA_API_SECRET is missing! Data fetching will likely fail."
            )

        self.option_historical_client = OptionHistoricalDataClient(
            api_key=os.getenv("ALPACA_API_KEY", ""),
            secret_key=os.getenv("ALPACA_API_SECRET", ""),
        )

        # Initialize StockDataCache
        self._cache = StockDataCache(cache_dir=cache_dir, verbose=True)

        # Rate Limiting
        self.rate_limit_per_min = rate_limit_per_min
        self.request_timestamps = deque()

        # In-memory live bar store (read fast path). Keyed by (ticker, interval, adjustment).
        # Sole writer: BarWorker's thread when a stream is running, else the caller in
        # tests — readers only snapshot-slice (GIL-atomic), never mutate.
        self._bars = MemBarsAggregator()
        self._bar_worker: BarWorker | None = None
        self._bar_source: BarSource | None = None
        # Head-soundness verdict cache: (ticker, interval, adjustment) ->
        # (cov0_seen, allowed). The head span is immutable between seeds,
        # so the disk probe runs once per seed; the hot path is a lookup.
        self._mem_head_cache: dict = {}
        self._mem_head_lock = threading.RLock()

    def seed_mem_bars(self, ticker, interval, adjustment, frame, seed_end=None) -> None:
        """Seed one adjustment key from a REST frame (warmup hook). Off the decision
        path. Applied by the worker thread when a stream is running (single writer),
        synchronously otherwise (tests)."""
        frame = frame.copy()
        if self._bar_worker is not None:
            self._bar_worker.request_seed(ticker, interval, adjustment, frame, seed_end)
        else:
            self._bars.seed(ticker, interval, adjustment, frame, seed_end)

    def start_bar_stream(
        self,
        tickers,
        interval="15m",
        provider="alpaca",
        feed: DataFeed = DataFeed.DELAYED_SIP,
    ) -> None:
        """Start live minute-bar ingestion (trader startup): worker thread first so
        early minutes are never dropped, then the provider stream.

        ``feed`` selects the stream tier (default: free 15-min-delayed SIP; pass
        ``DataFeed.SIP``/``DataFeed.IEX`` for real-time).
        """
        if self._bar_worker is not None:
            return
        api_key = os.getenv("ALPACA_API_KEY", "")
        secret = os.getenv("ALPACA_API_SECRET", "")
        self._bar_worker = BarWorker(self._bars)
        self._bar_worker.start()
        self._bar_source = build_bar_source(
            provider,
            self._bar_worker,
            api_key=api_key,
            secret_key=secret,
            tickers=list(tickers),
            interval=interval,
            feed=feed,
        )
        self._bar_source.start()

    def _mem_serve_mode(
        self, cov1: pd.Timestamp, needed: pd.Timestamp, interval: str
    ) -> str:
        """How the MemBars fast path should answer the requested decision bar
        *needed* against the stored tail *cov1* — the single boundary decision:

        ``"serve"`` - banked (``needed <= cov1``), or a **never-banks** pre-open
          bar of the next session (a DATA_DELAY first-ask, e.g. 09:00/09:15 ET for
          ``delay=15m``). Regular Trading Hours (RTH) filtering means it can never bank, so the stored
          prior-day tail IS the fresh answer — serve it.
        ``"wait"``  - a real bar the stream is about to bank (next intraday
          bucket, or the next session's open bar): wait briefly, then serve.
        ``"miss"``  - anything else (same-session / deep misses): fall through to
          REST.
        """
        if needed <= cov1:
            return "serve"
        if needed == cov1 + _interval_timedelta(interval):
            return "wait"
        try:
            nxt = _SESSION.next_open_after(
                cov1
            )  # next session open (e.g. Tue 09:30 ET)
        except Exception:
            return "miss"
        if needed == nxt:
            return "wait"
        # Never-banks pre-open: same ET day as the next open, before its 09:30 open.
        # The ET-day check keeps a real same-session later bar out of "serve".
        try:
            n_et = needed.tz_localize("UTC").tz_convert(_ET)
            x_et = nxt.tz_localize("UTC").tz_convert(_ET)
            return (
                "serve"
                if n_et.date() == x_et.date() and n_et.hour * 60 + n_et.minute < 570
                else "miss"
            )
        except Exception:
            return "miss"

    def _mem_ensure_head(
        self, ticker: str, interval: str, adjustment: str, s0, cov0
    ) -> pd.Timestamp | None:
        """Return the store head to slice from for a request starting at *s0*
        (healing the store when the disk holds head rows the memory lacks), or
        None when a required heal could not land — caller falls through.

        The first-day RTH/cache clip legitimately starts memory before *s0*;
        serving from the stored head is identical to the disk answer ONLY when
        the disk has no rows in the head span either.  When it does (repair
        writes), the rows are merged in (worker-routed to preserve the
        single-writer contract) and the fresh head is returned.  The disk probe
        is memoized per (key, cov0): the head span is immutable between seeds,
        so the hot path pays a dict lookup, not a disk read.
        """
        if s0 >= cov0:
            return cov0
        key = (ticker, interval, adjustment)
        with self._mem_head_lock:
            seen, healed = self._mem_head_cache.get(key, (None, False))
            if seen == cov0 and healed:
                return cov0
        ci = f"{interval}_raw" if adjustment == "raw" else interval
        head = self._cache.get(ticker, s0, cov0, ci)
        disk_head = head if head is not None else pd.DataFrame()
        disk_had_head = not disk_head.empty
        if disk_had_head:
            disk_head = self._finalize_equity_index(
                disk_head.rename(
                    columns={
                        "open": "Open",
                        "high": "High",
                        "low": "Low",
                        "close": "Close",
                        "volume": "Volume",
                    }
                ),
                interval,
            )
            disk_had_head = not disk_head.empty
            if disk_had_head and not (disk_head.index < cov0).any():
                # Every disk row in the head span is extended-hours (or the
                # already-stored boundary): clamping at the stored head loses
                # nothing — memoize and serve.
                disk_had_head = False
        if disk_had_head:
            if self._bar_worker is not None and hasattr(
                self._bar_worker, "request_merge"
            ):
                self._bar_worker.request_merge(ticker, interval, adjustment, disk_head)
                deadline = time.perf_counter() + self._MEM_HEAD_MERGE_WAIT_S
                while time.perf_counter() < deadline:
                    time.sleep(0.001)
                    _cov0b, _ = self._bars.coverage(key)
                    if _cov0b is not None and _cov0b <= s0:
                        break
            else:
                self._bars.merge(ticker, interval, adjustment, disk_head)
        cov0b, _ = self._bars.coverage(key)
        with self._mem_head_lock:
            self._mem_head_cache[key] = (cov0b if cov0b is not None else cov0, True)
        if disk_had_head and (cov0b is None or cov0b == cov0):
            # Merge did not land within the bounded window — do not truncate.
            return None
        return cov0b if cov0b is not None else cov0

    def stop_bar_stream(self) -> None:
        """Stop ingestion; reads fall back to the REST/cache path until the next
        warmup seed + restart."""
        if self._bar_source is not None:
            self._bar_source.stop()
            self._bar_source = None
        if self._bar_worker is not None:
            self._bar_worker.stop()
            self._bar_worker = None

    def register_client(self, name: str, key: str, secret: str, paper: bool = True):
        """
        Registers a new TradingClient with the given credentials.
        """
        try:
            self.clients[name] = TradingClient(
                api_key=key, secret_key=secret, paper=paper
            )
            self.logger.info("Registered Trading Client: %s (Paper=%s)", name, paper)
        except Exception as e:
            self.logger.error("Failed to register client %s: %s", name, e)

    def _map_interval_to_timeframe(self, interval: str) -> TimeFrame:
        interval = interval.lower().replace("min", "m").replace("minute", "m")
        if interval in ["1m", "1"]:
            return TimeFrame.Minute
        elif interval in ["5m", "5"]:
            return TimeFrame(5, TimeFrameUnit.Minute)  # ty: ignore[invalid-argument-type]
        elif interval in ["15m", "15"]:
            return TimeFrame(15, TimeFrameUnit.Minute)  # ty: ignore[invalid-argument-type]
        elif interval in ["30m", "30"]:
            return TimeFrame(30, TimeFrameUnit.Minute)  # ty: ignore[invalid-argument-type]
        elif interval in ["1h", "60"]:
            return TimeFrame.Hour
        elif interval in ["3h", "120"]:
            return TimeFrame(3, TimeFrameUnit.Hour)  # ty: ignore[invalid-argument-type]
        elif interval in ["1d", "d"]:
            return TimeFrame.Day
        else:
            # Try to parse "Xm" or "Xh"
            if interval.endswith("m"):
                try:
                    return TimeFrame(int(interval[:-1]), TimeFrameUnit.Minute)  # ty: ignore[invalid-argument-type]
                except Exception:
                    self.logger.debug("Could not parse minute interval: %s", interval)
            if interval.endswith("h"):
                try:
                    return TimeFrame(int(interval[:-1]), TimeFrameUnit.Hour)  # ty: ignore[invalid-argument-type]
                except Exception:
                    self.logger.debug("Could not parse hour interval: %s", interval)

            return TimeFrame(5, TimeFrameUnit.Minute)  # ty: ignore[invalid-argument-type]

    def _wait_for_rate_limit(self):
        now = time.time()
        # Remove timestamps older than 60s
        while self.request_timestamps and now - self.request_timestamps[0] > 60:
            self.request_timestamps.popleft()

        if len(self.request_timestamps) >= self.rate_limit_per_min:
            # Wait until the oldest expires
            sleep_time = 60 - (now - self.request_timestamps[0]) + 0.1  # buffer
            if sleep_time > 0:
                time.sleep(sleep_time)
            # Cleanup again after sleep
            now = time.time()
            while self.request_timestamps and now - self.request_timestamps[0] > 60:
                self.request_timestamps.popleft()

        self.request_timestamps.append(time.time())

    def _fetch_api_data(
        self,
        ticker: str,
        start: str,
        end: str,
        interval: str,
        adjustment: str = "all",
        use_cache: bool = False,
        cache_interval: str | None = None,
    ) -> pd.DataFrame:
        """Helper to fetch data from the configured provider."""
        adjustment = adjustment.lower()

        # Pre-process datetimes for consistent usage at the handler boundary.
        # ``parse_naive_utc`` keeps nanosecond precision (the coverage-gap
        # repair can create one-nanosecond boundaries; ``to_pydatetime()``
        # truncates those and leaves a permanent apparent cache hole).
        _ns_start, _ns_end = parse_naive_utc(start, end)
        # Cache coverage bounds are stored UTC-aware; localize the canonical
        # naive stamps once here (never re-derive tz semantics per call).
        req_start = _ns_start.tz_localize("UTC")
        req_end = _ns_end.tz_localize("UTC")

        if self.provider == "databento":
            df = self._fetch_from_databento(ticker, start, end, interval)
            # Databento caching logic
            if use_cache and cache_interval:
                if not df.empty:
                    self._cache.put(ticker, req_start, req_end, cache_interval, df)
                self._cache.update_coverage(ticker, cache_interval, req_start, req_end)
            return df

        # Alpaca logic
        timeframe = self._map_interval_to_timeframe(interval)

        # Determine if we should batch
        norm_interval = self._normalize_interval(interval)
        do_batch = norm_interval in ["1m", "5m", "15m", "30m"]

        # Define chunk size based on interval (approx 30 days)
        chunk_days = 30

        chunks = []
        if do_batch and (req_end - req_start).days > chunk_days:
            curr = req_start
            while curr < req_end:
                next_chunk = curr + pd.Timedelta(days=chunk_days)
                if next_chunk > req_end:
                    next_chunk = req_end
                chunks.append((curr, next_chunk))
                curr = next_chunk
        else:
            chunks = [(req_start, req_end)]

        all_dfs = []

        if len(chunks) > 1:
            self.logger.info(
                "Batching fetch for %s (%s to %s) into %d requests.",
                ticker,
                start,
                end,
                len(chunks),
            )

        for c_start, c_end in chunks:
            # Avoid empty chunks
            if c_start >= c_end:
                continue

            self._wait_for_rate_limit()

            if adjustment == "raw":
                adj_enum = Adjustment.RAW
            else:
                adj_enum = Adjustment.ALL

            if len(chunks) > 1:
                self.logger.debug("Fetching %s chunk: %s -> %s", ticker, c_start, c_end)
            else:
                self.logger.debug(
                    "Fetching data for %s from %s to %s at %s interval (%s)",
                    ticker,
                    c_start,
                    c_end,
                    interval,
                    adjustment,
                )

            req = StockBarsRequest(
                symbol_or_symbols=[ticker],
                timeframe=timeframe,
                start=c_start,
                end=c_end,
                adjustment=adj_enum,
            )
            try:
                bars_dict = self.stock_client.get_stock_bars(req)
                # NOTE: do not change the below check, its correct and any other way breaks for empty data
                if ticker != bars_dict[ticker][0].symbol or len(bars_dict[ticker]) == 0:
                    # Even if empty, we do not want to mark coverage (might be error case)
                    continue

                bar_list = bars_dict[ticker]
                data = [
                    {
                        "symbol": x.symbol,
                        "timestamp": x.timestamp,
                        "open": x.open,
                        "high": x.high,
                        "low": x.low,
                        "close": x.close,
                        "volume": x.volume,
                    }
                    for x in bar_list
                ]

                df_chunk = pd.DataFrame(data)
                df_chunk = df_chunk.set_index("timestamp")

                # INCREMENTAL CACHING
                if use_cache and cache_interval:
                    if not df_chunk.empty:
                        self._cache.put(
                            ticker, c_start, c_end, cache_interval, df_chunk
                        )
                    self._cache.update_coverage(ticker, cache_interval, c_start, c_end)

                all_dfs.append(df_chunk)

            except Exception as e:
                error_message = str(getattr(e, "message", None) or e).casefold()
                if (
                    "subscription does not permit querying recent sip data"
                    in error_message
                ):
                    self.logger.error(
                        "Alpaca denied recent SIP data for %s (%s to %s): %s. "
                        "Refusing to return partial market data.",
                        ticker,
                        c_start,
                        c_end,
                        e,
                    )
                    raise HistoricalDataFetchError(
                        f"Alpaca SIP coverage unavailable for {ticker} "
                        f"[{c_start}, {c_end}]"
                    ) from e

                if isinstance(e, (KeyError, IndexError)):
                    # The response contained no bars for this symbol (omitted key
                    # or an empty list — the two shapes an empty Alpaca response
                    # can take).  Skip this chunk and do NOT record cache
                    # coverage: an empty response may be an error case and must
                    # never make the range look fully covered.
                    continue

                self.logger.error(
                    "Error fetching chunk %s-%s for %s: %s", c_start, c_end, ticker, e
                )
                # Do not return a partial series or record coverage after a provider
                # failure.  A partial underlying series changes signal and breach
                # timing just as materially as a stale option quote.
                raise HistoricalDataFetchError(
                    f"Alpaca fetch failed for {ticker} [{c_start}, {c_end}]"
                ) from e

        if not all_dfs:
            return pd.DataFrame()

        final_df = pd.concat(all_dfs)
        # Remove duplicates — keep='last' for consistency with cache
        # read from the Arrow cache and merge below, so the same row
        # always wins regardless of code path.
        final_df = final_df[~final_df.index.duplicated(keep="last")]
        final_df = final_df.sort_index()

        final_df = final_df.rename(
            columns={
                "open": "Open",
                "high": "High",
                "low": "Low",
                "close": "Close",
                "volume": "Volume",
            }
        )

        return final_df

    # Intervals that represent intraday (sub-daily) data
    _INTRADAY_INTERVALS: ClassVar[frozenset[str]] = frozenset(
        {"1m", "5m", "15m", "30m", "60m", "1h", "2h", "4h"}
    )

    def _filter_market_hours(self, df: pd.DataFrame, interval: str) -> pd.DataFrame:
        """Filter intraday data to regular US market hours (9:30-16:00 ET).

        Handles DST automatically via tz_convert to America/New_York.
        Daily and higher intervals pass through unfiltered.
        Cache stores all data (including extended hours) — filtering is output-only.
        """
        if df.empty or interval not in self._INTRADAY_INTERVALS:
            return df
        if not isinstance(df.index, pd.DatetimeIndex):
            return df
        if df.index.tz is not None:
            _et = df.index.tz_convert("America/New_York")
        else:
            _et = df.index.tz_localize("UTC").tz_convert("America/New_York")
        _et_hour: pd.Index = _et.hour  # type: ignore[attr-defined]  # DatetimeIndex has .hour
        _et_minute: pd.Index = _et.minute  # type: ignore[attr-defined]  # DatetimeIndex has .minute
        _et_min = _et_hour * 60 + _et_minute
        return df[(_et_min >= 570) & (_et_min < 960)]  # 9:30-16:00 ET

    def _finalize_equity_index(self, df: pd.DataFrame, interval: str) -> pd.DataFrame:
        """Apply RTH filter and return intraday indexes as UTC-naive.

        Daily bars keep vendor timezone (typically UTC-aware) so strategies can
        map to ET session dates via ``to_et_session_midnight``.  Intraday bars
        are normalized to UTC-naive (naive == UTC wall clock) at the handler
        boundary so consumers do not re-localize.
        """
        df = self._filter_market_hours(df, interval)
        if df.empty or interval not in self._INTRADAY_INTERVALS:
            return df
        if not isinstance(df.index, pd.DatetimeIndex):
            return df
        # Rebuild as DatetimeIndex so type checkers see .tz (copy().index is Index).
        idx = pd.DatetimeIndex(df.index)
        if idx.tz is None:
            idx = idx.tz_localize("UTC")
        else:
            idx = idx.tz_convert("UTC")
        out = df.copy()
        out.index = pd.DatetimeIndex(idx.tz_localize(None)).as_unit("us")
        assert_canonical_index(out, allow_aware_daily=False)
        return out

    def _normalize_interval(self, interval: str) -> str:
        """Normalize interval string to a standard format."""
        interval = interval.lower().replace("minute", "m").replace("min", "m")
        if interval in ["1", "1m"]:
            return "1m"
        if interval in ["5", "5m"]:
            return "5m"
        if interval in ["15", "15m"]:
            return "15m"
        if interval in ["30", "30m"]:
            return "30m"
        if interval in ["60", "1h"]:
            return "1h"
        if interval in ["d", "1d"]:
            return "1d"
        return interval

    def get_historical_data(
        self,
        ticker: str,
        start: str,
        end: str,
        interval: str,
        use_cache=True,
        adjustment="all",
    ) -> pd.DataFrame:
        """
        Get historical data with caching support.

        Args:
            ticker: Symbol to fetch data for
            start: Start date/time
            end: End date/time
            interval: Data interval (1m, 5m, 15m, 1h, 1d)
            use_cache: Whether to use cached data (default: True)
            adjustment: 'all' (split/div adjusted) or 'raw' (unadjusted)

        Returns:
            DataFrame with historical price data
        """
        # Normalize interval for consistent caching
        interval = self._normalize_interval(interval)
        adjustment = adjustment.lower()

        # Preserve sub-microsecond boundaries used by Arrow coverage repair.
        start_dt = pd.Timestamp(start)
        end_dt = pd.Timestamp(end)

        # ── MemBars fast path (live) ──────────────────────────────────────────
        # Fully-covered windows are served straight from the in-memory store, skipping
        # the Arrow stock_cache read, deserialization and re-normalization entirely.
        # Store rows are output-shaped (RTH-filtered, UTC-naive, Title cols + symbol),
        # so the slice is the final result.  Envelope-only coverage: interior gaps
        # are served as-is (REST parity) and select_signal_row holds on a missing
        # tail bar.
        if (
            use_cache
            and adjustment in ("all", "raw")
            and interval in self._INTRADAY_INTERVALS
        ):
            _key = (ticker, interval, adjustment)
            if self._bars.has(_key):
                _cov0, _cov1 = self._bars.coverage(_key)
                assert _cov0 is not None and _cov1 is not None  # has() above
                s0 = start_dt.tz_localize(None) if start_dt.tz is not None else start_dt
                s1 = end_dt.tz_localize(None) if end_dt.tz is not None else end_dt
                # Live-edge gate: serve when the newest bar the request can
                # consume — closed_bar_asof(s1), i.e. the engine's decision bar —
                # is banked (<= _cov1).  This exact form tolerates the feed's
                # sub-bucket skew (a bucket banks right at its close +
                # pipeline-delay).  The un-banked remainder past the slice is
                # clamped, mirroring the delayed feed's view.
                _needed = closed_bar_asof(s1, interval)
                _mode = self._mem_serve_mode(_cov1, _needed, interval)
                # Serve (banked, or a never-banks pre-open first-ask), wait for the
                # next real bar the stream is about to bank, else fall to REST below.
                if _mode == "serve" and s0 <= _cov1:
                    _head = self._mem_ensure_head(
                        ticker, interval, adjustment, s0, _cov0
                    )
                    if _head is not None and _head <= s1:
                        _out = self._bars.window(_key, _head, s1)
                        if _out is not None and not _out.empty:
                            self.logger.debug(
                                "MemBars hit %s (%s, %s): %d rows in [%s, %s]",
                                adjustment,
                                ticker,
                                interval,
                                len(_out),
                                _head,
                                s1,
                            )
                            return _out
                # Decision bar not banked YET.  If it is DUE — the very next
                # bucket past the stored tail (the ws settles it within moments
                # of the engine's boundary fire) — wait it out BEFORE racing
                # REST, waking on the worker's settle event (no blind polling);
                # a genuinely lagging/dead stream still falls through (warn).
                elif _mode == "wait":
                    _deadline = time.perf_counter() + self._MEM_EDGE_WAIT_S
                    _ev = (
                        self._bar_worker._settle
                        if self._bar_worker is not None
                        else None
                    )
                    while time.perf_counter() < _deadline:
                        _cov0, _cov1 = self._bars.coverage(_key)
                        if _cov0 is None or _cov1 is None:
                            break
                        if _needed <= _cov1 and s0 <= _cov1:
                            _head = self._mem_ensure_head(
                                ticker, interval, adjustment, s0, _cov0
                            )
                            if _head is not None and _head <= s1:
                                _out = self._bars.window(_key, _head, s1)
                                if _out is not None and not _out.empty:
                                    self.logger.debug(
                                        "MemBars hit (edge wait) %s (%s, %s): %d rows",
                                        adjustment,
                                        ticker,
                                        interval,
                                        len(_out),
                                    )
                                    return _out
                        if _ev is not None:
                            _ev.clear()
                            _ev.wait(self._MEM_EDGE_POLL_S)
                        else:
                            time.sleep(self._MEM_EDGE_POLL_S)
                self.logger.warning(
                    "MemBars miss %s (%s, %s): window [%s, %s] not covered by "
                    "in-memory store [%s, %s] — falling back to REST/cache",
                    adjustment,
                    ticker,
                    interval,
                    s0,
                    s1,
                    _cov0,
                    _cov1,
                )
            else:
                self.logger.warning(
                    "MemBars miss %s (%s, %s): store is empty (not seeded / "
                    "stream idle) — falling back to REST/cache",
                    adjustment,
                    ticker,
                    interval,
                )

        # Note: Cache key should include adjustment type to avoid mixing raw/adjusted
        cache_interval = interval
        if adjustment == "raw":
            cache_interval = f"{interval}_raw"

        if use_cache:
            # Check coverage first
            cov_min, cov_max = self._cache.get_coverage(ticker, cache_interval)

            req_start_utc, req_end_utc = parse_naive_utc(start_dt, end_dt)

            # Arrow coverage is segment-based.  A min/max envelope can hide an
            # interior provider failure, so repair every actual gap before the
            # legacy head/tail fast path considers this range complete.
            uncovered = self._cache.missing_ranges(
                ticker, start_dt, end_dt, cache_interval
            )
            interior_gap = (
                bool(uncovered)
                and cov_min is not None
                and cov_max is not None
                and cov_min <= req_start_utc
                and cov_max >= req_end_utc
            )
            if interior_gap:
                self.logger.warning(
                    "Repairing %d interior cache gap(s) for %s: %s to %s",
                    len(uncovered),
                    ticker,
                    start,
                    end,
                )
                for gap_start, gap_end in uncovered:
                    self._fetch_api_data(
                        ticker,
                        gap_start.isoformat(),
                        gap_end.isoformat(),
                        interval,
                        adjustment,
                        use_cache=True,
                        cache_interval=cache_interval,
                    )
                repaired = self._cache.get_superset(
                    ticker, start_dt, end_dt, cache_interval
                )
                if repaired is None:
                    raise HistoricalDataCoverageError(
                        f"Cache coverage remains incomplete for {ticker} "
                        f"[{start}, {end}] after gap repair"
                    )
                if repaired.empty:
                    return self._finalize_equity_index(repaired, interval)
                repaired = repaired.rename(
                    columns={
                        "open": "Open",
                        "high": "High",
                        "low": "Low",
                        "close": "Close",
                        "volume": "Volume",
                    }
                )
                repaired["symbol"] = ticker
                return self._finalize_equity_index(repaired, interval)

            # If we have coverage
            if cov_min and cov_max:
                # Check if we need to extend
                needs_head = req_start_utc < cov_min
                # Bar-boundary floored tail: a request end that advanced by less
                # than one INTERVAL (no full new bar closed yet) is a cache hit,
                # not an uncovered tail — never race the provider for a bar that
                # cannot exist yet. This matches the MemBars fast path, which
                # already gates on ``closed_bar_asof``; the Arrow fallback was
                # request-envelope based and re-fetched the sub-INTERVAL tail on
                # every call (redundant API traffic, no coverage recorded).
                needs_tail = closed_bar_asof(req_end_utc, interval) > closed_bar_asof(
                    cov_max, interval
                )

                if not needs_head and not needs_tail:
                    # Fully covered
                    df = self._cache.get(ticker, start_dt, end_dt, cache_interval)
                    if df is None:
                        df = pd.DataFrame()
                    if not df.empty:
                        df = df.rename(
                            columns={
                                "open": "Open",
                                "high": "High",
                                "low": "Low",
                                "close": "Close",
                                "volume": "Volume",
                            }
                        )
                        df["symbol"] = ticker
                        self.logger.info(
                            "Cache hit for %s: %s to %s at %s interval, %d rows",
                            ticker,
                            start,
                            end,
                            interval,
                            len(df),
                        )
                    return self._finalize_equity_index(df, interval)

                # Partial coverage
                df_head = pd.DataFrame()
                df_tail = pd.DataFrame()

                if needs_head:
                    # Fetch head
                    # Note: Using cached fetching to update coverage safely
                    self.logger.info(
                        "Fetching missing head for %s: %s to %s", ticker, start, cov_min
                    )
                    df_head = self._fetch_api_data(
                        ticker,
                        start,
                        cov_min.isoformat(),
                        interval,
                        adjustment,
                        use_cache=True,
                        cache_interval=cache_interval,
                    )

                if needs_tail:
                    # Fetch tail
                    self.logger.info(
                        "Fetching missing tail for %s: %s to %s", ticker, cov_max, end
                    )
                    df_tail = self._fetch_api_data(
                        ticker,
                        cov_max.isoformat(),
                        end,
                        interval,
                        adjustment,
                        use_cache=True,
                        cache_interval=cache_interval,
                    )

                # Return combined from cache - since we just fetched/cached the missing parts
                df_mid = self._cache.get(ticker, start_dt, end_dt, cache_interval)

                # Prepare parts for robust merge
                parts = []

                # 1. Head (Memory)
                if not df_head.empty:
                    parts.append(df_head)

                # 2. Mid (Cache)
                if df_mid is not None and not df_mid.empty:
                    # Normalize columns to match API output (Title Case)
                    df_mid = df_mid.rename(
                        columns={
                            "open": "Open",
                            "high": "High",
                            "low": "Low",
                            "close": "Close",
                            "volume": "Volume",
                        }
                    )
                    df_mid["symbol"] = ticker
                    parts.append(df_mid)

                # 3. Tail (Memory)
                if not df_tail.empty:
                    parts.append(df_tail)

                # Merge and Deduplicate
                if parts:
                    # Combine all available sources
                    combined = pd.concat(parts)
                    combined = combined[~combined.index.duplicated(keep="last")]
                    combined = combined.sort_index()
                    combined["symbol"] = ticker
                    return self._finalize_equity_index(combined, interval)
                else:
                    # Fallthrough if get returns None/Empty but we fetched ???
                    # This might happen if fetch returned empty DF (no data for that period).
                    return pd.DataFrame()

        # No cache or no coverage -> Fetch all
        self.logger.info("Fetching all data for %s: %s to %s", ticker, start, end)
        df = self._fetch_api_data(
            ticker,
            start,
            end,
            interval,
            adjustment,
            use_cache=use_cache,
            cache_interval=cache_interval,
        )

        # Note: Cache is updated inside _fetch_api_data if use_cache is True
        return self._finalize_equity_index(df, interval)

    def get_data(
        self,
        subscription: Subscription,
        start,
        end,
        *,
        use_cache: bool = True,
        adjustment: str | None = None,
    ) -> pd.DataFrame:
        """Fulfill a typed bars subscription for ``[start, end]``.

        Routes the subscription's instrument symbol and cadence into the
        existing live fetch/cache path so a ``bars`` subscription and the
        engine's ``get_historical_data`` path share one source of truth with
        the same canonical column shape.
        """
        if subscription.datatype is not Datatype.BARS:
            raise ValueError(
                f"AlpacaDataHandler ships bars only, got {subscription.datatype}"
            )
        return self.get_historical_data(
            ticker=subscription.instrument.symbol,
            start=start,
            end=end,
            interval=subscription.cadence,
            use_cache=use_cache,
            adjustment=adjustment or "all",
        )

    def _interval_to_schema(self, interval: str) -> str:
        """Convert interval string to Databento schema."""
        if interval in ["1m", "1"]:
            return "ohlcv-1m"
        if interval in ["5m", "5"]:
            return "ohlcv-1m"
        if interval in ["15m", "15"]:
            return "ohlcv-1m"
        if interval in ["30m", "30"]:
            return "ohlcv-1m"
        if interval in ["1h", "60"]:
            return "ohlcv-1h"
        if interval in ["1d", "d"]:
            return "ohlcv-1d"
        return "ohlcv-1m"

    def _fetch_from_databento(
        self, ticker: str, start: str, end: str, interval: str
    ) -> pd.DataFrame:
        """Fetch historical data from Databento."""
        schema = self._interval_to_schema(interval)

        try:
            # Suppress Databento warnings
            with warnings.catch_warnings():
                warnings.simplefilter("ignore")
                data = self.databento_client.timeseries.get_range(
                    dataset="XNAS.ITCH",
                    symbols=ticker,
                    schema=schema,
                    start=start,
                    end=end,
                    stype_in="raw_symbol",
                )

            df = data.to_df()

            if df.empty:
                return pd.DataFrame()

            # Rename columns to match Alpaca format
            column_mapping = {
                "ts_event": "timestamp",
                "open": "Open",
                "high": "High",
                "low": "Low",
                "close": "Close",
                "volume": "Volume",
            }
            df = df.rename(columns=column_mapping)

            if "timestamp" in df.columns:
                df = df.set_index("timestamp")
            df = df.sort_index()

            # Convert prices from int64 if needed (Databento often returns fixed point)
            for col in ["Open", "High", "Low", "Close"]:
                if col in df.columns and df[col].dtype == "int64":
                    df[col] = df[col] / 1e9

            # Ensure required columns exist
            required_cols = ["Open", "High", "Low", "Close", "Volume"]
            for col in required_cols:
                if col not in df.columns:
                    df[col] = 0.0

            # Resample if needed (e.g. 1m -> 5m)
            if schema == "ohlcv-1m" and interval not in ["1m", "1"]:
                rule = interval.replace("m", "T").replace("min", "T")
                if rule.isdigit():  # e.g. "5"
                    rule += "T"

                # Resample OHLCV
                agg_dict = {
                    "Open": "first",
                    "High": "max",
                    "Low": "min",
                    "Close": "last",
                    "Volume": "sum",
                }
                df = df.resample(rule).agg(agg_dict).dropna()

            return df

        except Exception as e:
            self.logger.error("Databento fetch error for %s: %s", ticker, e)
            # An exception is not an empty successful response.  In particular,
            # do not let _fetch_api_data mark this interval as cache-covered.
            raise HistoricalDataFetchError(
                f"Databento fetch failed for {ticker} [{start}, {end}]"
            ) from e

    def clear_cache(self, older_than_days=None):
        """
        Clear the cached data.

        Args:
            older_than_days: If provided, only clear cache files older than this many days
        """
        if older_than_days is not None:
            self._cache.cleanup_old_entries(days_old=older_than_days)
        else:
            self._cache.clear_cache()

    def get_option_history(
        self, symbol: str, start_dt, end_dt, interval: str = "1m"
    ) -> pd.DataFrame:
        """
        Fetch historical bars for a known OCC symbol.
        """
        try:
            timeframe = self._map_interval_to_timeframe(interval)

            req_start = (
                pd.to_datetime(start_dt).to_pydatetime()
                if isinstance(start_dt, (str, pd.Timestamp))
                else start_dt
            )
            req_end = (
                pd.to_datetime(end_dt).to_pydatetime()
                if isinstance(end_dt, (str, pd.Timestamp))
                else end_dt
            )

            if req_start.tzinfo is None:
                req_start = req_start.replace(tzinfo=UTC)
            if req_end.tzinfo is None:
                req_end = req_end.replace(tzinfo=UTC)

            req = OptionBarsRequest(
                symbol_or_symbols=[symbol],
                timeframe=timeframe,
                start=req_start,
                end=req_end,
            )

            bars_dict = self.option_historical_client.get_option_bars(req)
            if symbol not in bars_dict or len(bars_dict[symbol]) == 0:
                return pd.DataFrame()

            bar_list = bars_dict[symbol]
            data = [
                {
                    "timestamp": bar.timestamp,
                    "open": bar.open,
                    "high": bar.high,
                    "low": bar.low,
                    "close": bar.close,
                    "volume": bar.volume,
                }
                for bar in bar_list
            ]

            df = pd.DataFrame(data)
            df = df.set_index("timestamp")
            df = df.sort_index()

            # Synthesize mid/bid/ask
            df["mid"] = (df["high"] + df["low"]) / 2.0
            spread_pct = 0.05
            spread = df["mid"] * spread_pct
            df["bid"] = df["mid"] - spread / 2.0
            df["ask"] = df["mid"] + spread / 2.0

            return df

        except Exception as e:
            self.logger.error("Error fetching option history for %s: %s", symbol, e)
            return pd.DataFrame()

    def get_option_expirations(
        self, ticker: str, exp_gte: str, exp_lte: str, limit: int = 10
    ) -> list[str]:
        """Return sorted unique expiration date strings (YYYY-MM-DD) for *ticker*
        within the given date range.  Uses the first available TradingClient so
        the result is broker-account-agnostic (reference data only).

        Returns an empty list when no expirations are found or on error.
        """
        client = next(iter(self.clients.values()), None)
        if client is None:
            self.logger.warning("get_option_expirations: no TradingClient available.")
            return []
        try:
            resp = client.get_option_contracts(
                GetOptionContractsRequest(
                    underlying_symbols=[ticker],
                    expiration_date_gte=exp_gte,
                    expiration_date_lte=exp_lte,
                    limit=limit,
                )
            )
            contracts = resp.option_contracts  # ty:ignore[unresolved-attribute]
            if contracts:
                return sorted(
                    {
                        c.expiration_date.strftime("%Y-%m-%d")
                        if hasattr(c.expiration_date, "strftime")
                        else str(c.expiration_date)
                        for c in contracts
                    }
                )
        except Exception as e:
            self.logger.warning("Alpaca expiration lookup failed for %s: %s", ticker, e)
        return []

    def get_latest_stock_quotes(self, symbols: list) -> pd.DataFrame:
        """
        Fetches latest stock snapshots (price/bid/ask) for a list of symbols.
        Returns a DataFrame indexed by Symbol with columns ['bid', 'ask', 'mid', 'price'].
        """
        if not symbols:
            return pd.DataFrame()

        try:
            req = StockSnapshotRequest(symbol_or_symbols=symbols)
            snapshots = self.stock_client.get_stock_snapshot(req)

            data = []
            for symbol, snap in snapshots.items():
                price = 0.0
                bid = 0.0
                ask = 0.0

                # Check for latest trade
                if snap.latest_trade:
                    price = snap.latest_trade.price

                # Check for latest quote
                if snap.latest_quote:
                    bid = snap.latest_quote.bid_price
                    ask = snap.latest_quote.ask_price

                # Fallback if quote is zero/missing but we have a trade price
                if bid == 0 and price > 0:
                    bid = price
                if ask == 0 and price > 0:
                    ask = price

                mid = (bid + ask) / 2.0 if (bid > 0 and ask > 0) else price

                if mid > 0 or price > 0:
                    data.append(
                        {
                            "symbol": symbol,
                            "price": price,
                            "bid": bid,
                            "ask": ask,
                            "mid": mid,
                        }
                    )

            df = pd.DataFrame(data)
            if not df.empty:
                df = df.set_index("symbol")
            return df

        except Exception as e:
            self.logger.error("Error fetching stock snapshots: %s", e)
            return pd.DataFrame()
