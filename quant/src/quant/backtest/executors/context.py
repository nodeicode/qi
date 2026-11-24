"""Shared backtest execution context — one object, not 20 constructor kwargs.

Child asset-class executors and ``ExecutorRouter`` all hold a reference to the
same ``BacktestExecContext``.  Mutable bookkeeping (positions, trade log,
equity, collateral) lives here so multi-asset runs share one ledger.

Asset-class-specific prefetch bags (``options``, ``ic``) are optional and only
populated when the backtester actually prefetched that data.  Equity runs
never touch them; Option/IC executors fail loudly if their bag is missing.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from logging import Logger
from typing import TYPE_CHECKING, Any

import pandas as pd

from quant.utils.strategy_config import BaseStrategyConfig

if TYPE_CHECKING:
    from quant.core.engine import TradingEngine


@dataclass
class OptionsPrefetch:
    """Option / spread contract data attached when those asset classes run."""

    expiration_cache: dict[str, str] = field(default_factory=dict)
    option_data_cache: dict[tuple, pd.DataFrame] = field(default_factory=dict)
    option_data_handler: Any = None


@dataclass
class ICPrefetch:
    """Iron-condor leg prices / CBBO / drop stats."""

    expiration_cache: dict[str, str] = field(default_factory=dict)
    leg_prices: dict = field(default_factory=dict)
    leg_cbbo: dict = field(default_factory=dict)
    drop_stats: dict[str, int] = field(default_factory=dict)
    option_data_handler: Any = None


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

    # Callbacks
    update_reason_stats: Callable[[dict], None] = field(default=lambda _trade: None)

    # Optional asset-class bags — set by Backtester after prefetch
    options: OptionsPrefetch | None = None
    ic: ICPrefetch | None = None

    def require_options(self) -> OptionsPrefetch:
        if self.options is None:
            raise RuntimeError(
                "OptionsPrefetch is not attached — Backtester must populate "
                "ctx.options before OPTION/SPREAD execution."
            )
        return self.options

    def require_ic(self) -> ICPrefetch:
        if self.ic is None:
            raise RuntimeError(
                "ICPrefetch is not attached — Backtester must populate "
                "ctx.ic before IC execution."
            )
        return self.ic
