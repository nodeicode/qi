from collections.abc import Sequence
from typing import Any, Protocol, runtime_checkable

import pandas as pd

from quant.data.subscription import Subscription
from quant.utils.positions import PositionDict
from quant.utils.strategy_config import BaseStrategyConfig

# ─────────────────────────────────────────────────────────────────────────────
# Strategy Protocol — catches bad strategies at construction time
# ─────────────────────────────────────────────────────────────────────────────


@runtime_checkable
class StrategyProtocol(Protocol):
    """Declarative strategy surface (spec `strategy-authoring`).

    A strategy declares its typed instruments, data subscriptions, and venue,
    then implements a single author body ``decide(events, portfolio)`` that
    BOTH generates entries and evaluates the portfolio's existing open
    positions for exits. It returns typed ``Order``/exit intents — no signal
    strings or 3-method contracted ``calculate_indicators`` /
    ``generate_signals`` / ``manage_trade`` surface appears here.
    """

    name: str
    config: BaseStrategyConfig
    instruments: list[Any]
    subscriptions: list[Any]
    venue: str

    def decide(self, events, portfolio) -> list[Any]: ...


# ─────────────────────────────────────────────────────────────────────────────
# DataHandler Protocol — abstracts data fetching for backtest and live
# ─────────────────────────────────────────────────────────────────────────────
@runtime_checkable
class DataHandlerProtocol(Protocol):
    """Interface for fetching historical data.  Implemented by both
    ``AlpacaDataHandler`` (live) and ``BacktestDataHandler`` (backtest).
    """

    def get_data(
        self,
        subscription: Subscription,
        start,
        end,
        use_cache: bool = True,
        adjustment: str | None = None,
    ) -> pd.DataFrame:
        """Fulfill a typed ``(instrument, datatype, cadence)`` subscription.

        Ships the ``bars`` datatype: returns OHLCV bars for the subscription's
        instrument over ``[start, end]`` at its cadence, with the canonical
        ``BARS_COLUMNS`` shape on both the backtest and live roots. The
        ``adjustment`` argument is the equity-bar adjustment space (``"all"`` /
        ``"raw"``); handlers for instruments without adjustments accept and
        ignore it. Unsupported datatypes raise.
        """
        ...

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

        The engine-owned fetch path (``fetch_data`` / ``fetch_manage_data``)
        still consumes this while the engine is migrated to subscription
        buffers; it resolves to the same bars as :meth:`get_data`.
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

    Live implementation wraps the live venue's execution client (the Alpaca
    data caches + broker submission).  Backtest implementation wraps
    prefetched DataFrames + fill simulators.
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
