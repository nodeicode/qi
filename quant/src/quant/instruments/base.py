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

from dataclasses import dataclass
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
    tick_size: float | None = None
    lot_size: float | None = None
    multiplier: float | None = None  # contract multiplier / point value
    currency: str = "USD"
    # D14: reference to another instrument — options point at their underlying.
    underlying: Instrument | None = None

    def __post_init__(self) -> None:
        if not self.symbol:
            raise ValueError("Instrument requires a non-empty symbol")
        if not self.venue:
            raise ValueError("Instrument requires a non-empty venue")

    def key(self) -> tuple[str, str]:
        """Identity key ``(venue, symbol)`` — the position/portfolio keying unit."""
        return (self.venue, self.symbol)
