"""Shared helpers for the quant CLI entry points.

The CLI's job is a runnable, self-contained SDK experience: everything it does
offline uses a deterministic synthetic bar source so ``quant-backtest`` /
``quant-paper`` work with no API keys and no network. Importing ``quant.backtest``
and ``quant.strategies`` here is a sanctioned cross-leaf edge for the CLI (the
same layering the live ``strategy_loader`` uses) — hence the inline markers.
"""

from __future__ import annotations

from typing import Any

import numpy as np
import pandas as pd

from quant.backtest.backtester import Backtester  # layering-ok (CLI front-end)
from quant.strategies import get_strategy  # layering-ok (CLI front-end)
from quant.strategies.example_equity.strategy import (  # layering-ok (CLI front-end)
    ExampleEquityConfig,
    ExampleEquityStrategy,
)
from quant.utils.strategy_config import BaseStrategyConfig

DEFAULT_START = "2024-01-02"
DEFAULT_END = "2024-06-28"
DEFAULT_TICKER = "SPY"
DEFAULT_PRELOAD_DAYS = 90


class SyntheticDataHandler:
    """Deterministic, offline bar source (no network, no keys).

    An oscillating equity series so the MA-cross reference strategy actually
    crosses its windows and produces signals. Mirrors the golden-regression
    fixtures' contract: tz-naive UTC business-day bars, sliced to [start, end],
    ``adjustment`` accepted and ignored (no corporate actions on a synthetic).
    """

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
        del ticker, interval, use_cache, adjustment  # synthetic ignores these
        s = pd.Timestamp(start)
        e = pd.Timestamp(end)
        return self._df.loc[(self._df.index >= s) & (self._df.index <= e)].copy()


def make_equity_config(**overrides: Any) -> BaseStrategyConfig:
    """A runnable ``example_equity`` config on daily bars (offline defaults)."""
    cfg = ExampleEquityConfig(
        INTERVAL="1d",
        MANAGE_INTERVAL="1d",
        PRE_LOAD_DAYS=DEFAULT_PRELOAD_DAYS,
        INITIAL_CAPITAL=100_000.0,
        CAPITAL_PER_TRADE=0.95,
        MAX_CONCURRENT_POSITIONS=1,
        STOP_LOSS_PERCENT=0.05,
        TAKE_PROFIT_PERCENT=-1.0,  # disabled sentinel
        USE_TRAIL_LOSS=False,
        MAX_HOLDING_MINUTES=60 * 24 * 30,
        MIN_SIGNAL_CONFIDENCE=0.0,
        BASE_SLIPPAGE_FACTOR=0.001,
        TICKERS=["SPY"],
        DATA_DELAY=0,
        FAST_WINDOW=10,
        SLOW_WINDOW=30,
    )
    for key, value in overrides.items():
        setattr(cfg, key, value)
    return cfg


def resolve_strategy(name: str, config: BaseStrategyConfig):
    """Resolve ``name`` from the entry-point group (or the built-in)."""
    if name == "example_equity":
        return ExampleEquityStrategy(config)
    cls = get_strategy(name)
    return cls(config)


def run_offline_backtest(
    start: str,
    end: str,
    ticker: str,
    strategy_name: str = "example_equity",
    *,
    verbose: bool = False,
) -> tuple[Backtester, pd.DataFrame]:
    """Run an offline backtest and return ``(backtester, trades)``.

    The backtester's own data handler is replaced with the synthetic source, so
    the run is fully deterministic and needs no Alpaca credentials.
    """
    cfg = make_equity_config()
    strategy = resolve_strategy(strategy_name, cfg)
    bt = Backtester(
        cfg,
        strategy,
        start,
        end,
        ticker,
        verbose=verbose,
        data_handler=SyntheticDataHandler(),
    )
    trades = bt.run_backtest()
    bt.compute_equity_curve()
    return bt, trades
