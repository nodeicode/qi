"""Live venue execution client tests (spec `execution` Live orders …).

Covers the tasks that migrated off the deleted ``LiveExecutor`` /
``UnifiedExecutor`` pair onto ``quant.venue.live.LiveExecutionClient``:

* **gated submission** — a typed order is checked against the venue session
  (market open / entry window) before any broker call; a gated order returns
  ``None`` and never reaches the broker,
* **partial-fill tracking** — a partially-filled order reports remaining
  quantity + status (via :meth:`order_report`) and the Portfolio the strategy
  reads reflects the actual filled quantity,
* **failures surface as errors** — a broker/reconciliation failure raises
  instead of being a silent no-op,
* full-fill + limit-order placement through the typed seam.

(The legacy bar-driven scheduling duck surface the shared engine loop used to
call — ``should_run`` / ``data_end_dt`` / ``record_run`` / ``after_entry`` /
``get_precomputed_signal`` / ``enter`` — was deleted with the engine phasing;
scheduling now lives in the engine's per-subscription cadence + venue session.)
"""

from __future__ import annotations

import datetime
import threading
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from quant.instruments import EquityInstrument
from quant.order import OrderPriceType
from quant.portfolio import Portfolio
from quant.venue.live import FillReport, LiveExecutionClient

_SPY = EquityInstrument(symbol="SPY", venue="alpaca")
_NOW = datetime.datetime(2026, 1, 1, 15, 0)


def _venue(is_open: bool = True):
    return SimpleNamespace(
        id="alpaca",
        is_open_at=lambda now: is_open,
        allows_entry_at=lambda now: is_open,
        session_for=lambda now: None,
    )


def _client(
    portfolio=None,
    *,
    is_open: bool = True,
    fill_resolver=None,
    price_source=100.0,
):
    """Build a LiveExecutionClient with a real broker mock (full-fill default)."""
    broker = MagicMock()
    broker.submit_order.return_value = MagicMock(id="o1")
    client = LiveExecutionClient(
        venue=_venue(is_open=is_open),
        clients={"paper": broker},
        strategies=[],
        portfolio=portfolio,
        logger=MagicMock(),
        lock=threading.RLock(),
        fill_resolver=fill_resolver,
        price_source=(lambda order: price_source) if price_source is not None else None,
    )
    return client, broker


# ===========================================================================
# Gated submission (spec scenario)
# ===========================================================================


class TestGatedSubmission:
    def test_open_venue_places_order(self) -> None:
        client, broker = _client()
        order = _SPY.make_order("BUY", 10, strategy="s")

        fill = client.submit(order, now=_NOW)

        assert (
            fill is not None and fill.qty == 10 and fill.price == pytest.approx(100.0)
        )
        broker.submit_order.assert_called_once()

    def test_closed_venue_rejects_without_broker_call(self) -> None:
        client, broker = _client(is_open=False)
        order = _SPY.make_order("BUY", 10, strategy="s")

        assert client.submit(order, now=_NOW) is None
        broker.submit_order.assert_not_called()

    def test_limit_order_fills(self) -> None:
        client, _ = _client()
        order = _SPY.make_order(
            "BUY", 5, price=101.0, price_type=OrderPriceType.LIMIT, strategy="s"
        )

        fill = client.submit(order, now=_NOW)

        # A limit order still places and fills through the typed seam.
        assert fill is not None and fill.qty == 5
        assert fill.price == pytest.approx(101.0)  # limit price is the reference


# ===========================================================================
# Partial fill tracked (spec scenario)
# ===========================================================================


class TestPartialFillTracking:
    def test_partial_fill_feeds_portfolio_and_reports_remaining(self) -> None:
        pf = Portfolio(starting_equity=100_000.0)
        client, _ = _client(
            portfolio=pf,
            fill_resolver=lambda oid, order, client: FillReport(
                "o1", "PARTIALLY_FILLED", order.quantity, 4.0, 6.0, 100.0
            ),
        )
        order = _SPY.make_order("BUY", 10, strategy="s")

        fill = client.submit(order, now=_NOW)

        assert fill is not None and fill.qty == 4.0
        # The Portfolio the strategy reads reflects the filled quantity only.
        pos = pf.position("alpaca", "SPY")
        assert pos is not None and pos.qty == 4.0
        # Remaining quantity + status tracked and reported by the client.
        report = client.order_report("o1")
        assert report is not None
        assert report.status == "PARTIALLY_FILLED"
        assert report.remaining_qty == 6.0


# ===========================================================================
# Failures surface as errors (spec — no silent no-ops)
# ===========================================================================


class TestFailuresSurface:
    def test_broker_exception_raises(self) -> None:
        client, broker = _client()
        broker.submit_order.side_effect = RuntimeError("broker down")

        with pytest.raises(RuntimeError):
            client.submit(_SPY.make_order("BUY", 10, strategy="s"), now=_NOW)

    def test_order_with_no_reported_fill_raises(self) -> None:
        client, _ = _client(
            fill_resolver=lambda oid, order, client: FillReport(
                oid, "REJECTED", order.quantity, 0.0, order.quantity, None
            )
        )

        with pytest.raises(RuntimeError, match="no fill"):
            client.submit(_SPY.make_order("BUY", 10, strategy="s"), now=_NOW)

    def test_no_broker_client_for_order_raises(self) -> None:
        local_client, _ = _client()
        local_client._clients = {}
        with pytest.raises(RuntimeError, match="no broker client"):
            local_client.submit(_SPY.make_order("BUY", 10, strategy="s"), now=_NOW)
