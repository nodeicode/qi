"""
Engine utilities — the routing vocabulary and leaf helpers (strategy-dataflow).

What remains here after the declarative engine reshape:
* ``SignalType`` / ``AssetClass`` + the ``SignalRegistry`` / ``SIGNAL_REGISTRY``
  and ``signal_asset_class`` / ``signal_direction`` — the legacy signal-string
  routing vocabulary removed in the final routing task (item 1.4).
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
from enum import StrEnum

import pandas as pd


class SignalType(StrEnum):
    """Every legacy signal string the registry/vocabulary recognizes.

    Each member carries its asset class and direction so routing/direction
    lookups are O(1) dict reads instead of substring scans. Kept for the
    final routing teardown task (item 1.4).
    """

    # Equity
    BUY = "BUY"
    SELL = "SELL"
    LONG = "LONG"
    SHORT = "SHORT"
    # Single-leg options
    BUY_CALL = "BUY_CALL"
    BUY_PUT = "BUY_PUT"
    BUY_CALL_MOM = "BUY_CALL_MOM"
    BUY_PUT_MOM = "BUY_PUT_MOM"
    SELL_CALL = "SELL_CALL"
    SELL_PUT = "SELL_PUT"
    # Spreads
    BUY_SPREAD = "BUY_SPREAD"
    SELL_SPREAD = "SELL_SPREAD"
    # Iron Condors
    SELL_IC = "SELL_IC"
    BUY_IC = "BUY_IC"


class AssetClass(StrEnum):
    """Every asset class the legacy routing vocabulary can map a signal to."""

    EQUITY = "EQUITY"
    OPTION = "OPTION"
    SPREAD = "SPREAD"
    IC = "IC"
    FUTURES = "FUTURES"
    OOF = "OOF"


class SignalRegistry:
    """Signal → ``(asset_class, direction)`` table (legacy routing vocabulary).

    ``signal_asset_class`` / ``signal_direction`` resolve through the module
    instance :data:`SIGNAL_REGISTRY`. Unknown signals resolve to ``EQUITY`` /
    direction ``+1`` unless *strict* is passed, which raises instead.
    """

    def __init__(self) -> None:
        # Keys are plain uppercased signal strings — deliberately not the
        # SignalType enum, so external modules can register new signals at
        # import time without editing core files.
        self._meta: dict[str, tuple[AssetClass, int]] = {}

    def register(
        self, signal: SignalType | str, asset_class: AssetClass | str, direction: int
    ) -> None:
        """Register (or override) routing metadata for *signal* (uppercased)."""
        stype = str(signal).upper()
        ac = AssetClass(str(asset_class).upper())
        self._meta[stype] = (ac, int(direction))

    def signals_for(self, asset_class: AssetClass | str) -> frozenset[str]:
        """All registered signal values routing to *asset_class*."""
        ac = AssetClass(str(asset_class).upper())
        return frozenset(s for s, (a, _d) in self._meta.items() if a == ac)

    def asset_class(self, signal: str, *, strict: bool = False) -> AssetClass:
        """Map *signal* to its :class:`AssetClass` (EQUITY fallback unless strict)."""
        try:
            return self._meta[str(signal).upper()][0]
        except KeyError:
            if strict:
                raise KeyError(
                    f"Unknown signal {signal!r} — not registered in SignalRegistry"
                ) from None
            return AssetClass.EQUITY

    def direction(self, signal: str, *, strict: bool = False) -> int:
        """Return +1 (long/call) or -1 (short/put/sell). +1 fallback unless strict."""
        try:
            return self._meta[str(signal).upper()][1]
        except KeyError:
            if strict:
                raise KeyError(
                    f"Unknown signal {signal!r} — not registered in SignalRegistry"
                ) from None
            return 1

    @property
    def entry_signals(self) -> frozenset[str]:
        """All registered signal values (the legacy actionable vocabulary)."""
        return frozenset(self._meta)


# Per-signal seed table.  ``(asset_class, direction)``  direction: +1 = long/call,
# -1 = short/put.
_SIGNAL_META: dict[SignalType, tuple[AssetClass, int]] = {
    SignalType.BUY: (AssetClass.EQUITY, 1),
    SignalType.SELL: (AssetClass.EQUITY, -1),
    SignalType.LONG: (AssetClass.EQUITY, 1),
    SignalType.SHORT: (AssetClass.EQUITY, -1),
    SignalType.BUY_CALL: (AssetClass.OPTION, 1),
    SignalType.BUY_PUT: (AssetClass.OPTION, -1),
    SignalType.BUY_CALL_MOM: (AssetClass.OPTION, 1),
    SignalType.BUY_PUT_MOM: (AssetClass.OPTION, -1),
    SignalType.SELL_CALL: (AssetClass.OPTION, -1),
    SignalType.SELL_PUT: (AssetClass.OPTION, -1),
    SignalType.BUY_SPREAD: (AssetClass.SPREAD, 1),
    SignalType.SELL_SPREAD: (AssetClass.SPREAD, -1),
    SignalType.SELL_IC: (AssetClass.IC, -1),
    SignalType.BUY_IC: (AssetClass.IC, 1),
}

# Global registry — seeded once at import; extensible via ``register``.
SIGNAL_REGISTRY: SignalRegistry = SignalRegistry()
for _st, (_ac, _dir) in _SIGNAL_META.items():
    SIGNAL_REGISTRY.register(_st, _ac, _dir)

# Derived sets — computed once from the registry.
ALL_ENTRY_SIGNALS: frozenset[str] = SIGNAL_REGISTRY.entry_signals
_OPTION_SIGNALS: frozenset[str] = SIGNAL_REGISTRY.signals_for(AssetClass.OPTION)
_SPREAD_SIGNALS: frozenset[str] = SIGNAL_REGISTRY.signals_for(AssetClass.SPREAD)
_IC_SIGNALS: frozenset[str] = SIGNAL_REGISTRY.signals_for(AssetClass.IC)
# Kept for any external code that still references _BEARISH_KEYWORDS.
_BEARISH_KEYWORDS = {"PUT", "SHORT", "SELL"}


def signal_asset_class(signal: str) -> str:
    """Map a signal string to its asset class (EQUITY fallback for unknown)."""
    return str(SIGNAL_REGISTRY.asset_class(signal))


def signal_direction(signal: str) -> int:
    """Return +1 (long/call) or -1 (short/put/sell). +1 fallback for unknown."""
    return SIGNAL_REGISTRY.direction(signal)


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
