"""Universe concept contract (docs/concepts/universe.md; design D12; task 11.2).

The universe is a *composition* (instruments ∘ data ∘ membership rule) — never a
9th primitive. Its one hard requirement is point-in-time identity across the
backtest and live roots (survivorship). This file reproduces the concept doc's
check and runs it against both roots: a conforming (point-in-time-correct) live
view passes; a survivorship-biased view fails loudly.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

import pytest

from ._conformance import ROOTS, run_against_roots


@dataclass(frozen=True)
class Listing:
    """An instrument's active window at a venue (the data source's view)."""

    symbol: str
    listed: datetime
    delisted: datetime | None = None  # None = still listed


def active_as_of(listing: Listing, as_of: datetime) -> bool:
    """Point-in-time activity: listed before/on `as_of`, not yet delisted."""
    return not (
        as_of < listing.listed
        or (listing.delisted is not None and as_of >= listing.delisted)
    )


def pit_universe(listings, membership_rule, as_of: datetime) -> list[str]:
    """The backtest PIT universe: members alive `as_of` and passing the rule."""
    return sorted(
        listing.symbol
        for listing in listings
        if active_as_of(listing, as_of) and membership_rule(listing.symbol)
    )


def check_survivorship(pit_view, live_view, membership_rule, as_of: datetime) -> None:
    """Assert the as-of universe is identical across backtest + live views."""
    expected = pit_universe(pit_view, membership_rule, as_of)
    actual = sorted(s for s in live_view if membership_rule(s))
    if actual != expected:
        missing = sorted(set(expected) - set(actual))
        raise AssertionError(
            f"point-in-time parity violated at {as_of.date()}: "
            f"survivorship bias — missing members {missing}"
        )


T0 = datetime(2020, 1, 1)
AS_OF = datetime(
    2021, 1, 15
)  # DINO is mid-life: listed 2020-06-01, delisted 2021-12-31

HISTORY = [
    Listing("SPY", T0),
    Listing("AAPL", T0),
    Listing("DINO", datetime(2020, 6, 1), datetime(2021, 12, 31)),
]


def membership_rule(symbol: str) -> bool:
    return symbol in {"SPY", "AAPL", "DINO"}


def pit_view(root) -> list[Listing]:
    return HISTORY


# ── both roots ─────────────────────────────────────────────────────────────
def test_conforming_live_view_passes_on_both_roots() -> None:
    def conform(root) -> None:
        listings = pit_view(root)
        live_view = {
            listing.symbol for listing in listings if active_as_of(listing, AS_OF)
        }
        check_survivorship(pit_view(root), live_view, membership_rule, AS_OF)

    results = run_against_roots(conform)
    assert results == {"backtest": None, "live": None}
    assert pit_universe(HISTORY, membership_rule, AS_OF) == ["AAPL", "DINO", "SPY"]


def test_survivorship_biased_view_fails_loudly() -> None:
    # Today's list — DINO was delisted, so it silently drops out of "live".
    biased_live = {"SPY", "AAPL"}
    for root in ROOTS:
        with pytest.raises(AssertionError, match="DINO"):
            check_survivorship(HISTORY, biased_live, membership_rule, AS_OF)
