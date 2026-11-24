"""ExecutorRouter — ``ExecutorProtocol`` adapter that dispatches by asset class.

The engine always talks to one executor.  This router is that executor: it owns
scheduling / precomputed-signal / position-list protocol methods, and forwards
``enter`` / ``exit`` / ``force_close`` to the registered per-asset-class child.

Construction is intentionally thin::

    ctx = BacktestExecContext(...)
    executor = ExecutorRouter(ctx)  # EQUITY only by default
    executor = ExecutorRouter(ctx, registry={...})  # extend / override

Children are lazy-built on first use so unused asset classes cost nothing.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import TYPE_CHECKING, cast

from quant.backtest.executors.base_executor import BaseBacktestExecutor
from quant.backtest.executors.context import BacktestExecContext
from quant.backtest.executors.equity import EquityExecutor
from quant.backtest.executors.futures import FuturesExecutor
from quant.backtest.executors.ic import IronCondorExecutor
from quant.backtest.utils import calculate_entry_time
from quant.core import TradingEngine
from quant.core._utils import SIGNAL_REGISTRY
from quant.core.protocols import ExecutorProtocol

if TYPE_CHECKING:
    from quant.core.protocols import PositionDict

import pandas as pd

# Default registry — register more classes (OPTION, SPREAD, IC) as they land.
DEFAULT_REGISTRY: dict[str, type[BaseBacktestExecutor]] = {
    "EQUITY": EquityExecutor,
    "IC": IronCondorExecutor,
    "FUTURES": FuturesExecutor,
}


class ExecutorRouter(ExecutorProtocol):
    """Single ``ExecutorProtocol`` face; routes fills by asset class."""

    def __init__(
        self,
        ctx: BacktestExecContext,
        *,
        registry: Mapping[str, type[BaseBacktestExecutor]] | None = None,
    ):
        self.ctx = ctx
        self._registry: dict[str, type[BaseBacktestExecutor]] = dict(
            registry if registry is not None else DEFAULT_REGISTRY
        )
        self._children: dict[str, BaseBacktestExecutor] = {}

    # ── Ledger accessors (Backtester syncs these after the run) ─────────
    @property
    def running_equity(self) -> float:
        return self.ctx.running_equity

    @property
    def collateral_in_use(self) -> float:
        return self.ctx.collateral_in_use

    # ── Child resolution ────────────────────────────────────────────────
    def _resolve(self, asset_class: str) -> BaseBacktestExecutor:
        ac = (asset_class or "EQUITY").upper()
        child = self._children.get(ac)
        if child is not None:
            return child
        cls = self._registry.get(ac)
        if cls is None:
            registered = ", ".join(sorted(self._registry)) or "(none)"
            raise KeyError(
                f"No backtest executor registered for asset class {ac!r}. "
                f"Registered: {registered}. "
                f"Pass registry={{'{ac}': YourExecutor}} to ExecutorRouter."
            )
        child = cls(self.ctx)
        self._children[ac] = child
        return child

    def _asset_class_for_signal(self, signal_row: dict, engine: TradingEngine) -> str:
        # Strict lookup: an unknown signal is a programming error in backtest —
        # fail loud instead of silently executing it as EQUITY (the live-facing
        # ``engine.route`` fallback).  Registered signals (any that passed
        # ``check_entry_gate``) resolve identically to the non-strict path.
        signal = str(signal_row.get("Signal", "")).upper()
        try:
            ac = SIGNAL_REGISTRY.asset_class(signal, strict=True)
        except KeyError as e:
            raise KeyError(
                f"Cannot route signal {signal!r} to a backtest executor: "
                f"it is not registered in quant.core._utils.SIGNAL_REGISTRY. "
                f"Register it (with its asset class) before emitting it."
            ) from e
        return str(ac).upper()

    def _asset_class_for_position(self, position: dict) -> str:
        return str(position.get("_asset_class", "EQUITY")).upper()

    # ── Scheduling (asset-agnostic — lives on the router, not children) ─
    def should_run(self, strategy, now) -> tuple[bool, bool]:
        """Every bar runs both phases — the bar loop drives the schedule."""
        return (True, True)

    def data_end_dt(self, now):
        """Manage-window cutoff = signal bar close (T+I), not fill clock.

        Fill clock is ``T+I+DATA_DELAY`` via enter / manage-triggered exits.
        Extending the manage cutoff to the delayed fill wall would let
        ``call_manage_trade`` see bars live cannot see when the signal first
        becomes known — silent parity drift (D5).
        """
        return pd.Timestamp(calculate_entry_time(now, self.ctx.config.INTERVAL))

    def get_precomputed_signal(self, ticker: str, now) -> pd.DataFrame | None:
        """Return the precomputed signal frame; engine selects by ``bar_ts``."""
        if self.ctx.df_signals is None or self.ctx.df_signals.empty:
            return None
        return self.ctx.df_signals

    # ── State ───────────────────────────────────────────────────────────
    def active_positions(self, strategy_name: str) -> list[PositionDict]:
        return cast("list[PositionDict]", self.ctx.active_positions)

    # ── Execution (dispatch) ────────────────────────────────────────────
    def enter(
        self,
        strategy,
        ticker: str,
        signal_row: dict,
        engine,
        *,
        now=None,
    ) -> dict | None:
        asset_class = self._asset_class_for_signal(signal_row, engine)
        return self._resolve(asset_class).enter(
            strategy, ticker, signal_row, engine, now=now
        )

    def exit(
        self,
        strategy_name: str,
        position: dict,
        sig: dict,
        *,
        manage_df: pd.DataFrame | None = None,
    ) -> bool:
        pos = sig.get("_position", position)
        asset_class = self._asset_class_for_position(pos)
        return self._resolve(asset_class).exit(
            strategy_name, pos, sig, manage_df=manage_df
        )

    def after_entry(self, strategy_name: str, position: dict) -> None:
        return None

    def after_exit(self, strategy_name: str, position: dict) -> None:
        return None

    def after_process_ticker(
        self, strategy_name: str, ticker: str, results: list[dict]
    ) -> None:
        return None

    def record_run(self, strategy_name: str, now, ran_signal: bool) -> None:
        return None

    # ── End-of-run ──────────────────────────────────────────────────────
    def force_close_remaining(self) -> None:
        """Close any positions still active at the end of the backtest."""
        if not self.ctx.active_positions:
            return
        self.ctx.logger.info(
            "Force-closing %d remaining position(s).",
            len(self.ctx.active_positions),
        )
        for pos in list(self.ctx.active_positions):
            asset_class = self._asset_class_for_position(pos)
            self._resolve(asset_class).force_close(pos)
        self.ctx.active_positions.clear()
