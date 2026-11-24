"""Typed instruments (design D14) — the #4 primitive in the counted set.

A single correctly-seeded hierarchy (base + ``instrument_class`` discriminator
+ ``underlying`` reference) keeps equity, futures, options, and options-on-
futures representable by construction. v1 ships full equity behavior; options
and futures are representable (thin, registrable types) but their accounting /
execution semantics are deferred to concept docs.
"""

from quant.instruments._registry import (
    equity_from_tickers,
    equity_instruments_from_config,
    instrument_class_for,
    register_instrument_class,
    resolve,
    validate_instrument,
)
from quant.instruments.base import AssetClass, Instrument, InstrumentClass
from quant.instruments.equity import EquityInstrument
from quant.instruments.future import FutureInstrument
from quant.instruments.option import OptionInstrument, OptionKind

__all__ = [
    "AssetClass",
    "EquityInstrument",
    "FutureInstrument",
    "Instrument",
    "InstrumentClass",
    "OptionInstrument",
    "OptionKind",
    "equity_from_tickers",
    "equity_instruments_from_config",
    "instrument_class_for",
    "register_instrument_class",
    "resolve",
    "validate_instrument",
]
