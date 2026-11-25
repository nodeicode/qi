"""Venue registration and discovery (spec `venue`)."""

from __future__ import annotations

from typing import Any

from quant.venue.alpaca import AlpacaVenue
from quant.venue.backtest import BacktestVenue
from quant.venue.base import Venue

_REGISTRY: dict[str, type[Venue]] = {
    "alpaca": AlpacaVenue,
    "backtest": BacktestVenue,
}


def register_venue(name: str, factory: type[Venue]) -> None:
    """Register (or override) a named venue for config-driven resolution."""
    if not isinstance(name, str) or not name:
        raise ValueError("venue name must be a non-empty string")
    _REGISTRY[name] = factory


def venue_registered(name: str) -> bool:
    """True when ``name`` is a registered venue id.

    Non-constructing: used to reject an unknown venue at strategy/config
    construction time without wiring a full ``Venue`` yet (that happens when
    the venue is resolved for execution).
    """
    return name in _REGISTRY


def registered_venues() -> list[str]:
    """Sorted ids of currently registered venues (for readable rejection errors)."""
    return sorted(_REGISTRY)


def venue_by_name(name: str, **overrides: Any) -> Venue:
    """Resolve a venue by name (built-ins ``alpaca``/``backtest``; extras registered)."""
    factory = _REGISTRY.get(name)
    if factory is None:
        raise ValueError(
            f"Unknown venue {name!r}; registered venues: {sorted(_REGISTRY)}"
        )
    return factory(**overrides)
