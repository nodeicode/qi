"""Strategy registry and loader.

Strategies are loadable by name through :func:`get_strategy`. The public SDK
ships the built-in reference strategy ``example_equity`` directly; any other
distribution may register additional strategies under the ``quant.strategies``
entry-point group with **no SDK code changes**.

Entry-point schema (in a plugin distribution's ``pyproject.toml``)::

    [project.entry-points."quant.strategies"]
    my_strategy = "my_package.strategy:MyStrategyClass"

The entry-point *name* is the strategy name used with ``get_strategy``; the
entry-point *value* is a ``module:attr`` reference to the strategy class.
Registry lookup order is built-in registry first, then the entry-point group.
Unknown names raise a ``ValueError`` listing the names that are available.
"""

from __future__ import annotations

from importlib.metadata import entry_points

from quant.strategies.example_equity.strategy import ExampleEquityStrategy

# The entry-point group under which plugin distributions register strategies.
STRATEGY_ENTRY_POINT_GROUP = "quant.strategies"

# Built-in strategies are resolvable without any installed entry points. The
# reference strategy `example_equity` is also registered under the entry-point
# group in this SDK's own pyproject so the group is exercised end-to-end.
STRATEGY_REGISTRY = {
    "example_equity": ExampleEquityStrategy,
}


def _entry_point_strategy(name: str):
    """Return the strategy class registered under ``name`` via an entry point.

    Returns ``None`` when no entry point matches. The matching entry point's
    value is a ``module:attr`` reference that is loaded lazily.
    """
    for ep in entry_points(group=STRATEGY_ENTRY_POINT_GROUP):
        if ep.name == name:
            return ep.load()
    return None


def _available_strategies() -> list[str]:
    """Sorted strategy names resolvable from built-ins and the entry-point group."""
    names = set(STRATEGY_REGISTRY)
    names.update(ep.name for ep in entry_points(group=STRATEGY_ENTRY_POINT_GROUP))
    return sorted(names)


def get_strategy(name: str):
    """Resolve the strategy class registered under ``name``.

    Resolution order: the built-in registry first, then the
    ``quant.strategies`` entry-point group (any installed plugin distribution).
    An unknown name raises a ``ValueError`` listing the available names.
    """
    strategy = STRATEGY_REGISTRY.get(name)
    if strategy is None:
        strategy = _entry_point_strategy(name)
    if strategy is None:
        available = ", ".join(_available_strategies())
        raise ValueError(
            f"Strategy '{name}' not found. Available strategies: {available}"
        )
    return strategy
