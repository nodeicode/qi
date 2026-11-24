"""Venue-port conformance suite (spec `venue`; D13; task 11.4).

Runs the venue contract invariants against both roots: identity + session/calendar
surface + the naive-UTC decision boundary. `BacktestVenue` passes by construction;
a conforming custom venue passes and one missing the required surface fails loudly.
"""

from __future__ import annotations

from datetime import datetime

import pandas as pd

from quant.venue import BacktestVenue, to_utc_naive

from ._conformance import ROOTS, run_against_roots


def conform(venue, root) -> None:
    assert venue.id, f"{root.label}: venue must expose a stable id"
    assert venue.session_for and venue.is_open_at, (
        f"{root.label}: venue must expose session_for / is_open_at"
    )
    # Naive-UTC boundary: a tz-aware input becomes naive; naive stays naive.
    converted = to_utc_naive(pd.Timestamp("2024-06-03 15:00", tz="UTC"))
    assert converted.tzinfo is None, f"{root.label}: boundary must be naive-UTC"
    assert to_utc_naive(datetime(2024, 6, 3, 15, 0)).tzinfo is None
    # Session resolution answers an open/closed decision without raising.
    venue.is_open_at(datetime(2024, 6, 3, 15, 0))


# ── adapters ───────────────────────────────────────────────────────────────
class ConformingVenue(BacktestVenue):
    """A valid custom venue; passes by satisfying the Venue protocol."""

    id = "conforming"


class ViolatingVenue:
    """Missing the required venue surface — must be rejected."""

    id = "violating"


# ── both roots ─────────────────────────────────────────────────────────────
def test_conforming_venue_passes_on_both_roots() -> None:
    results = run_against_roots(lambda root: conform(ConformingVenue(), root))
    assert results == {"backtest": None, "live": None}


def test_violating_venue_fails_loudly() -> None:
    for root in ROOTS:
        try:
            conform(ViolatingVenue(), root)
        except (AssertionError, AttributeError, TypeError):
            continue
        raise AssertionError(f"{root.label}: violating venue must fail loudly")
