#!/usr/bin/env python
"""
Unified Trader
Unified workflow engine for running multiple strategies.
Deploys self-contained modular strategies in a multi-threaded runner.
Manages market open/close, order management, data preloading, and persistence.

Supported live strategy shape (parity with backtest):
    INTERVAL == MANAGE_INTERVAL
    DATA_DELAY on strategy config (env LIVE_DATA_DELAY_MINUTES is fallback only)
    optional IC_ENTRY_* bar gate inside generate_signals only
    no internal data fetches in generate_signals / calculate_indicators

Reference strategy: example_equity (15m / 15m).

Usage:
    python trader.py
"""

import atexit
import concurrent.futures
import datetime
import logging
import os
import threading
import time
from typing import Any

from quant.core.engine import TradingEngine
from quant.core.protocols import ExecutorProtocol
from quant.data.data_handler import AlpacaDataHandler

# -----------------------------------------------------------------------------
# Imports
# -----------------------------------------------------------------------------
from quant.live.config_sync import ConfigSync
from quant.live.executors.live_executor import LiveExecutor
from quant.live.executors.unified_executor import UnifiedExecutor
from quant.live.market_schedule import MarketSchedule
from quant.live.protocols import (
    LivePositionDict,
    LiveStrategyProtocol,
    is_live_position,
)
from quant.live.strategy_loader import load_strategies
from quant.live.utils import (
    load_state,
    resolve_env,
    save_state,
    setup_logging,
)
from quant.observability import Span, SpanName

# -----------------------------------------------------------------------------
# Logging Setup
# -----------------------------------------------------------------------------
logger = setup_logging()


def cleanup():
    logger.info("Shutting down trader...")
    logging.shutdown()


atexit.register(cleanup)


# -----------------------------------------------------------------------------
# Data-delay constants
# Alpaca free-tier delivers data with a 15-minute delay.  Env value is the
# **fallback** when a strategy config omits DATA_DELAY; strategy.config wins.
# TODO: set LIVE_DATA_DELAY_MINUTES=0 when upgrading to real-time SIP.
# -----------------------------------------------------------------------------
_LIVE_DATA_DELAY_MINUTES: int = int(os.environ.get("LIVE_DATA_DELAY_MINUTES", "15"))


# -----------------------------------------------------------------------------
# Unified Trader Class
# -----------------------------------------------------------------------------
class UnifiedTrader:
    def __init__(self):
        self.logger = logger

        # Determine environment and SSM parameter name.
        _env = resolve_env(self.logger)
        _ssm_param = f"/trading/{_env}/strategies_config"

        # ConfigSync is a passive poller: no threads, no callbacks. The main
        # loop calls poll() each tick and applies any change synchronously.
        self.config_sync = ConfigSync(ssm_param_name=_ssm_param, logger=self.logger)

        # Initial config load — poll() returns the SSM payload or None on any
        # error / validation failure; fall back to empty so the trader starts.
        self.config = self.config_sync.poll() or {"strategies": []}

        # Initialize Alpaca Clients
        self.data_handler = AlpacaDataHandler()

        # Initialize Market Schedule
        self.market_schedule = MarketSchedule(self.logger)

        # Initialize Strategies
        self.strategies = load_strategies(self.config, self.data_handler, self.logger)

        # Optional monitor emit hook (signal drift + latency telemetry). On by
        # default; MONITOR=0 to opt out. Resolved before engines so
        # TradingEngine can take an inject emit callback — quant never imports
        # quant.live.monitor. The queue consumer is a developer-built
        # extension (design D7); without one wired, ``emit`` is a no-op and
        # nothing is emitted.
        self._monitor_emit = None
        self._monitor_enabled = os.environ.get("MONITOR", "1") == "1"
        if self._monitor_enabled:
            try:
                from quant.live.monitor.emit import emit as monitor_emit

                self._monitor_emit = monitor_emit
            except Exception as e:
                self.logger.exception("[MONITOR] failed to wire emit hook: %s", e)
                self._monitor_enabled = False
        else:
            self.logger.info("[MONITOR] disabled (set MONITOR=1 to enable)")

        # Centralized TradingEngine per strategy (shared logic with backtester)
        self.engines: dict[str, TradingEngine] = {
            s.name: TradingEngine(
                s,
                self.data_handler,
                monitor_emit=self._monitor_emit,
            )
            for s in self.strategies
        }

        # Initialize Unified Executor
        # Pass all registered clients so each account's equity is tracked independently.
        self.unified_executor = UnifiedExecutor(
            self.data_handler.clients,
            self.data_handler,
            self.logger,
        )

        # Sync Trading Clients (Paper and Live)
        self.clients = self.data_handler.clients
        self.logger.info(
            f"Trading Clients loaded from DataHandler: {list(self.clients.keys())}"
        )

        # State Management
        self.STATE_FILE = "trader_state.json"
        self.active_positions: dict[str, list[LivePositionDict]] = {
            s.name: [] for s in self.strategies
        }
        self.last_update: dict[str, datetime.datetime] = {}
        # Separate de-dup timer for MANAGE_INTERVAL exit checks so that
        # exits are evaluated every MANAGE_INTERVAL even when INTERVAL is 1d.
        self.last_manage_update: dict[str, datetime.datetime] = {}

        self._load_local_state()

        # RLock guards shared state accessed from process_strategy worker threads.
        self.lock = threading.RLock()

        # Purge any IC positions that expired while the trader was offline.
        self._cleanup_expired_positions()

        # Update Market Hours
        client = next(iter(self.clients.values()), None)
        self.market_schedule.update_market_hours(client)

    # -------------------------------------------------------------------------
    # Live Config Updates
    # -------------------------------------------------------------------------

    def _apply_config_update(self, new_config: dict[str, Any]) -> None:
        """Reconcile running strategies against an SSM config update.

        Called from the main loop only, between strategy fan-outs — no lock
        needed as no worker threads are active at this point.

        * **Added** — new base name: fresh instances wired into all state dicts.
        * **Removed** — base name gone from config: instance dropped; positions
          preserved so open positions are not orphaned.
        * **Changed** — base name present in both: overrides applied in-place.

        Removal is decided from raw config names, not from ``load_strategies``
        success, so a transient import failure cannot silently disable a strategy.
        """
        new_entries: dict[str, dict[str, Any]] = {
            sc["name"]: sc
            for sc in new_config.get("strategies", [])
            if isinstance(sc, dict) and isinstance(sc.get("name"), str)
        }

        # Guard: if every entry was malformed, bail out rather than removing all.
        if not new_entries and new_config.get("strategies"):
            self.logger.error(
                "[ConfigSync] update contained no valid strategy entries "
                "\u2014 ignoring to avoid wiping running strategies."
            )
            return

        # Instance names are "<base>_<client>" by convention (strategy_loader).
        def _base_of(full_name: str) -> str:
            return full_name.rsplit("_", 1)[0]

        new_instances: dict[str, LiveStrategyProtocol] = {
            s.name: s
            for s in load_strategies(new_config, self.data_handler, self.logger)
        }

        current: dict[str, LiveStrategyProtocol] = {s.name: s for s in self.strategies}
        added = [name for name in new_instances if name not in current]

        # Removal uses a two-tier check so that both of these work correctly:
        #
        # 1. Base name gone from config → always remove the instance.
        # 2. Base name still in config but a *different* instance name was
        #    produced by load_strategies → client changed; remove old, add new.
        # 3. Base name still in config and load_strategies returned nothing
        #    (transient import failure) → preserve the running instance; do NOT
        #    remove it just because load_strategies couldn't recreate it.
        #
        # Case 3 is distinguished from case 2 by checking whether *any*
        # new instance shares the same base name.  An empty new_instances set
        # caused by a load failure contains no such entry, so the running
        # instance is kept.
        new_instance_bases = {_base_of(name) for name in new_instances}
        removed = [
            name
            for name in current
            if _base_of(name) not in new_entries  # case 1: dropped from config
            or (
                name not in new_instances  # not recreated verbatim
                and _base_of(name)
                in new_instance_bases  # but base exists → client changed (case 2)
            )
        ]
        kept = [name for name in current if name not in removed]

        for name in removed:
            self.strategies = [s for s in self.strategies if s.name != name]
            self.engines.pop(name, None)
            self.last_update.pop(name, None)
            self.last_manage_update.pop(name, None)
            self.logger.info(f"[ConfigSync] {name}: removed.")

        for name in added:
            instance = new_instances[name]
            self.strategies.append(instance)
            self.engines[name] = TradingEngine(
                instance,
                self.data_handler,
                monitor_emit=self._monitor_emit,
            )
            # preserve any orphaned active positions
            self.active_positions.setdefault(name, [])
            # clean last_update and last_manage_update to avoid processing with stale timestamps
            self.last_update.pop(name, None)
            self.last_manage_update.pop(name, None)
            self.logger.info(f"[ConfigSync] {name}: added.")

        for name in kept:
            strategy = current[name]
            entry = new_entries.get(_base_of(name))
            if entry is None or not hasattr(strategy, "config"):
                continue
            overrides = entry.get("config", {}) or {}
            if not overrides:
                continue
            applied: list[str] = []
            for k, v in overrides.items():
                if not hasattr(strategy.config, k):
                    self.logger.warning(
                        f"[ConfigSync] {strategy.name}: unknown key '{k}' \u2014 skipped."
                    )
                    continue
                try:
                    setattr(strategy.config, k, v)
                    applied.append(k)
                except Exception as e:
                    self.logger.error(
                        f"[ConfigSync] {strategy.name}: failed to set '{k}': {e}"
                    )
            if applied:
                self.logger.info(
                    f"[ConfigSync] {strategy.name}: applied {len(applied)} override(s): {applied}"
                )

        # Strategy basket may have changed (tickers/interval) - keep the live
        # bar stream subscription aligned.
        try:
            self._reconcile_bar_stream()
        except Exception as e:  # pragma: no cover - defensive
            self.logger.warning(f"[ConfigSync] bar stream reconcile failed: {e}")

    # -------------------------------------------------------------------------
    # State Reconciliation
    # -------------------------------------------------------------------------

    def _cleanup_expired_positions(self) -> None:
        """
        Remove IC positions whose expiration date has already passed.

        Called at startup and every market-closed minute so that positions
        auto-settled by Alpaca are always purged from local state, even when
        the trader was offline at expiry or the exit order never filled.
        """
        today = datetime.datetime.now(datetime.UTC).date()
        changed = False
        with self.lock:
            for positions in self.active_positions.values():
                expired = []
                for pos in positions:
                    if str(pos.get("AssetType", "")).upper() != "IC":
                        continue
                    exp_str = pos.get("Expiration", "")
                    if not exp_str:
                        continue
                    try:
                        if datetime.date.fromisoformat(exp_str) < today:
                            expired.append(pos)
                    except ValueError:
                        pass
                for pos in expired:
                    self.logger.info(
                        f"[Reconcile] Expired IC removed from state: "
                        f"{pos.get('Ticker')} exp={pos.get('Expiration')}"
                    )
                    positions.remove(pos)
                    changed = True
        if changed:
            self._save_state()

    # -------------------------------------------------------------------------
    # State Persistence
    # -------------------------------------------------------------------------
    def _save_state(self) -> None:
        # Uses helper function with self.lock
        save_state(
            self.STATE_FILE,
            {
                "active_positions": self.active_positions,
                "last_update": {k: v.isoformat() for k, v in self.last_update.items()},
                "last_manage_update": {
                    k: v.isoformat() for k, v in self.last_manage_update.items()
                },
            },
            self.lock,
            self.logger,
        )

    def _load_local_state(self) -> None:
        state = load_state(self.STATE_FILE, self.logger)
        if not state:
            return

        # Restore positions
        saved_positions = state.get("active_positions", {})
        for s_name, positions in saved_positions.items():
            if s_name not in self.active_positions:
                # Strategy removed since state was written — do not restore into
                # an unknown slot.
                continue
            restored: list[LivePositionDict] = []
            for p in positions:
                # Boundary validation at recovery: a partial/malformed record is
                # dropped + logged loudly (fail closed on that record) instead
                # of being cast in and later traded with required keys missing.
                if isinstance(p, dict) and is_live_position(p):
                    restored.append(p)
                else:
                    self.logger.warning(
                        "[TRADER] dropping malformed recovered position for %s: %s",
                        s_name,
                        p,
                    )
            self.active_positions[s_name] = restored

        # Restore timestamps
        for k, v in state.get("last_update", {}).items():
            self.last_update[k] = datetime.datetime.fromisoformat(v)
        for k, v in state.get("last_manage_update", {}).items():
            self.last_manage_update[k] = datetime.datetime.fromisoformat(v)

    # -------------------------------------------------------------------------
    # Data & Strategy Execution
    # -------------------------------------------------------------------------
    def process_strategy(
        self, strategy: LiveStrategyProtocol, now: datetime.datetime
    ) -> list:
        """Delegate to ``engine.process_strategy`` via ``LiveExecutor``.

        Scheduling, data fetching, signal generation, and order execution
        all happen inside this call through the executor hooks.

        Exceptions are caught here so that one broken strategy cannot prevent
        others from running in the same tick.  The per-future try/except in
        ``run()`` acts as a secondary safety net for any exception that escapes
        this handler.

        ``now`` is canonicalised to tz-naive UTC (the engine's contract) before
        delegation; aware callers (and production ``run()``) are normalized here.
        """
        if now.tzinfo is not None:
            now = now.astimezone(datetime.UTC).replace(tzinfo=None)
        try:
            engine = self.engines[strategy.name]
            delay = getattr(strategy.config, "DATA_DELAY", _LIVE_DATA_DELAY_MINUTES)
            executor: ExecutorProtocol = LiveExecutor(
                last_update=self.last_update,
                last_manage_update=self.last_manage_update,
                active_positions=self.active_positions,
                strategies=self.strategies,
                clients=self.clients,
                unified_executor=self.unified_executor,
                lock=self.lock,
                logger=self.logger,
                save_state=self._save_state,
                delay_minutes=int(delay),
            )
            engine.process_strategy(strategy, now, executor)
        except Exception as e:
            self.logger.exception(
                f"[{getattr(strategy, 'name', '?')}] Unhandled error in strategy runner: {e}"
            )
        return []

    def _process_strategy_timed(
        self, strategy: LiveStrategyProtocol, now: datetime.datetime, submit_ns: int
    ) -> list:
        """Worker entrypoint that measures dispatch jitter.

        ``submit_ns`` is captured by the caller at ``pool.submit`` time; the
        elapsed wall time until this worker actually starts is published via the
        ``tick_dispatch_ns`` contextvar, which the engine's monitor events pick
        up without any engine↔trader coupling.
        """
        from quant.core._utils import set_tick_dispatch_ns, tick_dispatch_ns
        from quant.live.utils import suspend_gc

        # Optional GC hot-path pause around the decision compute. Measured
        # neutral-to-slightly-worse on example_equity at live frame size (see
        # scripts/bench_gc.py), so OFF by default. Flip GC_PAUSE_HOTPATH=1 to
        # A/B it against strategy_calc_total_ns / generate_signals_ns on future
        # workloads.
        use_gc_pause = os.environ.get("GC_PAUSE_HOTPATH", "0") == "1"

        dispatch_ns = time.perf_counter_ns() - submit_ns
        prev = tick_dispatch_ns()
        set_tick_dispatch_ns(dispatch_ns)
        try:
            if use_gc_pause:
                with suspend_gc():
                    return self.process_strategy(strategy, now)
            return self.process_strategy(strategy, now)
        finally:
            set_tick_dispatch_ns(prev)

    # -------------------------------------------------------------------------
    # Main Loop
    # -------------------------------------------------------------------------
    # Intervals the MemBars aggregator can bucket from minute bars.
    _AGG_INTERVALS = frozenset({"1m", "5m", "15m", "30m", "1h"})

    def _desired_bar_stream(self) -> tuple[list[str], str | None]:
        """Desired (tickers, interval) from the current strategy basket.

        Tickers = union of configured TICKERS. Interval = first strategy
        interval the aggregator supports (1m/5m/15m/30m/1h). Strategies with
        other intervals (e.g. 1d, or 2h/4h) keep the existing HTTP/cache path.
        Returns (tickers, None) when nothing is streamable.
        """
        tickers = sorted(
            {
                str(t).upper()
                for s in self.strategies
                for t in (getattr(s.config, "TICKERS", None) or [])
            }
        )
        intervals = sorted(
            {str(getattr(s.config, "INTERVAL", "15m")).lower() for s in self.strategies}
        )
        supported = [i for i in intervals if i in self._AGG_INTERVALS]
        return tickers, (supported[0] if supported else None)

    def _reconcile_bar_stream(self) -> None:
        """Keep the live delayed-SIP subscription aligned with the strategies.

        Feeds the data handler's in-memory bar store (MemBars), which then
        serves fully-covered intraday ``get_historical_data`` windows without
        touching stock_cache or HTTP. Alpaca enforces a single live stream
        connection per account, and the handler guards against double-start.

        Called at startup and after every config update: if the desired
        ticker/interval basket changed, stop + restart the stream so
        subscriptions follow strategy changes. The aggregator retains its
        frames across restarts, and reads fall back to REST/cache during the
        brief reconnect window. Best-effort: failure leaves the REST/cache
        path authoritative; new-ticker seeds land at the next warmup.
        """
        if not hasattr(self.data_handler, "start_bar_stream"):
            return
        tickers, interval = self._desired_bar_stream()
        desired = (tickers, interval)
        if desired == getattr(self, "_active_bar_stream", None):
            return  # unchanged
        self._stop_bar_stream()  # no-op when nothing active
        self._active_bar_stream = None
        if not tickers:
            self.logger.info("no tickers configured - bar stream not started")
            return
        if interval is None:
            self.logger.info(
                "no aggregator-supported interval configured - bar stream not started"
            )
            return
        try:
            self.data_handler.start_bar_stream(tickers=tickers, interval=interval)
        except Exception as e:  # best-effort: HTTP/cache remains authoritative
            self.logger.warning("bar stream failed to start: %s", e)
            return
        self._active_bar_stream = desired
        self.logger.info("delayed-SIP bar stream: %s @ %s", tickers, interval)

    def _stop_bar_stream(self) -> None:
        """Stop live ingestion (shutdown); reads fall back to REST/cache."""
        stop = getattr(self.data_handler, "stop_bar_stream", None)
        if stop is not None:
            try:
                stop()
            except Exception as e:
                self.logger.warning("bar stream stop failed: %s", e)

    def _warm_up_all(self, now: datetime.datetime) -> bool:
        """Best-effort strategy-agnostic cache warm-up for every engine.

        Runs the standard ``run_signals`` pipeline (``calculate_indicators`` +
        ``generate_signals``) + a lookback fetch via each engine's own data
        handler — the same code path ``process_strategy`` uses — so any strategy
        warms its in-memory caches (AmFeatureStore, ET-decode, signal cache)
        before the first bar of a session is processed. Strategy-agnostic by
        construction: only base-config attributes are read and the generic
        ``StrategyProtocol`` interface is used (get strategy name / tickers).

        This is an *startup/pre-open* operation and is intentionally **excluded
        from the latency SLOs**: the trader now supports starting a minute or two
        before market open so warm-up happens off the first-bar decision path.
        Returns True if anything was warmed.
        """
        # Warmup is off the decision-path SLOs by design, but it is still
        # observable: the monitor tracks it as its own non-SLO series
        # (``kind: warmup`` / ``warmup_rollup``), never inside the hot-path
        # span rollup.

        per_strategy_ns: dict[str, int] = {}
        event: dict[str, Any] = {
            "kind": "warmup",
            "warmed": False,
            "per_strategy_ns": per_strategy_ns,
        }
        warmed = False
        with Span(SpanName.WARMUP_TOTAL, emit=self._monitor_emit, event=event):
            for s_name, engine in self.engines.items():
                try:
                    _ts = time.perf_counter_ns()
                    if engine.warmup(now):
                        warmed = True
                    per_strategy_ns[str(s_name)] = time.perf_counter_ns() - _ts
                except Exception:
                    # Warm-up is best-effort — a failure just means the first tick
                    # runs cold (the normal pre-warm behavior). Never crash startup.
                    self.logger.exception(f"[{s_name}] warm-up failed, will run cold")
            event["warmed"] = warmed
        return warmed

    def run(self) -> None:
        self.logger.info("Starting Unified Trader...")

        # Start the live delayed-SIP minute-bar fill FIRST so bars bank in the
        # in-memory store while warm-up runs; the store then serves intraday
        # fetches without HTTP or stock_cache.
        try:
            self._reconcile_bar_stream()
        except Exception as e:  # pragma: no cover - defensive
            self.logger.warning("bar stream failed to start: %s", e)

        # Startup warm-up — run BEFORE the loop so every engine is pre-heated
        # for the first bar of the session. Works whether we start during market
        # hours or #TODO (as intended for the future) remove or run a minute or two before open. This
        # is off the SLO path by design.
        try:
            self._warm_up_all(datetime.datetime.now(datetime.UTC))
        except Exception as e:  # pragma: no cover - defensive
            self.logger.exception(f"startup warm-up failed: {e}")

        # One strategy pool for the process lifetime (thread spawn per minute was
        # pure churn); sized generously so runtime-added strategies queue rather
        # than spawn new threads.  Shut down on exit.
        pool = concurrent.futures.ThreadPoolExecutor(
            max_workers=max(4, len(self.strategies) * 2)
        )
        try:
            self._main_loop(pool)
        finally:
            self._stop_bar_stream()
            pool.shutdown(wait=True)

    def _main_loop(self, pool: concurrent.futures.ThreadPoolExecutor) -> None:
        last_loop_start: datetime.datetime | None = None
        while True:
            try:
                now = datetime.datetime.now(datetime.UTC)
                # Main-loop cycle drift vs the nominal 60 s cadence
                # (sleep drift + config-poll contention), published on the
                # heartbeat so it is never charged to any single strategy.
                main_loop_lag_ns = 0
                if last_loop_start is not None:
                    main_loop_lag_ns = (
                        int((now - last_loop_start).total_seconds() * 1e9)
                        - 60_000_000_000
                    )
                last_loop_start = now

                client = next(iter(self.clients.values()), None)
                if not self.market_schedule.is_market_open(client, now):
                    self._cleanup_expired_positions()
                    self.logger.info("Market closed. Waiting...")
                    time.sleep(60)
                    continue

                # Poll SSM for config changes once per tick (lightweight — only
                # parses JSON when the SSM Version actually changes).
                new_cfg = self.config_sync.poll()
                if new_cfg is not None:
                    self._apply_config_update(new_cfg)

                # Run strategies in parallel. Signals are Generated and Executed
                # within engine.process_strategy.  One pool for the process
                # lifetime (created in run()); runtime-added strategies queue.
                # The submit timestamp lets each worker measure dispatch
                # jitter (loop → pool → worker start) for the monitor plane.
                if self.strategies:
                    futures = {
                        pool.submit(
                            self._process_strategy_timed, s, now, time.perf_counter_ns()
                        ): s
                        for s in self.strategies
                    }
                    for future in concurrent.futures.as_completed(futures):
                        strategy = futures[future]
                        try:
                            future.result()
                        except Exception as e:
                            self.logger.exception(
                                f"[{strategy.name}] Unhandled error in strategy runner: {e}"
                            )

                # Save State
                self._save_state()

                if self._monitor_enabled:
                    try:
                        from quant.live.monitor.emit import emit as monitor_emit

                        monitor_emit(
                            {"kind": "heartbeat", "main_loop_lag_ns": main_loop_lag_ns}
                        )
                    except Exception:
                        pass

                # Sleep until next minute starts to prevent drift
                now_local = datetime.datetime.now()
                next_minute = (now_local + datetime.timedelta(minutes=1)).replace(
                    second=0, microsecond=0
                )
                sleep_seconds = (next_minute - now_local).total_seconds()
                time.sleep(max(0.1, sleep_seconds))

            except KeyboardInterrupt:
                self.logger.info("Stopping...")
                self._stop_bar_stream()
                break
            except Exception as e:
                self.logger.error(f"Main loop error: {e}")
                time.sleep(60)


if __name__ == "__main__":  # pragma: no cover
    trader = UnifiedTrader()
    trader.run()
