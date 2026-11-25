"""Typed order primitive (strategy-dataflow).

The single shaped object a strategy hands to a venue's execution client: a
side, a non-negative quantity, the typed instrument, the price context, and
relevant timestamps. It exposes a canonical ``to_schema`` / ``from_schema``
pair so an ``Order`` crosses the backtest/live seam (and any network boundary)
byte-identically — serialization into a plain JSON-able dictionary and back
reconstructs an equal order, with the typed ``Instrument`` embedded verbatim
so the venue/portfolio seam never needs a separate lookup.

``venue`` is *derived* from the embedded instrument (an instrument always
carries its venue), so the two can never diverge and there is a single source
of truth for where an order executes.
"""

from __future__ import annotations

import dataclasses
import enum
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import StrEnum
from typing import Any

from quant.instruments.base import Instrument
from quant.instruments.equity import EquityInstrument
from quant.instruments.future import FutureInstrument
from quant.instruments.option import OptionInstrument
from quant.portfolio.base import Side

# name -> dataclass constructor, used to rebuild an embedded instrument from
# its canonical schema. Only the shipped concrete types are considered.
_INSTRUMENT_CLASSES: dict[str, type[Instrument]] = {
    EquityInstrument.__name__: EquityInstrument,
    FutureInstrument.__name__: FutureInstrument,
    OptionInstrument.__name__: OptionInstrument,
}


class OrderPriceType(StrEnum):
    """Price context discriminator — whether the order carries a limit price."""

    MARKET = "MARKET"
    LIMIT = "LIMIT"


def _inst_to_schema(inst: Instrument) -> dict:
    """Canonical instrument serialization used inside the order schema.

    ``__cls__`` names the concrete constructor and ``fields`` carries every
    dataclass field; nested instruments (an option's ``underlying``) and enums
    are serialized recursively / to their ``.value`` so the plain dictionary
    round-trips back into an equal frozen instrument.
    """
    fields: dict[str, Any] = {}
    for f in dataclasses.fields(inst):
        value = getattr(inst, f.name)
        if isinstance(value, Instrument):
            fields[f.name] = _inst_to_schema(value)
        elif isinstance(value, enum.Enum):
            fields[f.name] = value.value
        else:
            fields[f.name] = value
    return {"__cls__": type(inst).__name__, "fields": fields}


def _inst_from_schema(schema: dict) -> Instrument:
    """Rebuild a typed instrument from ``_inst_to_schema``'s canonical dict."""
    cls_name = schema["__cls__"]
    try:
        cls = _INSTRUMENT_CLASSES[cls_name]
    except KeyError:
        raise ValueError(
            f"Unsupported instrument class in order schema: {cls_name!r}"
        ) from None
    fields: dict[str, Any] = {}
    for key, value in schema["fields"].items():
        if isinstance(value, dict) and "__cls__" in value:
            fields[key] = _inst_from_schema(value)
        else:
            fields[key] = value
    # The field values are heterogeneous; strings stand in for StrEnum members
    # (archived via ``.value``) that the concrete dataclass constructor restores.
    return cls(**fields)


def _ts_to_str(ts) -> str | None:
    """Serialize a timestamp to an ISO string (or None) for the schema."""
    return None if ts is None else ts.isoformat()


def _ts_from_str(ts: str | None):
    """Deserialize an ISO string (or None) back into a timestamp."""
    return None if ts is None else datetime.fromisoformat(ts)


@dataclass(frozen=True)
class Order:
    """A strategy's intent, expressed for a venue's execution client.

    ``quantity`` is a non-negative magnitude; ``side`` gives the direction from
    the instrument's frame (BUY adds to the long side). ``venue`` is always the
    embedded instrument's venue — there is no separate venue field.
    ``price_type`` distinguishes a MARKET from a LIMIT order; ``price`` is the
    limit price for a LIMIT order and is left None for MARKET. ``tag`` carries
    free-form attribution. The timestamps track the order's journey through the
    seam: ``created_at`` (set at construction), then optional ``submitted_at``
    and ``filled_at`` recorded by the venue.
    """

    side: Side
    quantity: float
    instrument: Instrument
    price_type: OrderPriceType = OrderPriceType.MARKET
    price: float | None = None
    tag: str | None = None
    strategy: str | None = None
    created_at: object = dataclasses.field(default_factory=lambda: datetime.now(UTC))
    submitted_at: object = None
    filled_at: object = None

    def __post_init__(self) -> None:
        if self.quantity is None or self.quantity < 0:
            raise ValueError("Order requires a non-negative quantity")
        if not isinstance(self.instrument, Instrument):
            raise ValueError("Order requires a typed Instrument")

    @property
    def venue(self) -> str:
        """Execution venue — always the embedded instrument's venue (single source)."""
        return self.instrument.venue

    # ── canonical schema round-trip ────────────────────────────────────────
    def to_schema(self) -> dict:
        """Serialize to a plain JSON-able dictionary (byte-identical across seams)."""
        return {
            "side": self.side.value,
            "quantity": self.quantity,
            "instrument": _inst_to_schema(self.instrument),
            "price_type": self.price_type.value,
            "price": self.price,
            "tag": self.tag,
            "strategy": self.strategy,
            "created_at": _ts_to_str(self.created_at),
            "submitted_at": _ts_to_str(self.submitted_at),
            "filled_at": _ts_to_str(self.filled_at),
        }

    @classmethod
    def from_schema(cls, schema: dict) -> Order:
        """Rebuild an equal ``Order`` from :meth:`to_schema`'s canonical dict."""
        return cls(
            side=Side(schema["side"]),
            quantity=schema["quantity"],
            instrument=_inst_from_schema(schema["instrument"]),
            price_type=OrderPriceType(schema["price_type"]),
            price=schema.get("price"),
            tag=schema.get("tag"),
            strategy=schema.get("strategy"),
            created_at=_ts_from_str(schema.get("created_at")),
            submitted_at=_ts_from_str(schema.get("submitted_at")),
            filled_at=_ts_from_str(schema.get("filled_at")),
        )
