"""Data-port conformance suite (spec `data-access`; D13; Data-source conformance).

Runs the typed offer/fulfillment contract against both roots: a conforming
custom source declares its ``(instrument, datatype, cadence)`` offers and
``get_data`` fulfills them with the canonical column shape + naive-UTC
timestamps; a violating source (no offers and/or a mis-shaped fulfillment)
fails loudly rather than silently risking backtest/live drift. Registry
discovery by typed offers is covered here too.
"""

from __future__ import annotations

import pandas as pd

from quant.data import (
    Datatype,
    Subscription,
    datasource_offers,
    find_sources,
    register_datasource,
)
from quant.instruments import EquityInstrument

from ._conformance import ROOTS, run_against_roots

SUB = Subscription(
    instrument=EquityInstrument(symbol="SPY", venue="paper"),
    datatype=Datatype.BARS,
    cadence="1d",
)


def conform(handler, root) -> None:
    # A conforming source declares the offer it fulfills.
    assert datasource_offers(handler), (
        f"{root.label}: data source must declare its subscription offers"
    )
    df = handler.get_data(SUB, "2024-01-02", "2024-01-10", adjustment="all")
    assert {"Open", "High", "Low", "Close", "Volume"}.issubset(df.columns), (
        f"{root.label}: data source must return canonical OHLCV columns"
    )
    idx = pd.DatetimeIndex(df.index)
    assert idx.tz is None, f"{root.label}: timestamps must be naive-UTC"


# ── adapters ───────────────────────────────────────────────────────────────
class ConformingDataHandler:
    """Declares a bars offer and fulfills it: canonical columns, naive-UTC."""

    def offers(self) -> list[Subscription]:
        return [SUB]

    def get_data(self, subscription, start, end, use_cache=True, adjustment=None):
        del use_cache, adjustment
        assert subscription.datatype is Datatype.BARS
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
    """Mis-handles the contract: no declared offers + tz-aware non-canonical bars."""

    def get_data(self, subscription, start, end, use_cache=True, adjustment=None):
        del subscription, start, end, use_cache, adjustment
        idx = pd.to_datetime(["2024-01-02 14:00", "2024-01-03 14:00"], utc=True)
        return pd.DataFrame({"Close": [1.2, 2.2]}, index=idx)


# ── both roots ─────────────────────────────────────────────────────────────
def test_conforming_data_passes_on_both_roots() -> None:
    results = run_against_roots(lambda root: conform(ConformingDataHandler(), root))
    assert results == {"backtest": None, "live": None}


def test_violating_data_fails_loudly() -> None:
    for root in ROOTS:
        try:
            conform(ViolatingDataHandler(), root)
        except (AssertionError, AttributeError, KeyError):
            continue
        raise AssertionError(f"{root.label}: violating data source must fail loudly")


# ── discovery by typed offers ──────────────────────────────────────────────
def _conforming_factory():
    return ConformingDataHandler()


def _empty_factory():
    return object()


def test_source_found_by_its_typed_offers() -> None:
    register_datasource("offer_source", _conforming_factory, offers=[SUB])
    register_datasource("other_source", _empty_factory, offers=[])
    assert "offer_source" in find_sources(SUB)
    other_sub = Subscription(
        instrument=EquityInstrument(symbol="QQQ", venue="paper"),
        datatype=Datatype.BARS,
        cadence="1m",
    )
    assert "offer_source" not in find_sources(other_sub)
