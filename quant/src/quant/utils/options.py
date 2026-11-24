"""Shared options / iron-condor helpers — leaf home in ``quant.utils``.

Hosts strike snapping + selection, spread-width, IC credit/spread gates, and
the canonical ``build_ic_position`` so both the backtest executors and the
live layer build identical IC positions.  ``quant.utils`` is a leaf package —
this module imports nothing from ``quant.core``.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import TypedDict, TypeGuard

from quant.utils.positions import PositionDict, build_position_dict
from quant.utils.strategy_config import BaseStrategyConfig


class ICMeta(TypedDict):
    """Shared typed envelope for iron-condor strike/direction state.

    Declares the IC fields that ride the root position record as flat keys
    (K_LP/K_SP/K_SC/K_LC/IC_Direction) so the strategy, both executors, and the
    monitor/sidecar access them through one shared declaration instead of an
    ad hoc ``**extra`` grab-bag on ``PositionDict``. The envelope is shared and
    intentionally not opaque to core: the monitor reads the strikes for
    distance-to-strike reconciliation.
    """

    K_LP: float
    K_SP: float
    K_SC: float
    K_LC: float
    IC_Direction: int


def is_ic_position(pos: Mapping[str, object]) -> TypeGuard[ICMeta]:
    """Boundary lift: true when *pos* carries a complete, numeric IC bundle.

    Validates (never ``cast``) required keys so a partial record from crash
    recovery or a malformed producer cannot be silently treated as a valid IC
    position. Rejects on any missing strike or a non-numeric strike value;
    ``IC_Direction`` needs only to be present.
    """
    for key in ("K_LP", "K_SP", "K_SC", "K_LC"):
        if not isinstance(pos.get(key), (int, float)):
            return False
    return "IC_Direction" in pos


def snap_ic_strikes(
    spot: float,
    offset: float,
    wing: float,
    increment: float = 1.0,
) -> tuple[float, float, float, float]:
    """Return (K_lp, K_sp, K_sc, K_lc) snapped to *increment*."""

    def _snap(k: float) -> float:
        return round(round(k / increment) * increment, 2)

    k_sp = _snap(spot - offset)
    k_lp = _snap(k_sp - wing)
    k_sc = _snap(spot + offset)
    k_lc = _snap(k_sc + wing)
    return k_lp, k_sp, k_sc, k_lc


def check_ic_credit_gates(
    credit: float,
    *,
    min_credit: float = 0.0,
    max_credit: float = 0.0,
) -> str | None:
    """Return a rejection reason if *credit* is outside the configured band.

    ``min_credit`` / ``max_credit`` <= 0 means that side of the gate is off
    (matches backtest executor convention).
    """
    if min_credit > 0 and credit < min_credit:
        return "credit_too_low"
    if max_credit > 0 and max_credit != float("inf") and credit > max_credit:
        return "credit_too_high"
    return None


def check_ic_spread_gate(
    credit: float,
    combo_spread: float,
    max_spread_pct: float,
) -> str | None:
    """Return ``"spread_too_wide"`` when combo bid/ask width exceeds the cap."""
    if max_spread_pct <= 0 or combo_spread <= 0:
        return None
    if credit <= 0:
        return "spread_too_wide"
    if (combo_spread / credit) > max_spread_pct:
        return "spread_too_wide"
    return None


def select_strike(
    underlying_price: float, opt_type: str, config: BaseStrategyConfig
) -> float:
    """ATM / ITM / OTM strike selection based on config.

    Extracted from OptionBacktester._select_strike / SpreadBacktester._select_strike
    so both backtest and live use the exact same logic.
    """
    method = config.OPTION_STRIKE_METHOD
    increment = config.OPTION_STRIKE_INCREMENT

    if method == "itm":
        itm_depth = config.OPTION_ITM_DEPTH
        if opt_type == "call":
            raw = underlying_price * (1.0 - itm_depth)
            strike = (raw // increment) * increment
        else:
            raw = underlying_price * (1.0 + itm_depth)
            strike = ((raw + increment - 1) // increment) * increment

    elif method == "otm":
        otm_depth = config.OPTION_OTM_DEPTH
        if opt_type == "call":
            raw = underlying_price * (1.0 + otm_depth)
            strike = ((raw + increment - 1) // increment) * increment
        else:
            raw = underlying_price * (1.0 - otm_depth)
            strike = (raw // increment) * increment

    else:  # atm
        strike = round(underlying_price / increment) * increment

    return float(strike)


def calc_spread_width(underlying_price: float, config: BaseStrategyConfig) -> float:
    """Adaptive spread width or config override.

    Extracted from SpreadBacktester._calc_spread_width.
    """
    config_width = config.MIN_SPREAD_WIDTH
    if config_width is not None:
        return float(config_width)
    if underlying_price <= 200:
        return 5.0
    elif underlying_price <= 1000:
        return 10.0
    else:
        return 20.0


def build_ic_position(
    entry_time,
    underlying_price: float,
    ic_direction: int,
    signal: str,
    confidence: float,
    strikes: dict,
    config: BaseStrategyConfig,
    atr: float = 0.0,
    **extra,
) -> PositionDict:
    """Build canonical IC position dict consumed by ``manage_trade``.

    All keys in *strikes* are forwarded into the position dict so
    IC strategies can pass custom strike fields without editing the engine.
    Both callers (backtest + live executors) pass ``strategy.config`` here.
    """
    pos = build_position_dict(
        entry_time,
        underlying_price,
        ic_direction,
        signal,
        confidence,
        config,
        UnderlyingEntryPrice=underlying_price,
        EntryBasePrice=underlying_price,
        AssetType="IC",
        IC_Direction=ic_direction,
        ATR=atr,
        **strikes,
    )
    pos.update(extra)
    return pos
