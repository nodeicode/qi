"""Option instrument — representable in v1, accounting deferred (D14).

An option's ``underlying`` points at another ``Instrument``; pointing it at a
``FutureInstrument`` yields an option-on-futures with no extra type or casing.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum

from quant.instruments.base import AssetClass, Instrument, InstrumentClass


class OptionKind(StrEnum):
    CALL = "call"
    PUT = "put"


@dataclass(frozen=True)
class OptionInstrument(Instrument):
    asset_class: AssetClass = AssetClass.OPTION
    instrument_class: InstrumentClass = InstrumentClass.OPTION
    strike: float | None = None
    expiration: str | None = None
    option_kind: OptionKind | None = None
    exercise_style: str | None = None

    def __post_init__(self) -> None:
        super().__post_init__()
        # A free-standing option without an underlying is meaningless; the
        # boundary is enforced here (conformance) rather than mid-run.
        if not isinstance(self.underlying, Instrument):
            raise ValueError("OptionInstrument requires an `underlying` instrument")
        if self.strike is None:
            raise ValueError("OptionInstrument requires a strike")
        if self.expiration is None:
            raise ValueError("OptionInstrument requires an expiration")
        if self.option_kind is None:
            raise ValueError("OptionInstrument requires an option_kind")
