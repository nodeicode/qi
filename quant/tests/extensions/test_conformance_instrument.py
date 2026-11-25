"""Instrument-port conformance suite (spec `instruments`, design D14; D13; task 11.4).

Uses `quant.instruments.validate_instrument` — the shipment conformance check —
to assert identity/contract invariants against both roots. A conforming
instrument passes; a structure missing required contract fields (e.g. an option
without an underlying/strike) is rejected at the boundary and fails loudly.
"""

from __future__ import annotations

from quant.instruments import (
    EquityInstrument,
    FutureInstrument,
    OptionInstrument,
    OptionKind,
    equity_from_tickers,
    validate_instrument,
)

from ._conformance import ROOTS, run_against_roots


def conform(inst, root) -> None:
    problems = validate_instrument(inst)
    assert not problems, f"{root.label}: instrument conformance violated: {problems}"


# ── adapters ───────────────────────────────────────────────────────────────
def conforming_equity() -> EquityInstrument:
    return equity_from_tickers("alpaca", ["SPY"])[0]


def conforming_option() -> OptionInstrument:
    fut = FutureInstrument(
        symbol="ESZ4",
        venue="cme",
        asset_class="FUTURE",
        instrument_class="FUTURE",
        multiplier=50.0,
    )
    return OptionInstrument(
        symbol="ESZ4 C4000",
        venue="cme",
        asset_class="OPTION",
        instrument_class="OPTION",
        underlying=fut,
        strike=4000.0,
        expiration="2024-12-20",
        option_kind=OptionKind.CALL,
    )


def violating_instrument() -> EquityInstrument:
    # Built via __new__ to skip the constructor -> missing identity fields:
    # validate_instrument must catch it at the boundary (no symbol/venue).
    return object.__new__(EquityInstrument)


# ── both roots ─────────────────────────────────────────────────────────────
def test_conforming_instruments_pass_on_both_roots() -> None:
    results = run_against_roots(
        lambda root: (
            conform(conforming_equity(), root),
            conform(conforming_option(), root),
        )
    )
    assert results == {"backtest": (None, None), "live": (None, None)}


def test_violating_instrument_fails_loudly() -> None:
    bad = violating_instrument()
    for root in ROOTS:
        try:
            conform(bad, root)
        except AssertionError:
            continue
        raise AssertionError(f"{root.label}: violating instrument must fail loudly")


def test_instrument_missing_make_order_rejected() -> None:
    # An adapter built without the order-construction surface (not an
    # Instrument subclass, no make_order) must be rejected at the boundary.
    class _NoMakeOrder:
        symbol = "SPY"
        venue = "alpaca"

    problems = validate_instrument(_NoMakeOrder())
    assert "instrument.make_order is missing" in problems
    for root in ROOTS:
        try:
            conform(_NoMakeOrder(), root)
        except AssertionError:
            continue
        raise AssertionError(
            f"{root.label}: instrument missing make_order must fail loudly"
        )


def test_equity_missing_contract_facts_rejected() -> None:
    # An equity definition that omits price_precision/lot_size (key sizing
    # facts) must be surfaced by the conformance check, not relied on mid-run.
    bad = EquityInstrument(
        symbol="SPY", venue="alpaca", price_precision=None, lot_size=None
    )
    problems = validate_instrument(bad)
    assert "equity.price_precision is missing" in problems
    assert "equity.lot_size is missing" in problems
