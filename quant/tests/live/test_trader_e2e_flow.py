import datetime
import os
import sys
import unittest
from dataclasses import dataclass, field
from unittest.mock import MagicMock, patch

import pandas as pd

from quant.data import Datatype, Subscription
from quant.instruments import EquityInstrument
from quant.live.protocols import LiveStrategyProtocol
from quant.live.trader import UnifiedTrader
from quant.portfolio import Side
from quant.strategies.base import ExitIntent, Strategy
from quant.strategies.sizing import size_equity_lot
from quant.utils.strategy_config import BaseStrategyConfig
from tests.live.conftest import always_open_venue

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

_SPY = EquityInstrument(symbol="SPY", venue="alpaca")


@dataclass
class DummyConfig(BaseStrategyConfig):
    """Declarative equity config for the live e2e flow (risk fields + declarations)."""

    PRE_LOAD_DAYS: int = 1
    INITIAL_CAPITAL: float = 100000.0
    MAX_CONCURRENT_POSITIONS: int = 5
    STOP_LOSS_PERCENT: float = 0.05
    TAKE_PROFIT_PERCENT: float = 0.10
    USE_TRAIL_LOSS: bool = False
    MAX_HOLDING_MINUTES: int = 60
    MIN_SIGNAL_CONFIDENCE: float = 0.5
    BASE_SLIPPAGE_FACTOR: float = 0.01
    DATA_DELAY: int = 0

    instruments: list = field(
        default_factory=lambda: [EquityInstrument(symbol="SPY", venue="alpaca")]
    )
    subscriptions: list = field(
        default_factory=lambda: [
            Subscription(
                instrument=EquityInstrument(symbol="SPY", venue="alpaca"),
                datatype=Datatype.BARS,
                cadence="1m",
            )
        ]
    )
    venue: str = "alpaca"


class DummyStrategy(Strategy, LiveStrategyProtocol):
    """Declarative live strategy: BUY when flat; exit when ``exit_on`` is set.

    ``decide`` is the single author body — it sizes entries from the instrument
    lot + the injected portfolio and evaluates the portfolio's open positions
    for a manage-pass exit when ``exit_on`` is toggled (the test scaffold).
    """

    name = "DummyStrategy_paper"
    venue = "alpaca"

    def __init__(self, name: str = "DummyStrategy_paper") -> None:
        self.name = name
        self.config = DummyConfig()
        self.instruments = list(self.config.instruments)
        self.subscriptions = list(self.config.subscriptions)
        self.env = "paper"
        self.client_name = "paper"
        # External manage-pass toggle (test scaffold): when True an open
        # position is exited on the next due tick.
        self.exit_on = False
        super().__init__()

    def decide(self, events, portfolio) -> list:
        bars = events.bars("SPY")
        if bars is None or bars.empty:
            return []
        close = float(bars["Close"].iloc[-1])
        out: list = []

        # Manage pass — exit an open position when the toggle is set.
        for pos in portfolio.open_positions(self.name):
            if self.exit_on:
                out.append(ExitIntent(self.name, pos, reason="test exit"))

        # Signal pass — BUY when flat, sized on the instrument lot + buying power.
        if not self.exit_on and not portfolio.open_positions(self.name):
            qty = size_equity_lot(
                portfolio,
                close,
                instrument=self.instruments[0],
                buying_power_fraction=0.5,
            )
            if qty > 0:
                out.append(
                    self.instruments[0].make_order(Side.BUY, qty, strategy=self.name)
                )
        return out


class TestUnifiedTraderE2E(unittest.TestCase):
    def setUp(self):
        self.mock_data_handler = MagicMock()
        self.mock_client = MagicMock()
        self.mock_client.get_account.return_value.equity = "100000.0"
        self.mock_data_handler.clients = {
            "paper": self.mock_client,
            "prod": self.mock_client,
        }

    def _stub_bars(self, now):
        """Return 1m bars aligned to *now*, served on both handler surfaces."""
        dates = pd.date_range(end=now.replace(tzinfo=None), periods=20, freq="1min")
        close_vals = list(range(100, 120))  # 100..119, Close[-1]=119
        df = pd.DataFrame(
            {"Open": 100, "High": 105, "Low": 99, "Close": close_vals, "Volume": 1000},
            index=dates,
        )
        self.mock_data_handler.get_historical_data.return_value = df
        self.mock_data_handler.get_data.return_value = df

    @patch("quant.live.trader.load_state", return_value={})
    @patch(
        "quant.live.trader.venue_by_name",
        side_effect=lambda *a, **k: always_open_venue(),
    )
    @patch("quant.live.trader.MarketSchedule")
    @patch("quant.live.trader.load_strategies")
    @patch("quant.live.trader.AlpacaDataHandler")
    def test_e2e_flow_equity(
        self,
        mock_dh_cls,
        mock_load_strategies,
        mock_schedule_cls,
        mock_venue_cls,
        mock_load_state,
    ):
        """One declarative equity strategy runs through the live venue: the author
        body's order reaches the broker, feeds the Portfolio, and the trader's
        live position book reflects it."""
        mock_dh_cls.return_value = self.mock_data_handler
        strategy = DummyStrategy()
        mock_load_strategies.return_value = [strategy]

        trader = UnifiedTrader()
        trader.clients = {"paper": self.mock_client}

        now = datetime.datetime.now(datetime.UTC).replace(second=0, microsecond=0)
        self._stub_bars(now)

        # The broker reports the fill price so the venue can price the order.
        self.mock_client.get_order_by_id.return_value = {
            "status": "FILLED",
            "filled_qty": "100",
            "filled_avg_price": "100.0",
        }
        self.mock_client.submit_order.return_value = MagicMock(id="order_123")

        trader.process_strategy(strategy, now)

        self.mock_client.submit_order.assert_called_once()

        # The fill fed the Portfolio and the trader mirrors it in the live book.
        self.assertEqual(len(trader.active_positions["DummyStrategy_paper"]), 1)
        pos = trader.active_positions["DummyStrategy_paper"][0]
        self.assertEqual(pos["Ticker"], "SPY")
        self.assertIsNotNone(trader.portfolio.open_positions("DummyStrategy_paper"))

    @patch("quant.live.trader.load_state", return_value={})
    @patch(
        "quant.live.trader.venue_by_name",
        side_effect=lambda *a, **k: always_open_venue(),
    )
    @patch("quant.live.trader.MarketSchedule")
    @patch("quant.live.trader.load_strategies")
    @patch("quant.live.trader.AlpacaDataHandler")
    def test_manage_exit_flows_through_decide(
        self,
        mock_dh_cls,
        mock_load_strategies,
        mock_schedule_cls,
        mock_venue_cls,
        mock_load_state,
    ):
        """A manage-pass exit is decided in the author body, routed to the venue,
        and closes the position (Portfolio + live book)."""
        mock_dh_cls.return_value = self.mock_data_handler
        strategy = DummyStrategy()
        mock_load_strategies.return_value = [strategy]

        trader = UnifiedTrader()
        trader.clients = {"paper": self.mock_client}

        now = datetime.datetime.now(datetime.UTC).replace(second=0, microsecond=0)
        self._stub_bars(now)
        self.mock_client.get_order_by_id.return_value = {
            "status": "FILLED",
            "filled_qty": "10",
            "filled_avg_price": "100.0",
        }
        self.mock_client.submit_order.return_value = MagicMock(id="exit_1")

        # Entry first — BUY on the flat portfolio.
        trader.process_strategy(strategy, now)
        self.assertEqual(len(trader.portfolio.open_positions("DummyStrategy_paper")), 1)

        # Toggle the manage exit and advance to the next due bar minute.
        strategy.exit_on = True
        now2 = now + datetime.timedelta(minutes=1)
        self._stub_bars(now2)
        self.mock_client.submit_order.reset_mock()
        self.mock_client.get_order_by_id.return_value = {
            "status": "FILLED",
            "filled_qty": "10",
            "filled_avg_price": "95.0",
        }
        self.mock_client.submit_order.return_value = MagicMock(id="exit_2")

        trader.process_strategy(strategy, now2)

        # The exit order reached the broker and the position was closed.
        self.mock_client.submit_order.assert_called_once()
        self.assertEqual(trader.active_positions["DummyStrategy_paper"], [])
        self.assertEqual(trader.portfolio.open_positions("DummyStrategy_paper"), [])


# -----------------------------------------------------------------------------
# Per-subscription cadence scheduling (engine-driven)
# -----------------------------------------------------------------------------


class TestCadenceScheduling(unittest.TestCase):
    """Verify the reshaped path schedules by per-subscription cadence + dedup."""

    def setUp(self):
        self.mock_data_handler = MagicMock()
        self.mock_client = MagicMock()
        self.mock_data_handler.clients = {"paper": self.mock_client}
        self.mock_data_handler.get_account.return_value.equity = "100000.0"

    def _build_trader(self, mock_dh_cls, strategy):
        mock_dh_cls.return_value = self.mock_data_handler
        with (
            patch("quant.live.trader.load_state", return_value={}),
            patch(
                "quant.live.trader.venue_by_name",
                side_effect=lambda *a, **k: always_open_venue(),
            ),
            patch("quant.live.trader.MarketSchedule"),
            patch("quant.live.trader.load_strategies", return_value=[strategy]),
        ):
            trader = UnifiedTrader()
        trader.clients = {"paper": self.mock_client}
        now = datetime.datetime.now(datetime.UTC).replace(second=0, microsecond=0)
        dates = pd.date_range(end=now.replace(tzinfo=None), periods=20, freq="1min")
        df = pd.DataFrame(
            {"Open": 100, "High": 105, "Low": 99, "Close": 102, "Volume": 1000},
            index=dates,
        )
        self.mock_data_handler.get_historical_data.return_value = df
        self.mock_data_handler.get_data.return_value = df
        return trader, now

    @patch("quant.live.trader.AlpacaDataHandler")
    def test_same_closed_bar_dedup_skips_second_call(self, mock_dh_cls) -> None:
        """Two ticks within the same closed 1m bar only run the body once."""
        strategy = DummyStrategy()
        trader, now = self._build_trader(mock_dh_cls, strategy)
        with patch.object(strategy, "decide", wraps=strategy.decide) as spy:
            trader.process_strategy(strategy, now)
            trader.process_strategy(strategy, now + datetime.timedelta(seconds=30))
        assert spy.call_count == 1  # no-op on the repeated bar

    @patch("quant.live.trader.AlpacaDataHandler")
    def test_new_closed_bar_runs_body_again(self, mock_dh_cls) -> None:
        """A tick on the next closed bar runs the author body again."""
        strategy = DummyStrategy()
        trader, now = self._build_trader(mock_dh_cls, strategy)
        self.mock_client.submit_order.return_value = MagicMock(id="o1")
        self.mock_client.get_order_by_id.return_value = {
            "status": "FILLED",
            "filled_qty": "10",
            "filled_avg_price": "100.0",
        }
        with patch.object(strategy, "decide", wraps=strategy.decide) as spy:
            trader.process_strategy(strategy, now)
            trader.process_strategy(strategy, now + datetime.timedelta(minutes=1))
        assert spy.call_count == 2

    @patch("quant.live.trader.AlpacaDataHandler")
    def test_manage_exit_fires_on_due_bar(self, mock_dh_cls) -> None:
        """A manage-pass exit is decided once the subscription cadence advances,
        even though the entry gate no longer produces an order (position open)."""
        strategy = DummyStrategy()
        trader, now = self._build_trader(mock_dh_cls, strategy)
        self.mock_client.submit_order.return_value = MagicMock(id="o1")
        self.mock_client.get_order_by_id.return_value = {
            "status": "FILLED",
            "filled_qty": "10",
            "filled_avg_price": "100.0",
        }
        trader.process_strategy(strategy, now)  # entry
        assert len(trader.portfolio.open_positions("DummyStrategy_paper")) == 1
        self.mock_client.submit_order.reset_mock()

        strategy.exit_on = True
        self.mock_client.get_order_by_id.return_value = {
            "status": "FILLED",
            "filled_qty": "10",
            "filled_avg_price": "95.0",
        }
        trader.process_strategy(strategy, now + datetime.timedelta(minutes=1))
        self.mock_client.submit_order.assert_called_once()
        assert trader.portfolio.open_positions("DummyStrategy_paper") == []


if __name__ == "__main__":
    unittest.main()
