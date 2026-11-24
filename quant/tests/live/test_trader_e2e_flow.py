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

# Alpaca is stubbed per-test by the autouse fixture in quant/tests/live/conftest.py.
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

# -----------------------------------------------------------------------------
# 1. Define Dummy Configuration & Strategy
# -----------------------------------------------------------------------------


@dataclass
class DummyConfig(BaseStrategyConfig):
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
    DATA_DELAY: int = 0


class DummyStrategy(LiveStrategyProtocol):
    def __init__(self):
        self.name = "DummyStrategy_paper"
        self.config = DummyConfig()
        self.env = "paper"
        self.client_name = "paper"

    def calculate_indicators(self, df: pd.DataFrame, **kw) -> pd.DataFrame:
        df = df.copy()
        df["SMA"] = df["Close"].rolling(10).mean()
        return df

    def generate_signals(self, df: pd.DataFrame) -> pd.DataFrame:
        """Return Signal / Confidence / Reason for every bar (parity-safe)."""
        out = df[["Close"]].copy()
        out["Signal"] = "HOLD"
        out["Confidence"] = 0.0
        out["Reason"] = ""

        if "SMA" in df.columns:
            buy = df["Close"] > df["SMA"]
            out.loc[buy, "Signal"] = "BUY_CALL"
            out.loc[buy, "Confidence"] = 1.0
            out.loc[buy, "Reason"] = "Close > SMA"

        return out

    def manage_trade(self, position: dict, df: pd.DataFrame) -> int:
        # Hold-only dummy — matches StrategyProtocol contract (exit idx, -1 = hold).
        return -1


# -----------------------------------------------------------------------------
# 2. Test Class
# -----------------------------------------------------------------------------


class TestUnifiedTraderE2E(unittest.TestCase):
    def setUp(self):
        self.mock_data_handler = MagicMock()
        self.mock_client = MagicMock()

        self.mock_data_handler.fetch_real_time_option_data.return_value = pd.DataFrame()
        self.mock_data_handler.clients = {
            "paper": self.mock_client,
            "prod": self.mock_client,
        }

    @patch("quant.live.trader.load_state", return_value={})
    @patch("quant.live.trader.AlpacaDataHandler")
    @patch("quant.live.trader.MarketSchedule")
    @patch("quant.live.trader.load_strategies")
    def test_e2e_flow_equity(
        self,
        mock_load_strategies,
        mock_schedule_cls,
        mock_dh_cls,
        mock_load_state,
    ):
        # Setup Mocks
        mock_dh_cls.return_value = self.mock_data_handler
        mock_schedule = mock_schedule_cls.return_value
        mock_schedule.is_market_open.return_value = True

        # Strategy (equity mode)
        strategy = DummyStrategy()
        strategy.config.MIN_OPTION_DTE = -1
        mock_load_strategies.return_value = [strategy]

        trader = UnifiedTrader()
        trader.clients = {"paper": self.mock_client}

        now = datetime.datetime.now(datetime.UTC).replace(second=0, microsecond=0)
        # Bars aligned to UTC so closed_bar_asof(end_dt) hits an index label.
        dates = pd.date_range(end=now.replace(tzinfo=None), periods=20, freq="1min")
        close_vals = list(
            range(100, 120)
        )  # 100..119, SMA(10) at end ≈ 114.5, Close[-1]=119
        df = pd.DataFrame(
            {"Open": 100, "High": 105, "Low": 99, "Close": close_vals, "Volume": 1000},
            index=dates,
        )

        self.mock_data_handler.get_historical_data.return_value = df

        # Mock order returned by Alpaca client
        mock_order = MagicMock()
        mock_order.id = "order_123"
        self.mock_client.submit_order.return_value = mock_order

        # process_strategy now handles execution inline via engine + LiveExecutor.
        trader.process_strategy(strategy, now)

        self.mock_client.submit_order.assert_called_once()

        # Verify position recorded
        self.assertEqual(len(trader.active_positions["DummyStrategy_paper"]), 1)
        pos = trader.active_positions["DummyStrategy_paper"][0]
        self.assertEqual(pos["Ticker"], "SPY")

        # Monitor: real sidecar is stubbed; emit still records signal_evaluated.
        from quant.live.monitor.emit import start_sidecar

        start_sidecar.assert_called_once()
        self.assertIsNotNone(trader._monitor_emit)
        sig_events = [
            call.args[0]
            for call in trader._monitor_emit.call_args_list
            if call.args
            and isinstance(call.args[0], dict)
            and call.args[0].get("kind") == "signal_evaluated"
        ]
        self.assertEqual(len(sig_events), 1)
        self.assertEqual(sig_events[0]["ticker"], "SPY")
        self.assertEqual(sig_events[0]["strategy_name"], "DummyStrategy_paper")
        self.assertIn(sig_events[0]["live_signal"], {"BUY_CALL", "HOLD"})
        self.assertIn("generate_signals_ns", sig_events[0])

    @patch("quant.live.trader.load_state", return_value={})
    @patch("quant.live.trader.AlpacaDataHandler")
    @patch("quant.live.trader.MarketSchedule")
    @patch("quant.live.trader.load_strategies")
    def test_manage_trade_exit(
        self,
        mock_load_strategies,
        mock_schedule_cls,
        mock_dh_cls,
        mock_load_state,
    ):
        """Verify manage_trade can trigger an EXIT signal via MANAGE_INTERVAL data."""
        mock_dh_cls.return_value = self.mock_data_handler
        mock_schedule_cls.return_value.is_market_open.return_value = True

        strategy = DummyStrategy()
        strategy.config.MIN_OPTION_DTE = -1

        mock_load_strategies.return_value = [strategy]

        trader = UnifiedTrader()
        trader.clients = {"paper": self.mock_client}

        # Seed an active position
        trader.active_positions["DummyStrategy_paper"] = [
            {
                "Strategy": "DummyStrategy_paper",
                "Ticker": "SPY",
                "OrderId": "pos_1",
                "Qty": 5,
            }
        ]

        dates = pd.date_range(end=datetime.datetime.now(), periods=20, freq="1min")
        df = pd.DataFrame(
            {"Open": 100, "High": 105, "Low": 99, "Close": 102, "Volume": 1000},
            index=dates,
        )
        self.mock_data_handler.get_historical_data.return_value = df

        now = datetime.datetime.now(datetime.UTC)
        # manage_trade returns 0 (exit at position index 0) for every call.
        # exit_trade must be mocked so the executor.exit() call succeeds.
        trader.unified_executor.exit_trade = MagicMock(return_value=True)
        with patch.object(strategy, "manage_trade", return_value=0) as mock_manage:
            trader.process_strategy(strategy, now)
            # manage_trade was called with manage-interval data
            mock_manage.assert_called()

        # EXIT was dispatched through unified_executor
        trader.unified_executor.exit_trade.assert_called()


# -----------------------------------------------------------------------------
# 3. Dual-cadence scheduling tests
# -----------------------------------------------------------------------------


@dataclass
class DualCadenceConfig(DummyConfig):
    """1h signal interval, 5m manage interval (independent closed-bar gates)."""

    INTERVAL: str = "1h"
    MANAGE_INTERVAL: str = "5m"
    DATA_DELAY: int = 0


class DualCadenceStrategy(DummyStrategy):
    def __init__(self):
        super().__init__()
        self.name = "DualCadence_paper"
        self.config = DualCadenceConfig()


class TestDualCadenceScheduling(unittest.TestCase):
    """Verify that INTERVAL and MANAGE_INTERVAL fire independently (bar-driven)."""

    _PATCHED_DELAY = 15

    def setUp(self):
        self.mock_data_handler = MagicMock()
        self.mock_client = MagicMock()
        self.mock_data_handler.fetch_real_time_option_data.return_value = pd.DataFrame()
        self.mock_data_handler.clients = {
            "paper": self.mock_client,
            "prod": self.mock_client,
        }

    def _build_trader(self, mock_dh_cls, mock_schedule_cls, mock_load_strategies):
        mock_dh_cls.return_value = self.mock_data_handler
        mock_schedule = mock_schedule_cls.return_value
        mock_schedule.is_market_open.return_value = True
        mock_schedule.market_open_time = datetime.time(13, 30)

        strategy = DualCadenceStrategy()
        mock_load_strategies.return_value = [strategy]

        trader = UnifiedTrader()
        trader.clients = {"paper": self.mock_client}

        dates = pd.date_range(end=datetime.datetime.now(), periods=20, freq="1min")
        df = pd.DataFrame(
            {"Open": 100, "High": 105, "Low": 99, "Close": 102, "Volume": 1000},
            index=dates,
        )
        self.mock_data_handler.get_historical_data.return_value = df

        return trader, strategy

    @patch("quant.live.trader.load_state", return_value={})
    @patch("quant.live.trader.AlpacaDataHandler")
    @patch("quant.live.trader.MarketSchedule")
    @patch("quant.live.trader.load_strategies")
    @patch("quant.live.trader._LIVE_DATA_DELAY_MINUTES", _PATCHED_DELAY)
    def test_manage_only_cycle_passes_skip_signals_true(
        self,
        mock_load_strategies,
        mock_schedule_cls,
        mock_dh_cls,
        mock_load_state,
    ):
        """1h signal unchanged, 5m manage advances → skip_signals=True."""
        trader, strategy = self._build_trader(
            mock_dh_cls, mock_schedule_cls, mock_load_strategies
        )
        trader.active_positions[strategy.name] = [
            {"Strategy": strategy.name, "Ticker": "SPY"}
        ]

        now = datetime.datetime(2026, 4, 27, 14, 25, tzinfo=datetime.UTC)
        # Signal: 1h bar still 13:00 (closes 15:00). Manage: 14:20 is new vs 14:10.
        trader.last_update[strategy.name] = datetime.datetime(
            2026, 4, 27, 14, 5, tzinfo=datetime.UTC
        )
        trader.last_manage_update[strategy.name] = datetime.datetime(
            2026, 4, 27, 14, 16, tzinfo=datetime.UTC
        )

        engine = trader.engines[strategy.name]
        with patch.object(engine, "process_ticker", return_value=[]) as mock_process:
            trader.process_strategy(strategy, now)

        mock_process.assert_called_once()
        kwargs = mock_process.call_args.kwargs
        self.assertTrue(
            kwargs.get("skip_signals"),
            "skip_signals must be True on manage-only cycles",
        )
        self.assertIn(strategy.name, trader.last_manage_update)
        # Signal phase did not run — last_update stays at the seeded value.
        self.assertEqual(
            trader.last_update[strategy.name],
            datetime.datetime(2026, 4, 27, 14, 5, tzinfo=datetime.UTC),
        )

    @patch("quant.live.trader.load_state", return_value={})
    @patch("quant.live.trader.AlpacaDataHandler")
    @patch("quant.live.trader.MarketSchedule")
    @patch("quant.live.trader.load_strategies")
    @patch("quant.live.trader._LIVE_DATA_DELAY_MINUTES", _PATCHED_DELAY)
    def test_signal_and_manage_coincide_skip_signals_false(
        self,
        mock_load_strategies,
        mock_schedule_cls,
        mock_dh_cls,
        mock_load_state,
    ):
        """New closed bars for both intervals → skip_signals=False."""
        trader, strategy = self._build_trader(
            mock_dh_cls, mock_schedule_cls, mock_load_strategies
        )

        # First tick of the day (no last_*): both phases fire.
        now = datetime.datetime(2026, 4, 27, 15, 0, tzinfo=datetime.UTC)

        engine = trader.engines[strategy.name]
        with patch.object(engine, "process_ticker", return_value=[]) as mock_process:
            trader.process_strategy(strategy, now)

        mock_process.assert_called_once()
        kwargs = mock_process.call_args.kwargs
        self.assertFalse(
            kwargs.get("skip_signals"),
            "skip_signals must be False when signal cycle fires",
        )
        self.assertIn(strategy.name, trader.last_update)
        self.assertIn(strategy.name, trader.last_manage_update)

    @patch("quant.live.trader.load_state", return_value={})
    @patch("quant.live.trader.AlpacaDataHandler")
    @patch("quant.live.trader.MarketSchedule")
    @patch("quant.live.trader.load_strategies")
    @patch("quant.live.trader._LIVE_DATA_DELAY_MINUTES", _PATCHED_DELAY)
    def test_no_op_when_neither_should_signal_nor_should_manage(
        self,
        mock_load_strategies,
        mock_schedule_cls,
        mock_dh_cls,
        mock_load_state,
    ):
        """Same closed bars as last run → process_ticker not invoked."""
        trader, strategy = self._build_trader(
            mock_dh_cls, mock_schedule_cls, mock_load_strategies
        )

        now = datetime.datetime(2026, 4, 27, 14, 22, tzinfo=datetime.UTC)
        trader.last_update[strategy.name] = now - datetime.timedelta(minutes=1)
        trader.last_manage_update[strategy.name] = now - datetime.timedelta(minutes=1)

        engine = trader.engines[strategy.name]
        with patch.object(engine, "process_ticker", return_value=[]) as mock_process:
            signals = trader.process_strategy(strategy, now)

        mock_process.assert_not_called()
        self.assertEqual(signals, [])

    @patch("quant.live.trader.load_state", return_value={})
    @patch("quant.live.trader.AlpacaDataHandler")
    @patch("quant.live.trader.MarketSchedule")
    @patch("quant.live.trader.load_strategies")
    @patch("quant.live.trader._LIVE_DATA_DELAY_MINUTES", _PATCHED_DELAY)
    def test_last_manage_update_dedup_skips_back_to_back_calls(
        self,
        mock_load_strategies,
        mock_schedule_cls,
        mock_dh_cls,
        mock_load_state,
    ):
        """Second call within the same closed manage bar is suppressed."""
        trader, strategy = self._build_trader(
            mock_dh_cls, mock_schedule_cls, mock_load_strategies
        )
        trader.active_positions[strategy.name] = [
            {"Strategy": strategy.name, "Ticker": "SPY"}
        ]

        now = datetime.datetime(2026, 4, 27, 14, 25, tzinfo=datetime.UTC)
        trader.last_update[strategy.name] = datetime.datetime(
            2026, 4, 27, 14, 5, tzinfo=datetime.UTC
        )
        # No last_manage → first call is manage-only

        engine = trader.engines[strategy.name]
        with patch.object(engine, "process_ticker", return_value=[]) as mock_process:
            trader.process_strategy(strategy, now)
            self.assertEqual(mock_process.call_count, 1)

            now2 = now + datetime.timedelta(seconds=30)
            trader.process_strategy(strategy, now2)
            self.assertEqual(
                mock_process.call_count, 1, "dedup must suppress the second call"
            )


if __name__ == "__main__":
    unittest.main()
