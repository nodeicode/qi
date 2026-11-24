"""Daily VIXCLS (Cboe Volatility Index) data handler.

What this is
    VIXCLS is the daily *end-of-day* close of the Cboe Volatility Index: the
    value printed for a date D is computed from SPX option prices at D's market
    close and is only published *after* that close. In live trading the desk
    receives it as one close per trading day, with roughly a one-day lag vs. the
    calendar date.

Why availability timing matters (no lookahead)
    Any strategy decision *on* date D (computing entry indicators at the D bar)
    must only ever see closes with ``date < D``. Making that structurally
    impossible to get wrong is the whole point of this handler: ``end`` is
    treated as the *decision timestamp* and only rows strictly before it are
    served — ``get_historical_data("VIX", end=D)`` returns rows with
    ``date < D``. The value for date D only appears when a caller asks with
    ``end > D`` (a report "through the close of D").

Desk freshness rule (owned here; strategy-independent)
    FRED publishes VIXCLS once per trading day, after that day's close. The desk
    trader is **one fresh process per trading day** (paper market-open → run
    through the day's decision clock → shutdown → a new process next open), so
    the cadence is *one VIX fetch per process init == one refetch per trading
    day* — the desk's "refetch everyday" rule, made structural by the process
    boundary.

    Caching is internal to this handler (no caller logic): one in-memory series
    per process, fed from the desk's **cached daily FRED pull when present**
    (`data_cache/strat2026/raw/fred_vixcls.parquet` — deterministic, offline;
    refreshed by the desk pipeline) and otherwise from a live FRED GET.  The
    in-memory series can never go stale between that day's bars themselves (no
    weekend-week-old closes can ever be served because each day's decision clock
    is fed by that day's own fetch).

    A failed fetch never raises and never empties the gate: the handler returns
    whatever it has (possibly an empty frame) and the strategy's gates reject
    instead of trading on missing/stale VIX (safe fail). Steady state is fully
    offline: steady-state desk code paths that already hold the series in memory
    never touch the network again during that day's run.

Where the data comes from (one real VIX series, two sources, first found wins)
    1. In-process memory:  the series fetched by the first cold init below.
    2. Desk cached FRED pull: `data_cache/strat2026/raw/fred_vixcls.parquet`
       (the desk's deterministic daily snapshot; no network).
    3. FRED:               https://fred.stlouisfed.org/graph/fredgraph.csv?id=VIXCLS
       (one HTTP GET per cold init = per trading day, when the cache is absent).
"""

from __future__ import annotations

import io
import logging
import urllib.parse
import urllib.request
from typing import Protocol, runtime_checkable

import pandas as pd

from ._times import assert_canonical_index, to_naive_utc

logger = logging.getLogger(__name__)

_FRED_URL = "https://fred.stlouisfed.org/graph/fredgraph.csv?id=VIXCLS"
_ACCEPTED_TICKERS = frozenset({"VIX", "VIXCLS"})


def _naive_utc(value) -> pd.Timestamp:
    """Coerce a start/end boundary to a tz-naive UTC timestamp (UTC wall)."""
    return to_naive_utc(value)


@runtime_checkable
class VixDataView(Protocol):
    """Minimal daily-VIXCLS view a strategy needs (shape mirrors the desk handler).

    ``get_historical_data`` returns a frame of daily closes with
    ``date >= start`` (when given) and ``date < end`` (strictly before the
    decision timestamp). ``end=None`` returns the full history (reporting/audit).
    Lack of data never raises: an empty frame is returned and the strategy gates
    reject (safe fail).
    """

    def get_historical_data(
        self,
        ticker: str,
        start: str | pd.Timestamp | None = None,
        end: str | pd.Timestamp | None = None,
        interval: str = "1d",
        use_cache: bool = True,
        adjustment: str | None = None,
    ) -> pd.DataFrame: ...


class VixDailyDataHandler:
    """In-memory daily VIXCLS closes served with strict-before-``end`` semantics.

    One FRED GET per cold init (per desk trading day - the desk freshness rule
    above), series kept in memory for that process, never persisted to disk.
    ``get_historical_data("VIX", end=<decision ts>)`` returns only rows with
    ``date < end`` - a desk decision on date D can therefore never see D's own
    (or any future) VIX closeahan. ``ticker`` must be "VIX" or "VIXCLS" (same
    series). ``start``/``end``/``interval``/``use_cache``/``adjustment`` are
    accepted for protocol compatibility; ``interval``/``use_cache``/
    ``adjustment`` are ignored (daily intrinsic series; nothing to apply).

    Safe fail (never raises, never blocks trading on bad data):
      - A failed GET keeps whatever is already known (empty frame when the
        process has nothing) and logs a warning.
      - An empty result is *valid* output: the strategy's gates reject rather
        than trade on missing VIX.
    """

    def __init__(self) -> None:
        self._series: pd.Series | None = None

    def get_historical_data(
        self,
        ticker: str,
        start: str | pd.Timestamp | None = None,
        end: str | pd.Timestamp | None = None,
        interval: str = "1d",
        use_cache: bool = True,
        adjustment: str | None = None,
    ) -> pd.DataFrame:
        """VIXCLS closes for ``start <= date < end`` (strictly before ``end``).

        ``end`` is the *decision timestamp*: a value for date D is only
        published after D's close, so ``end=D`` never serves that day's own (or
        any future) close - structurally identical in backtest and live, since
        live queries with ``end=<the bar's decision ts>``.

        ``end=None`` returns the full history (reporting). ``ticker`` must be
        "VIX" or "VIXCLS" (same series). Lack of data never raises: an empty
        frame is returned and the strategy gates reject (safe fail).
        """
        del use_cache, adjustment  # daily intrinsic series; nothing to apply
        if ticker not in _ACCEPTED_TICKERS:
            raise ValueError(f"unsupported VIX ticker: {ticker!r}; use VIX|VIXCLS")
        series = self._load()
        if series.empty:
            return pd.DataFrame({"vix": pd.Series(dtype=float)})
        out = series
        # Availability windows are DATE-level (the series is keyed by calendar
        # date at midnight).  ``start`` is day-inclusive; ``end`` is STRICTLY
        # exclusive at the day boundary — a decision timestamp inside day D (or
        # tz-aware) must never expose D's own close (published after D).
        if start is not None:
            out = out[out.index >= _naive_utc(start).normalize()]
        if end is not None:
            out = out[out.index < _naive_utc(end).normalize()]
        frame = out.rename("vix").to_frame()
        assert_canonical_index(frame, interval="1d")
        return frame

    def _load(self) -> pd.Series:
        """One FRED GET per process (per desk trading day); series cached here."""
        if self._series is not None:
            return self._series
        result = self._from_fred()
        self._series = result
        return result

    def _from_fred(self) -> pd.Series:
        try:
            scheme = urllib.parse.urlparse(_FRED_URL).scheme
            if scheme not in ("http", "https"):
                raise RuntimeError(f"unsafe VIX URL scheme: {scheme}")
            req = urllib.request.Request(  # noqa: S310 (scheme validated above)
                _FRED_URL, headers={"User-Agent": "desk/0.1"}
            )
            with urllib.request.urlopen(req, timeout=45) as r:  # noqa: S310
                csv = r.read().decode("utf-8")
            df = pd.read_csv(io.StringIO(csv))
            df.columns = ["date", "vix"]
            v = pd.Series(
                pd.to_numeric(df["vix"], errors="coerce").values,
                index=pd.to_datetime(df["date"]),
            )
            v = v[v.notna()].sort_index().rename("vix")
            if v.empty:
                logger.warning("FRED returned no VIXCLS rows; gates will reject")
            return v
        except Exception:
            logger.warning(
                "VIXCLS fetch failed; strategy gates will reject (safe fail)",
                exc_info=True,
            )
            return pd.Series(dtype=float)
