"""Typed instrument seed (design D14).

The single untyped-accepting base every instrument shares. Two properties make
equity, futures, options, and options-on-futures representable by construction
with no future redesign:

* an ``instrument_class`` discriminator, so Qi can tell families apart without
  ``isinstance`` chains; and
* an ``underlying`` reference to another ``Instrument`` (not a string), so an
  ``OptionInstrument`` whose ``underlying`` is a ``FutureInstrument`` *is* an
  option-on-futures — no extra casing or new type.

Ticker strings stay legal everywhere as sugar: seams may accept ``str |
Instrument`` and wrap via the resolved helpers (see ``_registry``).
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import TYPE_CHECKING

if TYPE_CHECKING:  # only needed for the make_order return/param annotations
    from quant.order import Order, OrderPriceType
    from quant.portfolio.base import Side
from enum import StrEnum


class AssetClass(StrEnum):
    """Broad tradable category. Kept coarse; ``InstrumentClass`` refines."""

    EQUITY = "EQUITY"
    FUTURE = "FUTURE"
    OPTION = "OPTION"


class InstrumentClass(StrEnum):
    """Family discriminator — Qi's language for classifying instruments."""

    EQUITY = "equity"
    CONTINUOUS_FUTURE = "continuous_future"
    FUTURE = "future"
    OPTION = "option"


@dataclass(frozen=True)
class Instrument:
    """Smallest typed unit both environments reason over (design D14)."""

    symbol: str
    venue: str
    asset_class: AssetClass
    instrument_class: InstrumentClass
    # ── contract / settlement metadata ────────────────────────────────────
    # These are the single source of contract facts: sizing, fills, and P&L
    # read them off the instrument (folded in from the legacy ContractSpec
    # table, which is deleted). ``tick_size`` is the minimum price fluctuation
    # in quote points; ``multiplier`` is the dollar P&L per 1.00 index-point
    # move; ``lot_size`` the minimum tradable unit; ``price_precision`` the
    # number of decimal places to round fills/quotes to; ``margin`` the
    # per-contract buying-power requirement (futures).
    tick_size: float | None = None
    lot_size: float | None = None
    multiplier: float | None = None  # contract multiplier / point value
    currency: str = "USD"
    price_precision: int | None = None
    margin: float | None = None
    # reference to another instrument — options point at their underlying.
    underlying: Instrument | None = None

    def __post_init__(self) -> None:
        if not self.symbol:
            raise ValueError("Instrument requires a non-empty symbol")
        if not self.venue:
            raise ValueError("Instrument requires a non-empty venue")

    def key(self) -> tuple[str, str]:
        """Identity key ``(venue, symbol)`` — the position/portfolio keying unit."""
        return (self.venue, self.symbol)

    # ── contract math (was ContractSpec.round_price / pnl) ────────────────
    def round_price(self, price: float, *, side: str = "nearest") -> float:
        """Round ``price`` to this instrument's minimum price fluctuation.

        ``side`` controls the rounding direction for fills: ``"ask"`` rounds
        up (least-favorable for a buyer), ``"bid"`` rounds down
        (least-favorable for a seller), ``"nearest"`` half-up rounds
        (quotes/analysis). Requires a positive ``tick_size``.
        """
        ts = self.tick_size
        if ts is None or ts <= 0:
            raise ValueError(
                f"{type(self).__name__} needs a positive tick_size to round prices"
            )
        if price <= 0 or not math.isfinite(price):
            return price
        if side == "ask":
            return math.ceil(price / ts - 1e-9) * ts
        if side == "bid":
            return math.floor(price / ts + 1e-9) * ts
        return math.floor(price / ts + 0.5) * ts

    def pnl(
        self, entry_price: float, exit_price: float, n_contracts: int, direction: int
    ) -> float:
        """Dollar P&L for ``n_contracts`` at ``direction`` (+1 long / -1 short).

        ``multiplier`` is dollars per 1.00 index point, so a +1.00 move on one
        contract yields exactly the multiplier (ES $50, MES $5, SR3 $2,500)
        independent of the instrument's level. Requires a ``multiplier``.
        """
        if self.multiplier is None:
            raise ValueError(f"{type(self).__name__} needs a multiplier to compute P&L")
        return (exit_price - entry_price) * self.multiplier * n_contracts * direction

    def make_order(
        self,
        side: Side | str,
        qty: float,
        *,
        price: float | None = None,
        price_type: OrderPriceType | None = None,
        tag: str | None = None,
        strategy: str | None = None,
    ) -> Order:
        """Build a typed :class:`~quant.order.Order` bound to this instrument.

        ``side`` accepts a ``Side`` member or the string alias (``"BUY"`` /
        ``"SELL"``); ``qty`` is a non-negative magnitude. A ``price`` without
        an explicit ``price_type`` produces a LIMIT order; otherwise the order
        is MARKET. ``venue`` is derived from the instrument, so the order can
        never point at a different venue than its instrument.

        Imported lazily to keep the dataclass layer free of an eager
        ``quant.order`` dependency (``quant.order`` imports these instruments).
        """
        from quant.order import Order, OrderPriceType
        from quant.portfolio.base import Side

        side_member = Side(side.upper()) if isinstance(side, str) else side
        if price_type is None:
            price_type = (
                OrderPriceType.LIMIT if price is not None else OrderPriceType.MARKET
            )
        return Order(
            side=side_member,
            quantity=qty,
            instrument=self,
            price_type=price_type,
            price=price,
            tag=tag,
            strategy=strategy,
        )
