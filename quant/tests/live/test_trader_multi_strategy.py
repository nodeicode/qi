import datetime
import os
import sys
import unittest
from dataclasses import dataclass, field
from unittest.mock import MagicMock, patch

import pandas as pd

from quant.live.protocols import LiveStrategyProtocol
from quant.live.trader import UnifiedTrader
from quant.utils.strategy_config import BaseStrategyConfig

# N.B. alpaca is stubbed per-test by the autouse fixture in tests/live/conftest.py.
sys.path.append(
    os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
)
sys.path.append(
    os.path.join(
        os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))),
        "python",
    )
)


@dataclass
class DummyConfig(BaseStrategyConfig):
    INTERVAL: str = "1m"
    MANAGE_INTERVAL: str = "1m"
    PRE_LOAD_DAYS: int = 1
    DATA_DELAY: int = 0
    TICKERS: list = field(default_factory=lambda: ["SPY"])
    MIN_OPTION_DTE: int | None = None  # Equity
    OPTION_INTERVAL: str = "1m"
    INITIAL_CAPITAL: float = 100000.0
    CAPITAL_PER_TRADE: float = 0.10
    MAX_CONCURRENT_POSITIONS: int = 5
    STOP_LOSS_PERCENT: float = 0.05
    TAKE_PROFIT_PERCENT: float = 0.10
    BREAKEVEN_PROFIT_PERCENT: float = 0.05
    USE_TRAIL_LOSS: bool = False
    MAX_HOLDING_MINUTES: int = 60
    MIN_SIGNAL_CONFIDENCE: float = 0.5
    MAX_DAY_TRADES: int = 10
    BASE_SLIPPAGE_FACTOR: float = 0.01


class DummyStrategy(LiveStrategyProtocol):
    def __init__(self, name, env):
        self.name = name
        self.config = DummyConfig()
        self.env = env
        self.client_name = env  # 'paper' or 'prod'

    def calculate_indicators(self, df: pd.DataFrame, **kw) -> pd.DataFrame:
        return df

    def generate_signals(self, df: pd.DataFrame) -> pd.DataFrame:
        """Return DataFrame with BUY on every bar (equity path)."""
        out = df[["Close"]].copy()
        out["Signal"] = "BUY"
        out["Confidence"] = 1.0
        out["Reason"] = "test"
        return out


class TestUnifiedTraderMultiStrategy(unittest.TestCase):
    def setUp(self):
        self.mock_data_handler = MagicMock()
        self.mock_client = MagicMock()
        self.mock_data_handler.clients = {
            "paper": self.mock_client,
            "prod": self.mock_client,
        }

    @patch("quant.live.trader.load_state", return_value={})
    @patch("quant.live.trader.AlpacaDataHandler")
    @patch("quant.live.trader.MarketSchedule")
    @patch("quant.live.trader.load_strategies")
    def test_multi_strategy_execution(
        self,
        mock_load_strategies,
        mock_schedule_cls,
        mock_dh_cls,
        mock_load_state,
    ):
        mock_dh_cls.return_value = self.mock_data_handler
        mock_schedule_cls.return_value.is_market_open.return_value = True

        s1 = DummyStrategy("Strat1", "paper")
        s2 = DummyStrategy("Strat2", "prod")
        mock_load_strategies.return_value = [s1, s2]

        trader = UnifiedTrader()
        trader.clients = {"paper": self.mock_client, "prod": self.mock_client}
        trader.unified_executor.equity_mgr.get = MagicMock(return_value=100_000.0)
        trader.unified_executor.equity_mgr.debit = MagicMock()
        trader.unified_executor.equity_mgr.credit = MagicMock()
        trader.unified_executor.equity_mgr.set_active = MagicMock(return_value=True)

        now = datetime.datetime.now(datetime.UTC).replace(second=0, microsecond=0)
        # UTC-naive minute bars aligned with closed_bar_asof floor semantics.
        dates = pd.date_range(end=now.replace(tzinfo=None), periods=20, freq="1min")
        df = pd.DataFrame(
            {"Open": 100, "High": 105, "Low": 99, "Close": 102, "Volume": 1000},
            index=dates,
        )
        self.mock_data_handler.get_historical_data.return_value = df

        mock_order = MagicMock()
        mock_order.id = "order_1"
        self.mock_client.submit_order.return_value = mock_order

        # process_strategy now handles execution inline via engine + LiveExecutor.
        trader.process_strategy(s1, now)
        trader.process_strategy(s2, now)

        # Both strategies should have generated + executed an order
        self.assertEqual(self.mock_client.submit_order.call_count, 2)

        self.assertEqual(len(trader.active_positions["Strat1"]), 1)
        self.assertEqual(len(trader.active_positions["Strat2"]), 1)


if __name__ == "__main__":
    unittest.main()
