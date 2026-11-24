"""Per-port conformance helpers (design D13; task group 11.4).

A conformance suite runs a port's invariants against BOTH roots — backtest and
live — so a replacement port is only parity-safe if it passes in both. Here the
roots are lightweight, offline stand-ins over a deterministic synthetic world
(the same suite shape runs against real backtest/live wiring in production).
Qi's shipped defaults pass by construction; a conforming custom adapter passes
and a violating one fails loudly (raises, never warns).
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass


@dataclass(frozen=True)
class Root:
    """A named execution root a conformance suite is bound to."""

    label: str


BACKTEST = Root("backtest")
LIVE = Root("live")
ROOTS: tuple[Root, Root] = (BACKTEST, LIVE)


def run_against_roots[T](fn: Callable[[Root], T], roots=ROOTS) -> dict[str, T]:
    """Run ``fn`` for each root; return ``{root_label: result}``.

    If ``fn`` raises for either root the violation surfaces loudly — a
    conforming adapter must pass on every root.
    """
    return {root.label: fn(root) for root in roots}
