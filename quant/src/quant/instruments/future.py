"""Future instrument — representable in v1, accounting deferred (D14)."""

from __future__ import annotations

from dataclasses import dataclass

from quant.instruments.base import AssetClass, Instrument, InstrumentClass


@dataclass(frozen=True)
class FutureInstrument(Instrument):
    asset_class: AssetClass = AssetClass.FUTURE
    instrument_class: InstrumentClass = InstrumentClass.FUTURE
    contract_month: str | None = None
