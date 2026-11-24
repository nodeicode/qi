"""Offline fixtures for the ``example_equity`` golden regression (group 7.3).

Stable synthetic equity bars live under ``quant/tests/data/fixtures/`` and are
served through a data handler that never touches a network or API key, so the
backtest path runs fully offline. The bars are a deterministic deterministic
oscillation (no RNG) that produces a realistic run of MA-cross signals for
``ExampleEquityStrategy`` (1h interval).
"""

from __future__ import annotations

from pathlib import Path

import pandas as pd

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
    """Serves the synthetic bars for any (ticker/start/end/interval) window."""

    def get_historical_data(
        self,
        ticker: str,
        start,
        end,
        interval: str,
        use_cache: bool = True,
        adjustment: str | None = None,
    ) -> pd.DataFrame:
        frame = load_example_equity_bars()
        s = pd.Timestamp(start)
        e = pd.Timestamp(end)
        return frame.loc[(frame.index >= s) & (frame.index <= e)].copy()


def make_example_config(**overrides) -> ExampleEquityConfig:
    """Frozen config for the example_equity golden run."""
    cfg = ExampleEquityConfig(TICKERS=[TICKER])
    for key, value in overrides.items():
        setattr(cfg, key, value)
    return cfg
