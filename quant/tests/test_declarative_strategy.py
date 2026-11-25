"""Declarative strategy surface (spec `strategy-authoring` Declarative surface).

Verifies the new ``Strategy`` base: a strategy declares typed instruments,
subscriptions, and a venue, and its single ``decide(events, portfolio)`` body
is what constructs and resolves — the 3-method contracted
(calculate_indicators/generate_signals/manage_trade) surface is absent here.
"""

from __future__ import annotations

from typing import ClassVar

from quant.data import Datatype, Subscription
from quant.instruments import EquityInstrument
from quant.strategies.base import Strategy

_SPY = EquityInstrument(symbol="SPY", venue="backtest")


class MaCross(Strategy):
    name: ClassVar[str] = "ma_cross"
    venue: ClassVar[str] = "backtest"
    config: ClassVar[None] = None
    instruments: ClassVar[list] = [_SPY]
    subscriptions: ClassVar[list] = [
        Subscription(instrument=_SPY, datatype=Datatype.BARS, cadence="1h")
    ]

    def decide(self, events, portfolio) -> list:
        # generate entries from events AND evaluate portfolio positions: one body.
        return []


class ExitAware(Strategy):
    name: ClassVar[str] = "exit_aware"
    venue: ClassVar[str] = "backtest"

    def decide(self, events, portfolio) -> list:
        # reads the injected portfolio's open positions to decide exits
        return [p for p in portfolio if getattr(p, "should_exit", False)]


def test_declarative_strategy_constructs_and_resolves() -> None:
    s = MaCross()
    assert s.name == "ma_cross"
    assert s.venue == "backtest"
    assert s.instruments == [_SPY]
    assert len(s.subscriptions) == 1
    assert callable(s.decide)
    assert s.decide([], None) == []


def test_decide_is_single_decision_body() -> None:
    calls: list = []

    class Rec(Strategy):
        def decide(self, events, portfolio) -> list:
            calls.append(events)
            return []

    Rec().decide(["bar"], object())
    assert calls == [["bar"]]


def test_decide_evaluates_portfolio_for_exits() -> None:
    held = [
        type("Pos", (), {"should_exit": True})(),
        type("Pos", (), {"should_exit": False})(),
    ]
    out = ExitAware().decide([], held)
    assert len(out) == 1 and out[0].should_exit is True
