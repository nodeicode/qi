"""
Trading Engine — centralized decision logic for backtest and live.
=================================================================

Both the backtester (``BacktestExecutor``) and live trader instantiate a
``TradingEngine`` per strategy.  **Every** trading decision — signal
generation, entry gating, sizing, indicator injection, manage_trade,
and position schema — flows through this class so that both environments
run identical logic.

Environment-specific concerns (simulated fills vs API orders) remain
in the ``ExecutorProtocol`` implementor; this module owns the
*decisions*, not the *execution*.

Pipeline (via ``process_strategy``)
------------------------------------
1. **Signals** — ``run_signals`` calls ``calculate_indicators`` + ``generate_signals``.
2. **Manage** — ``call_manage_trade`` injects indicators and runs ``manage_trade``.
3. **Sizing & position** — ``route`` / ``size_*`` / ``build_position``.
"""

from __future__ import annotations

import datetime
import logging
import time
from collections import OrderedDict
from collections.abc import Callable, Sequence
from typing import TYPE_CHECKING, Any, cast

import pandas as pd

from quant.core._utils import (
    DEFAULT_INDICATOR_COLUMNS,
    SIGNAL_REGISTRY,
    MonitorTelemetry,
    TickMonitorContext,
    check_entry_gate,
    evaluate_manage_exit,
    fetch_data,
    fetch_manage_data,
    resolve_data_delay_min,
    select_signal_row,
    signal_asset_class,
    signal_direction,
)
from quant.observability import Span, SpanName, span_key
from quant.portfolio import Position, PositionState
from quant.utils.positions import build_position_dict
from quant.utils.strategy_config import BaseStrategyConfig
from quant.utils.time import closed_bar_asof

if TYPE_CHECKING:
    from quant.core.protocols import (
        DataHandlerProtocol,
        ExecutorProtocol,
        PositionDict,
        StrategyProtocol,
    )

__all__ = ["TradingEngine"]

logger = logging.getLogger(__name__)


class TradingEngine:
    """Shared trading decisions used by both backtester and live trader.

    Instantiate one per strategy (each strategy has its own config).

    Parameters
    ----------
    strategy : object
        Strategy instance that exposes ``config`` (a ``BaseStrategyConfig`` instance),
        ``manage_trade(position, df)``, ``calculate_indicators(df)``, ``generate_signals(df_ind)``.
    data_handler : DataHandlerProtocol
        Provides ``get_historical_data(ticker, start, end, interval)``.
        Live: ``AlpacaDataHandler`` (persistent disk cache).
        Backtest: ``BacktestDataHandler`` (searchsorted views of
        pre-loaded DataFrames).
    """

    # Maximum number of tickers to keep in the signal cache.
    _SIGNAL_CACHE_MAXLEN: int = 64

    def __init__(
        self,
        strategy: StrategyProtocol,
        data_handler: DataHandlerProtocol,
        *,
        monitor_emit: Callable[[dict[str, Any]], None] | None = None,
    ):
        # The 3-method contracted strategy validation (calculate_indicators /
        # generate_signals / manage_trade) is dissolved: the declarative surface
        # speaks one author body, ``decide(events, portfolio)``. Construction no
        # longer enforces the legacy methods (the engine drives the declarative
        # body through the reshaped pipeline).
        config = getattr(strategy, "config", None)
        if not isinstance(config, BaseStrategyConfig):
            raise TypeError(
                f"Strategy {type(strategy).__name__!r} config must be a "
                "BaseStrategyConfig instance"
            )

        # ``Any`` while the engine is mid-transition to the declarative surface:
        # construction no longer enforces the legacy methods, and the author
        # body (``decide``) is driven through the reshaped pipeline. The legacy
        # signal/manage calls below remain until that pipeline lands.
        self.strategy: Any = strategy
        # Data handler implementing ``DataHandlerProtocol``.
        # Live: ``AlpacaDataHandler`` (persistent disk cache via StockDataCache).
        # Backtest: ``BacktestDataHandler`` wrapping pre-loaded DataFrames.
        self.data_handler: DataHandlerProtocol = data_handler
        # Optional live-only hook (wired by trader → python.monitor.emit).
        # Backtest leaves this None so quant never imports python.
        self._monitor_emit = monitor_emit
        self._monitor_telemetry = MonitorTelemetry(monitor_emit)
        self._log = logging.getLogger(
            f"{__name__}.{getattr(strategy, 'name', 'engine')}"
        )

        # --- P2: Resolve indicator columns from strategy or default ---
        self._indicator_columns: list[str] = list(
            getattr(strategy, "INDICATOR_COLUMNS", DEFAULT_INDICATOR_COLUMNS)
        )

        # --- P3: Bounded signal cache (LRU via OrderedDict) ---
        self._signal_cache: OrderedDict[str, pd.DataFrame] = OrderedDict()
        # Columns present at cache time — used to detect mid-run config
        # changes that would leave inject_indicators silently NaN-filling (D6).
        self._signal_cache_cols: dict[str, frozenset[str]] = {}

        self._last_signal_spans: dict[str, int] = {}

    # ─────────────────────────────────────────────────────────────────
    # 1. Signal pipeline
    # ─────────────────────────────────────────────────────────────────

    def run_signals(
        self,
        df: pd.DataFrame,
        ticker: str
        | None = None,  # TODO: we don't need the ticker kwarg it should be deprecated
    ) -> pd.DataFrame:
        """Run the full signal pipeline: indicators -> signals -> cache.

        Strategies that accept a ``ticker`` kwarg for cross-asset logic
        receive it automatically; those that don't are called without it.
        """
        self._last_signal_spans = {}
        with Span(SpanName.STRATEGY_CALC_TOTAL, collect=self._last_signal_spans):
            # No signature sniffing: the author body consumes delivered events,
            # not a per-call ticker kwarg, so indicators always run over the bar df.
            df_ind = self.strategy.calculate_indicators(df)
        with Span(SpanName.GENERATE_SIGNALS, collect=self._last_signal_spans):
            df_sig = self.strategy.generate_signals(df_ind)
        self._last_signal_spans[span_key(SpanName.RUN_SIGNALS)] = sum(
            self._last_signal_spans.values()
        )

        if ticker is not None:
            self.cache_signals(ticker, df_sig)

        return df_sig

    # ─────────────────────────────────────────────────────────────────
    # Warm start
    # ─────────────────────────────────────────────────────────────────

    def warmup(self, now: datetime.datetime) -> bool:
        """Pre-warm this engine's caches so the first bar of a session is cheap.

        Strategy-agnostic by construction: it reads only base-config attributes
        (``INTERVAL``, ``PRE_LOAD_DAYS``, ``TICKERS``, ``DATA_DELAY``) and runs the standard ``run_signals`` pipeline via
        the generic ``StrategyProtocol`` interface — no strategy-specific code is
        touched. Any strategy is warmed the same way on init before processing
        the first bar at market open.

        Best-effort: fetches the same lookback frame the engine would fetch and
        runs the full signal pipeline (calculate_indicators + generate_signals)
        so the strategy's in-memory caches (e.g. AmFeatureStore, ET-decode for example_equity) and
        the engine signal cache are warm. Prevents the ~127ms cold-start spike on
        the first ``process_ticker`` of a trading day (or after a restart). Call
        once per trading date (e.g. trader startup / session rollover).

        **Warmup is *compute* warm, not *edge* warm.** It snapshots as-of
        ``now - DATA_DELAY``; bars newer than that (the first decision bars of
        the session) are delivered by the feed, never pre-fetched here. The
        warmup seed tags the store through the *next* decision bar
        (``seed_end = end_dt + MANAGE_INTERVAL``) so an early pre-open seed never
        truncates bars the stream banks in the warmup-to-first-decision window;
        MemBars clamps the un-banked tail.

        **SLO exclusion:** this runs off the decision path (startup / pre-open)
        and emits no monitor events, so it never contributes to the hot-path,
        fresh-generation, or queue-lag latency SLOs.

        Returns True if any ticker was warmed.
        """
        strategy = self.strategy
        config = getattr(strategy, "config", None)
        interval = getattr(config, "INTERVAL", "1h")
        lookback = getattr(config, "PRE_LOAD_DAYS", 5)
        tickers = getattr(config, "TICKERS", []) or []
        end_dt = pd.Timestamp(now) - pd.to_timedelta(
            getattr(config, "DATA_DELAY", 0) or 0, unit="m"
        )
        # Seed boundary for the read fast path: REST authority ends one manage
        # interval past the signal cutoff so a pre-open warmup never truncates
        # freshly banked decision bars (see docstring).
        _manage_interval = getattr(config, "MANAGE_INTERVAL", interval) or interval
        try:
            seed_end = end_dt + pd.to_timedelta(_manage_interval)
        except (TypeError, ValueError):
            seed_end = end_dt
        warmed = False
        for ticker in tickers:
            try:
                df = fetch_data(
                    self.data_handler,
                    ticker,
                    end_dt,
                    lookback,
                    interval,
                )
                if df is None or df.empty:
                    continue
                # Seed the in-memory live store for both adjustment keys so the
                # read fast path can serve them without touching stock_cache. The
                # raw twin is fetched exactly once here and seeded for strategy-
                # internal raw intraday reads; Raw_Close itself is backtest-only
                # (data-passage invariant) and never attached on the live path.
                df_raw = None
                _seed_mem = getattr(self.data_handler, "seed_mem_bars", None)
                if _seed_mem is not None:
                    _seed_mem(ticker, interval, "all", df, seed_end=seed_end)
                    df_raw = fetch_data(
                        self.data_handler,
                        ticker,
                        end_dt,
                        lookback,
                        interval,
                        adjustment="raw",
                    )
                    if df_raw is not None and not df_raw.empty:
                        _seed_mem(ticker, interval, "raw", df_raw, seed_end=seed_end)
                if df is not None and not df.empty:
                    self.run_signals(df, ticker=ticker)
                    warmed = True
            except Exception as e:  # warm is best-effort
                self._log.exception("warmup failed for %s: %s", ticker, e)
        return warmed

    def latest_signal(
        self,
        df_sig: pd.DataFrame,
        bar_ts=None,
    ) -> tuple[str, float, dict]:
        """Extract (signal, confidence, signal_row) at *bar_ts*.

        When *bar_ts* is omitted, uses the last index label (tests / callers
        that already narrowed the frame). Prefer passing *bar_ts* from
        ``closed_bar_asof`` so live and backtest share selection semantics.
        """
        if bar_ts is None:
            if df_sig is None or df_sig.empty:
                return "HOLD", 0.0, {"_bar_time": None}
            bar_ts = df_sig.index[-1]
        selected = select_signal_row(df_sig, bar_ts)
        if selected is None:
            return "HOLD", 0.0, {"_bar_time": pd.Timestamp(bar_ts)}
        return selected

    # ─────────────────────────────────────────────────────────────────
    # Signal routing
    # ─────────────────────────────────────────────────────────────────

    def route(self, signal: str) -> str:
        """Map a signal string to EQUITY / OPTION / SPREAD / IC."""
        return signal_asset_class(signal)

    def direction(self, signal: str) -> int:
        """Return +1 (long/call) or -1 (short/put/sell)."""
        return signal_direction(signal)

    @staticmethod
    def is_actionable(signal: str) -> bool:
        """Whether the signal should trigger an entry attempt.

        Consults the live :data:`SIGNAL_REGISTRY` so signals registered at
        runtime (new asset classes) are actionable without a core edit.
        """
        return str(signal).upper() in SIGNAL_REGISTRY.entry_signals

    def should_enter(
        self,
        signal: str,
        confidence: float,
        active_count: int,
    ) -> str | None:
        """Check whether an entry is permissible.

        Returns ``None`` when allowed, or a rejection reason string.
        Thin delegate to ``check_entry_gate`` from ``_utils``.
        """
        return check_entry_gate(signal, confidence, active_count, self.strategy.config)

    # ─────────────────────────────────────────────────────────────────
    # 2. Manage trades
    # ─────────────────────────────────────────────────────────────────

    def cache_signals(self, ticker: str, signal_df: pd.DataFrame):
        """Store latest signal DataFrame for indicator injection (bounded LRU)."""
        if ticker in self._signal_cache:
            self._signal_cache.move_to_end(ticker)
        self._signal_cache[ticker] = signal_df
        self._signal_cache_cols[ticker] = frozenset(signal_df.columns)
        while len(self._signal_cache) > self._SIGNAL_CACHE_MAXLEN:
            evicted, _ = self._signal_cache.popitem(last=False)
            self._signal_cache_cols.pop(evicted, None)

    # ─────────────────────────────────────────────────────────────────
    # 3. Per-ticker pipeline  (unified loop body)
    # ─────────────────────────────────────────────────────────────────

    def process_ticker(
        self,
        df: pd.DataFrame,
        manage_df: pd.DataFrame,
        ticker: str,
        active_positions: Sequence[PositionDict],
        *,
        df_sig: pd.DataFrame | None = None,
        skip_signals: bool = False,
        end_dt=None,
    ) -> list[dict]:
        """Full per-ticker pipeline: manage exits -> signals -> entry gate.

        Used by both live trader (once per cycle) and backtest (per bar).

        Parameters
        ----------
        df
            OHLCV bar data at the signal interval.  May be an empty
            DataFrame when *skip_signals* is ``True`` (manage-only cycle).
        manage_df
            OHLCV bar data at the manage interval (may be *df* itself).
        ticker
            Symbol being processed.
        active_positions
            Current open positions for this strategy (**not** mutated).
        df_sig
            Pre-computed signal DataFrame.  When provided ``run_signals``
            is skipped.
        skip_signals
            When ``True``, the signal pipeline is skipped entirely and no
            ENTRY dicts are emitted.  Used by ``trader.process_strategy``
            on cycles where ``MANAGE_INTERVAL`` has elapsed but
            ``INTERVAL`` has not — e.g. a ``1d``-signal / ``5m``-manage
            strategy monitors exits every 5 minutes without re-running the
            (potentially expensive) indicator / signal computation.
            ``_signal_cache`` remains populated from the most recent signal
            cycle, so ``call_manage_trade`` can still inject daily
            indicators into intraday ``manage_df`` via ``merge_asof``.
        end_dt
            Data cutoff for ``closed_bar_asof`` signal-row selection.
            Live and backtest both pass ``executor.data_end_dt(now)``.

        Returns
        -------
        list[dict]
            EXIT dicts: ``{"ticker", "type", "order_id", "reason", "_position"}``.
            ENTRY dicts: ``{"ticker", "type", "signal_row"}`` (omitted when
            *skip_signals* is ``True``).
        """
        signals: list[dict] = []
        self._monitor_telemetry.reset_manage_observations()

        # --- Manage exits (manage_trade is a mandatory StrategyProtocol method) ---
        cached_cols = self._signal_cache_cols.get(ticker)
        if cached_cols is not None:
            needed = set(self._indicator_columns)
            missing = needed - cached_cols
            if missing:
                self._log.warning(
                    "Signal cache for %s missing indicator columns %s "
                    "(config/strategy changed since last signal cycle?); "
                    "inject_indicators will fill NaN/0",
                    ticker,
                    sorted(missing),
                )
        for pos in list(active_positions):
            if pos.get("Ticker") != ticker:
                continue
            # A position may not be managed until it is FILLED.  In the backtest
            # the entry is dispatched at the signal bar but its fill (EntryTime) is
            # stamped data-delay bars later, so the manage frame can end before the
            # fill — letting a pre-fill bar trigger an immediate exit (the 2026-11
            # weekly round-trip bug).  Skip manage until ``end_dt >= EntryTime``.
            # Live is unaffected: a position exists in the list only after its real
            # fill, so ``EntryTime <= end_dt`` there always.
            _pe_ts = pos.get("EntryTime") or pos.get("entry_time")
            if _pe_ts is not None and end_dt is not None:
                try:
                    _pe = pd.Timestamp(_pe_ts)
                    _ed = pd.Timestamp(end_dt)
                    if _pe > _ed:
                        continue
                except Exception:
                    self._log.exception(
                        "[%s] Error processing entry time <= end_dt %s: %s",
                        _pe_ts,
                        end_dt,
                    )
            # Route the engine-side position lifecycle through the group-4
            # ``Position`` primitive: a position whose state machine is CLOSED
            # is never managed. The canonical request schema is passed through
            # unchanged for OPENED/ADJUSTING positions, so manage_trade sees the
            # identical dict as ever (fills/executors still drive state).
            if (
                Position.from_position_dict(cast("dict[str, Any]", pos)).state
                is PositionState.CLOSED
            ):
                self._log.debug(
                    "[%s] skipping CLOSED position %s on the manage path",
                    getattr(self.strategy, "name", "?"),
                    pos.get("Ticker", ticker),
                )
                continue

            decision = evaluate_manage_exit(
                self.strategy,
                pos,
                manage_df,
                signal_df=self._signal_cache.get(ticker),
                indicator_columns=self._indicator_columns,
                config=self.strategy.config,
            )
            exit_idx = decision["exit_idx"]
            self._monitor_telemetry.record_manage_decision(pos, decision)
            if exit_idx is not None and exit_idx != -1:
                self._log.info("manage_trade triggered exit for %s", ticker)
                signals.append(
                    {
                        "ticker": ticker,
                        "order_id": pos.get("OrderId"),
                        "type": "EXIT",
                        "reason": "Strategy Signal (manage_trade)",
                        "_position": pos,
                        "exit_idx": exit_idx,
                        "manage_exit": decision["exit_class"],
                        "exit_bar": decision["exit_bar"],
                    }
                )

        # --- Signal pipeline + entry gate (skipped on manage-only cycles) ---
        if skip_signals:
            return signals

        if df_sig is None and not df.empty:
            df_sig = self.run_signals(df, ticker=ticker)

        if df_sig is not None and not df_sig.empty:
            interval = self.strategy.config.INTERVAL
            if end_dt is not None:
                bar_ts = closed_bar_asof(end_dt, interval)
            else:
                # Narrowed 1-row precomputed frames (legacy callers).
                bar_ts = df_sig.index[-1]
            signal_val, confidence, signal_row = self.latest_signal(
                df_sig, bar_ts=bar_ts
            )
            # Subtract positions already exited in this bar's manage pass so
            # the entry gate matches live trading (where those positions are
            # genuinely closed before the 1 PM entry window runs).
            _pending_exits = sum(1 for s in signals if s["type"] == "EXIT")
            active_count = max(0, len(active_positions) - _pending_exits)
            rejection = check_entry_gate(
                signal_val, confidence, active_count, self.strategy.config
            )

            if rejection is None:
                _bar_close = signal_row.get("Close", signal_row.get("Raw_Close", "?"))
                self._log.info(
                    "Actionable signal: %s for %s (Conf: %.3f)  bar=%s  close=%s  reason=%s",
                    signal_val,
                    ticker,
                    confidence,
                    signal_row.get("_bar_time", "?"),
                    _bar_close,
                    signal_row.get("Reason", ""),
                )
                signals.append(
                    {
                        "ticker": ticker,
                        "signal_row": signal_row,
                        "type": "ENTRY",
                    }
                )
            elif rejection != "not_actionable":
                self._log.info(
                    "Signal %s for %s DROPPED: %s (conf=%.3f)",
                    signal_val,
                    ticker,
                    rejection,
                    confidence,
                )
            else:
                # Signal is HOLD/NEUTRAL — log the skip reason from generate_signals
                # so it's visible in trader.log without requiring DEBUG mode.
                _reason = signal_row.get("Reason", "")
                if _reason:
                    self._log.info("Signal HOLD for %s: %s", ticker, _reason)

        return signals

    # ─────────────────────────────────────────────────────────────────
    # Top-level orchestration  (live + backtest entrypoint)
    # ─────────────────────────────────────────────────────────────────

    def process_strategy(
        self, strategy, now: datetime.datetime, executor: ExecutorProtocol
    ) -> None:
        """Run one full processing tick for *strategy* at *now*.

        Mirrors the live ``trader.process_strategy`` + ``execute_signals``
        pipeline.  Environment-specific behaviour (scheduling, order
        submission, fill simulation, position-state mutation) is
        delegated to *executor*; data fetching lives on the engine via
        ``self.data_handler`` (live: ``AlpacaDataHandler``; backtest:
        ``BacktestDataHandler``).

        Pipeline:
          1. ``executor.should_run`` — decide signal / manage phases.
          2. Compute lookback (DAILY_START vs PRE_LOAD_DAYS).
          3. Per-ticker:
             a. ``self.fetch_data`` / ``self.fetch_manage_data`` — bars
                via ``self.data_handler.get_historical_data``.
             b. ``executor.get_precomputed_signal`` — optional signal
                bypass (backtest precomputes).
             c. ``self.process_ticker`` — manage exits + entry gate.
             d. Dispatch EXIT / ENTRY via ``executor.exit`` / ``enter``.
          4. ``executor.record_run`` — dedup bookkeeping.
        """
        s_name: str = getattr(strategy, "name", type(strategy).__name__)

        interval = getattr(strategy.config, "INTERVAL", "1h")
        manage_interval = getattr(strategy.config, "MANAGE_INTERVAL", interval)

        should_signal, should_manage = executor.should_run(strategy, now)
        if not should_signal and not should_manage:
            return

        # Manage-only cycle with no positions → skip ticker loop entirely.
        # The manage phase still consumed this bar, so record the run before
        # returning; otherwise bar-driven scheduling re-fires it every tick.
        if (
            not should_signal
            and should_manage
            and not executor.active_positions(s_name)
        ):
            executor.record_run(s_name, now, ran_signal=False)
            return

        # Record the run now — mirrors trader.py's lock update, which happens
        # before the ticker loop (after the early-exit checks above).
        executor.record_run(s_name, now, ran_signal=should_signal)

        # Phase logging — mirrors trader.py
        if should_signal and should_manage:
            _phase = f"signal + manage  [{interval} / {manage_interval}]"
        elif should_signal:
            _phase = f"signal  [{interval}]"
        else:
            _phase = f"manage  [{manage_interval}]"
        self._log.info("[%s] Running %s", s_name, _phase)

        tickers = getattr(strategy.config, "TICKERS", [])
        if not tickers:
            self._log.warning("[%s] No tickers configured — skipping.", s_name)
            return

        end_dt = executor.data_end_dt(now)

        # Freshness reference — captured once, before any data work, so every
        # ticker reports the data age at tick start.  Measuring at emit time
        # instead would charge each ticker for the pipeline time of the tickers
        # ahead of it and turn the freshness metric into a duration metric.
        tick_wall_ns = time.time_ns()
        tick_perf_ns = time.perf_counter_ns()
        data_delay_min = resolve_data_delay_min(now, end_dt, strategy.config)

        # Lookback — DAILY_START forces full-history fetch for walk-forward
        # ML strategies; otherwise fall back to PRE_LOAD_DAYS rolling window.
        daily_start = getattr(strategy.config, "DAILY_START", None)
        if interval == "1d" and daily_start:
            ref_date = (
                end_dt.date()
                if hasattr(end_dt, "date")
                else datetime.date.fromisoformat(str(end_dt)[:10])
            )
            lookback = (ref_date - datetime.date.fromisoformat(daily_start)).days
        else:
            lookback = getattr(strategy.config, "PRE_LOAD_DAYS", 5)

        for ticker in tickers:
            try:
                # Signal-phase fetch only. Manage-only cycles consume just the
                # manage frame; the signal-interval frame is never read when
                # signals are skipped (signals always run on adjusted bars, and
                # Raw_Close is backtest-only). All tick-local durations land in
                # one Span-collect bucket, read into the monitor context below.
                tick_spans: dict[str, int] = {}
                df = pd.DataFrame()
                if should_signal:
                    with Span(SpanName.FETCH_DATA, collect=tick_spans):
                        df = fetch_data(
                            self.data_handler,
                            ticker,
                            end_dt,
                            lookback,
                            interval,
                        )

                with Span(SpanName.FETCH_MANAGE_DATA, collect=tick_spans):
                    manage_df = fetch_manage_data(
                        self.data_handler,
                        strategy.config,
                        ticker,
                        end_dt,
                        lookback,
                        interval,
                        signal_df=df if should_signal else None,
                    )

                if manage_df is None or manage_df.empty:
                    continue
                if should_signal and (df is None or df.empty):
                    continue

                df_sig_pre = executor.get_precomputed_signal(ticker, now)

                self._last_signal_spans = {}
                with Span(SpanName.PROCESS_TICKER, collect=tick_spans):
                    results = self.process_ticker(
                        df=df,
                        manage_df=manage_df,
                        ticker=ticker,
                        active_positions=executor.active_positions(s_name),
                        df_sig=df_sig_pre,
                        skip_signals=not should_signal,
                        end_dt=end_dt,
                    )
                context = TickMonitorContext(
                    strategy_name=s_name,
                    client_name=getattr(strategy, "client_name", ""),
                    ticker=ticker,
                    end_dt=end_dt,
                    interval=interval,
                    manage_interval=manage_interval,
                    config=strategy.config,
                    active_positions=len(executor.active_positions(s_name)),
                    should_manage=should_manage,
                    tick_wall_ns=tick_wall_ns,
                    tick_perf_ns=tick_perf_ns,
                    data_delay_min=data_delay_min,
                    fetch_data_ns=tick_spans.get(span_key(SpanName.FETCH_DATA), 0),
                    fetch_manage_data_ns=tick_spans[
                        span_key(SpanName.FETCH_MANAGE_DATA)
                    ],
                    process_ticker_ns=tick_spans[span_key(SpanName.PROCESS_TICKER)],
                )
                if should_manage:
                    self._monitor_telemetry.emit_manage_evaluations(context, manage_df)
                if should_signal:
                    self._monitor_telemetry.emit_signal_evaluation(
                        context,
                        self._signal_cache.get(ticker),
                        dict(self._last_signal_spans),
                    )

                executor.after_process_ticker(s_name, ticker, results)

                for sig in results:
                    sig["strategy"] = s_name
                    sig["timestamp"] = end_dt
                    stype = str(sig.get("type", "")).upper()
                    if stype == "EXIT":
                        pos = sig.get("_position")
                        with Span(
                            SpanName.EXECUTION_DISPATCH,
                            emit=self._monitor_emit,
                            event={"kind": "execution_dispatch", "action": "exit"},
                        ):
                            if pos and executor.exit(
                                s_name, pos, sig, manage_df=manage_df
                            ):
                                executor.after_exit(s_name, pos)
                    elif stype == "ENTRY":
                        row = sig.get("signal_row")
                        if not row:
                            continue
                        row = dict(row)
                        _event: dict[str, Any] = {
                            "kind": "execution_dispatch",
                            "action": "entry",
                        }
                        with Span(
                            SpanName.EXECUTION_DISPATCH,
                            emit=self._monitor_emit,
                            event=_event,
                        ):
                            new_pos = executor.enter(
                                strategy, ticker, row, self, now=now
                            )
                            if new_pos:
                                executor.after_entry(s_name, new_pos)
                            _event["accepted"] = bool(new_pos)

            except Exception as e:
                self._log.exception("[%s] Error processing %s: %s", s_name, ticker, e)

    # ─────────────────────────────────────────────────────────────────
    # Position building
    # ─────────────────────────────────────────────────────────────────

    def build_position(
        self,
        entry_time,
        entry_price: float,
        direction: int,
        signal: str,
        confidence: float,
        **extra,
    ) -> PositionDict:
        """Build the canonical position request consumed by ``manage_trade``.

        The canonical ``PositionDict`` request schema is produced by
        ``build_position_dict`` and returned byte-identical (executors and
        strategies see exactly the same dict as ever) — ``keeping the canonical
        request schema`` per the decision-engine spec. The position *lifecycle*
        is routed through the group-4 ``Position`` primitive via
        :meth:`position_from_request`, so the engine and the portfolio share one
        position representation without changing the emitted request.
        """
        return build_position_dict(
            entry_time,
            entry_price,
            direction,
            signal,
            confidence,
            self.strategy.config,
            **extra,
        )

    def position_from_request(self, position: PositionDict) -> Position:
        """Represent a canonical engine/management position request as a ``Position``.

        Routes the engine-side position lifecycle through the group-4
        ``Position`` state machine (opened -> adjusting -> closed, fills-only).
        ``build_position`` keeps emitting the canonical ``PositionDict`` request
        schema unchanged; this is the bridge that treats that same schema as the
        primitive's state representation. Accepts partial records (a backtest
        request can lack venue/qty yet).
        """
        return Position.from_position_dict(cast("dict[str, Any]", position))
