"""Live in-memory bar store: provider-agnostic aggregator + single-writer worker.

Public surface:
  - ``MemBarsAggregator`` — buckets minute bars into output-shaped frames for both
    adjustment keys ("all"/"raw"); REST-seeded at warmup, ws-appended live.
  - ``BarWorker`` — queue + worker thread; the aggregator's sole writer.
  - ``MinuteBar`` / ``BarSink`` / ``BarSource`` — provider-agnostic contracts.
  - ``build_bar_source(provider, worker, **kwargs)`` — factory over a provider
    registry. Adding a provider = one ``data/ws/streams/<name>.py`` class
    implementing ``BarSource`` + one registry entry here.
"""

from __future__ import annotations

from collections.abc import Callable

from quant.data.ws.aggregator import MemBarsAggregator
from quant.data.ws.protocols import BarSink, BarSource, MinuteBar
from quant.data.ws.streams.alpaca import AlpacaDelayedSipStream
from quant.data.ws.worker import BarWorker

# Explicit provider registry: a new provider = one class implementing BarSource
# (e.g. data/ws/streams/databento.py) + one entry here. The aggregator, worker and
# data handler never change.
_SOURCES: dict[str, Callable[..., BarSource]] = {
    "alpaca": AlpacaDelayedSipStream,
}


def build_bar_source(provider: str, worker: BarWorker, **kwargs) -> BarSource:
    """Factory: provider name -> live minute-bar source bound to *worker*."""
    try:
        return _SOURCES[provider](worker, **kwargs)
    except KeyError:
        raise ValueError(f"unknown bar source provider: {provider!r}") from None


__all__ = [
    "BarSink",
    "BarSource",
    "MemBarsAggregator",
    "MinuteBar",
    "build_bar_source",
]
