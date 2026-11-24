from collections.abc import Sequence
from typing import Any, Protocol, runtime_checkable

import pandas as pd

from quant.utils.options import ICMeta as ICMeta
from quant.utils.positions import PositionDict
from quant.utils.strategy_config import BaseStrategyConfig

# ─────────────────────────────────────────────────────────────────────────────
# Strategy Protocol — catches bad strategies at construction time
# ─────────────────────────────────────────────────────────────────────────────


@runtime_checkable
class StrategyProtocol(Protocol):
    """Minimal interface every strategy must satisfy.
    ``config`` is a strategy-specific config object (subclass of ``BaseStrategyConfig``).
    ``calculate_indicators``, ``generate_signals`` and ``manage_trade`` are
    mandatory — the engine drives both entry and (manage-interval) exit
    management through them.
    """

    config: BaseStrategyConfig

    def calculate_indicators(self, df: pd.DataFrame, **kw) -> pd.DataFrame: ...
    def generate_signals(self, df: pd.DataFrame) -> pd.DataFrame: ...
    def manage_trade(self, position: dict[str, Any], df: pd.DataFrame) -> int: ...


# ─────────────────────────────────────────────────────────────────────────────
# DataHandler Protocol — abstracts data fetching for backtest and live
# ─────────────────────────────────────────────────────────────────────────────
@runtime_checkable
class DataHandlerProtocol(Protocol):
    """Interface for fetching historical data.  Implemented by both
    ``AlpacaDataHandler`` (live) and ``BacktestDataHandler`` (backtest).
    """

    def get_historical_data(
        self,
        ticker: str,
        start,
        end,
        interval: str,
        use_cache: bool = True,
        adjustment: str | None = None,
    ) -> pd.DataFrame:
        """Fetch OHLCV bars for *ticker* over ``[start, end]``.

        *adjustment* is the equity-bar adjustment space (``"all"`` / ``"raw"``);
        handlers for instruments without adjustments (e.g. futures index
        points) accept and ignore it.  ``None`` lets a handler apply its
        natural default.
        """
        ...


# ─────────────────────────────────────────────────────────────────────────────
# ExecutorProtocol — environment boundary between backtest and live
# ─────────────────────────────────────────────────────────────────────────────


@runtime_checkable
class ExecutorProtocol(Protocol):
    """Environment-specific operations consumed by ``engine.process_strategy``.

    Everything that differs between backtest and live trading lives behind
    this protocol: scheduling, data transport, order submission, fill
    simulation, position-state mutation.  Engine logic itself
    (``process_ticker``, ``run_signals``, ``call_manage_trade``, sizing,
    routing) is identical for both.

    Live implementation wraps ``UnifiedExecutor`` + Alpaca data caches.
    Backtest implementation wraps prefetched DataFrames + fill simulators.
    """

    # ── Scheduling ──────────────────────────────────────────────────────
    def should_run(self, strategy, now) -> tuple[bool, bool]:
        """Return ``(should_signal, should_manage)`` for this tick.

        Live: clock-based modulo + de-dup logic.
        Backtest: ``(True, True)`` — every bar runs both phases.
        """
        ...

    def data_end_dt(self, now) -> pd.Timestamp:
        """Return the upper bound for data-fetch windows.

        Live: ``now - LIVE_DATA_DELAY_MINUTES``.
        Backtest: signal bar close (intraday: *now*; daily: 16:00 ET) —
        never the entry clock time (that caused IC manage-window lookahead).
        """
        ...

    def get_precomputed_signal(self, ticker: str, now) -> pd.DataFrame | None:
        """Return a pre-computed 1-row signal DataFrame for *now*.

        Backtest: 1-row slice from the upfront ``df_signals`` so
        ``process_ticker`` can skip ``run_signals`` per bar.
        Live: ``None`` — ``process_ticker`` runs the signal pipeline.
        """
        ...

    # ── State ───────────────────────────────────────────────────────────
    def active_positions(self, strategy_name: str) -> Sequence[PositionDict]:
        """Return the current open positions for *strategy_name*."""
        ...

    # ── Execution ───────────────────────────────────────────────────────
    def enter(
        self,
        strategy: StrategyProtocol,
        ticker: str,
        signal_row: dict,
        engine,
        *,
        now=None,
    ) -> dict | None:
        """Open a position. Returns the new position dict (or None)."""
        ...

    def exit(
        self,
        strategy_name: str,
        position: dict,
        sig: dict,
        *,
        manage_df: pd.DataFrame | None = None,
    ) -> bool:
        """Close *position*. Returns True on success."""
        ...

    def after_entry(self, strategy_name: str, position: dict) -> None:
        """Hook fired after a successful entry (state persistence, etc.)."""
        ...

    def after_exit(self, strategy_name: str, position: dict) -> None:
        """Hook fired after a successful exit (state persistence, etc.)."""
        ...

    def after_process_ticker(
        self, strategy_name: str, ticker: str, results: list[dict]
    ) -> None:
        """Hook fired after each ``process_ticker`` call (debug logging, etc.)."""
        ...

    def record_run(self, strategy_name: str, now, ran_signal: bool) -> None:
        """Record the tick's run state (live: dedup timestamps; backtest: no-op)."""
        ...
