"""
LiveExecutor — ExecutorProtocol adapter for live trading via ``trader.py``.
===========================================================================

Implements ``quant.core.engine.ExecutorProtocol`` so that
``engine.process_strategy`` can drive live ticks with the same
orchestration code path the backtester uses.

Fully self-contained: all state is injected explicitly via constructor
parameters so the executor does not depend on ``UnifiedTrader`` or any
other concrete class.

Protocol method mapping
-----------------------
    should_run            → new closed bar since last run (bar-driven)
    data_end_dt           → now - delay_minutes (config.DATA_DELAY preferred)
    get_precomputed_signal→ None  (engine.run_signals drives live signals)
    active_positions      → active_positions[strategy_name]
    enter                 → _set_client + unified_executor.enter_trade
    exit                  → _set_client + unified_executor.exit_trade
    after_entry           → active_positions[s].append + save_state()
    after_exit            → active_positions[s].remove + save_state()
    after_process_ticker  → no-op
    record_run            → last_update[s] = now (signal only)
                            last_manage_update[s] = now (always)
"""

from __future__ import annotations

import datetime
import os
from typing import TYPE_CHECKING

import pandas as pd

from quant.core.protocols import ExecutorProtocol
from quant.utils.time import closed_bar_asof

if TYPE_CHECKING:
    from quant.core.engine import TradingEngine
    from quant.core.protocols import PositionDict

# Fallback when strategy.config omits DATA_DELAY.  Trader passes the
# env-derived default so @patch("quant.live.trader._LIVE_DATA_DELAY_MINUTES")
# still works in tests.
_LIVE_DATA_DELAY_MINUTES: int = int(os.environ.get("LIVE_DATA_DELAY_MINUTES", "15"))


class LiveExecutor(ExecutorProtocol):
    """ExecutorProtocol adapter for live trading.

    All state is injected explicitly so the executor is self-contained and
    decoupled from any concrete trader class.

    Parameters
    ----------
    last_update
        Shared dict mapping strategy name → last signal-phase timestamp.
    last_manage_update
        Shared dict mapping strategy name → last manage-phase timestamp.
    active_positions
        Shared dict mapping strategy name → list of open position dicts.
    strategies
        Shared list of live strategy instances (used to resolve strategy by name).
    clients
        Shared dict mapping client name → broker client object.
    unified_executor
        Execution sub-component exposing ``set_client``, ``enter_trade``,
        and ``exit_trade``.
    lock
        Re-entrant lock (``threading.RLock``) guarding shared state mutations.
    logger
        Logger instance for execution-phase messages.
    save_state
        Zero-argument callable that persists shared state to disk.
    delay_minutes
        Minutes to subtract from *now* when computing ``data_end_dt``.
        Prefer ``strategy.config.DATA_DELAY``; this is the env fallback.
    """

    def __init__(
        self,
        *,
        last_update: dict,
        last_manage_update: dict,
        active_positions: dict,
        strategies: list,
        clients: dict,
        unified_executor,
        lock,
        logger,
        save_state,
        delay_minutes: int = _LIVE_DATA_DELAY_MINUTES,
    ):
        self._last_update = last_update
        self._last_manage_update = last_manage_update
        self._active_positions = active_positions
        self._strategies = strategies
        self._clients = clients
        self._unified_executor = unified_executor
        self._lock = lock
        self._logger = logger
        self._save_state = save_state
        self._delay_min = delay_minutes

    # ── Scheduling ──────────────────────────────────────────────────────

    def _delay_for(self, strategy) -> int:
        """Config ``DATA_DELAY`` wins; constructor/env value is fallback."""
        cfg = getattr(strategy, "config", None)
        if cfg is not None and hasattr(cfg, "DATA_DELAY"):
            return int(cfg.DATA_DELAY)
        return int(self._delay_min)

    @staticmethod
    def _naive_utc(ts: datetime.datetime) -> datetime.datetime:
        """Canonicalise a scalar timestamp to tz-naive UTC (engine contract).

        Aware inputs are converted to UTC and stripped of tzinfo; naive inputs
        are left untouched. Scheduling math (``closed_bar_asof``, dedup bars)
        stays in the same naive-UTC space the engine and data handlers use.
        """
        if getattr(ts, "tzinfo", None) is not None:
            ts = ts.astimezone(datetime.UTC).replace(tzinfo=None)
        return ts

    def should_run(self, strategy, now: datetime.datetime) -> tuple[bool, bool]:
        """Return ``(should_signal, should_manage)`` for this live tick.

        Bar-driven: fire when ``closed_bar_asof(data_end_dt)`` advances past
        the bar implied by the last recorded run.  When
        ``MANAGE_INTERVAL == INTERVAL``, both flags share one check.
        """
        now = self._naive_utc(now)
        s_name = strategy.name
        interval = getattr(strategy.config, "INTERVAL", "1h")
        manage_interval = getattr(strategy.config, "MANAGE_INTERVAL", interval)

        end_dt = now - datetime.timedelta(minutes=self._delay_for(strategy))
        bar_ts = closed_bar_asof(end_dt, interval)

        should_signal = True
        last_run = self._last_update.get(s_name)
        if last_run is not None:
            last_run = self._naive_utc(last_run)
            last_end = last_run - datetime.timedelta(minutes=self._delay_for(strategy))
            last_bar = closed_bar_asof(last_end, interval)
            if pd.Timestamp(bar_ts) <= pd.Timestamp(last_bar):
                should_signal = False

        if manage_interval == interval:
            should_manage = should_signal
        else:
            manage_bar = closed_bar_asof(end_dt, manage_interval)
            should_manage = True
            last_manage = self._last_manage_update.get(s_name)
            if last_manage is not None:
                last_manage = self._naive_utc(last_manage)
                last_m_end = last_manage - datetime.timedelta(
                    minutes=self._delay_for(strategy)
                )
                last_m_bar = closed_bar_asof(last_m_end, manage_interval)
                if pd.Timestamp(manage_bar) <= pd.Timestamp(last_m_bar):
                    should_manage = False

        return should_signal, should_manage

    def data_end_dt(self, now):
        """Live: ``now - delay`` (delay from last ``should_run`` strategy via
        constructor default; prefer resolving via config at call sites).

        ``process_strategy`` constructs one executor per strategy tick, so
        ``_delay_min`` already reflects that strategy's ``DATA_DELAY``.
        """
        return now - datetime.timedelta(minutes=self._delay_min)

    # ── Data ────────────────────────────────────────────────────────────
    # Bar data is fetched by the engine via ``engine.data_handler``
    # (= ``AlpacaDataHandler``).  No fetch hooks on the executor.

    def get_precomputed_signal(self, ticker: str, now) -> pd.DataFrame | None:
        """Live always runs the full signal pipeline inside process_ticker."""
        return None

    # ── State ───────────────────────────────────────────────────────────

    def active_positions(self, strategy_name: str) -> list[PositionDict]:
        """Return a snapshot copy of the strategy's open positions.

        The backing dict is shared across strategy threads: the engine's own
        worker mutates it via ``after_entry`` / ``after_exit`` under the
        trader lock, while other threads (config sync, cleanup) may touch it
        too.  Returning a shallow copy keeps the engine's iteration and
        length checks stable and free of concurrent-mutation artifacts;
        engine entry-gating reads through this method per ticker, so it
        still sees the latest committed state.
        """
        return list(self._active_positions.get(strategy_name, []))

    # ── Execution helpers ────────────────────────────────────────────────

    def _set_client(self, strategy) -> bool:
        """Point ``unified_executor`` at *strategy*'s client.

        The binding is **thread-local** inside the unified executor: it
        applies to the calling thread only, so when strategies run
        concurrently each one stays pinned to its own broker account and
        cannot redirect another strategy's orders or equity reads.

        Returns ``False`` (and logs an error) if the client is absent or
        the equity tracker is not registered.
        """
        client_name = strategy.client_name
        if client_name not in self._clients:
            self._logger.error(
                f"[{strategy.name}] Client '{client_name}' not found in trading "
                "clients dict — skipping signal."
            )
            return False
        equity_ok = self._unified_executor.set_client(
            client_name, self._clients[client_name]
        )
        if not equity_ok:
            self._logger.error(
                f"[{strategy.name}] Client '{client_name}' not registered in "
                "EquityManager — skipping signal to prevent wrong-account "
                "capital usage. Check that ALPACA_PAPER1_API_KEY (or the "
                "relevant env var) is set and the client initialised correctly."
            )
        return equity_ok

    # ── Execution ───────────────────────────────────────────────────────

    def enter(
        self,
        strategy,
        ticker: str,
        signal_row: dict,
        engine: TradingEngine,  # TODO: update protocol in engine.py to type this as well
        *,
        now=None,
    ) -> dict | None:
        """Live: ``set_client`` + ``unified_executor.enter_trade``."""
        if not self._set_client(strategy):
            return None
        signal_val = signal_row.get("Signal", "UNKNOWN")
        confidence = float(signal_row.get("Confidence", 0.0))
        self._logger.info(
            f"[{strategy.name}] Executing ENTRY: {signal_val} {ticker}"
            f"  conf={confidence:.3f}"
        )
        return self._unified_executor.enter_trade(
            strategy, ticker, signal_row, engine=engine
        )

    def exit(
        self,
        strategy_name: str,
        position: dict,
        sig: dict,
        *,
        manage_df: pd.DataFrame | None = None,
    ) -> bool:
        """Live: ``set_client`` + ``unified_executor.exit_trade``."""
        strategy = next((s for s in self._strategies if s.name == strategy_name), None)
        if strategy is None:
            return False
        if not self._set_client(strategy):
            return False
        ticker = position.get("Ticker")
        reason = sig.get("reason", "Strategy Signal")
        qty_to_sell = float(position.get("Qty", 0))
        self._logger.info(
            f"[{strategy_name}] Executing EXIT: {ticker}"
            f"  id={position.get('OrderId')}  reason={reason}"
        )
        return self._unified_executor.exit_trade(position, qty_to_sell)

    def after_entry(self, strategy_name: str, position: dict) -> None:
        """Append *position* to ``active_positions`` and persist state."""
        with self._lock:
            self._active_positions[strategy_name].append(position)
            self._save_state()

    def after_exit(self, strategy_name: str, position: dict) -> None:
        """Remove *position* from ``active_positions`` and persist state."""
        with self._lock:
            positions = self._active_positions.get(strategy_name, [])
            if position in positions:
                positions.remove(position)
            self._save_state()

    def after_process_ticker(
        self, strategy_name: str, ticker: str, results: list[dict]
    ) -> None:
        """No live-side per-ticker instrumentation needed."""
        return None

    def record_run(self, strategy_name: str, now, ran_signal: bool) -> None:
        """Stamp ``last_update`` (signal phase) and ``last_manage_update`` (always)."""
        with self._lock:
            if ran_signal:
                self._last_update[strategy_name] = now
            self._last_manage_update[strategy_name] = now
