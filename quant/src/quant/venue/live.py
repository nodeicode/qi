"""Live venue execution client — the live venue's broker-bound client.

The live layer's ``LiveExecutor`` / ``UnifiedExecutor`` pair
(``quant/live/executors/*``, deleted) reshapes into this single object, the
execution client the live venue (``AlpacaVenue``) holds alongside its session
calendar and data source. It owns:

* **gated submission** — a typed order is checked against the venue's session
  (market open / entry window) before any broker call; a gated order returns
  ``None`` (rejected in the decision transcript) and never reaches the broker
  or the Portfolio,
* **fill reconciliation** — after placement the venue reports the order's
  outcome (full / partial fill, filled average price, remaining quantity),
  tracked per order id and reported via :meth:`LiveExecutionClient.order_report`,
* **portfolio feed** — every reconciled fill (partial or full) streams through
  the same ``Portfolio`` machinery the backtest venue uses, so the strategy
  reads live fills in the identical position schema (parity by construction).

The shared ``TradingEngine.process_strategy`` loop drives ordering solely
through :meth:`submit` (the typed-order seam); the legacy scheduling/gating
duck surface (``should_run`` / ``data_end_dt`` / ``get_precomputed_signal`` /
``active_positions`` / ``enter`` / ``exit`` / lifecycle hooks / ``record_run``)
is deleted with the engine phasing it served — the engine gates by
per-subscription cadence plus the venue's session calendar.

Failures surface as errors: a broker or reconciliation failure raises rather
than being silently swallowed; only a *gated/rejected* order returns ``None``.
"""

from __future__ import annotations

import datetime
import logging
import threading
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from quant.order import Order, OrderPriceType
from quant.portfolio import Fill, Side


@dataclass(frozen=True)
class FillReport:
    """Reconciled outcome of one submitted live order (venue-reported).

    ``requested_qty`` is what the broker accepted, ``filled_qty`` what actually
    printed (equal for a full fill, less for a partial fill), ``remaining_qty``
    what is still open on the order, ``status`` the venue's order status, and
    ``avg_price`` the venue-reported average fill price (``None`` when the
    venue did not surface one).
    """

    order_id: str
    status: str
    requested_qty: float
    filled_qty: float
    remaining_qty: float
    avg_price: float | None


class LiveExecutionClient:
    """Live execution client: gated submission + partial-fill reconciliation.

    All external bindings are injected explicitly (venue, broker clients,
    strategy basket, portfolio, logger, lock, persistence-free) so the client
    is self-contained and testable without broker credentials.
    """

    def __init__(
        self,
        *,
        venue=None,
        clients: dict[str, Any] | None = None,
        strategies: list[Any] | None = None,
        portfolio: Any | None = None,
        logger: logging.Logger | None = None,
        lock: threading.RLock | None = None,
        fill_resolver: Callable[[str, Order, Any], FillReport] | None = None,
        price_source: Callable[[Order], float | None] | None = None,
    ) -> None:
        self.venue = venue
        # Shared collections are kept by reference (when passed) so the trader's
        # live mutation (config-sync strategy churn) flows through the same
        # objects instead of silently forking.
        self._clients = clients if clients is not None else {}
        self._strategies = strategies if strategies is not None else []
        self._portfolio = portfolio
        self._log = logger or logging.getLogger(__name__)
        self._lock = lock or threading.RLock()
        self._fill_resolver = fill_resolver
        self._price_source = price_source
        # order_id -> FillReport: reconciled (incl. partial) fill outcomes.
        self._reports: dict[str, FillReport] = {}

    # ── Typed-order seam (spec `execution` Live orders … venue-bound clients)
    def submit(
        self,
        order: Order,
        *,
        now=None,
        cadence: str | None = None,
        reference_price: float | None = None,
    ) -> Fill | None:
        """Gate, place, and reconcile *order*.

        Verifies the venue session (market open / entry window) before any
        broker call — a gated order returns ``None`` (rejected) and never
        reaches the broker or the Portfolio. ``now`` is the tick timestamp the
        session gate is checked against (``None`` skips the clock gate — the
        broker still validates); ``cadence`` is accepted for shared-loop parity
        with the backtest client and is not used live. The venue's reported
        fill (full or partial) is applied to the Portfolio and returned as a
        ``Fill`` bearing the filled quantity. Broker/reconciliation failures
        raise so they surface as errors rather than silent no-ops.
        """
        allowed, reason = self._gate_allows(order, now)
        if not allowed:
            self._log.info(
                "[%s] order %s rejected: %s",
                order.strategy,
                order.instrument.symbol,
                reason,
            )
            return None
        if float(order.quantity) < 1:
            self._log.warning(
                "[%s] order %s has qty %s < 1 — skipped",
                order.strategy,
                order.instrument.symbol,
                order.quantity,
            )
            return None
        try:
            return self._place_and_reconcile(order, now, reference_price)
        except Exception:
            self._log.exception(
                "[%s] live submission failed for %s",
                order.strategy,
                order.instrument.symbol,
            )
            raise

    def order_report(self, order_id: str) -> FillReport | None:
        """Reconciled outcome of the (possibly partial) submitted *order_id*."""
        return self._reports.get(order_id)

    # ── Internals ─────────────────────────────────────────────────────────
    def _gate_allows(self, order: Order, now) -> tuple[bool, str]:
        """True when the venue session permits an entry at *now*.

        Fails closed: when the session lookup itself errors (calendar down),
        the order is rejected rather than placed into an unknown market. ``now``
        is ``None`` only for deferred calls made outside a tick — those skip the
        clock gate (the broker still validates).
        """
        venue = self._venue_or_none()
        if venue is None or now is None:
            return True, ""
        try:
            if not venue.allows_entry_at(now):
                return False, (
                    f"venue '{getattr(venue, 'id', 'live')}' is not open for "
                    f"entries at {now}"
                )
        except Exception as exc:  # calendar failure is not a trading signal
            self._log.warning(
                "session gate failed at %s (%s); rejecting order %s",
                now,
                exc,
                order.instrument.symbol,
            )
            return False, f"session lookup failed: {exc}"
        return True, ""

    def _place_and_reconcile(
        self, order: Order, now, reference_price: float | None
    ) -> Fill:
        """Place the order on the broker and apply its reconciled fill."""
        client = self._client_for_order(order)
        if client is None:
            raise RuntimeError(
                f"no broker client to place order for {order.instrument.symbol}"
            )
        order_id = self._submit_to_broker(order, client)
        # Reconciliation is an injected resolver (the broker's async order
        # surface, or an offline stub). With no resolver, an accepted order is
        # treated as a full fill at the resolved price — the client never
        # invents a partial-fill split without a venue to report it.
        report = (
            self._fill_resolver(order_id, order, client)
            if self._fill_resolver is not None
            else FillReport(
                order_id, "FILLED", order.quantity, order.quantity, 0.0, None
            )
        )
        self._reports[order_id] = report
        if report.filled_qty <= 0:
            raise RuntimeError(
                f"order {order_id} for {order.instrument.symbol} reported no "
                f"fill (status={report.status})"
            )
        price = report.avg_price
        if price is None:
            price = reference_price
        if price is None and order.price_type is OrderPriceType.LIMIT:
            price = order.price
        if price is None and self._price_source is not None:
            price = self._price_source(order)
        if price is None or float(price) <= 0:
            raise RuntimeError(
                f"order {order_id} filled without a reportable positive price"
            )
        fill = Fill(
            venue=order.venue,
            symbol=order.instrument.symbol,
            side=order.side,
            qty=float(report.filled_qty),
            price=float(price),
            strategy=order.strategy,
            instrument=order.instrument,
            timestamp=now,
            order_id=order_id,
        )
        if self._portfolio is not None:
            self._portfolio.apply_fill(fill)
        return fill

    def _submit_to_broker(self, order: Order, client: Any) -> str:
        """Place the (market or limit) order; return the broker order id."""
        from alpaca.trading.requests import (  # heavy import — keep lazy
            LimitOrderRequest,
            MarketOrderRequest,
            OrderSide,
            TimeInForce,
        )

        side_enum = OrderSide.BUY if order.side is Side.BUY else OrderSide.SELL
        common = {
            "symbol": order.instrument.symbol,
            "qty": order.quantity,
            "side": side_enum,
            "time_in_force": TimeInForce.DAY,
        }
        if order.price_type is OrderPriceType.LIMIT and order.price is not None:
            req = LimitOrderRequest(**common, limit_price=float(order.price))
        else:
            req = MarketOrderRequest(**common)
        submitted = client.submit_order(req)
        if isinstance(submitted, dict):
            order_id = str(submitted.get("id", "") or "")
        else:
            order_id = str(getattr(submitted, "id", "") or "")
        if not order_id:
            raise RuntimeError(
                f"broker returned no order id for {order.instrument.symbol}"
            )
        return order_id

    def _client_for(self, strategy) -> Any | None:
        """Broker client bound to *strategy*'s declared account, or None."""
        name = getattr(strategy, "client_name", None)
        if name is None:
            return None
        return self._clients.get(name)

    def _client_for_order(self, order: Order) -> Any | None:
        """Broker client for an order's declaring strategy (first-client fallback)."""
        if order.strategy is not None:
            for s in self._strategies:
                if s.name == order.strategy:
                    return self._client_for(s)
        # Unknown strategy name (tests / ad-hoc declarative orders) routes to
        # the first registered client; an empty ledger fails loudly upstream.
        return next(iter(self._clients.values()), None)

    def _venue_id(self) -> str:
        """Stable venue id stamped on venue-scoped instruments."""
        venue = self._venue_or_none()
        return getattr(venue, "id", "alpaca") if venue is not None else "alpaca"

    def _venue_or_none(self):
        """The live venue currently bound (swap-able for a session stub)."""
        return self.venue

    @staticmethod
    def _coerce_now(now: datetime.datetime) -> datetime.datetime:
        """Canonicalise a scalar timestamp to tz-naive UTC (engine contract)."""
        if getattr(now, "tzinfo", None) is not None:
            return now.astimezone(datetime.UTC).replace(tzinfo=None)
        return now
