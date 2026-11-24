"""Venue primitive (spec `venue`) — one swap-able unit that is simultaneously
the "broker" and the "market session" a strategy trades through.

Identity + session/calendar state + a market-data source + an execution client
in one object. Timezone lives inside the venue adapters (the Alpaca adapter
owns NY→UTC conversion); the decision boundary is always **UTC-naive**, which
``to_utc_naive`` enforces.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import UTC, date, datetime, time, timedelta
from typing import Any, Protocol, runtime_checkable

import pandas as pd

# US-equity fallback hours (generous defaults, logged when the calendar is
# unavailable) — mirrors the legacy ``MarketSchedule`` defaults.
DEFAULT_OPEN = time(13, 30)
DEFAULT_CLOSE = time(20, 0)
DEFAULT_FORCE_CLOSE = time(19, 45)
FALLBACK_FORCE_CLOSE_BEFORE_MINUTES = 15


@runtime_checkable
class DataSourceProtocol(Protocol):
    """Market-data source the engine reads bars through (Duck-typed)."""

    def get_historical_data(
        self,
        ticker: str,
        start,
        end,
        interval: str,
        use_cache: bool = True,
        adjustment: str | None = None,
    ) -> pd.DataFrame: ...


@runtime_checkable
class CalendarProtocol(Protocol):
    """Resolves a trading date's open/close window in UTC-naive time.

    A full-day holiday / closed date returns ``None``. The canonical timestamp
    convention (naive UTC across the decision boundary) lives here: adapters
    must convert source-local times to UTC and strip the tz before returning.
    """

    def open_close_for(self, on: date) -> tuple[datetime, datetime] | None:
        """Return `(open_utc_naive, close_utc_naive)` for *on*, or ``None`` if closed."""


def to_utc_naive(dt: datetime | pd.Timestamp) -> datetime:
    """Enforce the naive-UTC boundary convention. Returns a tz-naive datetime.

    Timezone-aware values are normalized to UTC and stripped; naive values are
    treated as already-UTC (never re-localized). This is the only sanctioned
    way for decision code to obtain a timestamp from a venue adapter.
    """
    if isinstance(dt, pd.Timestamp):
        ts = dt.tz_convert("UTC") if dt.tzinfo is not None else dt
        return ts.tz_localize(None).to_pydatetime()
    if dt.tzinfo is None:
        return dt
    return dt.astimezone(UTC).replace(tzinfo=None)


@dataclass(frozen=True)
class SessionDay:
    """The open/close/forced-close window for one trading day (UTC-naive)."""

    on: date
    open: datetime
    close: datetime
    forced_close: datetime
    open_on_date: bool = True

    def is_open(self, now: datetime | pd.Timestamp) -> bool:
        if not self.open_on_date:
            return False
        n = to_utc_naive(now)
        return self.open <= n <= self.close

    def allows_entry(self, now: datetime | pd.Timestamp) -> bool:
        """Entry is gated on the open window; ``forced_close`` only bounds exits."""
        return self.is_open(now)


class Venue(Protocol):
    """The cohesive broker-and-session unit (spec `venue`).

    A strategy/engine addresses all broker-and-session concerns through this
    object: ``data_source`` for bars, ``execution_client`` for orders,
    ``session_for``/``is_open_at`` for the calendar. ``id`` is stable per venue.
    """

    id: str
    data_source: DataSourceProtocol | None
    execution_client: Any

    def session_for(self, now: datetime | pd.Timestamp) -> SessionDay: ...
    def is_open_at(self, now: datetime | pd.Timestamp) -> bool: ...


class BaseVenue:
    """Shared session/calendar resolution for concrete venues.

    Recomputes on date rollover; falls back to documented default hours (with a
    log) when the calendar is unavailable or returns a closure. Subclasses set
    ``treat_no_calendar_as_closed``: live venues treat a failed lookup as
    closed-and-defaults; the backtest venue treats its simulation timeline as
    open-by-config.
    """

    id = "venue"

    def __init__(
        self,
        data_source: DataSourceProtocol | None = None,
        *,
        calendar: CalendarProtocol | None = None,
        execution_client: Any = None,
        logger: logging.Logger | None = None,
        default_open: time = DEFAULT_OPEN,
        default_close: time = DEFAULT_CLOSE,
        default_force_close: time = DEFAULT_FORCE_CLOSE,
        force_close_before_minutes: int = FALLBACK_FORCE_CLOSE_BEFORE_MINUTES,
        treat_no_calendar_as_closed: bool = True,
    ) -> None:
        self.data_source = data_source
        self.calendar = calendar
        self.execution_client = execution_client
        self._logger = logger or logging.getLogger(
            f"{__name__}.{self.__class__.__name__}"
        )
        self._default_open = default_open
        self._default_close = default_close
        self._default_force_close = default_force_close
        self._force_close_before_minutes = force_close_before_minutes
        self._treat_no_calendar_as_closed = treat_no_calendar_as_closed
        self._cached_date: date | None = None
        self._cached_session: SessionDay | None = None

    # ── session resolution ──────────────────────────────────────────────
    def session_for(self, now: datetime | pd.Timestamp) -> SessionDay:
        n = to_utc_naive(now)
        cached = self._cached_session
        if self._cached_date != n.date() or cached is None:
            cached = self._build_session(n.date())
            self._cached_date = n.date()
            self._cached_session = cached
        return cached

    def is_open_at(self, now: datetime | pd.Timestamp) -> bool:
        return self.session_for(now).is_open(now)

    def allows_entry_at(self, now: datetime | pd.Timestamp) -> bool:
        return self.session_for(now).allows_entry(now)

    # ── internals ───────────────────────────────────────────────────────
    def _build_session(self, on: date) -> SessionDay:
        raw = self._open_close_on(on)
        if raw is None:
            if self._treat_no_calendar_as_closed:
                self._logger.info(
                    "Market closed on %s (no calendar entry); using default hours", on
                )
            return SessionDay(
                on,
                _naive(on, self._default_open),
                _naive(on, self._default_close),
                _naive(on, self._default_force_close),
                open_on_date=not self._treat_no_calendar_as_closed,
            )
        open_dt, close_dt = raw
        forced = close_dt - timedelta(minutes=self._force_close_before_minutes)
        return SessionDay(on, open_dt, close_dt, forced, open_on_date=True)

    def _open_close_on(self, on: date) -> tuple[datetime, datetime] | None:
        if self.calendar is None:
            return None
        try:
            return self.calendar.open_close_for(on)
        except Exception as exc:  # pragma: no cover - defensive boundary
            self._logger.warning(
                "calendar lookup failed for %s (%s); falling back to defaults", on, exc
            )
            return None


def _naive(on: date, t: time) -> datetime:
    return datetime.combine(on, t)
