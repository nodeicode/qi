"""Tests for the typed-instrument seed (design D14 / spec `instruments`).

Covers: equity v1 coverage, options-on-futures composition via `underlying`,
registration/conformance, and ticker-string sugar preserved at seams.
"""

from typing import ClassVar

import pytest

from quant.instruments import (
    AssetClass,
    EquityInstrument,
    FutureInstrument,
    Instrument,
    InstrumentClass,
    OptionInstrument,
    OptionKind,
    equity_from_tickers,
    instrument_class_for,
    register_instrument_class,
    resolve,
    validate_instrument,
)
from quant.instruments.base import Instrument as BaseInstrument
from quant.instruments.future import FutureInstrument as FutureInstrumentCls
from quant.order import Order, OrderPriceType
from quant.portfolio.base import Side


class _FixtureConfig:
    """Minimal stand-in for a ``BaseStrategyConfig`` carrying ``TICKERS``."""

    TICKERS: ClassVar[list[str]] = ["SPY", "QQQ"]


# ── equity (v1) ────────────────────────────────────────────────────────
def test_equity_instrument_constructs_and_keys() -> None:
    inst = EquityInstrument(symbol="SPY", venue="alpaca")
    assert inst.asset_class is AssetClass.EQUITY
    assert inst.instrument_class is InstrumentClass.EQUITY
    assert inst.key() == ("alpaca", "SPY")
    assert validate_instrument(inst) == []


def test_equity_missing_venue_rejected() -> None:
    with pytest.raises(ValueError):
        EquityInstrument(symbol="SPY", venue="")


# ── instruments-on-futures (composition via underlying) ────────────────
def test_options_on_futures_compose_via_underlying() -> None:
    fut = FutureInstrumentCls(symbol="ESZ5", venue="backtest", multiplier=50.0)
    opt = OptionInstrument(
        symbol="ESZ5 C6000",
        venue="backtest",
        underlying=fut,
        strike=6000.0,
        expiration="2025-12-19",
        option_kind=OptionKind.CALL,
    )
    assert isinstance(opt, OptionInstrument)
    assert opt.underlying is fut  # D14: underlying is an Instrument ref, not a str
    assert validate_instrument(opt) == []


def test_option_requires_underlying() -> None:
    with pytest.raises(ValueError):
        OptionInstrument(
            symbol="SPY C500",
            venue="alpaca",
            strike=500.0,
            expiration="2025-12-19",
            option_kind=OptionKind.CALL,
        )


# ── registration + conformance ─────────────────────────────────────────
def test_instrument_class_for_resolves_registered() -> None:
    assert instrument_class_for("equity") is EquityInstrument
    assert instrument_class_for(InstrumentClass.OPTION) is OptionInstrument
    with pytest.raises(ValueError):
        instrument_class_for("bogus")


def test_register_instrument_class_extends() -> None:
    register_instrument_class("crypto", EquityInstrument)
    assert instrument_class_for("crypto") is EquityInstrument


def test_validate_reports_violations() -> None:
    # A hand-built instrument (e.g. from an agent/custom data source) with
    # missing contract fields must be caught at the boundary, not mid-run.
    inst = EquityInstrument.__new__(EquityInstrument)
    problems = validate_instrument(inst)
    assert "instrument.symbol is empty" in problems
    assert "instrument.venue is empty" in problems


# ── ticker-string sugar at seams ───────────────────────────────────────
def test_resolve_wraps_str_and_passes_typed() -> None:
    from_str = resolve("SPY", venue="alpaca")
    assert isinstance(from_str, EquityInstrument)
    assert from_str.venue == "alpaca"
    typed = EquityInstrument(symbol="AAPL", venue="alpaca")
    assert resolve(typed, venue="alpaca") is typed


def test_equity_from_cfg_tickers() -> None:
    instruments = equity_from_tickers("alpaca", _FixtureConfig.TICKERS)
    assert [i.symbol for i in instruments] == ["SPY", "QQQ"]
    assert all(i.venue == "alpaca" for i in instruments)


def test_typed_instrument_subclass_is_base() -> None:
    assert issubclass(EquityInstrument, BaseInstrument)
    assert issubclass(OptionInstrument, Instrument)


# ── contract facts + make_order (folded from legacy ContractSpec) ──────
def test_equity_contract_fact_defaults() -> None:
    inst = EquityInstrument(symbol="SPY", venue="alpaca")
    assert inst.price_precision == 2
    assert inst.lot_size == 1
    assert inst.multiplier == 1.0
    assert inst.tick_size == 0.01


def test_round_price_uses_tick_size() -> None:
    inst = EquityInstrument(symbol="SPY", venue="alpaca")
    assert inst.round_price(100.213, side="nearest") == pytest.approx(100.21)
    assert inst.round_price(100.216, side="nearest") == pytest.approx(100.22)
    fut = FutureInstrument(symbol="ES", venue="paper", multiplier=50, tick_size=0.25)
    assert fut.round_price(4010.13, side="ask") == 4010.25
    assert fut.round_price(4010.13, side="bid") == 4010.0


def test_pnl_computes_from_multiplier() -> None:
    fut = FutureInstrument(symbol="ES", venue="paper", multiplier=50, tick_size=0.25)
    assert fut.pnl(4000.0, 4002.0, 1, 1) == 100.0
    assert fut.pnl(4000.0, 4002.0, 1, -1) == -100.0


def test_make_order_builds_typed_order() -> None:
    inst = EquityInstrument(symbol="SPY", venue="alpaca")
    order = inst.make_order("BUY", 100)
    assert isinstance(order, Order)
    assert order.side is Side.BUY
    assert order.quantity == 100
    assert order.price_type is OrderPriceType.MARKET
    assert order.venue == "alpaca"
    assert order.instrument == inst
    assert Order.from_schema(order.to_schema()) == order


def test_make_order_with_limit_price() -> None:
    inst = EquityInstrument(symbol="AAPL", venue="paper")
    order = inst.make_order("SELL", 10, price=199.5)
    assert order.side is Side.SELL
    assert order.price_type is OrderPriceType.LIMIT
    assert order.price == 199.5


def test_future_contract_facts_are_single_source() -> None:
    fut = FutureInstrument(
        symbol="MES", venue="paper", multiplier=5, tick_size=0.25, margin=2000
    )
    assert fut.margin == 2000
    assert fut.round_price(4000.12, side="bid") == 4000.0


def test_round_price_guards_missing_tick() -> None:
    inst = EquityInstrument(symbol="SPY", venue="paper", tick_size=None)
    with pytest.raises(ValueError, match="tick_size"):
        inst.round_price(100.0)
    # non-positive / non-finite prices pass through without rounding
    assert EquityInstrument(symbol="SPY", venue="paper").round_price(0.0) == 0.0


def test_pnl_guards_missing_multiplier() -> None:
    inst = EquityInstrument(symbol="SPY", venue="paper", multiplier=None)
    with pytest.raises(ValueError, match="multiplier"):
        inst.pnl(100.0, 101.0, 1, 1)


def test_make_order_accepts_side_member_and_explicit_price_type() -> None:
    inst = EquityInstrument(symbol="SPY", venue="paper")
    order = inst.make_order(Side.BUY, 10, price_type=OrderPriceType.LIMIT, price=200.0)
    assert order.side is Side.BUY
    assert order.price_type is OrderPriceType.LIMIT
    assert order.price == 200.0
