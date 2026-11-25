"""Shared time / bar-clock math — leaf home in ``quant.utils``.

Consumed by the engine (``closed_bar_asof`` / ``bar_close_utc``), the live
layer (``calculate_entry_time``), and the backtest fill clock.  ``quant.utils``
is a leaf package — this module imports nothing from ``quant.core``.

``quant/data/_times.py`` intentionally stays separate: it is the data-handler
canonical-timestamp boundary (intraday tz-naive-UTC contract for feeds), a
different concern from the bar/entry-time math hosted here.
"""

from __future__ import annotations

import datetime
import zoneinfo
from typing import ClassVar, Literal, Protocol, overload

import pandas as pd

_ET = zoneinfo.ZoneInfo("America/New_York")


def et_wall_to_utc_naive(
    entry_date: datetime.date,
    hour: int,
    minute: int = 0,
) -> pd.Timestamp:
    """Convert an ET wall clock on *entry_date* to UTC-naive (DST-safe).

    Naive output means **UTC wall clock**, never ET.  Summer/winter offsets
    come from ``America/New_York`` (e.g. 10:00 ET → 14:00 UTC EDT, 15:00 UTC EST).
    """
    ts_et = pd.Timestamp(
        year=entry_date.year,
        month=entry_date.month,
        day=entry_date.day,
        hour=int(hour),
        minute=int(minute),
        second=0,
        tz=_ET,
    )
    return ts_et.tz_convert("UTC").tz_localize(None)


_DAILY_INTERVALS = frozenset({"1d", "1wk", "1mo"})

_INTRADAY_MINUTE = ("1m", "5m", "15m", "30m", "60m")
_INTRADAY_HOUR = ("1h", "2h", "4h")


def _interval_timedelta(interval: str) -> datetime.timedelta:
    """Parse ``1m`` / ``15m`` / ``1h`` / ``1d`` into a timedelta."""
    try:
        unit = interval[-1].lower()
        value = int(interval[:-1])
        if unit == "m":
            return datetime.timedelta(minutes=value)
        if unit == "h":
            return datetime.timedelta(hours=value)
        if unit == "d":
            return datetime.timedelta(days=value)
    except (TypeError, ValueError, IndexError):
        pass
    return datetime.timedelta(minutes=60)


def _interval_floor_freq(interval: str) -> str:
    """Pandas offset alias for flooring bar open stamps."""
    unit = interval[-1].lower()
    value = int(interval[:-1])
    if unit == "m":
        return f"{value}min"
    if unit == "h":
        return f"{value}h"
    if unit == "d":
        return f"{value}D"
    return "60min"


def closed_bar_asof(end_dt, interval: str) -> pd.Timestamp:
    """Return the open-stamp of the last fully closed bar as of *end_dt*.

    A bar stamped ``T`` covers ``[T, T+I)`` and is closed when
    ``end_dt >= T+I``.  For daily bars, the session bar is incomplete
    before 16:00 ET (same rule as ``_trim_incomplete_daily_bar``).

    **Equity-session assumption:** the 16:00 ET daily-completeness rule is
    the US-equity session convention.  Futures strategies trade intraday
    intervals (where the intraday branch below applies and no session logic
    is involved); a daily-interval futures strategy must supply its own
    completeness rule when it lands.
    """
    end = pd.Timestamp(end_dt)
    delta = _interval_timedelta(interval)

    if interval in _DAILY_INTERVALS:
        if end.tzinfo is not None:
            end_et = end.tz_convert(_ET)
        else:
            # Treat naive as UTC wall (engine/live convention).
            end_et = end.tz_localize("UTC").tz_convert(_ET)
        day = end_et.normalize()
        if end_et.hour < 16:
            day = day - pd.Timedelta(days=1)
        # Return tz-naive midnight UTC matching typical daily index stamps,
        # or keep ET-normalized date as UTC-naive date midnight.
        return pd.Timestamp(day.date())

    freq = _interval_floor_freq(interval)
    return (end - delta).floor(freq)


def bar_close_utc(bar_open, interval: str) -> pd.Timestamp:
    """Return the UTC instant at which the bar stamped *bar_open* closed.

    Inverse of ``closed_bar_asof``: intraday bars close at ``open + interval``,
    while daily-like bars close at 16:00 ET on the last session they cover —
    the same completeness rule ``closed_bar_asof`` applies.  Using
    ``open + 1 day`` for daily bars would place the close ~8 hours late and
    understate staleness.

    **Equity-session assumption:** the 16:00 ET daily close is the
    US-equity convention (see ``closed_bar_asof``).
    """
    ts = pd.Timestamp(bar_open)

    if interval in _DAILY_INTERVALS:
        # Daily stamps are session dates (tz-naive midnight) by convention.
        session = (ts.tz_convert(_ET) if ts.tzinfo is not None else ts).normalize()
        if interval == "1wk":
            session = session + pd.Timedelta(days=4)
        elif interval == "1mo":
            session = session + pd.offsets.MonthEnd(0)
        if session.tzinfo is None:
            session = session.tz_localize(_ET)
        return (session + pd.Timedelta(hours=16)).tz_convert("UTC")

    if ts.tzinfo is None:
        ts = ts.tz_localize("UTC")
    else:
        ts = ts.tz_convert("UTC")
    return ts + _interval_timedelta(interval)


def calculate_entry_time(signal_timestamp, interval: str) -> pd.Timestamp:
    """Bar-close / undelayed entry clock after a bar at ``T`` with interval ``I``.

    A bar stamped at ``T`` is not complete until ``T+I``.  Daily-ish intervals
    enter at the next session open — the equity clock (next business day
    09:30 ET → UTC-naive) is the default; session-aware variants plug in via
    :data:`quant.utils.time.SESSION_CLOCKS`.

    For SIP/feed delay, prefer :func:`quant.utils.fills.calculate_fill_time`
    (``T+I+DATA_DELAY``) so backtest fills match live wall when the delayed bar
    is first known.
    """
    ts = pd.Timestamp(signal_timestamp)
    if interval in _INTRADAY_MINUTE:
        return ts + datetime.timedelta(minutes=int(interval[:-1]))
    if interval in _INTRADAY_HOUR:
        return ts + datetime.timedelta(hours=int(interval[:-1]))
    # 1d / 1wk / 1mo (and any other non-intraday): next session open.
    # Session-agnostic bar-close math is intraday-only; the daily branch is
    # delegated to the equity session clock (verbatim behavior).
    clock = SESSION_CLOCKS.get("equity", strict=True)
    return clock.next_open_after(ts)


class SessionClock(Protocol):
    """Market-session rules for one instrument family.

    Only the question the engine actually asks is in the protocol: *when does
    the next tradable session open after a timestamp?*  Implementors are
    registered in :data:`SESSION_CLOCKS` keyed by ``name``; the equity clock
    is the default and its behavior is byte-identical to the historical
    ``calculate_entry_time`` daily branch.
    """

    name: ClassVar[str]

    def next_open_after(self, ts) -> pd.Timestamp:
        """Return the next session-open instant at/after *ts*.

        Convention: UTC-naive wall clock (same convention as equity entry
        timestamps throughout the engine/backtest).
        """
        ...


class EquitySessionClock:
    """US equities: 09:30 ET open on the next business day (verbatim port of
    ``backtest.utils.calculate_entry_time``'s daily branch)."""

    name: ClassVar[str] = "equity"

    def next_open_after(self, ts) -> pd.Timestamp:
        next_open = (pd.Timestamp(ts).normalize() + pd.offsets.BDay(1)).date()
        return et_wall_to_utc_naive(next_open, 9, 30)


class FuturesSessionClock:
    """CME Globex futures model: Sun 18:00 ET → Fri 17:00 ET continuous;
    Fri 17:00 → Sun 18:00 closed; next open resolves to the next 18:00 ET
    session boundary (Sun 18:00 after a weekend).

    Approximates the index/SOFR/treasury Globex schedules (which differ in
    daily maintenance breaks per product); refine per product when futures
    strategies need sub-hour precision.
    """

    name: ClassVar[str] = "cme-futures"

    def next_open_after(self, ts) -> pd.Timestamp:
        et = pd.Timestamp(ts)
        if et.tzinfo is not None:
            et = et.tz_convert(_ET)
        else:
            # Naive treated as UTC wall — engine/backtest convention.
            et = et.tz_localize("UTC").tz_convert(_ET)

        day = et.normalize()
        wd = et.weekday()  # Mon=0 … Sun=6
        at = et - day
        open_1800 = datetime.timedelta(hours=18)

        if wd == 6:  # Sunday
            if at < open_1800:
                target = day.date()
            else:
                target = (day + pd.Timedelta(days=1)).date()
        elif wd == 5:  # Saturday → Sunday 18:00
            target = (day + pd.Timedelta(days=1)).date()
        elif wd == 4 and at >= datetime.timedelta(hours=17):  # Fri after 17:00 ET
            # Tradable week is done: next session opens Sunday 18:00 ET.
            target = (day + pd.Timedelta(days=2)).date()
        else:  # Mon-Thu, or Fri before 17:00 -> next 18:00 boundary
            target = (
                day.date() if at < open_1800 else (day + pd.Timedelta(days=1)).date()
            )

        return et_wall_to_utc_naive(target, 18, 0)


class SessionClockRegistry:
    """Registry of :class:`SessionClock` implementations keyed by name."""

    def __init__(self) -> None:
        self._clocks: dict[str, SessionClock] = {}

    def register(self, clock: SessionClock) -> None:
        self._clocks[clock.name] = clock

    @overload
    def get(self, name: str, *, strict: Literal[True]) -> SessionClock: ...

    @overload
    def get(
        self, name: str, *, strict: Literal[False] = False
    ) -> SessionClock | None: ...

    def get(self, name: str, *, strict: bool = False) -> SessionClock | None:
        """Look up a session clock by name.

        Pass ``strict=True`` for a guaranteed-registered name (raises on
        typo); the return type is non-None then.  ``strict=False`` (default)
        returns ``None`` for unregistered names.
        """
        try:
            return self._clocks[name]
        except KeyError:
            if strict:
                raise KeyError(
                    f"No SessionClock registered as {name!r}. "
                    f"Registered: {sorted(self._clocks) or '(none)'}"
                ) from None
            return None


SESSION_CLOCKS: SessionClockRegistry = SessionClockRegistry()
SESSION_CLOCKS.register(EquitySessionClock())
SESSION_CLOCKS.register(FuturesSessionClock())
