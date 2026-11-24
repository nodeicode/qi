"""AlpacaVenue — the live venue: calendar + REST data + execution client.

Timezone (NY→UTC) is owned inside this adapter, so the decision boundary stays
naive-UTC. The calendar adapter wraps the Alpaca Calendar API; the data source
is an ``AlpacaDataHandler``; the execution client is an ``alpaca_py`` trading
client. All three are injectable so the venue is testable without keys.
"""

from __future__ import annotations

import logging
from datetime import date, datetime
from typing import Any

import pandas as pd

from quant.venue.base import (
    DEFAULT_CLOSE,
    DEFAULT_FORCE_CLOSE,
    DEFAULT_OPEN,
    BaseVenue,
    CalendarProtocol,
    DataSourceProtocol,
)


class AlpacaCalendar(CalendarProtocol):
    """Wraps the Alpaca Calendar API into naive-UTC open/close windows.

    ``open_close_for`` resolves a date's calendar entry and converts the
    NY-local open/close into UTC-naive times (handling DST/holidays/early
    closes that Alpaca's calendar already encodes). A day with no calendar
    entry is a market closure — returns ``None``.
    """

    def __init__(self, client: Any, *, logger: logging.Logger | None = None) -> None:
        self._client = client
        self._logger = logger or logging.getLogger(__name__)

    def open_close_for(self, on: date) -> tuple[datetime, datetime] | None:
        from alpaca.trading.requests import GetCalendarRequest

        req = GetCalendarRequest(start=on, end=on)
        raw = self._client.get_calendar(req)
        calendar_list = raw if isinstance(raw, list) else []
        if not calendar_list:
            return None
        cal = calendar_list[0]
        ny_open = pd.Timestamp(cal.open, tz="America/New_York")
        ny_close = pd.Timestamp(cal.close, tz="America/New_York")
        return (
            ny_open.tz_convert("UTC").tz_localize(None).to_pydatetime(),
            ny_close.tz_convert("UTC").tz_localize(None).to_pydatetime(),
        )


class AlpacaVenue(BaseVenue):
    """Live equity venue.

    Session = Alpaca calendar (with fallback); data = ``AlpacaDataHandler``;
    execution = a trading client. ``id`` is the stable ``"alpaca"``.
    """

    id = "alpaca"

    def __init__(
        self,
        data_source: DataSourceProtocol | None = None,
        *,
        calendar: CalendarProtocol | None = None,
        execution_client: Any = None,
        logger: logging.Logger | None = None,
    ) -> None:
        super().__init__(
            data_source,
            calendar=calendar,
            execution_client=execution_client,
            logger=logger,
            default_open=DEFAULT_OPEN,
            default_close=DEFAULT_CLOSE,
            default_force_close=DEFAULT_FORCE_CLOSE,
            treat_no_calendar_as_closed=True,
        )
