"""Guards the add-a-strategy example in ``agents/strategy-sdk.md``.

The exact strategy / config / registry / engine / backtest steps documented in
that agent doc are reproduced here so the doc's example is backed by a real
green test, and the spec `strategy-sdk` scenarios (missing method rejected,
wrong config-type rejected, valid strategy accepted) hold for it. The example
strategy is written against the declarative surface: typed instrument +
``bars`` subscription + a venue, with a single author body ``decide``.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import ClassVar

import numpy as np
import pandas as pd
import pytest

from quant.backtest.backtester import Backtester
from quant.core.engine import TradingEngine
from quant.data import Datatype, Subscription
from quant.instruments import EquityInstrument
from quant.portfolio import Side
from quant.strategies import STRATEGY_REGISTRY, get_strategy
from quant.strategies.base import ExitIntent, Strategy
from quant.utils.strategy_config import BaseStrategyConfig


# ── the doc's example strategy (mirrors agents/strategy-sdk.md) ──────────────
def _demo_instrument() -> EquityInstrument:
    """The traded equity instrument (venue ``backtest``, one-lot precision)."""
    return EquityInstrument(
        symbol="SPY",
        venue="backtest",
        price_precision=2,
        lot_size=1,
    )


@dataclass
class DemoMAConfig(BaseStrategyConfig):
    """A moving-average strategy's config: framework fields + MA windows."""

    PRE_LOAD_DAYS: int = 60
    INITIAL_CAPITAL: float = 100_000.0
    MAX_CONCURRENT_POSITIONS: int = 1
    STOP_LOSS_PERCENT: float = 0.05
    TAKE_PROFIT_PERCENT: float = -1.0  # disabled sentinel
    USE_TRAIL_LOSS: bool = False
    MAX_HOLDING_MINUTES: int = 60 * 24 * 30
    MIN_SIGNAL_CONFIDENCE: float = 0.0
    BASE_SLIPPAGE_FACTOR: float = 0.001

    # --- Declarative declarations ---
    instruments: list = field(default_factory=lambda: [_demo_instrument()])
    subscriptions: list = field(
        default_factory=lambda: [
            Subscription(
                instrument=_demo_instrument(),
                datatype=Datatype.BARS,
                cadence="1d",
            )
        ]
    )
    venue: str = "backtest"

    # strategy-specific params
    FAST_WINDOW: int = 5
    SLOW_WINDOW: int = 20


class DemoMAStrategy(Strategy):
    """Fast>slow → BUY, fast<slow → exit. One registry line; runs identically
    in backtest and live (the engine + venue are the only seams)."""

    name: ClassVar[str] = "demo_ma"

    def __init__(self, config: DemoMAConfig | None = None) -> None:
        self.config = config or DemoMAConfig()
        # Mirror the declared config onto the instance surface the engine and
        # data port read (instruments / subscriptions / venue).
        self.instruments: list = list(self.config.instruments)
        self.subscriptions: list = list(self.config.subscriptions)
        self.venue: str = self.config.venue or "backtest"
        super().__init__()

    def decide(self, events, portfolio) -> list:
        """Author body: exit on fast<slow, enter on fast>slow (no position)."""
        bars = events.bars("SPY")
        if bars is None or bars.empty:
            return []
        close = bars["Close"]
        fast = close.rolling(self.config.FAST_WINDOW).mean()
        slow = close.rolling(self.config.SLOW_WINDOW).mean()
        if len(fast) < 2 or pd.isna(fast.iloc[-1]) or pd.isna(slow.iloc[-1]):
            return []
        out: list = []
        for pos in portfolio.open_positions(self.name):
            if fast.iloc[-1] < slow.iloc[-1]:
                out.append(ExitIntent(self.name, pos, reason="fast<slow"))
        if fast.iloc[-1] > slow.iloc[-1] and not portfolio.open_positions(self.name):
            instrument = self.config.instruments[0]
            out.append(instrument.make_order(Side.BUY, 100, strategy=self.name))
        return out


class SynthDataHandler:
    """Deterministic offline bars (mirrors agents/strategy-sdk.md / quickstart)."""

    def __init__(self, days: int = 300) -> None:
        idx = pd.date_range("2023-06-01", periods=days, freq="B")
        close = 100.0 + 25.0 * np.sin(np.arange(len(idx)) / 12.0)
        self._df = pd.DataFrame(
            {
                "Open": close - 0.5,
                "High": close + 1.0,
                "Low": close - 1.0,
                "Close": close,
                "Volume": [1_000_000] * len(idx),
            },
            index=idx,
        )

    def get_data(self, subscription, start, end, use_cache=True, adjustment=None):
        del subscription, use_cache, adjustment
        s = pd.Timestamp(start)
        e = pd.Timestamp(end)
        return self._df.loc[(self._df.index >= s) & (self._df.index <= e)].copy()


# ── spec scenarios ──────────────────────────────────────────────────────────
def test_valid_strategy_accepted() -> None:
    strategy = DemoMAStrategy()
    engine = TradingEngine(strategy, SynthDataHandler())
    assert engine.strategy is strategy


def test_engine_drives_decide_and_buys() -> None:
    """The engine serves closed bars to ``decide`` and submits the BUY order.

    Runs the offline backtest and asserts the decision transcript contains a
    typed BUY order (the declarative replacement for the legacy
    indicator-injection ``run_signals`` behavior).
    """
    cfg = DemoMAConfig()
    bt = Backtester(
        cfg,
        DemoMAStrategy(cfg),
        "2024-01-02",
        "2024-06-28",
        "SPY",
        data_handler=SynthDataHandler(),
    )
    trades = bt.run_backtest()
    assert len(trades) >= 1
    transcript = bt.engine.transcript
    order_sides = [
        item["side"]
        for entry in transcript
        for item in entry.get("decisions", [])
        if item.get("kind") == "order"
    ]
    assert any(side.lower() == "buy" for side in order_sides)


def test_missing_methods_construct_with_declarative_surface() -> None:
    """The 3-method contracted rejection is dissolved: a strategy carrying only
    a config now constructs (the declarative ``decide`` body is engine-driven)."""

    class MissingManage:
        name = "missing_manage"

        def __init__(self) -> None:
            self.config = DemoMAConfig()

    engine = TradingEngine(MissingManage(), SynthDataHandler())
    assert engine.strategy.name == "missing_manage"


def test_wrong_config_type_rejected_at_construction() -> None:
    class BadConfig:
        name = "bad_config"

        def __init__(self) -> None:
            self.config = "not-a-config"

        def calculate_indicators(self, df, **kw):
            return df

        def generate_signals(self, df):
            return df

        def manage_trade(self, position, df):
            return -1

    with pytest.raises(TypeError) as exc:
        TradingEngine(BadConfig(), SynthDataHandler())
    assert "BaseStrategyConfig" in str(exc.value)


# ── end-to-end: one registry line → offline backtest ───────────────────────
def test_registry_resolves_demo_strategy() -> None:
    STRATEGY_REGISTRY["demo_ma"] = DemoMAStrategy
    assert get_strategy("demo_ma") is DemoMAStrategy


def test_offline_backtest_round_trip() -> None:
    cfg = DemoMAConfig()
    bt = Backtester(
        cfg,
        DemoMAStrategy(cfg),
        "2024-01-02",
        "2024-06-28",
        "SPY",
        data_handler=SynthDataHandler(),
    )
    trades = bt.run_backtest()
    bt.compute_equity_curve()
    assert len(trades) >= 1
    assert not bt.equity_curve.empty
