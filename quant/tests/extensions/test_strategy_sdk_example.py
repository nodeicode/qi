"""Guards the add-a-strategy example in ``agents/strategy-sdk.md`` (task 6.4).

The exact strategy / config / registry / engine / backtest steps documented in
that agent doc are reproduced here so the doc's example is backed by a real
green test, and the spec `strategy-sdk` scenarios (missing method rejected,
wrong config-type rejected, valid strategy accepted) hold for it.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import ClassVar

import numpy as np
import pandas as pd
import pytest

from quant.backtest.backtester import Backtester
from quant.core.engine import TradingEngine
from quant.core.protocols import PositionDict
from quant.strategies import STRATEGY_REGISTRY, get_strategy
from quant.utils.strategy_config import BaseStrategyConfig


# ── the doc's example strategy (mirrors agents/strategy-sdk.md) ──────────────
@dataclass
class DemoMAConfig(BaseStrategyConfig):
    """A moving-average strategy's config: the same fields, plus MA windows."""

    INTERVAL: str = "1d"
    MANAGE_INTERVAL: str = "1d"
    PRE_LOAD_DAYS: int = 60
    INITIAL_CAPITAL: float = 100_000.0
    CAPITAL_PER_TRADE: float = 0.5
    MAX_CONCURRENT_POSITIONS: int = 1
    STOP_LOSS_PERCENT: float = 0.05
    TAKE_PROFIT_PERCENT: float = -1.0  # disabled sentinel
    USE_TRAIL_LOSS: bool = False
    MAX_HOLDING_MINUTES: int = 60 * 24 * 30
    MIN_SIGNAL_CONFIDENCE: float = 0.0
    BASE_SLIPPAGE_FACTOR: float = 0.001
    TICKERS: list[str] = field(default_factory=lambda: ["SPY"])
    DATA_DELAY: int = 0

    # strategy-specific params
    FAST_WINDOW: int = 5
    SLOW_WINDOW: int = 20


class DemoMAStrategy:
    """Fast>slow → BUY. One registry line; runs identical in backtest and live."""

    name = "demo_ma"
    INDICATOR_COLUMNS: ClassVar[list[str]] = ["FAST", "SLOW"]

    def __init__(self, config: DemoMAConfig | None = None) -> None:
        self.config = config or DemoMAConfig()

    def calculate_indicators(self, df: pd.DataFrame, **kw) -> pd.DataFrame:
        out = df.copy()
        out["FAST"] = out["Close"].rolling(self.config.FAST_WINDOW).mean()
        out["SLOW"] = out["Close"].rolling(self.config.SLOW_WINDOW).mean()
        return out

    def generate_signals(self, df: pd.DataFrame) -> pd.DataFrame:
        out = df.copy()
        out["Signal"] = "NEUTRAL"
        out["Confidence"] = 0.0
        out["Reason"] = ""
        if out.empty or "SLOW" not in out.columns:
            return out
        mode = out["FAST"] > out["SLOW"]
        out.loc[mode, "Signal"] = "BUY"
        out.loc[mode, "Confidence"] = 1.0
        out.loc[mode, "Reason"] = "fast>slow"
        return out

    def manage_trade(self, position: PositionDict, df: pd.DataFrame) -> int:
        return -1


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

    def get_historical_data(
        self,
        ticker: str,
        start,
        end,
        interval: str,
        use_cache: bool = True,
        adjustment: str | None = None,
    ) -> pd.DataFrame:
        del ticker, interval, use_cache, adjustment
        s = pd.Timestamp(start)
        e = pd.Timestamp(end)
        return self._df.loc[(self._df.index >= s) & (self._df.index <= e)].copy()


# ── spec scenarios ──────────────────────────────────────────────────────────
def test_valid_strategy_accepted() -> None:
    strategy = DemoMAStrategy()
    engine = TradingEngine(strategy, SynthDataHandler())
    assert engine.strategy is strategy


def test_run_signals_injects_indicators_and_buys() -> None:
    engine = TradingEngine(DemoMAStrategy(), SynthDataHandler())
    df = SynthDataHandler().get_historical_data("SPY", "2023-10-01", "2024-06-28", "1d")
    signals = engine.run_signals(df, ticker="SPY")
    assert {"FAST", "SLOW"}.issubset(signals.columns)
    assert (signals["Signal"] == "BUY").sum() >= 1


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
