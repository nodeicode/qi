"""Author-side risk exits — the manage-pass role folded into ``decide``.

The declarative author body evaluates the portfolio's open positions for
exits the way the engine's manage evaluation used to: this module turns the
config risk fields (stop loss, take profit, max hold) into a single predicate
the author calls per position. The conditions are the legacy ones — a hard
stop loss as a decimal, a take-profit target as a decimal with ``<= 0``
meaning disabled (the legacy ``-1`` sentinel), and a max holding time in
minutes — so positions the old engine would have exited are exited by the
author body on the same inputs.
"""

from __future__ import annotations

from typing import Any

import pandas as pd


def _config_value(config: Any, *names: str, default: Any = None) -> Any:
    """First non-``None`` value among *names* on *config*.

    Reads both the legacy uppercase config spellings and the declarative
    lowercase spellings, so the predicate works against either surface.
    """
    for name in names:
        value = getattr(config, name, None)
        if value is not None:
            return value
    return default


def _position_value(position: Any, attr: str, default: Any = None) -> Any:
    """Read one field from a ``Position`` object or a legacy position dict."""
    if isinstance(position, dict):
        return position.get(attr, default)
    return getattr(position, attr, default)


def exit_risk_hit(
    position: Any,
    config: Any,
    *,
    spot: float | None = None,
    at: Any = None,
) -> bool:
    """Return ``True`` when *position* trips a config risk exit.

    *spot* is the current tradable price (the author feeds the latest closed
    bar's close); *at* is the current decision time for the max-hold check
    (the same bar's timestamp). Either may be omitted: without *spot* the
    stop/take legs are skipped, and without *at* the max-hold leg is skipped,
    so a caller evaluates exactly the legs it has data for.

    The stop-loss leg fires when the position's loss reaches
    ``STOP_LOSS_PERCENT`` (a decimal, e.g. ``0.30`` = 30% from entry; large
    sentinels like ``999`` simply never fire). The take-profit leg fires when
    the gain reaches ``TAKE_PROFIT_PERCENT`` and is disabled when that value
    is ``<= 0`` (the legacy ``-1`` sentinel). The max-hold leg fires once the
    position has been open at least ``MAX_HOLDING_MINUTES``. Long and short
    positions are evaluated symmetrically.
    """
    entry = _position_value(position, "entry_price", None)
    if entry is None:
        entry = _position_value(position, "EntryPrice", 0.0)
    direction = _position_value(position, "direction", 1)
    if direction is None:
        direction = (
            int(position.get("Direction", 1)) if isinstance(position, dict) else 1
        )
    direction = 1 if float(direction) >= 0 else -1
    entry = float(entry)

    spot_value = float(spot) if spot is not None else None
    if spot_value is not None and entry > 0:
        ret = (spot_value - entry) / entry  # signed return since entry
        stop = float(
            _config_value(config, "STOP_LOSS_PERCENT", "stop_loss_percent", default=0.0)
            or 0.0
        )
        take = float(
            _config_value(
                config, "TAKE_PROFIT_PERCENT", "take_profit_percent", default=-1.0
            )
            or -1.0
        )
        if direction > 0:
            if ret <= -stop:
                return True
            if take > 0 and ret >= take:
                return True
        else:
            if ret >= stop:
                return True
            if take > 0 and ret <= -take:
                return True

    max_hold_minutes = float(
        _config_value(config, "MAX_HOLDING_MINUTES", "max_holding_minutes", default=0.0)
        or 0.0
    )
    entry_time = _position_value(position, "entry_time", None)
    if entry_time is None:
        entry_time = _position_value(position, "EntryTime", None)
    if max_hold_minutes > 0 and at is not None and entry_time is not None:
        try:
            elapsed = (
                pd.Timestamp(at) - pd.Timestamp(entry_time)
            ).total_seconds() / 60.0
            if elapsed >= max_hold_minutes:
                return True
        except (TypeError, ValueError):
            # Unparseable timestamps just disable the max-hold leg.
            pass

    return False
