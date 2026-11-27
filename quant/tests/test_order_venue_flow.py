"""Venue is the order seam (spec `strategy-authoring` / `execution`).

Verifies the order flow: the author body builds typed ``Order`` objects
through ``instrument.make_order``, the engine submits them to the declared
venue's execution client, and the venue's reported fills feed the Portfolio
the strategy reads. The strategy surface holds only the venue declaration and
typed seams — no broker SDK is reachable. Covers the backtest execution
client's market/limit fill semantics and the no-fill (``None``) paths.
"""

from __future__ import annotations

from typing import ClassVar

import pytest

from quant.core.engine import TradingEngine
from quant.data import Datatype, Subscription
from quant.instruments import EquityInstrument, FutureInstrument
from quant.order import Order, OrderPriceType
from quant.portfolio import Portfolio, Side
from quant.strategies.base import Strategy
from quant.utils.strategy_config import BaseStrategyConfig
from quant.venue import BacktestExecutionClient, BacktestVenue

_ES = FutureInstrument(symbol="ES", venue="backtest", multiplier=50.0, margin=12_000.0)
_SPY = EquityInstrument(symbol="SPY", venue="backtest")


class OrderStrategy(Strategy):
    """Declares a venue and builds its orders via ``instrument.make_order``."""

    name = "order_strategy"
    venue = "backtest"
    instruments: ClassVar[list] = [_ES]
    subscriptions: ClassVar[list] = [
        Subscription(instrument=_ES, datatype=Datatype.BARS, cadence="1h")
    ]
    config = BaseStrategyConfig(
        PRE_LOAD_DAYS=60,
        INITIAL_CAPITAL=100_000.0,
        MAX_CONCURRENT_POSITIONS=1,
        STOP_LOSS_PERCENT=0.05,
        TAKE_PROFIT_PERCENT=-1.0,
        USE_TRAIL_LOSS=False,
        MAX_HOLDING_MINUTES=60 * 24 * 30,
        MIN_SIGNAL_CONFIDENCE=0.0,
        BASE_SLIPPAGE_FACTOR=0.0,
        instruments=[_ES],
        subscriptions=[
            Subscription(instrument=_ES, datatype=Datatype.BARS, cadence="1h")
        ],
        venue="backtest",
    )

    def decide(self, events, portfolio) -> list:
        # No events/portfolio reading here: the body simply decides to act.
        return [_ES.make_order("BUY", 8, strategy=self.name)]


class _FakeDataHandler:
    """Enough of the data-handler surface for construction (never fetched)."""

    def get_data(self, *args, **kwargs):
        return None


def _engine() -> TradingEngine:
    return TradingEngine(OrderStrategy(), data_handler=_FakeDataHandler())


def _spy_client(submitted: list[Order]):
    """A minimal execution client that captures submitted orders.

    Accepts (and ignores) the shared-loop ``now``/``cadence`` kwargs the engine
    passes through the typed-order seam.
    """
    return type(
        "SpyClient",
        (),
        {"submit": lambda self, order, **kwargs: submitted.append(order) or None},
    )()


# ========================================================================
# Scenario: order built on the instrument and submitted to the venue
# ========================================================================


class TestOrderReachesVenue:
    def test_decide_order_submitted_to_venue_execution_client(self) -> None:
        strategy = OrderStrategy()
        decisions = strategy.decide([], Portfolio(starting_equity=100_000.0))
        assert len(decisions) == 1
        order = decisions[0]
        assert isinstance(order, Order)
        assert order.instrument is _ES
        assert order.side is Side.BUY
        assert order.quantity == 8
        assert order.venue == "backtest"

        submitted: list[Order] = []
        venue = BacktestVenue(execution_client=_spy_client(submitted))
        engine = _engine()

        engine.submit_decisions(venue, None, decisions)

        assert len(submitted) == 1
        assert submitted[0].side is Side.BUY
        assert submitted[0].quantity == 8
        assert submitted[0].instrument is _ES
        assert submitted[0].strategy == "order_strategy"

    def test_engine_reports_one_fill_per_order(self) -> None:
        strategy = OrderStrategy()
        submissions: list[Order] = []

        class _Client:
            def submit(self, order, **kwargs):
                submissions.append(order)
                return None  # a rejected order still consumes the submit slot

        fills = _engine().submit_decisions(
            BacktestVenue(execution_client=_Client()), None, strategy.decide([], None)
        )
        assert len(fills) == 1 and fills[0] is None


# ========================================================================
# Scenario: fill reporting feeds the portfolio
# ========================================================================


class TestFillFeedsPortfolio:
    def test_venue_fill_grows_portfolio_exposure(self) -> None:
        pf = Portfolio(starting_equity=100_000.0)
        client = BacktestExecutionClient(
            price_source=lambda _order: 4_500.0, portfolio=pf
        )
        venue = BacktestVenue(execution_client=client)
        order = _ES.make_order("BUY", 8, strategy="order_strategy")

        fills = _engine().submit_decisions(venue, pf, [order])
        assert len(fills) == 1 and fills[0] is not None

        pos = pf.position("backtest", "ES")
        assert pos is not None
        assert pos.qty == 8  # filled quantity updates the schema
        assert pos.direction == 1  # long
        assert pos.avg_price == pytest.approx(4_500.0)
        assert pf.cash == pytest.approx(100_000.0 - 8 * 4_500.0)

    def test_sell_fill_opens_short_side(self) -> None:
        pf = Portfolio(starting_equity=100_000.0)
        client = BacktestExecutionClient(
            price_source=lambda _order: 100.0, portfolio=pf
        )
        order = _SPY.make_order("SELL", 10, strategy="order_strategy")

        _engine().submit_decisions(BacktestVenue(execution_client=client), pf, [order])
        pos = pf.position("backtest", "SPY")
        assert pos is not None and pos.direction == -1 and pos.qty == 10


# ========================================================================
# Scenario: no broker client in the strategy
# ========================================================================


def test_strategy_surface_has_no_broker_client() -> None:
    strategy = OrderStrategy()
    # The strategy declaration surface is venue id + typed seams + config; it
    # never holds a broker object (no client/broker/executor attributes).
    assert strategy.venue == "backtest"
    assert not hasattr(strategy, "client")
    assert not hasattr(strategy, "broker")
    assert not hasattr(strategy, "execution_client")
    assert isinstance(strategy.instruments[0], FutureInstrument)


def test_strategy_surface_ships_no_broker_import() -> None:
    # The declarative authoring surface must not reach a broker SDK: a
    # strategy declares a venue id and typed seams, never a client import.
    from pathlib import Path

    from quant.strategies import base

    source = Path(base.__file__).read_text(encoding="utf-8")
    assert "import alpaca" not in source
    assert "TradingClient" not in source
    assert "# layering-ok" not in source  # no sanctioned sideways import either


# ========================================================================
# Backtest execution client fill semantics
# ========================================================================


class TestBacktestExecutionClient:
    def test_market_order_fills_at_price_source(self) -> None:
        client = BacktestExecutionClient(price_source=lambda _order: 4_500.0)
        fill = client.submit(_ES.make_order("BUY", 4, strategy="s"))
        assert fill is not None
        assert fill.price == pytest.approx(4_500.0)
        assert fill.qty == 4
        assert fill.side is Side.BUY
        assert fill.venue == "backtest"
        assert fill.instrument is _ES

    def test_buy_limit_fills_when_market_below_limit(self) -> None:
        client = BacktestExecutionClient(price_source=lambda _order: 4_400.0)
        fill = client.submit(
            _ES.make_order("BUY", 4, price=4_500.0, price_type=OrderPriceType.LIMIT)
        )
        assert fill is not None and fill.price == pytest.approx(4_400.0)

    def test_buy_limit_not_crossed_returns_none(self) -> None:
        client = BacktestExecutionClient(price_source=lambda _order: 4_600.0)
        fill = client.submit(
            _ES.make_order("BUY", 4, price=4_500.0, price_type=OrderPriceType.LIMIT)
        )
        assert fill is None

    def test_sell_limit_not_crossed_returns_none(self) -> None:
        client = BacktestExecutionClient(price_source=lambda _order: 4_400.0)
        fill = client.submit(
            _ES.make_order("SELL", 4, price=4_500.0, price_type=OrderPriceType.LIMIT)
        )
        assert fill is None

    def test_no_price_source_fills_nothing(self) -> None:
        client = BacktestExecutionClient()  # no price source -> no fill
        assert client.submit(_ES.make_order("BUY", 4)) is None

    def test_zero_market_price_fills_nothing(self) -> None:
        client = BacktestExecutionClient(price_source=lambda _order: 0.0)
        assert client.submit(_ES.make_order("BUY", 4)) is None
