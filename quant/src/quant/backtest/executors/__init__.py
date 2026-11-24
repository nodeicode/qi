"""Per-asset-class backtest fill simulators + routing facade."""

from quant.backtest.executors.base_executor import BaseBacktestExecutor
from quant.backtest.executors.context import (
    BacktestExecContext,
    ICPrefetch,
    OptionsPrefetch,
)
from quant.backtest.executors.equity import EquityExecutor
from quant.backtest.executors.router import DEFAULT_REGISTRY, ExecutorRouter

__all__ = [
    "DEFAULT_REGISTRY",
    "BacktestExecContext",
    "BaseBacktestExecutor",
    "EquityExecutor",
    "ExecutorRouter",
    "ICPrefetch",
    "OptionsPrefetch",
]
