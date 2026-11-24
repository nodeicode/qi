"""Provider-agnostic contracts for live bar ingestion.

The aggregator and the single-writer worker understand only these shapes, so a new
market-data provider is a new ``BarSource`` implementation — nothing else changes.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Protocol

import pandas as pd


@dataclass(frozen=True)
class MinuteBar:
    """One settled minute bar — the unit every provider emits and the aggregator
    consumes. As-traded (raw): at the live edge raw == all, so the aggregator needs
    no adjustment knowledge.

    Attributes:
        symbol: Ticker identifier.
        timestamp: Minute open timestamp (UTC wall clock; aggregator normalizes to
            UTC-naive microseconds, matching ``_finalize_equity_index`` output).
        open/high/low/close: Rolled OHLCV prices for the minute.
        volume: Volume traded in the minute.
    """

    symbol: str
    timestamp: pd.Timestamp
    open: float
    high: float
    low: float
    close: float
    volume: float
    # Optional extras a provider may carry (vwap, trade_count); not consumed yet.
    vwap: float | None = field(default=None, repr=False, compare=False)
    trade_count: float | None = field(default=None, repr=False, compare=False)


class BarSink(Protocol):
    """The aggregator's write surface (used by ``BarWorker``)."""

    def on_minute(self, bar: MinuteBar, interval: str = "15m") -> None: ...

    def seed(self, ticker, interval, adjustment, frame, seed_end=None) -> None: ...

    def merge(self, ticker, interval, adjustment, frame) -> None: ...


class BarSource(Protocol):
    """A live minute-bar stream.

    ``start()`` begins delivery to the sink it was constructed with; ``stop()`` is
    idempotent. The constructor binds the provider's subscription configuration,
    so a stream is not reusable across restarts.
    """

    def start(self) -> None: ...

    def stop(self) -> None: ...
