"""
Engine utilities — types, constants, and pure helpers.
=======================================================

Everything that engine.py used to own at module level or as private helper
methods lives here.  ``engine.py`` imports and re-exports these symbols so
existing call-sites (tests, backtester, strategies) don't need to change.

What lives here
---------------
* ``PositionDict``                — now in ``quant.utils.positions``
* ``SignalType`` + metadata        — enum + routing/direction tables
* ``DEFAULT_INDICATOR_COLUMNS``   — default inject list
* Pure helper functions           — signal_asset_class, signal_direction,
                                    select_signal_row, _to_utc_naive_us,
                                    inject_indicators, check_entry_gate,
                                    _trim_incomplete_daily_bar
  Relocated upstream (import from ``quant.utils``): ``build_position_dict``
  → ``quant.utils.positions``; ``closed_bar_asof`` / ``bar_close_utc`` /
  session clocks + ``SESSION_CLOCKS`` → ``quant.utils.time``.
  ``is_ic_position`` (the IC envelope validator) lives here — previously
  ``quant.utils.options``, removed with the equity-only trim.
* Extracted engine internals      — fetch_data, fetch_manage_data,
                                    call_manage_trade
  These three were private TradingEngine methods called only from
  ``process_strategy`` / ``process_ticker``.  Making them standalone
  functions keeps TradingEngine's public surface clean.
* Futures utils                  — ``ContractSpec`` / ``ProductSpecRegistry`` /
                                    ``PRODUCT_SPECS`` (multiplier/tick tables),
                                    seed products (ES/MES/SR3).  Grouped at the
                                    bottom.
"""

from __future__ import annotations

import contextlib
import contextvars
import logging
import math
import time
from collections.abc import (  # noqa: F401  (re-exported type)
    Callable,
    Mapping,
    Sequence,
)
from dataclasses import dataclass
from enum import StrEnum
from numbers import Integral
from typing import Any, Literal, TypedDict, overload

import pandas as pd

from quant.observability import MonitorEmit, MonitorEvent
from quant.utils.positions import PositionDict
from quant.utils.strategy_config import BaseStrategyConfig
from quant.utils.time import bar_close_utc, closed_bar_asof

_log = logging.getLogger(__name__)


def is_ic_position(pos: Mapping[str, object]) -> bool:
    """Boundary lift: true when *pos* carries a complete, numeric IC bundle.

    Validates (never ``cast``) required keys so a partial record from crash
    recovery or a malformed producer cannot be silently treated as a valid IC
    position.  Rejects on any missing strike or a non-numeric strike value;
    ``IC_Direction`` needs only to be present.
    """
    for key in ("K_LP", "K_SP", "K_SC", "K_LC"):
        if not isinstance(pos.get(key), (int, float)):
            return False
    return "IC_Direction" in pos


class SignalType(StrEnum):
    """Every actionable signal the engine recognizes.

    Each member carries its asset class and direction so routing and
    direction lookups are O(1) dict reads instead of substring scans.
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


# ─────────────────────────────────────────────────────────────────────────────
# Asset classes — routing vocabulary for every tradable instrument family.
# StrEnum so existing string comparisons (``ac == "EQUITY"``, router registry
# keys, engine.route() return values) keep working unchanged.
# ─────────────────────────────────────────────────────────────────────────────


class AssetClass(StrEnum):
    """Every asset class the engine can route signals to.

    New asset classes are added here first (enum member = one-line change),
    then signals are registered against them via :class:`SignalRegistry` —
    adding a product family never edits engine/backtest routing code.
    """

    EQUITY = "EQUITY"
    OPTION = "OPTION"
    SPREAD = "SPREAD"
    IC = "IC"
    # Outright futures & options-on-futures (additive — no signals yet).
    FUTURES = "FUTURES"
    OOF = "OOF"


class SignalRegistry:
    """Extensible signal → ``(asset_class, direction)`` registry.

    Single source of truth for routing/direction.  ``signal_asset_class``,
    ``signal_direction`` and ``TradingEngine.route`` all resolve through the
    module-level :data:`SIGNAL_REGISTRY` instance, so a new signal is added
    in exactly one place — no core-file edit required::

        SIGNAL_REGISTRY.register(SignalType.BUY_FUTURES, AssetClass.FUTURES, 1)

    Behavioral contract (unchanged vs the pre-registry module dict):

    * Unknown signals resolve to ``AssetClass.EQUITY`` / direction ``+1``
      unless *strict* is passed, which raises ``KeyError`` instead.  The
      strict path is what the backtest executor router uses so an unknown
      signal fails loud in simulation instead of silently executing as
      equity; live keeps the forgiving fallback (legacy behavior).
    """

    def __init__(self) -> None:
        # Keys are plain uppercased signal strings — deliberately NOT the
        # SignalType enum, so asset/strategy modules can register brand-new
        # signals at import time without editing core files.  SignalType
        # remains the enumerated vocabulary for engine-native signals.
        self._meta: dict[str, tuple[AssetClass, int]] = {}

    # ── Mutation ────────────────────────────────────────────────────────
    def register(
        self,
        signal: SignalType | str,
        asset_class: AssetClass | str,
        direction: int,
    ) -> None:
        """Register (or override) routing metadata for *signal*.

        *signal* may be a :class:`SignalType` or any string — it is stored
        uppercased so lookups are case-insensitive.
        """
        stype = str(signal).upper()
        ac = AssetClass(asset_class.upper())
        self._meta[stype] = (ac, int(direction))

    def signals_for(self, asset_class: AssetClass | str) -> frozenset[str]:
        """All signal values routing to *asset_class*."""
        ac = AssetClass(asset_class.upper())
        return frozenset(s for s, (a, _d) in self._meta.items() if a == ac)

    # ── Read ────────────────────────────────────────────────────────────
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
        """All registered signal values (the actionable vocabulary)."""
        return frozenset(self._meta)


# Per-signal metadata — the seed table.  New signals are added here (or via
# ``SIGNAL_REGISTRY.register`` at import time from strategy/asset modules).
_SIGNAL_META: dict[SignalType, tuple[AssetClass, int]] = {
    # (asset_class, direction)  direction: +1 = long/call, -1 = short/put
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

# Derived sets — computed once at import time from the registry.
ALL_ENTRY_SIGNALS: frozenset[str] = SIGNAL_REGISTRY.entry_signals
_OPTION_SIGNALS: frozenset[str] = SIGNAL_REGISTRY.signals_for(AssetClass.OPTION)
_SPREAD_SIGNALS: frozenset[str] = SIGNAL_REGISTRY.signals_for(AssetClass.SPREAD)
_IC_SIGNALS: frozenset[str] = SIGNAL_REGISTRY.signals_for(AssetClass.IC)
# Kept for any external code that still references _BEARISH_KEYWORDS.
_BEARISH_KEYWORDS = {"PUT", "SHORT", "SELL"}


# Columns injected from signal-resolution data into manage-resolution data
# via ``merge_asof``.  Default is empty — strategies that need injection
# (e.g. 1d signals / 1h manage) must set a class-level ``INDICATOR_COLUMNS``.
DEFAULT_INDICATOR_COLUMNS: list[str] = []
# Alias kept for any external code still importing the private name.
_INDICATOR_COLUMNS = DEFAULT_INDICATOR_COLUMNS


# ─────────────────────────────────────────────────────────────────────────────
# Pure helper functions — no state, fully testable in isolation
# ─────────────────────────────────────────────────────────────────────────────


def select_signal_row(
    df_sig: pd.DataFrame,
    bar_ts,
) -> tuple[str, float, dict] | None:
    """Select the signal row at *bar_ts* (exact index match).

    Never falls back to blind ``iloc[-1]``.  Returns
    ``(signal, confidence, signal_row)`` or ``None`` if *bar_ts* is
    missing from the frame.
    """
    if df_sig is None or df_sig.empty:
        return None

    ts = pd.Timestamp(bar_ts)

    try:
        row = df_sig.loc[ts]
    except KeyError:
        return None

    # loc can return a DataFrame if duplicate index labels exist.
    if isinstance(row, pd.DataFrame):
        if row.empty:
            return None
        row = row.iloc[-1]

    signal = str(row.get("Signal", "HOLD")).upper()
    confidence = float(row.get("Confidence", 0.0))
    signal_row = row.to_dict()
    signal_row["_bar_time"] = ts
    return signal, confidence, signal_row


def signal_asset_class(signal: str) -> str:
    """Map a signal string to its asset class (EQUITY fallback for unknown).

    Thin delegate to :data:`SIGNAL_REGISTRY` — behavior identical to the
    historical module-dict lookup (unknown → ``"EQUITY"``).
    """
    return str(SIGNAL_REGISTRY.asset_class(signal))


def signal_direction(signal: str) -> int:
    """Return +1 (long/call) or -1 (short/put/sell). +1 fallback for unknown.

    Thin delegate to :data:`SIGNAL_REGISTRY`.
    """
    return SIGNAL_REGISTRY.direction(signal)


def _to_utc_naive_us(idx):
    """Normalise a DatetimeIndex to UTC-naive datetime64[us].

    Required for merge_asof dtype matching and searchsorted comparisons.
    """
    if getattr(idx, "tz", None) is not None:
        idx = idx.tz_convert("UTC").tz_localize(None)
    if hasattr(idx, "as_unit"):
        idx = idx.as_unit("us")
    return idx


def inject_indicators(
    manage_df: pd.DataFrame,
    signal_df: pd.DataFrame,
    config: BaseStrategyConfig,
    *,
    indicator_columns: list[str] | None = None,
) -> pd.DataFrame:
    """Inject signal-resolution indicators into manage-resolution data.

    Uses ``merge_asof`` with backward direction to prevent lookahead.
    Daily signals are shifted by 1 period before merging when the
    strategy interval is >= 1 day (indicators close-based, only known
    next day).

    Identical code path in both backtest and live.

    Parameters
    ----------
    indicator_columns
        Override which columns to inject.  When ``None``, falls back to
        ``DEFAULT_INDICATOR_COLUMNS``.
    """
    cols = (
        indicator_columns
        if indicator_columns is not None
        else DEFAULT_INDICATOR_COLUMNS
    )
    valid_inds = [c for c in cols if c in signal_df.columns]
    if not valid_inds:
        out = manage_df.copy()
        # Match the merge_asof path: always return a tz-naive UTC index so
        # manage_trade's elapsed-time math is consistent across both branches.
        out.index = _to_utc_naive_us(out.index)
        if "Raw_Close" not in out.columns:
            out["Raw_Close"] = out["Close"]
        return out

    manage_sorted = manage_df.sort_index()
    mapping = signal_df[valid_inds].copy()

    strat_interval = config.INTERVAL
    if strat_interval in ("1d", "1wk", "1mo"):
        mapping = mapping.shift(1)

    manage_sorted.index = _to_utc_naive_us(manage_sorted.index)
    mapping.index = _to_utc_naive_us(mapping.index)

    # Drop columns from manage_sorted that are also in mapping to prevent
    # merge_asof from creating _x/_y suffixed duplicates.
    overlap = [c for c in valid_inds if c in manage_sorted.columns]
    if overlap:
        manage_sorted = manage_sorted.drop(columns=overlap)

    merged = pd.merge_asof(
        manage_sorted,
        mapping,
        left_index=True,
        right_index=True,
        direction="backward",
    )
    merged[valid_inds] = merged[valid_inds].ffill().fillna(0)

    if "Raw_Close" not in merged.columns:
        merged["Raw_Close"] = merged["Close"]

    return merged


def check_entry_gate(
    signal: str,
    confidence: float,
    active_count: int,
    config: BaseStrategyConfig,
) -> str | None:
    """Check whether an entry is permissible.

    Returns ``None`` when the entry is allowed, or a short rejection
    reason string.
    """
    if signal.upper() not in SIGNAL_REGISTRY.entry_signals:
        return "not_actionable"
    min_conf = config.MIN_SIGNAL_CONFIDENCE
    if confidence < min_conf:
        return "low_confidence"
    max_concurrent = config.MAX_CONCURRENT_POSITIONS
    if active_count >= max_concurrent:
        return "max_concurrent"
    return None


def fetch_data(
    data_handler,
    ticker: str,
    end_dt,
    lookback: int,
    interval: str,
    adjustment: str = "all",
) -> pd.DataFrame:
    """Fetch bar data via ``data_handler.get_historical_data``.

    Live: ``AlpacaDataHandler`` — disk-cached via ``StockDataCache``.
    Backtest: ``BacktestDataHandler`` — searchsorted views of pre-loaded
    DataFrames.  Both implement the same interface.

    For daily bars, trim the trailing bar only when the query ends before
    the regular 4 PM ET close on the same Eastern calendar day.
    Otherwise leave the frame unchanged.

    Parameters
    ----------
    adjustment
        Passed through to the data handler.  IC strategies should use
        ``\"raw\"`` so daily Close / strike space match option CBBO.
    """
    import datetime as _dt

    if data_handler is None:
        return pd.DataFrame()

    start = end_dt - _dt.timedelta(days=lookback)
    _kwargs: dict = {
        "ticker": ticker,
        "start": start.isoformat() if hasattr(start, "isoformat") else str(start),
        "end": end_dt.isoformat() if hasattr(end_dt, "isoformat") else str(end_dt),
        "interval": interval,
        "adjustment": adjustment,
    }
    # ``adjustment`` is now formalized on ``DataHandlerProtocol``; the retry
    # only guards pre-protocol / minimal test handlers that lack the kwarg.
    try:
        df = data_handler.get_historical_data(**_kwargs)
    except TypeError as _te:
        if "adjustment" not in str(_te):
            raise
        _kwargs.pop("adjustment", None)
        df = data_handler.get_historical_data(**_kwargs)

    # TODO: interval comparison needs to use universal ENUM
    if interval == "1d":
        return _trim_incomplete_daily_bar(df, end_dt)

    return df


def _trim_incomplete_daily_bar(
    df: pd.DataFrame,
    end_dt,
) -> pd.DataFrame:
    """Drop the trailing daily bar when the query stops before 4 PM ET.

    Alpaca daily bars use the same timestamp every day, so we decide from
    the query end time, not from the bar timestamp itself.
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
    if (
        last_bar_date == end_et.date() and end_et.hour < 16
    ):  # end_et is timezone aware i.e it handles Daylight savings as well
        return df.iloc[:-1]

    return df


def attach_raw_close(
    df: pd.DataFrame,
    data_handler,
    ticker: str,
    start,
    end,
    interval: str,
    *,
    primary_adjustment: str = "all",
    log: logging.Logger | None = None,
    raw_df: pd.DataFrame | None = None,
) -> pd.DataFrame:
    """Ensure *df* has a ``Raw_Close`` column in tradable (unadjusted) space.

    When *primary_adjustment* is already ``"raw"``, copies ``Close``.
    Otherwise fetches a raw series for the same window and aligns it — unless
    *raw_df* is provided (already-fetched, same window), in which case that is
    used directly so callers can avoid a second fetch.
    """
    out = df.copy()
    _log = log or logging.getLogger(__name__)
    if primary_adjustment == "raw":
        out["Raw_Close"] = out["Close"]
        return out

    if data_handler is None:
        out["Raw_Close"] = out["Close"]
        return out

    if raw_df is not None and not raw_df.empty and "Close" in raw_df.columns:
        df_raw = raw_df
    else:
        try:
            df_raw = data_handler.get_historical_data(
                ticker=ticker,
                start=start.isoformat() if hasattr(start, "isoformat") else str(start),
                end=end.isoformat() if hasattr(end, "isoformat") else str(end),
                interval=interval,
                adjustment="raw",
            )
        except TypeError:
            df_raw = pd.DataFrame()
        except Exception as e:
            _log.warning("Could not fetch raw data for Raw_Close: %s. Using Close.", e)
            out["Raw_Close"] = out["Close"]
            return out

    if df_raw.empty or "Close" not in df_raw.columns:
        out["Raw_Close"] = out["Close"]
        return out

    try:
        _raw_index = df_raw.index
        aligned = df_raw.reindex(out.index)["Close"]
        out["Raw_Close"] = aligned.fillna(out["Close"])
    except Exception as e:
        _log.warning("Raw_Close alignment failed: %s. Using Close.", e)
        out["Raw_Close"] = out["Close"]
    return out


def fetch_manage_data(
    data_handler,
    config: BaseStrategyConfig,
    ticker: str,
    end_dt,
    lookback: int,
    signal_interval: str,
    signal_df: pd.DataFrame | None = None,
) -> pd.DataFrame:
    """Fetch manage-resolution data, inject indicators if intervals differ.

    When ``MANAGE_INTERVAL == INTERVAL`` and *signal_df* is available,
    reuses it to avoid a redundant fetch.
    """
    manage_interval = getattr(config, "MANAGE_INTERVAL", signal_interval)

    if manage_interval == signal_interval and signal_df is not None:
        df = signal_df
    else:
        df = fetch_data(
            data_handler,
            ticker,
            end_dt,
            lookback,
            manage_interval,
        )

    if signal_df is not None and not df.empty and df is not signal_df:
        df = inject_indicators(df, signal_df, config)

    return df


def call_manage_trade(
    strategy,
    position: PositionDict,
    manage_df: pd.DataFrame,
    signal_df: pd.DataFrame | None = None,
    indicator_columns: list[str] | None = None,
    config: BaseStrategyConfig | None = None,
    log: logging.Logger | None = None,
) -> int:
    """Run ``strategy.manage_trade`` with indicator injection.

    Returns ``exit_idx`` (-1 = hold).

    Parameters
    ----------
    strategy
        Strategy instance exposing ``manage_trade(position, df)``.
    position
        Open position dict.
    manage_df
        Manage-resolution OHLCV bars.
    signal_df
        When provided, injected into *manage_df* via
        ``inject_indicators`` before calling ``manage_trade``.
    indicator_columns
        Which columns to inject.  ``None`` → ``DEFAULT_INDICATOR_COLUMNS``.
    config
        Strategy config (needed by ``inject_indicators``).
    log
        Logger for error messages.
    """
    if signal_df is not None and config is not None:
        prepared = inject_indicators(
            manage_df,
            signal_df,
            config,
            indicator_columns=(
                DEFAULT_INDICATOR_COLUMNS
                if indicator_columns is None
                else indicator_columns
            ),
        )
    else:
        prepared = manage_df

    try:
        return strategy.manage_trade(position, prepared)
    except Exception as e:
        _logger = log or _log
        _logger.error("manage_trade error: %s", e)
        return -1


# ─────────────────────────────────────────────────────────────────────────────
# Futures utils — instrument contract specs (multiplier/tick) and session
# clocks for futures products.  Pure data + clocks; consumed by the futures
# sizing/executor paths and by backtest entry-time resolution.
# ─────────────────────────────────────────────────────────────────────────────


class ProductFamily(StrEnum):
    """Broad market family a futures product belongs to.

    Used for validation/grouping only — all trading math lives on
    :class:`ContractSpec` (multiplier/tick), not here.
    """

    EQUITY_INDEX = "EQUITY_INDEX"
    INTEREST_RATE = "INTEREST_RATE"
    ENERGY = "ENERGY"
    METALS = "METALS"
    AGRICULTURE = "AGRICULTURE"


@dataclass(frozen=True, slots=True)
class ContractSpec:
    """Fixed contract economics for one futures product.

    Pure data — no trading logic.  ``multiplier`` is the US-dollar P&L per
    1.00 move in the quoted index level; ``tick_size`` the minimum price
    fluctuation in quote points; ``tick_value`` its dollar value.  For
    products with tiered ticks (e.g. SOFR near-expiry 0.0025 = $6.25 vs
    0.005 = $12.50) the headline ``tick_value`` is authoritative and the
    tiering is a strategy-side concern, not this table's.

    Validated at construction so a bad product-table entry fails at
    import/test time, never mid-backtest.
    """

    root: str  # CME product root, e.g. "MES", "ES", "SR3", "2YY"
    family: ProductFamily | str
    multiplier: float  # $ per 1.00 index-point move
    tick_size: float  # minimum price fluctuation (quote points)
    tick_value: float  # $ per tick
    quote: str = "INDEX_POINTS"  # INDEX_POINTS | IMM_INDEX | YIELD_POINTS (info)
    session: str = "cme-futures"  # SessionClock registry key
    source: str = ""  # spec provenance (CME rulebook / spec page)

    def __post_init__(self) -> None:
        if not isinstance(self.family, ProductFamily):
            object.__setattr__(self, "family", ProductFamily(str(self.family).upper()))
        if not self.root or not str(self.root).strip():
            raise ValueError(f"ContractSpec root must be non-empty, got {self.root!r}")
        if not (self.multiplier > 0):
            raise ValueError(
                f"{self.root}: multiplier must be > 0, got {self.multiplier}"
            )
        if not (self.tick_size > 0):
            raise ValueError(
                f"{self.root}: tick_size must be > 0, got {self.tick_size}"
            )
        if not (self.tick_value > 0):
            raise ValueError(
                f"{self.root}: tick_value must be > 0, got {self.tick_value}"
            )

    # ── Contract math (pure functions of the spec — used by fills & P&L) ──
    def round_price(self, price: float, *, side: str = "nearest") -> float:
        """Round *price* to the product's minimum price fluctuation.

        ``side`` controls the rounding direction for fills:

        * ``"ask"`` — round **up** (least-favorable for a buyer, pays at the
          next tick — conservative).
        * ``"bid"`` — round **down** (least-favorable for a seller, receives
          the next lower tick).
        * ``"nearest"`` — half-up rounding (quotes/analysis).
        """
        if price <= 0 or not math.isfinite(price):
            return price
        ts = float(self.tick_size)
        if side == "ask":
            return math.ceil(price / ts - 1e-9) * ts
        if side == "bid":
            return math.floor(price / ts + 1e-9) * ts
        return math.floor(price / ts + 0.5) * ts

    def pnl(
        self, entry_price: float, exit_price: float, n_contracts: int, direction: int
    ) -> float:
        """Dollar P&L for *n_contracts* at *direction* (+1 long / -1 short).

        ``multiplier`` is $ per 1.00 index point, so a +1.00 move on one
        contract yields exactly ``multiplier`` dollars regardless of the
        instrumental level (ES $50, MES $5, SR3 $2,500).
        """
        return (exit_price - entry_price) * self.multiplier * n_contracts * direction


class ProductSpecRegistry:
    """Registry of :class:`ContractSpec` keyed by product root.

    Additive by construction — registering a new product never touches
    engine/backtest code (mirrors the ``SignalRegistry`` pattern).
    """

    def __init__(self) -> None:
        self._specs: dict[str, ContractSpec] = {}

    def register(self, spec: ContractSpec) -> None:
        key = str(spec.root).upper()
        existing = self._specs.get(key)
        if existing is not None and existing != spec:
            raise ValueError(
                f"Product root {key!r} already registered as {existing} — "
                f"cannot overwrite with {spec}. Register under a distinct root."
            )
        self._specs[key] = spec

    @overload
    def get(self, root: str, *, strict: Literal[True] = True) -> ContractSpec: ...

    @overload
    def get(self, root: str, *, strict: Literal[False]) -> ContractSpec | None: ...

    def get(self, root: str, *, strict: bool = True) -> ContractSpec | None:
        """Look up a product spec by root (case-insensitive).

        ``strict=True`` raises ``KeyError`` for unregistered roots so the
        futures executor fails loud on a mistyped/product-root symbol
        instead of silently defaulting.  Type-aware: the default path is
        typed ``ContractSpec`` (non-None); pass ``strict=False`` for the
        optional lookup.
        """
        try:
            return self._specs[str(root).upper()]
        except KeyError:
            if strict:
                registered = ", ".join(sorted(self._specs)) or "(none)"
                raise KeyError(
                    f"No ContractSpec registered for product root {root!r}. "
                    f"Registered: {registered}"
                ) from None
            return None

    def __contains__(self, root: str) -> bool:
        return str(root).upper() in self._specs

    def keys(self) -> list[str]:
        return sorted(self._specs)


# Global product table — seeded with the products we trade first (CME).
# Treasury micro (2YY/5YY/10Y/30Y) specs are added under step "Validate
# exact CME specs".  Sources: CME contract-spec pages (2025-08 research).
PRODUCT_SPECS: ProductSpecRegistry = ProductSpecRegistry()
PRODUCT_SPECS.register(
    ContractSpec(
        root="ES",
        family=ProductFamily.EQUITY_INDEX,
        multiplier=50.0,
        tick_size=0.25,
        tick_value=12.50,
        quote="INDEX_POINTS",
        session="cme-futures",
        source="CME E-mini S&P 500 futures (Rule 353; $50/point, 0.25 tick)",
    )
)
PRODUCT_SPECS.register(
    ContractSpec(
        root="MES",
        family=ProductFamily.EQUITY_INDEX,
        multiplier=5.0,
        tick_size=0.25,
        tick_value=1.25,
        quote="INDEX_POINTS",
        session="cme-futures",
        source="CME Micro E-mini S&P 500 futures (Rule 358; $5/point, 0.25 tick)",
    )
)
PRODUCT_SPECS.register(
    ContractSpec(
        root="SR3",
        family=ProductFamily.INTEREST_RATE,
        multiplier=2500.0,
        tick_size=0.005,
        tick_value=12.50,
        quote="IMM_INDEX",
        session="cme-futures",
        source=(
            "CME 3-Month SOFR: $2,500/point; 0.005 tick = $12.50 (0.0025/$6.25 near expiry)"
        ),
    )
)


# ─────────────────────────────────────────────────────────────────────────────
# Session clocks — market-open rules per instrument family.
# Equity's daily rules (09:30 ET next-BDay open) are the historical default;
# futures products plug in their own clock (CME Globex 18:00 ET opens).  Entry
# timing for intraday intervals (T+I bar close) is session-agnostic — only the
# daily/next-open branch is session-dependent.
# ─────────────────────────────────────────────────────────────────────────────


# ─────────────────────────────────────────────────────────────────────────────
# Monitor telemetry — the engine's best-effort event publisher plus the shared
# richness/freshness helpers it consumes.  Moved here in the group 3.3 rewire
# so the engine and live trader no longer import ``core/monitor_utils``:
# emission flows through the ``quant.observability`` emit interface, and these
# are the only engine-consumed pieces that used to live in the monitor-utils
# module.  ``core/monitor_utils`` and the isolated monitor process were
# deleted in the group 7 trim (the shadow/sidecar extension moved to the
# private superset).
# ─────────────────────────────────────────────────────────────────────────────

ManageExitClass = Literal["hold", "proximity", "max_hold", "strategy_manage"]
_NS_PER_MINUTE = 60_000_000_000
_MAX_PLAUSIBLE_DELAY_MIN = 1440

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


class ManageDecision(TypedDict):
    """Strategy-owned result of one ``manage_trade`` evaluation."""

    exit_class: ManageExitClass
    exit_idx: int
    exit_bar: str | None


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
