"""Abstract per-asset-class fill simulator.

Children do **not** implement ``ExecutorProtocol`` — that is the router's job.
Each subclass owns enter / exit / force_close for one asset class and reads
shared state from ``BacktestExecContext``.
"""

from __future__ import annotations

import logging
from abc import ABC, abstractmethod
from typing import Any

import pandas as pd

from quant.backtest.executors.context import BacktestExecContext
from quant.backtest.utils import calculate_entry_time
from quant.core import TradingEngine
from quant.core.protocols import StrategyProtocol
from quant.utils.strategy_config import BaseStrategyConfig


def tradable_close(frame: pd.DataFrame) -> pd.Series:
    """Return the *tradable* close series for a manage/signals frame.

    Priors from attached Raw_Close (unadjusted, matches the Databento CBBO
    book) — backtest executors price in tradable space even when indicators
    run on adjusted ``"all"`` bars. Falls back to ``Close`` when Raw_Close
    was never attached (raw-space runs where Close == Raw_Close anyway).
    """
    if "Raw_Close" in frame.columns:
        return frame["Raw_Close"]
    if "Close" in frame.columns:
        return frame["Close"]
    raise KeyError("frame has neither Raw_Close nor Close")


def tradable_close_at(row: pd.Series) -> float:
    """Scalar tradable close for one bar row (Raw_Close preferred over Close)."""
    row_series = row.to_frame().T if isinstance(row, pd.Series) else row
    return float(tradable_close(row_series).iloc[0])


class BaseBacktestExecutor(ABC):
    """Asset-class fill simulator. Constructed with a shared ``ctx`` only."""

    def __init__(self, ctx: BacktestExecContext):
        self.ctx = ctx

    # ── Ergonomic aliases (always references into ctx) ──────────────────
    @property
    def config(self) -> BaseStrategyConfig:
        return self.ctx.config

    @property
    def engine(self) -> TradingEngine:
        return self.ctx.engine

    @property
    def ticker(self) -> str:
        return self.ctx.ticker

    @property
    def logger(self) -> logging.Logger:
        return self.ctx.logger

    @property
    def trade_log(self) -> list:
        return self.ctx.trade_log

    @property
    def _active_positions(self) -> list[dict]:
        return self.ctx.active_positions

    @property
    def _df_signals(self) -> pd.DataFrame:
        return self.ctx.df_signals

    @property
    def _df_manage(self) -> pd.DataFrame:
        return self.ctx.df_manage

    @property
    def _df_manage_1m(self) -> pd.DataFrame:
        return self.ctx.df_manage_1m

    @property
    def _running_equity(self) -> float:
        return self.ctx.running_equity

    @_running_equity.setter
    def _running_equity(self, value: float) -> None:
        self.ctx.running_equity = float(value)

    @property
    def _collateral_in_use(self) -> float:
        return self.ctx.collateral_in_use

    @_collateral_in_use.setter
    def _collateral_in_use(self, value: float) -> None:
        self.ctx.collateral_in_use = float(value)

    @property
    def portfolio(self) -> Any | None:
        """Optional portfolio collector (spec `portfolio`); None disables fills."""
        return self.ctx.portfolio

    @property
    def venue(self) -> str:
        """Venue leg for simulated fills; defaults to ``"backtest"``."""
        return self.ctx.venue

    def _update_reason_stats(self, trade: dict) -> None:
        self.ctx.update_reason_stats(trade)

    # ── Execution contract ──────────────────────────────────────────────
    @abstractmethod
    def enter(
        self,
        strategy: StrategyProtocol,
        ticker: str,
        signal_row: dict,
        engine: TradingEngine,
        *,
        now=None,
    ) -> dict | None:
        """Open a position. Returns the new position dict (or None)."""
        ...

    @abstractmethod
    def exit(
        self,
        strategy_name: str,
        position: dict,
        sig: dict,
        *,
        forced: bool = False,
        manage_df: pd.DataFrame | None = None,
    ) -> bool:
        """Close *position*. Returns True on success (position removed)."""
        ...

    @abstractmethod
    def force_close(self, position: dict) -> None:
        """End-of-backtest close for one still-open position."""
        ...

    # ── Shared helpers ──────────────────────────────────────────────────
    def _constant_equity(self) -> float:
        """Sizing baseline that stays fixed for the whole backtest run."""
        return self.ctx.starting_equity

    def _slice_manage(self, start=None, end=None) -> pd.DataFrame:
        """Slice manage-resolution data for a time window."""
        if self.ctx.df_manage.empty:
            return self.ctx.df_manage

        idx = self.ctx.df_manage.index

        if start is not None and end is not None:
            mask = (idx >= start) & (idx <= end)
        elif start is not None:
            mask = idx >= start
        elif end is not None:
            mask = idx <= end
        else:
            return self.ctx.df_manage
        return self.ctx.df_manage[mask]

    def data_end_dt(self, now):
        """Undelayed bar close — shared helper; router owns the protocol method."""
        return pd.Timestamp(calculate_entry_time(now, self.config.INTERVAL))
