"""Shared monitor and shadow contracts for the engine and production sidecar.

This module owns observability semantics, not strategy semantics.  It calls
the established core decision helpers rather than reimplementing them, and is
safe to import from both the live process and the isolated monitor process.
"""

from __future__ import annotations

import contextlib
import contextvars
import logging
import time
from dataclasses import dataclass
from numbers import Integral
from typing import Any, Literal, TypedDict

import pandas as pd

from quant.core.protocols import PositionDict
from quant.observability import (
    SLO_SPAN_KEYS,
    SPAN_KEYS,
    MonitorEmit,
    MonitorEvent,
    Span,
    SpanName,
    span,
    span_key,
)
from quant.utils.options import is_ic_position
from quant.utils.time import bar_close_utc, closed_bar_asof

from ._utils import (
    call_manage_trade,
    fetch_manage_data,
    select_signal_row,
)

ManageExitClass = Literal["hold", "proximity", "max_hold", "strategy_manage"]
_NS_PER_MINUTE = 60_000_000_000
_MAX_PLAUSIBLE_DELAY_MIN = 1440
_log = logging.getLogger(__name__)

# Dispatch-jitter context (loop → pool → worker start), set by the trader's
# worker wrapper and read by the engine's telemetry — quant never imports
# python, and the trader never touches engine internals.
_tick_dispatch_ctx: contextvars.ContextVar[int] = contextvars.ContextVar(
    "tick_dispatch_ns", default=0
)


def tick_dispatch_ns() -> int:
    """Return the current thread's strategy dispatch wait (ns) or 0."""
    return _tick_dispatch_ctx.get()


def set_tick_dispatch_ns(value: int) -> None:
    """Set the dispatch wait for the calling thread (trader worker wrapper)."""
    _tick_dispatch_ctx.set(max(0, int(value)))


__all__ = [
    "SLO_SPAN_KEYS",
    "SPAN_KEYS",
    "ManageDecision",
    "ManageExitClass",
    "ManageObservation",
    "MonitorEmit",
    "MonitorEvent",
    "MonitorTelemetry",
    "ShadowEntryResult",
    "ShadowManageResult",
    "Span",
    "SpanName",
    "TickMonitorContext",
    "bar_staleness_ns",
    "classify_manage_exit",
    "compare_manage_exits",
    "compare_signals",
    "evaluate_manage_exit",
    "is_actionable_signal",
    "resolve_data_delay_min",
    "shadow_entry_signal",
    "shadow_manage_exit",
    "snapshot_config",
    "span",
    "span_key",
    "staleness_excess_ns",
]


class ManageDecision(TypedDict):
    """Strategy-owned result of one ``manage_trade`` evaluation."""

    exit_class: ManageExitClass
    exit_idx: int
    exit_bar: str | None


class ShadowEntryResult(TypedDict):
    shadow_signal: str
    shadow_confidence: float
    shadow_reason: str
    bar_ts: str
    entry_time: str
    shadow_prepare_signals_ns: int
    actionable: bool
    frame_has_bar: bool


class ShadowManageResult(ManageDecision):
    manage_bar_ts: str
    shadow_manage_ns: int


@dataclass(frozen=True, slots=True)
class TickMonitorContext:
    """Immutable tick facts consumed by event factories.

    New monitor planes can extend this contract without increasing the engine's
    event-construction surface or coupling the sidecar to engine internals.
    """

    strategy_name: str
    client_name: str
    ticker: str
    end_dt: Any
    interval: str
    manage_interval: str
    config: Any
    active_positions: int
    should_manage: bool
    tick_wall_ns: int
    tick_perf_ns: int
    data_delay_min: int
    fetch_data_ns: int
    fetch_manage_data_ns: int
    process_ticker_ns: int


@dataclass(frozen=True, slots=True)
class ManageObservation:
    """Canonical position snapshot plus its live strategy decision."""

    position: PositionDict
    decision: ManageDecision


def snapshot_config(config: Any) -> dict[str, Any]:
    """Return a picklable, primitive-only config snapshot for monitor events."""
    out: dict[str, Any] = {}
    if config is None:
        return out
    for key in dir(config):
        if key.startswith("_"):
            continue
        try:
            value = getattr(config, key)
        except Exception as exc:  # pragma: no cover - third-party descriptor guard
            _log.debug("config snapshot skipped %s: %s", key, exc)
            continue
        if callable(value):
            continue
        if isinstance(value, (str, int, float, bool)) or value is None:
            out[key] = value
        elif isinstance(value, (list, tuple)) and all(
            isinstance(item, (str, int, float, bool)) or item is None for item in value
        ):
            out[key] = list(value)
    return out


def bar_staleness_ns(
    bar_ts: Any, interval: str, *, at_ns: int | None = None
) -> int | None:
    """Return data age at a captured wall-clock instant, or ``None``."""
    try:
        close_ns = int(bar_close_utc(bar_ts, interval).value)
    except Exception:
        return None
    return (time.time_ns() if at_ns is None else at_ns) - close_ns


def staleness_excess_ns(staleness_ns: Any, data_delay_min: int) -> int | None:
    """Return staleness beyond expected feed delay, floored at zero."""
    if not isinstance(staleness_ns, int) or isinstance(staleness_ns, bool):
        return None
    return max(0, staleness_ns - max(0, int(data_delay_min)) * _NS_PER_MINUTE)


def resolve_data_delay_min(now: Any, end_dt: Any, config: Any) -> int:
    """Resolve the delay actually applied by the executor for this tick."""
    try:
        minutes = round(
            (pd.Timestamp(now) - pd.Timestamp(end_dt)).total_seconds() / 60.0
        )
        if 0 <= minutes <= _MAX_PLAUSIBLE_DELAY_MIN:
            return minutes
    except (TypeError, ValueError):
        pass
    try:
        return max(0, int(getattr(config, "DATA_DELAY", 0) or 0))
    except (TypeError, ValueError):
        return 0


def classify_manage_exit(
    position: PositionDict,
    manage_df: pd.DataFrame,
    exit_idx: int | None,
    config: Any,
) -> ManageDecision:
    """Classify the legacy index result without inventing strategy reasons.

    ``exit_idx`` is returned by ``strategy.manage_trade`` in coordinates of
    the frame it actually scanned — the bars from the position's entry
    onward (strategies slice ``df.loc[df.index >= EntryTime]`` internally;
    the backtest IC executor applies the same trim before indexing).
    ``manage_df`` here is the full frame passed to ``evaluate_manage_exit``,
    which normally starts long before entry, so the frame is trimmed to the
    entry bar FIRST; otherwise ``exit_ts`` / ``elapsed`` / ``spot`` would be
    read from the wrong rows and the exit class/bar mislabeled for any
    position entered after the frame's first bar.
    """
    if exit_idx is None or exit_idx == -1:
        return {"exit_class": "hold", "exit_idx": -1, "exit_bar": None}

    manage_df = _trim_manage_to_entry(position, manage_df)

    if not isinstance(exit_idx, Integral):
        return {
            "exit_class": "strategy_manage",
            "exit_idx": int(exit_idx),
            "exit_bar": None,
        }
    if exit_idx < 0 or exit_idx >= len(manage_df):
        return {
            "exit_class": "strategy_manage",
            "exit_idx": int(exit_idx),
            "exit_bar": None,
        }

    resolved_exit_idx = int(exit_idx)
    exit_ts = pd.Timestamp(manage_df.index[resolved_exit_idx])
    exit_class: ManageExitClass = "strategy_manage"
    simple_ic_path = float(getattr(config, "STOP_LOSS_PERCENT", 10.0)) >= 10.0
    entry = position.get("EntryTime", position.get("entry_time"))
    if simple_ic_path and entry is not None:
        with contextlib.suppress(Exception):
            entry_ts = pd.Timestamp(entry)
            elapsed = (exit_ts - entry_ts).total_seconds() / 60
            if elapsed >= float(config.MAX_HOLDING_MINUTES):
                exit_class = "max_hold"
    # Strike reads go through the typed IC envelope validated at the boundary;
    # a non-IC position never reaches the proximity path so a missing strike
    # cannot silently disable it with a default 0.
    if simple_ic_path and exit_class != "max_hold" and is_ic_position(position):
        k_sp = position["K_SP"]
        k_sc = position["K_SC"]
        buffer = float(config.IC_PROXIMITY_EXIT)
        spot = float(manage_df.iloc[resolved_exit_idx]["Close"])
        if (
            k_sp > 0
            and k_sc > 0
            and buffer > 0
            and ((k_sc - spot) < buffer or (spot - k_sp) < buffer)
        ):
            exit_class = "proximity"
    return {
        "exit_class": exit_class,
        "exit_idx": resolved_exit_idx,
        "exit_bar": exit_ts.isoformat(),
    }


def _trim_manage_to_entry(
    position: PositionDict, manage_df: pd.DataFrame
) -> pd.DataFrame:
    """Return *manage_df* rows from the position's ``EntryTime`` onward.

    Mirrors the backtest IC executor's trim so ``exit_idx`` coordinates and
    the classification timestamps line up with the frame ``manage_trade``
    scanned (exit_idx is entry-slice-relative).  The entry timestamp is
    aligned to the index's timezone — the timestamp is never stripped — so
    aware frames keep aware exit bars and naive frames stay naive.  Returns
    *manage_df* unchanged when entry is missing/unparseable, when the trim
    would leave the frame empty, or when the index is not datetime.
    """
    entry = position.get("EntryTime", position.get("entry_time"))
    if entry is None:
        return manage_df
    try:
        _et = pd.Timestamp(entry)
        if pd.isna(_et):
            return manage_df
    except Exception:
        return manage_df

    if not isinstance(manage_df.index, pd.DatetimeIndex):
        return manage_df

    _trimmed = manage_df[manage_df.index >= _et]
    return _trimmed if not _trimmed.empty else manage_df


def evaluate_manage_exit(
    strategy: Any,
    position: PositionDict,
    manage_df: pd.DataFrame,
    *,
    signal_df: pd.DataFrame | None = None,
    indicator_columns: list[str] | None = None,
    config: Any = None,
) -> ManageDecision:
    """Evaluate through the shared core ``call_manage_trade`` path."""
    effective_config = config or strategy.config
    exit_idx = call_manage_trade(
        strategy, position, manage_df, signal_df, indicator_columns, effective_config
    )
    return classify_manage_exit(position, manage_df, exit_idx, effective_config)


def _strategy_from_snapshot(strategy_name: str, config_snapshot: dict[str, Any] | None):
    from quant.strategies import (  # layering-ok  (single dynamic-load line)
        get_strategy,
    )

    strategy = get_strategy(strategy_name.rsplit("_", 1)[0])()
    if config_snapshot and hasattr(strategy, "config"):
        for key, value in config_snapshot.items():
            with contextlib.suppress(Exception):
                setattr(strategy.config, key, value)
    return strategy


def shadow_entry_signal(
    *,
    strategy_name: str,
    ticker: str,
    end_dt: str,
    interval: str,
    bar_ts: str,
    config_snapshot: dict[str, Any] | None,
) -> ShadowEntryResult:
    """Research-reference entry signal through ``Backtester.prepare_signals``."""
    # Shadow-signal reference boundary, not the decision path — sanctioned TID251 exception.
    from quant.backtest.backtester import Backtester  # layering-ok
    from quant.utils.time import calculate_entry_time

    started = time.perf_counter_ns()
    strategy = _strategy_from_snapshot(strategy_name, config_snapshot)
    end = pd.Timestamp(end_dt)
    bt = Backtester(
        strategy.config,
        strategy,
        end.strftime("%Y-%m-%d"),
        end.isoformat(),
        ticker,
        verbose=False,
    )
    signal_df = bt.prepare_signals()
    bar = closed_bar_asof(end, interval)
    if bar_ts:
        with contextlib.suppress(Exception):
            bar = pd.Timestamp(bar_ts)
    selected = select_signal_row(signal_df, bar)
    if selected is None:
        signal, confidence, row = "HOLD", 0.0, {}
    else:
        signal, confidence, row = selected
    return {
        "shadow_signal": str(signal).upper(),
        "shadow_confidence": float(confidence),
        "shadow_reason": str(row.get("Reason", "")) if isinstance(row, dict) else "",
        "bar_ts": pd.Timestamp(bar).isoformat(),
        "entry_time": pd.Timestamp(calculate_entry_time(bar, interval)).isoformat(),
        "shadow_prepare_signals_ns": time.perf_counter_ns() - started,
        "actionable": is_actionable_signal(str(signal)),
        "frame_has_bar": selected is not None,
    }


def _entry_signals() -> frozenset[str]:
    from ._utils import SIGNAL_REGISTRY

    # Live registry — picks up signals registered at runtime (new asset
    # classes) without importing a stale module-level snapshot.
    return SIGNAL_REGISTRY.entry_signals


def is_actionable_signal(signal: str) -> bool:
    """Whether a signal creates a strategy-owned entry expectation."""
    return str(signal).upper() in _entry_signals()


def shadow_manage_exit(
    *,
    strategy_name: str,
    ticker: str,
    position: PositionDict,
    end_dt: str,
    interval: str,
    manage_interval: str | None = None,
    config_snapshot: dict[str, Any] | None = None,
) -> ShadowManageResult:
    """Research-reference managed exit using the same core decision path.

    The manage window is fetched **independently** from the position's entry
    time — not reused from the end-day-only frame that ``prepare_signals``
    returns.  ``prepare_signals`` masks the signal frame to the end day by
    design (entry shadows only need that day's bar), but a manage decision
    depends on every bar since entry; feeding it the masked frame made the
    shadow evaluate a one-session snapshot and diverge from the live engine
    (permanent ``manage_exit_drift``).  The window here spans from one day
    before entry through ``end_dt``, trimmed to the last closed manage bar,
    which is exactly the bar history live's ``manage_trade`` scans.
    """
    from quant.data.data_handler import AlpacaDataHandler

    started = time.perf_counter_ns()
    strategy = _strategy_from_snapshot(strategy_name, config_snapshot)
    end = pd.Timestamp(end_dt)
    resolved_interval = manage_interval or getattr(
        strategy.config, "MANAGE_INTERVAL", interval
    )
    config = strategy.config

    entry = position.get("EntryTime", position.get("entry_time"))
    lookback = _manage_lookback_days(entry, end, config)

    # A fresh data handler mirrors the backtester's live feed.  Lazy import
    # keeps this module importable without data-layer side effects.
    data_handler = AlpacaDataHandler()
    # Indicator injection is only needed by cross-interval strategies that
    # declare INDICATOR_COLUMNS (e.g. 1d signals / 5m manage).  Same-interval
    # strategies (example_equity 15m/15m) inject nothing, so skip the expensive
    # signal pipeline entirely — the decision only depends on manage bars.
    signal_df = (
        _shadow_inject_frame(data_handler, strategy, ticker, end, lookback, interval)
        if _needs_indicator_injection(strategy)
        else None
    )

    manage_df = fetch_manage_data(
        data_handler,
        config,
        ticker,
        end,
        lookback,
        interval,
        signal_df=signal_df,
    )
    if not manage_df.empty:
        cutoff = pd.Timestamp(closed_bar_asof(end, resolved_interval))
        manage_df = manage_df.loc[manage_df.index <= cutoff]
    decision = evaluate_manage_exit(
        strategy, position, manage_df, signal_df=signal_df, config=config
    )
    return ShadowManageResult(
        **decision,
        manage_bar_ts=pd.Timestamp(closed_bar_asof(end, resolved_interval)).isoformat(),
        shadow_manage_ns=time.perf_counter_ns() - started,
    )


def _manage_lookback_days(entry, end: pd.Timestamp, config: Any) -> int:
    """Days of manage history needed to reproduce the live manage decision.

    The window must start at or before the position's entry session, so it
    spans ``entry - 1 day`` through ``end``.  Falls back to the strategy's
    ``PRE_LOAD_DAYS`` when entry is missing or unparseable, and is clamped to
    that value so the sidecar never fetches more than the live preload.
    """
    preload = max(1, int(getattr(config, "PRE_LOAD_DAYS", 1200) or 1200))
    if entry is None:
        return preload
    try:
        entry_dt = pd.Timestamp(entry)
        days = (end.normalize() - entry_dt.normalize()).days + 1
        return max(1, min(int(days), preload))
    except Exception:
        return preload


def _needs_indicator_injection(strategy: Any) -> bool:
    """True when ``manage_trade`` consumes injected signal-resolution columns."""
    return bool(getattr(strategy, "INDICATOR_COLUMNS", ()) or ())


def _shadow_inject_frame(
    data_handler: Any,
    strategy: Any,
    ticker: str,
    end: pd.Timestamp,
    lookback: int,
    interval: str,
) -> pd.DataFrame:
    """Recompute the signal frame over the manage window for injection.

    Cross-interval strategies only: mirrors the backtester's canonical
    pipeline (``calculate_indicators`` + ``generate_signals``) so the injected
    columns match what the live engine's signal cache holds.  Returns an empty
    frame when the data source is unavailable, which ``inject_indicators``
    treats as "nothing to inject".
    """
    import inspect as _inspect

    start = end - pd.Timedelta(days=lookback)
    try:
        df = data_handler.get_historical_data(
            ticker=ticker,
            start=start.isoformat() if hasattr(start, "isoformat") else str(start),
            end=end.isoformat() if hasattr(end, "isoformat") else str(end),
            interval=interval,
        )
    except Exception:
        return pd.DataFrame()
    if df is None or df.empty:
        return pd.DataFrame()
    params = _inspect.signature(strategy.calculate_indicators).parameters
    if "ticker" in params:
        ind = strategy.calculate_indicators(df, ticker=ticker)
    else:
        ind = strategy.calculate_indicators(df)
    if ind is None or ind.empty:
        return pd.DataFrame()
    return strategy.generate_signals(ind)


def compare_signals(live_signal: str, shadow_signal: str) -> str | None:
    return (
        None
        if str(live_signal or "").upper() == str(shadow_signal or "").upper()
        else "signal_drift"
    )


def compare_manage_exits(
    live_exit: str, live_exit_bar: Any, shadow_exit: str, shadow_exit_bar: Any
) -> str | None:
    """Exact parity requires matching class and strategy decision bar."""
    if str(live_exit or "hold").lower() != str(shadow_exit or "hold").lower():
        return "manage_exit_drift"
    if live_exit_bar or shadow_exit_bar:
        try:
            if pd.Timestamp(live_exit_bar) != pd.Timestamp(shadow_exit_bar):
                return "manage_exit_drift"
        except Exception:
            if str(live_exit_bar or "") != str(shadow_exit_bar or ""):
                return "manage_exit_drift"
    return None


class MonitorTelemetry:
    """Best-effort event publisher that keeps telemetry state out of the engine."""

    def __init__(self, emit: MonitorEmit | None) -> None:
        self._emit_callback = emit
        self._manage_observations: list[ManageObservation] = []

    @property
    def enabled(self) -> bool:
        return self._emit_callback is not None

    def reset_manage_observations(self) -> None:
        self._manage_observations.clear()

    def record_manage_decision(
        self, position: PositionDict, decision: ManageDecision
    ) -> None:
        if self.enabled:
            self._manage_observations.append(
                ManageObservation(position=position, decision=decision)
            )

    def emit_manage_evaluations(
        self, context: TickMonitorContext, manage_df: pd.DataFrame
    ) -> None:
        if not self.enabled:
            return
        try:
            manage_bar = closed_bar_asof(context.end_dt, context.manage_interval)
            # The cutoff inherits ``context.end_dt``'s timezone, but the frame
            closed = manage_df.loc[manage_df.index <= manage_bar]
            spot = float(closed.iloc[-1]["Close"]) if not closed.empty else None
            for observed in self._manage_observations:
                position = observed.position
                if is_ic_position(position):
                    k_sp: float | None = position["K_SP"]
                    k_sc: float | None = position["K_SC"]
                else:
                    k_sp = k_sc = None
                self._publish(
                    {
                        "kind": "manage_evaluated",
                        "strategy_name": context.strategy_name,
                        "client_name": context.client_name,
                        "ticker": context.ticker,
                        "end_dt": pd.Timestamp(context.end_dt).isoformat(),
                        "bar_ts": pd.Timestamp(manage_bar).isoformat(),
                        "interval": context.interval,
                        "manage_interval": context.manage_interval,
                        "position": position,
                        "fetch_data_ns": context.fetch_data_ns,
                        "fetch_manage_data_ns": context.fetch_manage_data_ns,
                        "entry_time": position.get(
                            "EntryTime", position.get("entry_time")
                        ),
                        "live_exit": observed.decision["exit_class"],
                        "exit_bar": observed.decision["exit_bar"],
                        "K_SP": k_sp,
                        "K_SC": k_sc,
                        "S": spot,
                        "dist_sp": (spot - float(k_sp))
                        if spot is not None and k_sp is not None
                        else None,
                        "dist_sc": (float(k_sc) - spot)
                        if spot is not None and k_sc is not None
                        else None,
                        "config_snapshot": snapshot_config(context.config),
                    }
                )
        except Exception:
            # Warning, not debug: a failed manage emit drops manage parity
            # telemetry (shadow_manage_result / manage_exit_drift) for this
            # manage cycle — log at warning so the loss is visible in the logs.
            _log.warning(
                "manage monitor emit failed for %s — manage parity plane blank",
                context.ticker,
                exc_info=True,
            )
        finally:
            self.reset_manage_observations()

    def emit_signal_evaluation(
        self,
        context: TickMonitorContext,
        signal_df: pd.DataFrame | None,
        signal_spans: dict[str, int],
    ) -> None:
        if not self.enabled:
            return
        try:
            bar_ts = closed_bar_asof(context.end_dt, context.interval)
            signal, confidence, reason, frame_has_bar = "HOLD", 0.0, "", False
            if signal_df is not None and not signal_df.empty:
                selected = select_signal_row(signal_df, bar_ts)
                frame_has_bar = selected is not None
                if selected is not None:
                    signal, confidence, row = selected
                    reason = str(row.get("Reason", "") or "")
            staleness = bar_staleness_ns(
                bar_ts, context.interval, at_ns=context.tick_wall_ns
            )
            self._publish(
                {
                    "kind": "signal_evaluated",
                    "strategy_name": context.strategy_name,
                    "client_name": context.client_name,
                    "ticker": context.ticker,
                    "bar_ts": pd.Timestamp(bar_ts).isoformat(),
                    "end_dt": pd.Timestamp(context.end_dt).isoformat(),
                    "interval": context.interval,
                    "live_signal": str(signal).upper(),
                    "live_confidence": float(confidence),
                    "live_reason": reason,
                    "live_frame_has_bar": frame_has_bar,
                    "active_positions": context.active_positions,
                    "phase": "signal+manage" if context.should_manage else "signal",
                    "fetch_data_ns": context.fetch_data_ns,
                    "fetch_manage_data_ns": context.fetch_manage_data_ns,
                    "process_ticker_ns": context.process_ticker_ns,
                    "tick_dispatch_ns": tick_dispatch_ns(),
                    "strategy_calc_total_ns": signal_spans.get(
                        "strategy_calc_total_ns", 0
                    ),
                    "generate_signals_ns": signal_spans.get("generate_signals_ns", 0),
                    "run_signals_ns": signal_spans.get("run_signals_ns", 0),
                    "bar_staleness_ns": staleness,
                    "DATA_DELAY": context.data_delay_min,
                    "bar_staleness_excess_ns": staleness_excess_ns(
                        staleness, context.data_delay_min
                    ),
                    "emit_staleness_ns": bar_staleness_ns(bar_ts, context.interval),
                    "tick_elapsed_ns": time.perf_counter_ns() - context.tick_perf_ns,
                    "config_snapshot": snapshot_config(context.config),
                }
            )
        except Exception:
            _log.debug(
                "signal monitor emit failed for %s", context.ticker, exc_info=True
            )

    def _publish(self, event: MonitorEvent) -> None:
        if self._emit_callback is not None:
            self._emit_callback(event)
