"""Execution-port conformance suite (spec `execution` `Executor conformance`).

Conformance lives on the **venue seam**: a custom venue's ``execution_client``
is only parity-safe when it passes this suite against both roots. A conforming
client honors the typed-order contract — accepting a strategy-built ``Order``
(via ``instrument.make_order``), reporting back a ``Fill`` (fill-ledger) whose
``(venue, symbol)`` key opens the position surface — on every root. Qi's
shipped backtest/live venues pass by construction; a replacement missing the
seam (e.g. no ``submit``) or reporting a broken fill fails loudly instead of
silently risking drift between research and production.
"""

from __future__ import annotations

from quant.instruments import EquityInstrument
from quant.portfolio import Fill, Portfolio, Side

from ._conformance import ROOTS, run_against_roots

# A typed instrument used to build the conformance order (spec `execution`
# `Typed order primitive`): ``make_order`` is the strategy-side construction
# seam a venue's execution client consumes.
_ORDER_INSTRUMENT = EquityInstrument(symbol="SPY", venue="backtest")


def conform(execution_client, root) -> None:
    """The execution-port invariants for one root."""
    # The typed-order seam must be present: accept a strategy ``Order``.
    assert callable(getattr(execution_client, "submit", None)), (
        f"{root.label}: execution client must expose submit(order)"
    )
    order = _ORDER_INSTRUMENT.make_order(side=Side.BUY, qty=10)
    assert order.venue == _ORDER_INSTRUMENT.venue, (
        f"{root.label}: order venue derives from its instrument"
    )

    fill = execution_client.submit(order)
    # A valid market order with a resolvable price must report a fill.
    assert isinstance(fill, Fill), (
        f"{root.label}: submit must report a Fill for the conformance order"
    )
    # Fill-ledger invariant: the reported fill carries the order's identity and
    # a positive price (never a silent zero/no-op).
    assert fill.venue == order.venue and fill.symbol == order.instrument.symbol, (
        f"{root.label}: fill must key the same (venue, symbol) as the order"
    )
    assert fill.price > 0, f"{root.label}: a fill must report a positive price"

    # Position surface: the fill opens a position in a ``Portfolio`` (spec
    # `portfolio`, fill-driven accounting) — the same surface the strategy body
    # reads on the next tick.
    pf = Portfolio(starting_equity=10_000.0)
    pf.apply_fill(fill)
    assert (order.venue, order.instrument.symbol) in pf.positions, (
        f"{root.label}: the fill must open the position surface"
    )


# ── adapters ───────────────────────────────────────────────────────────────
class ConformingExecutionClient:
    """Honors the typed-order seam: ``submit(order) -> Fill`` (always fills)."""

    def submit(self, order) -> Fill:
        return Fill(
            venue=order.venue,
            symbol=order.instrument.symbol,
            side=order.side,
            qty=float(order.quantity),
            price=100.0,
            strategy=order.strategy,
            instrument=order.instrument,
        )


class ViolatingExecutionClient:
    """Missing the typed-order seam (no ``submit``) — must be rejected."""

    def submit(self, order) -> None:
        return None


# ── both roots ─────────────────────────────────────────────────────────────
def test_conforming_execution_client_passes_on_both_roots() -> None:
    results = run_against_roots(lambda root: conform(ConformingExecutionClient(), root))
    assert results == {"backtest": None, "live": None}


def test_violating_execution_client_fails_loudly() -> None:
    for root in ROOTS:
        try:
            conform(ViolatingExecutionClient(), root)
        except (AssertionError, AttributeError, TypeError):
            continue
        raise AssertionError(
            f"{root.label}: violating execution client must fail loudly"
        )
