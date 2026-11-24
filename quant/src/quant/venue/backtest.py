"""BacktestVenue — a simulation-timeline venue (spec `venue`).

State is answered exclusively from the *injected* simulation ``now`` + the
configured window — never the host wall clock — so the same simulated
timestamp always yields the same open/closed answer, independent of when or
where the run executes (determinism). A ``calendar`` may mark specific days
closed (e.g. holidays); with no calendar the venue is open during its hours.
"""

from __future__ import annotations

import logging
from datetime import time
from typing import Any

from quant.venue.base import (
    BaseVenue,
    CalendarProtocol,
    DataSourceProtocol,
)


class BacktestVenue(BaseVenue):
    """Deterministic backtest venue: ``is_open_at(now)`` uses only simulated input."""

    id = "backtest"

    def __init__(
        self,
        data_source: DataSourceProtocol | None = None,
        *,
        calendar: CalendarProtocol | None = None,
        execution_client: Any = None,
        logger: logging.Logger | None = None,
        open_time: time = time(13, 30),
        close_time: time = time(20, 0),
        force_close_before_minutes: int = 15,
    ) -> None:
        super().__init__(
            data_source,
            calendar=calendar,
            execution_client=execution_client,
            logger=logger,
            default_open=open_time,
            default_close=close_time,
            default_force_close=close_time,
            force_close_before_minutes=force_close_before_minutes,
            treat_no_calendar_as_closed=False,
        )
