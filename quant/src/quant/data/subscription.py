"""Typed data subscription surface (spec `data-access` Data source protocol).

The port generalizes from ``get_historical_data(ticker, start, end, interval)``
to a per-``(instrument, datatype, cadence)`` **subscription** that a strategy
declares and the data handler fulfills. ``bars`` is the shipped datatype.
``Subscription`` carries the typed ``Instrument`` (not a bare ticker), the
datatype, and the cadence string, with a canonical ``key`` so backtest replay
and the live stream fulfil the *same* buffer. ``BARS_COLUMNS`` and
``normalize_bars_frame`` pin the canonical OHLCV shape both roots must emit so
research-to-live parity holds by construction.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum

import pandas as pd

from quant.instruments import Instrument

# Canonical bars output shape — every bars subscription returns exactly these
# columns (Title-case, matching the live Alpaca shape and the backtest fixtures).
BARS_COLUMNS: tuple[str, ...] = ("Open", "High", "Low", "Close", "Volume")


class Datatype(StrEnum):
    """Typed data kinds the port ships. ``BARS`` is the v1 datatype."""

    BARS = "bars"


def normalize_bars_frame(df: pd.DataFrame) -> pd.DataFrame:
    """Project *df* onto the canonical bars shape.

    Returns a frame with exactly ``BARS_COLUMNS`` in canonical order. Timestamps
    are left as the handler produced them (both roots emit naive-UTC that the
    engine feeds ``decide``); asserting the columns here is what makes the
    backtest/live shapes agree by construction.
    """
    missing = [c for c in BARS_COLUMNS if c not in df.columns]
    if missing:
        raise KeyError(f"bars frame missing canonical columns: {missing}")
    return df[list(BARS_COLUMNS)]


@dataclass(frozen=True)
class Subscription:
    """A strategy-declared ``(instrument, datatype, cadence)`` data request.

    ``instrument`` is the typed instrument (symbol + venue); ``datatype`` is a
    :class:`Datatype` (``bars`` today); ``cadence`` is the bar interval string
    (``"1d"`` / ``"1h"`` / ``"15m"`` / ``"1m"``). ``key`` is the canonical
    identity used to key subscription buffers across backtest replay and the
    live stream.
    """

    instrument: Instrument
    datatype: Datatype
    cadence: str

    def __post_init__(self) -> None:
        if not isinstance(self.instrument, Instrument):
            raise ValueError("Subscription requires a typed Instrument")
        datatype = (
            Datatype(self.datatype)
            if not isinstance(self.datatype, Datatype)
            else self.datatype
        )
        object.__setattr__(self, "datatype", datatype)
        if not isinstance(self.cadence, str) or not self.cadence:
            raise ValueError("Subscription requires a non-empty cadence string")

    def key(self) -> tuple[str, str, Datatype, str]:
        """Canonical subscription identity ``(venue, symbol, datatype, cadence)``."""
        return (
            self.instrument.venue,
            self.instrument.symbol,
            self.datatype,
            self.cadence,
        )
