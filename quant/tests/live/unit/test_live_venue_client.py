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
* full-fill + limit-order placement through the typed seam, and
* the bar-driven **scheduling duck surface** the shared loop drives.
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
    equity: float | None = None,
):
    """Build a LiveExecutionClient with a real broker mock (full-fill default)."""
    broker = MagicMock()
    broker.submit_order.return_value = MagicMock(id="o1")
    kwargs = {}
    if equity is not None:
        em = MagicMock()
        em.get.return_value = equity
        em.set_active.return_value = True
        kwargs["equity_mgr"] = em
    client = LiveExecutionClient(
        venue=_venue(is_open=is_open),
        clients={"paper": broker},
        strategies=[],
        active_positions={},
        portfolio=portfolio,
        logger=MagicMock(),
        lock=threading.RLock(),
        save_state=MagicMock(),
        fill_resolver=fill_resolver,
        price_source=(lambda order: price_source) if price_source is not None else None,
        **kwargs,
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

    def test_duck_entry_records_partial_fill_in_position_schema(self) -> None:
        pf = Portfolio(starting_equity=100_000.0)
        client, _ = _client(
            portfolio=pf,
            equity=50_000.0,
            fill_resolver=lambda oid, order, client: FillReport(
                oid, "PARTIALLY_FILLED", order.quantity, order.quantity - 6, 6.0, 100.0
            ),
        )
        strat = MagicMock()
        strat.name = "S"
        strat.client_name = "paper"
        strat.env = "paper"
        strat.config.CAPITAL_PER_TRADE = 0.1
        engine = MagicMock()
        engine.direction.return_value = 1
        engine.build_position.side_effect = lambda *a, **kw: dict(kw)

        pos = client.enter(
            strat, "SPY", {"Signal": "BUY", "Close": 100.0, "Confidence": 0.9}, engine
        )

        assert pos is not None
        assert pos["OrderId"] == "o1"
        assert pos["OrderStatus"] == "PARTIALLY_FILLED"
        assert pos["FilledQty"] == pos["Qty"]
        assert pos["RemainingQty"] == 6.0
        assert pf.position("alpaca", "SPY").qty == pos["Qty"]


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


# ===========================================================================
# Bar-driven scheduling duck surface (shared engine loop)
# ===========================================================================


class TestSchedulingSurface:
    def _strat(
        self,
        name: str = "S",
        interval: str = "15m",
        manage_interval: str | None = None,
        data_delay: int = 0,
    ):
        s = MagicMock()
        s.name = name
        s.client_name = "paper"
        s.config.INTERVAL = interval
        s.config.MANAGE_INTERVAL = manage_interval or interval
        s.config.DATA_DELAY = data_delay
        return s

    def test_first_tick_fires_both(self) -> None:
        client, _ = _client()
        now = datetime.datetime(2024, 6, 1, 14, 20, tzinfo=datetime.UTC)
        assert client.should_run(self._strat(), now) == (True, True)

    def test_same_closed_bar_suppressed(self) -> None:
        client, _ = _client()
        strat = self._strat()
        now = datetime.datetime(2024, 6, 1, 14, 20, tzinfo=datetime.UTC)
        client.last_update[strat.name] = datetime.datetime(
            2024, 6, 1, 14, 16, tzinfo=datetime.UTC
        )
        assert client.should_run(strat, now) == (False, False)

    def test_new_closed_bar_fires(self) -> None:
        client, _ = _client()
        strat = self._strat()
        now = datetime.datetime(2024, 6, 1, 14, 30, tzinfo=datetime.UTC)
        client.last_update[strat.name] = datetime.datetime(
            2024, 6, 1, 14, 16, tzinfo=datetime.UTC
        )
        assert client.should_run(strat, now) == (True, True)

    def test_manage_fires_independently_on_finer_bar(self) -> None:
        client, _ = _client()
        strat = self._strat("S", interval="1h", manage_interval="5m", data_delay=0)
        now = datetime.datetime(2024, 6, 1, 14, 25, tzinfo=datetime.UTC)
        client.last_update[strat.name] = datetime.datetime(
            2024, 6, 1, 14, 5, tzinfo=datetime.UTC
        )
        client.last_manage_update[strat.name] = datetime.datetime(
            2024, 6, 1, 14, 16, tzinfo=datetime.UTC
        )

        should_signal, should_manage = client.should_run(strat, now)

        assert should_signal is False
        assert should_manage is True

    def test_data_end_dt_uses_per_strategy_delay(self) -> None:
        client, _ = _client()
        strat = self._strat(data_delay=0)
        now = datetime.datetime(2024, 6, 1, 14, 20)  # canonical naive UTC
        client.should_run(strat, now)  # records this thread's delay
        assert client.data_end_dt(now) == now

    def test_record_run_stamps_signal_and_manage(self) -> None:
        client, _ = _client()
        now = datetime.datetime(2024, 6, 1, 12, 0)  # canonical naive UTC
        client.record_run("S", now, ran_signal=True)
        assert client.last_update["S"] == now
        assert client.last_manage_update["S"] == now

    def test_after_entry_appends_and_persists(self) -> None:
        client, _ = _client()
        position = {"Ticker": "SPY", "OrderId": "new-1"}
        client.after_entry("S", position)
        assert position in client._active_positions["S"]

    def test_get_precomputed_signal_is_none_live(self) -> None:
        client, _ = _client()
        assert client.get_precomputed_signal("SPY", None) is None
