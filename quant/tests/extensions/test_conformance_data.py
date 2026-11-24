"""Data-port conformance suite (spec `data-access`; D13; task 11.4).

Runs the handler contract invariants against both roots: canonical column shape,
naive-UTC timestamps, and tolerance of the ``adjustment`` argument. Qi's shipped
handlers pass by construction; a conforming custom handler passes and a
violating one (non-naive index / inconsistent shape) fails loudly.
"""

from __future__ import annotations

import pandas as pd

from ._conformance import ROOTS, run_against_roots


def conform(handler, root) -> None:
    df = handler.get_historical_data(
        "SPY", "2024-01-02", "2024-01-10", "1d", adjustment="all"
    )
    assert {"Open", "High", "Low", "Close", "Volume"}.issubset(df.columns), (
        f"{root.label}: data source must return canonical OHLCV columns"
    )
    idx = pd.DatetimeIndex(df.index)
    assert idx.tz is None, f"{root.label}: timestamps must be naive-UTC"


# ── adapters ───────────────────────────────────────────────────────────────
class ConformingDataHandler:
    """Deterministic in-memory bars: canonical columns, naive-UTC, tolerates adjustment."""

    def get_historical_data(
        self, ticker, start, end, interval, use_cache=True, adjustment=None
    ):
        del ticker, start, end, interval, use_cache  # synthetic ignores these
        idx = pd.to_datetime(["2024-01-02 14:00", "2024-01-03 14:00"])
        return pd.DataFrame(
            {
                "Open": [1.0, 2.0],
                "High": [1.5, 2.5],
                "Low": [0.8, 1.8],
                "Close": [1.2, 2.2],
                "Volume": [100, 200],
            },
            index=idx,
        )


class ViolatingDataHandler:
    """Mis-handles the contract: tz-aware index and non-canonical columns."""

    def get_historical_data(
        self, ticker, start, end, interval, use_cache=True, adjustment=None
    ):
        del ticker, start, end, interval, use_cache
        idx = pd.to_datetime(["2024-01-02 14:00", "2024-01-03 14:00"], utc=True)
        return pd.DataFrame(
            {"Close": [1.2, 2.2]}, index=idx
        )  # missing OHLCV + tz-aware


# ── both roots ─────────────────────────────────────────────────────────────
def test_conforming_data_passes_on_both_roots() -> None:
    results = run_against_roots(lambda root: conform(ConformingDataHandler(), root))
    assert results == {"backtest": None, "live": None}


def test_violating_data_fails_loudly() -> None:
    for root in ROOTS:
        try:
            conform(ViolatingDataHandler(), root)
        except AssertionError:
            continue
        raise AssertionError(f"{root.label}: violating data source must fail loudly")
