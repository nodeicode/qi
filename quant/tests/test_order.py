"""Tests for the typed Order primitive (spec `execution` Typed order primitive).

Covers the canonical ``to_schema`` / ``from_schema`` round-trip across sides,
price contexts, timestamps, and embedded instruments — including an option on
a future — so orders cross the backtest/live seam byte-identically.
"""

from __future__ import annotations

from datetime import UTC, datetime, timezone

import pytest

from quant.instruments import (
    EquityInstrument,
    FutureInstrument,
    OptionInstrument,
    OptionKind,
)
from quant.order import Order, OrderPriceType
from quant.portfolio.base import Side


def _option_on_future() -> OptionInstrument:
    underlying = FutureInstrument(symbol="ES", venue="paper", multiplier=50)
    return OptionInstrument(
        symbol="ES2406C5000",
        venue="paper",
        underlying=underlying,
        strike=5000,
        expiration="2024-06-21",
        option_kind=OptionKind.CALL,
    )


def test_order_round_trips_equity_through_canonical_schema() -> None:
    inst = EquityInstrument(symbol="SPY", venue="alpaca")
    created = datetime(2024, 1, 2, 3, 4, 5, tzinfo=UTC)
    order = Order(
        side=Side.BUY,
        quantity=100,
        instrument=inst,
        price_type=OrderPriceType.LIMIT,
        price=410.25,
        tag="ma-cross",
        strategy="example",
        created_at=created,
        submitted_at=created,
        filled_at=created,
    )
    schema = order.to_schema()
    # The schema is a plain JSON-able dict that round-trips through json.
    import json

    reloaded = json.loads(json.dumps(schema))
    assert Order.from_schema(reloaded) == order


def test_order_venue_is_derived_from_instrument() -> None:
    inst = EquityInstrument(symbol="SPY", venue="alpaca")
    order = Order(side=Side.SELL, quantity=25, instrument=inst)
    assert order.venue == "alpaca"
    # venue survives the round-trip only via the embedded instrument.
    assert Order.from_schema(order.to_schema()).venue == "alpaca"


def test_order_round_trips_option_on_future_underlying() -> None:
    inst = _option_on_future()
    order = Order(side=Side.BUY, quantity=2, instrument=inst)
    rounded = Order.from_schema(order.to_schema())

    assert rounded == order
    assert isinstance(rounded.instrument, OptionInstrument)
    assert isinstance(rounded.instrument.underlying, FutureInstrument)
    assert rounded.instrument.underlying.multiplier == 50
    assert rounded.venue == "paper"


def test_market_order_default_price_context() -> None:
    inst = EquityInstrument(symbol="AAPL", venue="paper")
    order = Order(side=Side.BUY, quantity=10, instrument=inst)
    assert order.price_type is OrderPriceType.MARKET
    assert order.price is None
    assert Order.from_schema(order.to_schema()) == order


def test_order_rejects_negative_quantity() -> None:
    inst = EquityInstrument(symbol="SPY", venue="paper")
    with pytest.raises(ValueError):
        Order(side=Side.BUY, quantity=-1, instrument=inst)


def test_future_order_round_trip() -> None:
    inst = FutureInstrument(symbol="ES", venue="paper", multiplier=50, tick_size=0.25)
    order = Order(side=Side.SELL, quantity=1, instrument=inst)
    assert Order.from_schema(order.to_schema()) == order


def test_from_schema_rejects_unknown_instrument_class() -> None:
    inst = EquityInstrument(symbol="SPY", venue="paper")
    schema = Order(side=Side.BUY, quantity=1, instrument=inst).to_schema()
    schema["instrument"]["__cls__"] = "NotARealInstrument"
    with pytest.raises(ValueError, match="Unsupported instrument class"):
        Order.from_schema(schema)


def test_order_rejects_non_instrument() -> None:
    with pytest.raises(ValueError, match="typed Instrument"):
        Order(side=Side.BUY, quantity=1, instrument="SPY")  # type: ignore[arg-type]
