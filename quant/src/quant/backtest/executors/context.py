"""Shared backtest execution context — one object, not 20 constructor kwargs.

Child asset-class executors and ``ExecutorRouter`` all hold a reference to the
same ``BacktestExecContext``.  Mutable bookkeeping (positions, trade log,
equity, collateral) lives here so multi-asset runs share one ledger.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from logging import Logger
from typing import TYPE_CHECKING

import pandas as pd

from quant.utils.strategy_config import BaseStrategyConfig

if TYPE_CHECKING:
    from quant.core.engine import TradingEngine
    from quant.portfolio.portfolio import Portfolio


@dataclass
class BacktestExecContext:
    """Everything a backtest executor needs, bundled once at simulate-time."""

    # Identity / decision surface
    config: BaseStrategyConfig
    engine: TradingEngine
    ticker: str
    logger: Logger

    # Bar data (signal + manage resolution)
    df_signals: pd.DataFrame
    df_manage: pd.DataFrame
    df_manage_1m: pd.DataFrame = field(default_factory=pd.DataFrame)

    # Shared mutable ledger (injected by reference from Backtester)
    active_positions: list[dict] = field(default_factory=list)
    trade_log: list = field(default_factory=list)
    starting_equity: float = 0.0
    running_equity: float = 0.0
    collateral_in_use: float = 0.0

    # Portfolio-collecting mode (spec `portfolio`): executor fills stream into
    # this optional Portfolio (same primitives as live). ``venue`` is the
    # (venue, symbol) position key's venue leg for simulated fills.
    portfolio: Portfolio | None = field(default=None)
    venue: str = "backtest"

    # Callbacks
    update_reason_stats: Callable[[dict], None] = field(default=lambda _trade: None)
