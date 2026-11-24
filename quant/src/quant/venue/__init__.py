"""Venue primitive (spec `venue`) — the #2 counted primitive.

One swap-able unit that is simultaneously the "broker" and the "market session"
a strategy trades through: identity + session/calendar state + market-data
source + execution client. ``AlpacaVenue`` is the live default; ``BacktestVenue``
answers from the simulation timeline (determinism). Timezone lives inside the
adapters; the decision boundary is naive-UTC (``to_utc_naive``).
"""

from quant.venue._registry import register_venue, venue_by_name
from quant.venue.alpaca import AlpacaCalendar, AlpacaVenue
from quant.venue.backtest import BacktestVenue
from quant.venue.base import (
    DEFAULT_CLOSE,
    DEFAULT_FORCE_CLOSE,
    DEFAULT_OPEN,
    BaseVenue,
    CalendarProtocol,
    DataSourceProtocol,
    SessionDay,
    Venue,
    to_utc_naive,
)

__all__ = [
    "DEFAULT_CLOSE",
    "DEFAULT_FORCE_CLOSE",
    "DEFAULT_OPEN",
    "AlpacaCalendar",
    "AlpacaVenue",
    "BacktestVenue",
    "BaseVenue",
    "CalendarProtocol",
    "DataSourceProtocol",
    "SessionDay",
    "Venue",
    "register_venue",
    "to_utc_naive",
    "venue_by_name",
]
