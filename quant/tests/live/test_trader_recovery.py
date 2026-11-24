import os
import sys
import unittest

# Alpaca is stubbed per-test by the autouse fixture in quant/tests/live/conftest.py.
from dataclasses import dataclass, field
from unittest.mock import MagicMock, patch

# Add project root to path
sys.path.append(
    os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
)
sys.path.append(
    os.path.join(
        os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))),
        "python",
    )
)

from quant.live.trader import UnifiedTrader
from quant.utils.strategy_config import BaseStrategyConfig


@dataclass
class RecoveryTestConfig(BaseStrategyConfig):
    INTERVAL: str = "1m"
    MANAGE_INTERVAL: str = "1m"
    PRE_LOAD_DAYS: int = 1
    OPTION_INTERVAL: str = "1m"
    INITIAL_CAPITAL: float = 100000.0
    CAPITAL_PER_TRADE: float = 0.01
    MAX_CONCURRENT_POSITIONS: int = 5
    STOP_LOSS_PERCENT: float = 0.05
    TAKE_PROFIT_PERCENT: float = 0.10
    BREAKEVEN_PROFIT_PERCENT: float = 0.05
    USE_TRAIL_LOSS: bool = False
    MAX_HOLDING_MINUTES: int = 60
    MIN_SIGNAL_CONFIDENCE: float = 0.5
    MAX_DAY_TRADES: int = 10
    BASE_SLIPPAGE_FACTOR: float = 0.01
    TICKERS: list = field(default_factory=lambda: ["SPY"])
    MIN_OPTION_DTE: int = 0


class RecoveryTestStrategy:
    def __init__(self):
        self.name = "RecoveryStrategy"
        self.config = RecoveryTestConfig()
        self.env = "paper"
        self.client_name = "paper"

    def calculate_indicators(self, df, **kwargs):
        return df

    def generate_signals(self, df):
        out = df[["Close"]].copy()
        out["Signal"] = "HOLD"
        out["Confidence"] = 0.0
        out["Reason"] = ""
        return out

    def manage_trade(self, position, df):
        return -1


class TestUnifiedTraderRecovery(unittest.TestCase):
    def setUp(self):
        self.mock_data_handler = MagicMock()
        self.mock_client = MagicMock()
        self.mock_data_handler.clients = {"paper": self.mock_client}

    @patch("quant.live.trader.load_state")
    @patch("quant.live.trader.AlpacaDataHandler")
    @patch("quant.live.trader.MarketSchedule")
    @patch("quant.live.trader.load_strategies")
    def test_state_recovery(
        self,
        mock_load_strategies,
        mock_schedule_cls,
        mock_dh_cls,
        mock_load_state,
    ):
        mock_dh_cls.return_value = self.mock_data_handler
        mock_schedule_cls.return_value.is_market_open.return_value = True

        strategy = RecoveryTestStrategy()
        mock_load_strategies.return_value = [strategy]

        saved_pos = {
            "RecoveryStrategy": [
                {
                    "Strategy": "RecoveryStrategy",
                    "Ticker": "SPY",
                    "Env": "paper",
                    "OrderId": "old_order_1",
                    "Qty": 10,
                    "EntryTime": "2024-06-21T14:00:00+00:00",
                    "Direction": 1,
                    "EntryPrice": 100.0,
                }
            ]
        }
        saved_last_update = {"RecoveryStrategy": "2023-01-01T12:00:00+00:00"}

        mock_load_state.return_value = {
            "active_positions": saved_pos,
            "orders": {},
            "last_update": saved_last_update,
        }

        trader = UnifiedTrader()

        # Positions recovered
        self.assertIn("RecoveryStrategy", trader.active_positions)
        self.assertEqual(len(trader.active_positions["RecoveryStrategy"]), 1)
        self.assertEqual(
            trader.active_positions["RecoveryStrategy"][0]["OrderId"], "old_order_1"
        )

        # Last update recovered
        self.assertIn("RecoveryStrategy", trader.last_update)
        self.assertEqual(trader.last_update["RecoveryStrategy"].year, 2023)

    @patch("quant.live.trader.load_state")
    @patch("quant.live.trader.AlpacaDataHandler")
    @patch("quant.live.trader.MarketSchedule")
    @patch("quant.live.trader.load_strategies")
    def test_recovery_drops_malformed_position(
        self,
        mock_load_strategies,
        mock_schedule_cls,
        mock_dh_cls,
        mock_load_state,
    ):
        mock_dh_cls.return_value = self.mock_data_handler
        mock_schedule_cls.return_value.is_market_open.return_value = True
        mock_load_strategies.return_value = [RecoveryTestStrategy()]

        valid = {
            "Strategy": "RecoveryStrategy",
            "Ticker": "SPY",
            "Env": "paper",
            "OrderId": "ok_1",
            "Qty": 10,
            "EntryTime": "2024-06-21T14:00:00+00:00",
            "Direction": 1,
            "EntryPrice": 100.0,
        }
        malformed = {
            "Strategy": "RecoveryStrategy",
            "Ticker": "SPY",  # missing Env / Qty / OrderId — fail closed
        }
        mock_load_state.return_value = {
            "active_positions": {
                "RecoveryStrategy": [valid, malformed],
                "RemovedStrategy": [malformed],
            },
            "last_update": {"RecoveryStrategy": "2023-01-01T12:00:00+00:00"},
        }

        trader = UnifiedTrader()

        # Valid record survives; malformed one is dropped (fail closed on it).
        self.assertEqual(len(trader.active_positions["RecoveryStrategy"]), 1)
        self.assertEqual(
            trader.active_positions["RecoveryStrategy"][0]["OrderId"], "ok_1"
        )
        # Strategy removed since state was written is not re-created.
        self.assertNotIn("RemovedStrategy", trader.active_positions)


if __name__ == "__main__":
    unittest.main()
