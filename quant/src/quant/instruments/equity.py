"""Equity instrument — the only asset class with full v1 behavior."""

from __future__ import annotations

from dataclasses import dataclass

from quant.instruments.base import AssetClass, Instrument, InstrumentClass


@dataclass(frozen=True)
class EquityInstrument(Instrument):
    asset_class: AssetClass = AssetClass.EQUITY
    instrument_class: InstrumentClass = InstrumentClass.EQUITY
    tick_size: float | None = 0.01
