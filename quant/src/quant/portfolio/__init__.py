"""Portfolio primitives (spec `portfolio`, design D4).

Positions are explicit state machines per (venue, symbol) driven only by fills
(``opened -> adjusting -> closed``); ``Portfolio`` aggregates them with account
state and per-strategy + combined metrics. The same classes and the same
semantic functions run in backtest (fill simulation) and live (venue fill
events) — portfolio results are comparable across environments by construction.
"""

from quant.portfolio.base import Fill, Position, PositionState, Side
from quant.portfolio.portfolio import Portfolio

__all__ = [
    "Fill",
    "Portfolio",
    "Position",
    "PositionState",
    "Side",
]
