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

from collections.abc import Callable
from importlib.metadata import entry_points
from typing import Any

# Entry-point group under which plugin distributions register data sources.
DATASOURCE_ENTRY_POINT_GROUP = "quant.datasources"

_REGISTRY: dict[str, Callable[..., Any]] = {}


def register_datasource(name: str, factory: Callable[..., Any]) -> None:
    """Register (or override) a named data source factory."""
    if not isinstance(name, str) or not name:
        raise ValueError("data-source name must be a non-empty string")
    _REGISTRY[name] = factory


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
