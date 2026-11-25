"""
Engine leaf helpers (strategy-dataflow).

The legacy signal-string routing vocabulary (`SignalType`, `AssetClass`,
`SignalRegistry`/`SIGNAL_REGISTRY`, `signal_asset_class`, `signal_direction`)
is removed — routing starts from typed instruments and the author body owns
decisions, sizing, and risk exits, so no signal string determines who
executes (spec `execution` `Asset-class routing` REMOVED).

What remains here:
* ``tick_dispatch_ns`` / ``set_tick_dispatch_ns`` — the trader's dispatch-jitter
  context read by the monitor plane.
* ``_trim_incomplete_daily_bar`` — the backtest data handler's daily-closure
  trim.

The deleted engine machinery (``fetch_data`` / ``fetch_manage_data`` /
``check_entry_gate`` / ``evaluate_manage_exit`` / ``call_manage_trade`` /
``inject_indicators``/``DEFAULT_INDICATOR_COLUMNS`` / the manage-telemetry
contexts) is gone with the methods it served; the author body now sizes and
evaluates exits from instrument contract facts + the injected Portfolio, and
contract facts moved to the typed instruments (``quant.instruments``).
"""

from __future__ import annotations

import contextvars

import pandas as pd

# Dispatch-jitter context (loop → pool → worker start), set by the trader and
# read by the monitor plane — quant never imports python, and the trader never
# touches engine internals.
_tick_dispatch_ctx: contextvars.ContextVar[int] = contextvars.ContextVar(
    "tick_dispatch_ns", default=0
)


def tick_dispatch_ns() -> int:
    """Return the current thread's strategy dispatch wait (ns) or 0."""
    return _tick_dispatch_ctx.get()


def set_tick_dispatch_ns(value: int) -> None:
    """Set the dispatch wait for the calling thread (trader worker wrapper)."""
    _tick_dispatch_ctx.set(max(0, int(value)))


def _trim_incomplete_daily_bar(df: pd.DataFrame, end_dt) -> pd.DataFrame:
    """Drop the trailing daily bar when the query stops before 4 PM ET.

    Alpaca daily bars use the same timestamp every day, so we decide from the
    query end time, not from the bar timestamp itself.
    """
    if df.empty or len(df) < 2:
        return df

    last_bar_ts = pd.Timestamp(df.index[-1])

    if last_bar_ts.tzinfo is not None:
        last_bar_date = last_bar_ts.tz_convert("America/New_York").date()
    else:
        last_bar_date = last_bar_ts.date()

    end_ts = pd.Timestamp(end_dt)
    if end_ts.tzinfo is not None:
        end_et = end_ts.tz_convert("America/New_York")
    else:
        end_et = end_ts

    # Before the regular close, the last daily bar may still be changing.
    if last_bar_date == end_et.date() and end_et.hour < 16:
        return df.iloc[:-1]

    return df
