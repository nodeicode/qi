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
from typing import Any, Callable

from quant.order import Order, OrderPriceType
from quant.portfolio import Fill
from quant.venue.base import (
    BaseVenue,
    CalendarProtocol,
    DataSourceProtocol,
)


class BacktestExecutionClient:
    """Deterministic backtest execution client: typed ``Order`` -> simulated fill.

    ``price_source`` resolves the tradable price for an order at submission —
    the engine feeds the closed signal bar's tradable close, so the author
    body ran on adjusted bars while the fill prices in tradable space. A
    MARKET order fills at that price; a LIMIT order fills at the market price
    only when the limit is crossed (BUY: market <= limit; SELL: market >=
    limit). An order with no resolvable positive price, or a limit that is
    not crossed, reports ``None`` (not filled), so an unfulfillable order
    never touches the portfolio. When a ``portfolio`` is held, a reported
    fill is applied to it on submission (venue-owned fills feed the
    portfolio the strategy reads).
    """

    def __init__(
        self,
        *,
        price_source: Callable[[Order], float | None] | None = None,
        portfolio: Any = None,
        logger: logging.Logger | None = None,
    ) -> None:
        self._price_source: Callable[[Order], float | None] = price_source or (
            lambda _order: None
        )
        self.portfolio = portfolio
        self._log = logger or logging.getLogger(__name__)

    def submit(self, order: Order) -> Fill | None:
        """Simulate a fill for *order* at the tradable price, or ``None``."""
        market = self._price_source(order)
        if market is None or float(market) <= 0:
            return None
        fill_price = self._resolve_fill_price(order, float(market))
        if fill_price is None or fill_price <= 0:
            return None

        fill = Fill(
            venue=order.venue,
            symbol=order.instrument.symbol,
            side=order.side,
            qty=float(order.quantity),
            price=fill_price,
            strategy=order.strategy,
            instrument=order.instrument,
        )
        if self.portfolio is not None:
            self.portfolio.apply_fill(fill)
        return fill

    def _resolve_fill_price(self, order: Order, market: float) -> float | None:
        """Fill at *market* only when a limit order's limit is crossed."""
        if order.price_type is OrderPriceType.MARKET or order.price is None:
            return market
        if order.side.value == "BUY" and market <= float(order.price):
            return market
        if order.side.value == "SELL" and market >= float(order.price):
            return market
        return None


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
