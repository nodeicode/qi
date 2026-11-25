"""Registry + conformance for typed instruments (design D14).

Kept deliberately tiny: a name→class registry so asset classes register as
thin additions, an ``instrument_class_for`` resolver, and a
``validate_instrument`` conformance check so an externally-introduced
instrument (from an agent or a custom data source) is rejected at the boundary
instead of surfacing mid-run. Ticker strings stay legal everywhere as sugar;
``resolve`` / ``equity_from_tickers`` wrap them into typed instruments.
"""

from __future__ import annotations

from collections.abc import Iterable
from typing import TypeVar

from quant.instruments.base import Instrument, InstrumentClass
from quant.instruments.equity import EquityInstrument
from quant.instruments.future import FutureInstrument
from quant.instruments.option import OptionInstrument

InstrumentT = TypeVar("InstrumentT", bound=Instrument)
SymbolOrInstrument = str | Instrument

_REGISTRY: dict[str, type[Instrument]] = {
    InstrumentClass.EQUITY.value: EquityInstrument,
    InstrumentClass.CONTINUOUS_FUTURE.value: FutureInstrument,
    InstrumentClass.FUTURE.value: FutureInstrument,
    InstrumentClass.OPTION.value: OptionInstrument,
}


def register_instrument_class(name: str, cls: type[Instrument]) -> None:
    """Register (or override) a named instrument class for thin asset growth."""
    if not isinstance(name, str) or not name:
        raise ValueError("instrument class name must be a non-empty string")
    _REGISTRY[name] = cls


def instrument_class_for(
    name: str | InstrumentClass,
) -> type[Instrument]:
    """Resolve a registered instrument class by name (``InstrumentClass`` ok)."""
    key = name.value if isinstance(name, InstrumentClass) else name
    try:
        return _REGISTRY[key]
    except KeyError:
        raise ValueError(
            f"Unknown instrument class {name!r}; registered: {sorted(_REGISTRY)}"
        ) from None


def validate_instrument(inst: Instrument) -> list[str]:
    """Return the violated conformance invariants (empty list = conforming).

    Identity uniqueness at a venue and required contract fields are the seed's
    invariants; asset-family rules come from the subtype (e.g. an option needs
    an underlying + strike). Consumers treat a non-empty result as a boundary
    rejection.
    """
    problems: list[str] = []
    # getattr: an externally-built instrument (via `__new__`) may lack fields.
    if not getattr(inst, "symbol", None):
        problems.append("instrument.symbol is empty")
    if not getattr(inst, "venue", None):
        problems.append("instrument.venue is empty")
    if isinstance(inst, OptionInstrument):
        if inst.underlying is None:
            problems.append("option.underlying is missing")
        if inst.strike is None:
            problems.append("option.strike is missing")
        if inst.expiration is None:
            problems.append("option.expiration is missing")
        if inst.option_kind is None:
            problems.append("option.option_kind is missing")
    if isinstance(inst, FutureInstrument) and inst.multiplier is None:
        problems.append("future.multiplier is missing (point value required)")
    # ── order-construction surface ────────────────────────────────────────
    # An instrument must be able to build the typed Order the venue seam
    # consumes; a definition without a working ``make_order`` is rejected at
    # the boundary rather than surfacing mid-run.
    make_order = getattr(inst, "make_order", None)
    if not callable(make_order):
        problems.append("instrument.make_order is missing")
    else:
        try:
            from quant.order import Order

            order = make_order("BUY", 1)
        except Exception as exc:  # production code should not raise on a valid order
            problems.append(f"instrument.make_order failed: {exc}")
        else:
            if not isinstance(order, Order):
                problems.append(
                    "instrument.make_order must return a typed Order, "
                    f"got {type(order).__name__}"
                )
    # ── required equity contract facts ────────────────────────────────────
    if isinstance(inst, EquityInstrument):
        if getattr(inst, "price_precision", None) is None:
            problems.append("equity.price_precision is missing")
        if getattr(inst, "lot_size", None) is None:
            problems.append("equity.lot_size is missing")
    return problems


def resolve(inst: SymbolOrInstrument, venue: str) -> Instrument:
    """Wrap a ticker string into an instrument, or pass a typed one through.

    Keeps the ``str`` convenience across seams: callers that hold a ticker may
    pass it directly and get a deterministic ``EquityInstrument`` for *venue*.
    """
    if isinstance(inst, Instrument):
        return inst
    return EquityInstrument(symbol=inst, venue=venue)


def equity_from_tickers(venue: str, tickers: Iterable[str]) -> list[EquityInstrument]:
    """Build equity instruments for a set of tickers at a venue (config support)."""
    return [EquityInstrument(symbol=t, venue=venue) for t in tickers]


def equity_instruments_from_config(
    venue: str, tickers: Iterable[str]
) -> list[EquityInstrument]:
    """Convenience synonym for wiring a ``BaseStrategyConfig.TICKERS`` list."""
    return equity_from_tickers(venue, tickers)
