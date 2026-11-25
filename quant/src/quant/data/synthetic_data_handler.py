"""Deterministic, offline bar source for demos and notebooks.

``SyntheticDataHandler`` is a self-contained ``get_historical_data`` provider
that needs no network and no API keys: it generates an oscillating equity
series so a MA-cross style strategy actually crosses its windows and produces
signals.  It is the keyless way to stand up a backtest or a quick research
notebook against a live-looking bar stream without wiring in a broker.

Contract mirrors the canonical backtest fixtures: tz-naive UTC business-day
bars, sliced to ``[start, end]``, ``adjustment`` accepted and ignored (no
corporate actions on a synthetic series).
"""

from __future__ import annotations

import numpy as np
import pandas as pd


class SyntheticDataHandler:
    """Deterministic, offline bar source (no network, no keys)."""

    def __init__(self, days: int = 300) -> None:
        idx = pd.date_range("2023-06-01", periods=days, freq="B")
        close = 100.0 + 25.0 * np.sin(np.arange(len(idx)) / 12.0)
        self._df = pd.DataFrame(
            {
                "Open": close - 0.5,
                "High": close + 1.0,
                "Low": close - 1.0,
                "Close": close,
                "Volume": [1_000_000] * len(idx),
            },
            index=idx,
        )

    def get_historical_data(
        self,
        ticker: str,
        start,
        end,
        interval: str,
        use_cache: bool = True,
        adjustment: str | None = None,
    ) -> pd.DataFrame:
        del ticker, interval, use_cache, adjustment  # synthetic ignores these
        s = pd.Timestamp(start)
        e = pd.Timestamp(end)
        return self._df.loc[(self._df.index >= s) & (self._df.index <= e)].copy()
