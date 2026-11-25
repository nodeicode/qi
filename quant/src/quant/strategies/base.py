r"""Declarative strategy surface (spec `strategy-authoring`).

A strategy is *declare + react*: it declares typed instruments, typed data
subscriptions, and a venue, then implements a single author body
``decide(events, portfolio)`` that BOTH generates entries from the delivered
bars and evaluates the portfolio's existing open positions for exits. That one
body replaces the legacy two-half pipeline (``generate_signals`` + sizing for
entries; ``manage_trade`` + the engine's manage-exit evaluation for exits).

``decide`` returns a list of decision items: typed ``Order``\ s the strategy
builds through ``instrument.make_order``, or exit intents for positions held
in the injected (read-only) portfolio. The strategy sizes from instrument
contract facts + the portfolio — no engine- or registry-owned sizing exists on
this surface.
"""

from __future__ import annotations

from typing import Any, ClassVar


class Strategy:
    """Base for strategies written against the declarative surface.

    Subclasses declare ``instruments`` (typed instruments), ``subscriptions``
    (``(instrument, datatype, cadence)`` tuples), and ``venue`` (a registered
    venue id), provide a ``config``, and override ``decide``. The engine drives
    ``decide`` over closed bars from the subscription buffers plus the injected
    read-only portfolio.
    """

    #: Human-readable strategy name (registry key / log prefix).
    name: str = ""
    #: Strategy-owned config carrying risk/execution fields for the author.
    config: Any = None
    #: Declared typed instruments the strategy trades.
    instruments: ClassVar[list[Any]] = []
    #: Declared data subscriptions ``(instrument, datatype, cadence)``.
    subscriptions: ClassVar[list[Any]] = []
    #: Declared venue id the strategy's orders route to.
    venue: ClassVar[str] = ""

    def decide(self, events, portfolio) -> list[Any]:
        """Author body: generate entries and evaluate positions for exits.

        ``events`` are closed bars delivered for the strategy's subscriptions;
        ``portfolio`` is the injected read-only portfolio holding this
        strategy's open positions. Returns a list of typed ``Order``/exit
        intents (empty = do nothing).
        """
        raise NotImplementedError(
            f"Strategy {type(self).__name__!r} must implement decide(events, portfolio)"
        )
