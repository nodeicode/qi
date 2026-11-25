"""Shared position schema and builder — leaf home in ``quant.utils``.

Consumed by the backtest venue's execution client and the live layer
(``python/``) so ``manage_trade`` always receives an identical schema.
``quant.utils`` is a leaf package — this module imports nothing from
``quant.core``.
"""

from __future__ import annotations

from typing import Any, TypedDict

from quant.utils.strategy_config import BaseStrategyConfig


class PositionDict(TypedDict, total=False):
    """Core position schema consumed by ``manage_trade``.

    Contains **only** the universal contract fields every position type uses.
    Strategy-specific extras (strikes, direction, metrics) are passed through
    via ``**extra`` in ``build_position_dict`` — parity requires the data
    travel, not that this type know about it.  The shared typed IC envelope
    and its boundary validators were removed with the equity-only trim;
    cross-cutting code reads live positions only through the boundary check
    in ``quant.live.protocols`` (never ``cast``, never ``dict.get(..., 0)``).

    The only sanctioned ``*PositionDict`` subclass is the live layer's
    ``LivePositionDict`` (``quant.live.protocols``): it extends the shared
    contract with live-only execution/broker fields and is guarded at entry by
    ``is_live_position``. Do not add further subclasses; strategy fields
    belong in a strategy-local store, not on this dict.
    """

    # ── Universal fields (every position) ──
    EntryTime: Any
    EntryPrice: float
    EntryBasePrice: float
    UnderlyingEntryPrice: float
    Direction: int
    Signal: str
    Confidence: float
    StopLoss: float | None
    TakeProfit: float | None
    UseTrail: bool
    MaxHoldTime: int
    AssetType: str
    ATR: float
    # ── Live execution (optional) ──
    OrderId: str
    Ticker: str
    Qty: float
    Expiration: str


def build_position_dict(
    entry_time,
    entry_price: float,
    direction: int,
    signal: str,
    confidence: float,
    config: BaseStrategyConfig,
    **extra,
) -> PositionDict:
    """Build the canonical position dict consumed by ``manage_trade``.

    Both backtest and live produce positions through this function so
    ``manage_trade`` always receives an identical schema.
    """
    pos: PositionDict = {
        "EntryTime": entry_time,
        "EntryPrice": entry_price,
        "EntryBasePrice": entry_price,
        "UnderlyingEntryPrice": entry_price,
        "Direction": direction,
        "Signal": signal,
        "Confidence": confidence,
        "StopLoss": config.STOP_LOSS_PERCENT,
        "TakeProfit": config.TAKE_PROFIT_PERCENT,
        "UseTrail": config.USE_TRAIL_LOSS,
        "MaxHoldTime": config.MAX_HOLDING_MINUTES,
    }
    pos.update(extra)
    return pos
