"""Deterministic synthetic fixtures for the golden backtest regression.

The point of the golden harness (``test_golden_regression.py``) is to freeze
end-to-end ``Backtester.run_backtest`` output *before* a refactor layer and
prove the refactor did not change behavior.  This module builds that frozen
world from scratch — no network, no real strategy — so the same fixture can
run on the equity path today and (with a different strategy) on the futures
path later.
"""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np
import pandas as pd

from quant.core.protocols import PositionDict
from quant.utils.strategy_config import BaseStrategyConfig

TICKER = "TEST"
START_DATE = "2024-01-02"
END_DATE = "2024-02-29"
PRELOAD = 5  # business days before START_DATE (config PRE_LOAD_DAYS)


class FakeDataHandler:
    """Serves deterministic daily OHLCV bars for any (ticker/start/end/interval).

    ``adjustment`` is accepted for ``DataHandlerProtocol`` parity and ignored
    (synthetic series needs no adjustment).  Bars are tz-naive UTC-wall
    midnight, matching the ``prepare_signals`` contract.
    """

    def __init__(self, days: int = 60):
        idx = pd.date_range("2023-12-15", periods=days, freq="B")
        rng_close = 100.0 + np.arange(len(idx), dtype=float) * 0.5
        df = pd.DataFrame(
            {
                "Open": rng_close - 0.5,
                "High": rng_close + 1.0,
                "Low": rng_close - 1.0,
                "Close": rng_close,
                "Volume": [1_000_000] * len(idx),
            },
            index=idx,
        )
        self._df = df

    def get_historical_data(
        self,
        ticker: str,
        start,
        end,
        interval: str,
        use_cache: bool = True,
        adjustment: str | None = None,
    ) -> pd.DataFrame:
        s = pd.Timestamp(start)
        e = pd.Timestamp(end)
        return self._df.loc[(self._df.index >= s) & (self._df.index <= e)].copy()


class GoldenEquityStrategy:
    """Minimal EQUITY strategy: BUY every N-th bar, NEUTRAL otherwise.

    ``manage_trade`` force-exits after ``manage_after_days`` calendar days
    (deterministic), so exits exercise the manage path instead of only the
    end-of-run force close.
    """

    name = "golden_equity"
    client_name = "golden"

    def __init__(
        self, config: BaseStrategyConfig, *, n: int = 7, manage_after_days: int = 5
    ):
        self.config = config
        self._n = n
        self._manage_after_days = manage_after_days

    def calculate_indicators(self, df: pd.DataFrame, **kw) -> pd.DataFrame:
        out = df.copy()
        out["sma"] = out["Close"].rolling(3, min_periods=1).mean()
        return out

    def generate_signals(self, df: pd.DataFrame) -> pd.DataFrame:
        out = df.copy()
        sig = np.where(np.arange(len(out)) % self._n == 0, "BUY", "NEUTRAL")
        out["Signal"] = sig
        out["Confidence"] = 0.9
        out["Reason"] = "golden_test"
        return out

    def manage_trade(self, position: PositionDict, df: pd.DataFrame) -> int:
        """Exit at the first bar >= EntryTime + manage_after_days (or hold)."""
        entry_raw = position.get("EntryTime")
        if entry_raw is None:
            entry_raw = pd.NaT
        # Engine guarantees EntryTime on positions; defensive fallback only.
        entry = pd.Timestamp(entry_raw)  # ty: ignore[invalid-argument-type]
        if entry.tzinfo is not None:
            entry = entry.tz_convert("UTC").tz_localize(None)
        cutoff = entry + pd.Timedelta(days=self._manage_after_days)
        hit = df.index[df.index >= cutoff]
        if len(hit) == 0:
            return -1
        return df.index.get_indexer([hit[0]])[0]


def make_config(**overrides) -> BaseStrategyConfig:
    """Frozen config for the golden equity run."""
    cfg = BaseStrategyConfig(
        INTERVAL="1d",
        MANAGE_INTERVAL="1d",
        PRE_LOAD_DAYS=PRELOAD,
        INITIAL_CAPITAL=10_000.0,
        CAPITAL_PER_TRADE=0.1,
        MAX_CONCURRENT_POSITIONS=2,
        STOP_LOSS_PERCENT=0.30,
        TAKE_PROFIT_PERCENT=-1.0,  # disabled sentinel
        USE_TRAIL_LOSS=False,
        MAX_HOLDING_MINUTES=43_200,  # 30 days - never force-exits mid-run
        MIN_SIGNAL_CONFIDENCE=0.5,
        BASE_SLIPPAGE_FACTOR=0.0,
        TICKERS=[TICKER],
        DATA_DELAY=0,
    )
    for key, value in overrides.items():
        setattr(cfg, key, value)
    return cfg
