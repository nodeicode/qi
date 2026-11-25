"""Deterministic synthetic fixtures for the golden backtest regression.

The golden harness (``test_golden_regression.py``) freezes end-to-end
``Backtester.run_backtest`` output over this fixture and proves the declarative
path (resume strategy-dataflow: engine → author body → backtest venue) is
deterministic and reproducible. No network, no real strategy — the fixture is
built from scratch so the same bars drive the equity path today and the futures
path later.

The strategy here is declarative (``decide(events, portfolio)``): it BUYs on
every N-th served bar and exits via the max-hold risk rule after
``manage_after_days`` calendar days.
"""

from __future__ import annotations

from typing import ClassVar

import numpy as np
import pandas as pd

from quant.data import Datatype, Subscription
from quant.instruments import EquityInstrument
from quant.portfolio import Side
from quant.strategies.base import ExitIntent, Strategy
from quant.strategies.risk import exit_risk_hit
from quant.strategies.sizing import size_equity_lot
from quant.utils.strategy_config import BaseStrategyConfig

TICKER = "TEST"
START_DATE = "2024-01-02"
END_DATE = "2024-02-29"
PRELOAD = 5  # business days before START_DATE (config PRE_LOAD_DAYS)


class FakeDataHandler:
    """Serves deterministic daily OHLCV bars for any (ticker/start/end/interval).

    ``adjustment`` is accepted for ``DataHandlerProtocol`` parity and ignored
    (the synthetic series needs no adjustment). Bars are tz-naive UTC-wall
    midnight, matching the engine's bar-clock contract.
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

    def get_data(self, subscription, start, end, use_cache=True, adjustment=None):
        del subscription, use_cache, adjustment
        s = pd.Timestamp(start)
        e = pd.Timestamp(end)
        return self._df.loc[(self._df.index >= s) & (self._df.index <= e)].copy()

    def get_historical_data(
        self,
        ticker: str,
        start,
        end,
        interval: str,
        use_cache: bool = True,
        adjustment: str | None = None,
    ) -> pd.DataFrame:
        del ticker, interval
        return self.get_data(
            None, start, end, use_cache=use_cache, adjustment=adjustment
        )


class GoldenEquityStrategy(Strategy):
    """Deterministic declarative equity strategy: BUY every N-th bar, max-hold exit."""

    name: ClassVar[str] = "golden_equity"
    venue: ClassVar[str] = "backtest"
    instruments: ClassVar[list] = [EquityInstrument(symbol=TICKER, venue="backtest")]
    subscriptions: ClassVar[list] = [
        Subscription(instrument=instruments[0], datatype=Datatype.BARS, cadence="1d")
    ]

    def __init__(
        self,
        config: BaseStrategyConfig | None = None,
        *,
        n: int = 7,
        manage_after_days: int = 5,
    ):
        self.config = config or make_config()
        # Max-hold exit = ``manage_after_days`` calendar days (deterministic).
        self.config.MAX_HOLDING_MINUTES = max(1, int(manage_after_days) * 24 * 60)
        self._n = max(1, int(n))
        super().__init__()

    def decide(self, events, portfolio) -> list:
        bars = events.bars(TICKER)
        if bars is None or bars.empty:
            return []
        now = bars.index[-1]
        close = float(bars["Close"].iloc[-1])
        out: list = []

        # Manage pass — the folded exit evaluation (legacy manage_trade role).
        for pos in portfolio.open_positions(self.name):
            if exit_risk_hit(pos, self.config, spot=close, at=now):
                out.append(ExitIntent(self.name, pos, reason="max hold"))

        # Signal pass — BUY every N-th served bar, sized on the instrument lot.
        if (
            len(bars) % self._n == 0
            and len(portfolio.open_positions(self.name))
            < self.config.MAX_CONCURRENT_POSITIONS
        ):
            qty = size_equity_lot(
                portfolio,
                close,
                instrument=self.instruments[0],
                buying_power_fraction=0.1,
            )
            if qty > 0:
                out.append(
                    self.instruments[0].make_order(Side.BUY, qty, strategy=self.name)
                )
        return out


def make_config(**overrides) -> BaseStrategyConfig:
    """Frozen config for the golden equity run (declarative default fields)."""
    cfg = BaseStrategyConfig(
        PRE_LOAD_DAYS=PRELOAD,
        INITIAL_CAPITAL=10_000.0,
        MAX_CONCURRENT_POSITIONS=2,
        STOP_LOSS_PERCENT=0.30,
        TAKE_PROFIT_PERCENT=-1.0,  # disabled sentinel
        USE_TRAIL_LOSS=False,
        MAX_HOLDING_MINUTES=43_200,  # 30 days default (overridden per strategy)
        MIN_SIGNAL_CONFIDENCE=0.5,
        BASE_SLIPPAGE_FACTOR=0.0,
        DATA_DELAY=0,
    )
    for key, value in overrides.items():
        setattr(cfg, key, value)
    return cfg
