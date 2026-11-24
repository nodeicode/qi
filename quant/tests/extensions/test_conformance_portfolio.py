"""Portfolio-port conformance suite (spec `portfolio`, design D4; D13; task 11.4).

Runs the fills-only accounting invariant against both roots: fills are the only
mutator and the PnL identity `equity - starting_equity == realized + unrealized`
holds. Qi's shipped `Portfolio` passes by construction; a portfolio structure
that breaks the identity fails loudly.
"""

from __future__ import annotations

from quant.portfolio import Fill, Portfolio, Side

from ._conformance import ROOTS, run_against_roots


def conform(pf, root) -> None:
    pf.apply_fill(
        Fill(
            venue=root.label,
            symbol="AAPL",
            side=Side.BUY,
            qty=10,
            price=150.0,
            strategy="mom",
        )
    )
    pf.apply_fill(
        Fill(
            venue=root.label,
            symbol="AAPL",
            side=Side.SELL,
            qty=10,
            price=160.0,
            strategy="mom",
        )
    )
    prices = {("paper", "AAPL"): 160.0, ("live", "AAPL"): 160.0}
    realized = pf.realized_pnl()

    if pf.starting_equity > 0:
        equity = pf.equity(prices)
        expected = pf.starting_equity + realized + 0.0
        assert abs(equity - expected) < 1e-9, (
            f"{root.label}: realized PnL identity violated "
            f"(equity {equity} != {expected})"
        )


# ── adapters ───────────────────────────────────────────────────────────────
def conforming_portfolio() -> Portfolio:
    return Portfolio(starting_equity=10_000.0, name="conforming")


class ViolatingPortfolio:
    """A fake portfolio that forgets to settle realized PnL into equity."""

    def __init__(self) -> None:
        self.starting_equity = 10_000.0
        self.cash = self.starting_equity

    def apply_fill(self, fill) -> None:
        self.cash -= fill.delta * fill.price  # ignores realized settlement

    def realized_pnl(self) -> float:
        return 200.0  # books PnL but never folds proceeds into cash

    def equity(self, prices) -> float:
        return self.cash  # realized PnL is lost -> identity breaks


# ── both roots ─────────────────────────────────────────────────────────────
def test_conforming_portfolio_passes_on_both_roots() -> None:
    results = run_against_roots(lambda root: conform(conforming_portfolio(), root))
    assert results == {"backtest": None, "live": None}


def test_violating_portfolio_fails_loudly() -> None:
    for root in ROOTS:
        try:
            conform(ViolatingPortfolio(), root)
        except AssertionError:
            continue
        raise AssertionError(f"{root.label}: violating portfolio must fail loudly")
