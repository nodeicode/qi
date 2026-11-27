"""Offline fixtures for the ``example_equity`` golden regression (declarative).

Stable synthetic equity bars live under ``quant/tests/data/fixtures/`` and are
served through a data handler that never touches a network or API key, so the
backtest path runs fully offline. The bars are a deterministic oscillation (no
RNG) that produces a realistic run of MA-cross decisions for the declarative
``ExampleEquityStrategy`` (1h bar interval).
"""

from __future__ import annotations

from pathlib import Path

import pandas as pd

from quant.data import Datatype, Subscription
from quant.instruments import EquityInstrument
from quant.strategies.example_equity.strategy import ExampleEquityConfig

TICKER = "SPY"
START_DATE = "2024-01-02"
END_DATE = "2024-03-01"

_BARS_CSV = (
    Path(__file__).resolve().parents[1] / "data" / "fixtures" / "example_equity_h1.csv"
)
GOLDEN_JSON = (
    Path(__file__).resolve().parents[1]
    / "data"
    / "fixtures"
    / "example_equity_golden.json"
)


def load_example_equity_bars() -> pd.DataFrame:
    """Load the stable synthetic hourly bars (tz-naive UTC, like the engine)."""
    df = pd.read_csv(_BARS_CSV)
    df["timestamp"] = pd.to_datetime(df["timestamp"])
    df = df.set_index("timestamp")
    df.index.name = None
    return df


class ExampleEquityDataHandler:
    """Serves the synthetic bars for a subscription or a (ticker/interval) window."""

    _SUB = Subscription(
        instrument=EquityInstrument(symbol=TICKER, venue="backtest"),
        datatype=Datatype.BARS,
        cadence="1h",
    )

    def get_data(self, subscription, start, end, use_cache=True, adjustment=None):
        del use_cache, adjustment
        # Serve any bars subscription from the loaded frame (the synthetic series
        # has a single cadence; the MTM/benchmark reads reuse it for ``1d``).
        frame = load_example_equity_bars()
        s = pd.Timestamp(start)
        e = pd.Timestamp(end)
        return frame.loc[(frame.index >= s) & (frame.index <= e)].copy()


def make_example_config(**overrides) -> ExampleEquityConfig:
    """Frozen config for the example_equity golden run (declarative defaults)."""
    cfg = ExampleEquityConfig()
    for key, value in overrides.items():
        setattr(cfg, key, value)
    return cfg
