"""Data-source registration and discovery (spec `data-access`, design D13).

Sibling of the venue/config registries: ``get_datasource(name)`` resolves a
data source by name from the built-in set (``alpaca`` live handler, ``backtest``
handler) and from any entry point under the ``quant.datasources`` group. The
built-ins are resolved *lazily* so importing ``quant.data`` never pulls alpaca
(and never pulls the terminal ``quant.backtest`` package) until a source is
actually requested. An unknown name raises a ``ValueError`` listing the
available names.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable
from importlib.metadata import entry_points
from typing import Any

from quant.data.subscription import Subscription

# Entry-point group under which plugin distributions register data sources.
DATASOURCE_ENTRY_POINT_GROUP = "quant.datasources"

_REGISTRY: dict[str, Callable[..., Any]] = {}
# Declared subscription offers per registered source (name -> set of keys).
# Offer keys are the canonical ``(venue, symbol, datatype, cadence)`` tuples.
_OFFERS: dict[str, set[tuple]] = {}


def _offer_keys(offers: Iterable[Any]) -> set[tuple]:
    """Normalize declared offers into canonical subscription keys."""
    keys: set[tuple] = set()
    for offer in offers:
        if isinstance(offer, Subscription):
            keys.add(offer.key())
        elif isinstance(offer, tuple) and len(offer) == 4:
            keys.add(tuple(offer))
        else:
            raise ValueError(
                "offers must be Subscription objects or (venue, symbol, datatype, cadence) tuples"
            )
    return keys


def register_datasource(
    name: str, factory: Callable[..., Any], *, offers: Iterable[Any] = ()
) -> None:
    """Register (or override) a named data source factory.

    ``offers`` declares the ``(instrument, datatype, cadence)`` subscriptions
    this source can fulfill, enabling discovery by capability via
    :func:`find_sources`. Empty offers leaves the source resolvable by name
    but not discoverable by subscription.
    """
    if not isinstance(name, str) or not name:
        raise ValueError("data-source name must be a non-empty string")
    _REGISTRY[name] = factory
    _OFFERS[name] = _offer_keys(offers)


def datasource_offers(handler: Any) -> set[tuple]:
    """Read a handler instance's declared subscription offers.

    A source declares its capabilities either as a callable ``offers()``
    returning an iterable of ``Subscription``/keys, or as an ``offers``
    attribute. A handler without offers exposes nothing (empty set).
    """
    declared = getattr(handler, "offers", None)
    if declared is None:
        return set()
    if callable(declared):
        declared = declared()
    return _offer_keys(declared)


def find_sources(subscription: Subscription) -> list[str]:
    """Return registered source names whose offers include *subscription*.

    Built-ins (``backtest`` / ``alpaca``) ship every ``bars`` subscription and
    are resolvable by name directly; capability discovery applies to custom
    sources that declare typed offers.
    """
    key = subscription.key()
    return sorted(name for name, offers in _OFFERS.items() if key in offers)


def _builtin(name: str) -> Callable[..., Any] | None:
    """Resolve a lazily-imported built-in data source (or ``None``)."""
    if name == "backtest":
        from quant.backtest.backtest_data_handler import (  # layering-ok (registry)
            BacktestDataHandler,
        )

        return BacktestDataHandler
    if name == "alpaca":
        from quant.data.data_handler import AlpacaDataHandler

        return AlpacaDataHandler
    return None


def _entry_point(name: str) -> Callable[..., Any] | None:
    """Resolve a data source registered under an entry point (lazily loaded)."""
    for ep in entry_points(group=DATASOURCE_ENTRY_POINT_GROUP):
        if ep.name == name:
            return ep.load()
    return None


def _available() -> list[str]:
    """Sorted data-source names from the registry, built-ins, and entry points."""
    names = set(_REGISTRY)
    names |= {"alpaca", "backtest"}
    names.update(ep.name for ep in entry_points(group=DATASOURCE_ENTRY_POINT_GROUP))
    return sorted(names)


def get_datasource(name: str, **overrides: Any):
    """Resolve a data source by name (built-ins; extras via ``quant.datasources``).

    Resolution order: the code registry, then built-ins, then the
    ``quant.datasources`` entry-point group. Unknown names raise a
    ``ValueError`` listing the available names.
    """
    factory = _REGISTRY.get(name)
    if factory is None:
        factory = _builtin(name)
    if factory is None:
        factory = _entry_point(name)
    if factory is None:
        available = ", ".join(_available())
        raise ValueError(f"Unknown data source {name!r}; available: {available}")
    return factory(**overrides)
