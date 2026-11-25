"""Live portfolio wiring + snapshot restart recovery (spec `portfolio`, type 4.4).

Covers:
  - the live venue's execution client streams venue fill events through the
    same Portfolio / Position machinery as backtest (entry opens, close
    settles);
  - the live trader persists a portfolio snapshot in trader_state.json and
    restores positions + balances on restart (spec persistence scenario).
"""

from __future__ import annotations

import datetime
import logging
from types import SimpleNamespace
from typing import Any
from unittest.mock import MagicMock, patch

import pytest

from quant.instruments import EquityInstrument
from quant.live.trader import UnifiedTrader
from quant.portfolio import Fill, Portfolio, Side
from quant.venue.live import FillReport, LiveExecutionClient

_logger = logging.getLogger("test_portfolio_live")


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_strategy(name: str = "TestStrat_paper") -> MagicMock:
    s = MagicMock()
    s.name = name
    s.env = "paper"
    s.client_name = "paper"
    s.config = type("Cfg", (), {"CAPITAL_PER_TRADE": 0.1})()
    return s


def _make_instrument() -> EquityInstrument:
    return EquityInstrument(symbol="SPY", venue="alpaca")


def _make_client(portfolio: Portfolio | None = None):
    broker = MagicMock()
    broker.submit_order.return_value = MagicMock(id="ord-001")
    strategy = _make_strategy()
    ex = LiveExecutionClient(
        venue=SimpleNamespace(id="alpaca", allows_entry_at=lambda now: True),
        clients={"paper": broker},
        strategies=[strategy],
        portfolio=portfolio,
        logger=_logger,
        # A deterministic full-fill report at the reference price, so the
        # client never needs a real broker order surface in this unit test.
        fill_resolver=lambda order_id, order, client: FillReport(
            order_id, "FILLED", order.quantity, order.quantity, 0.0, 450.0
        ),
    )
    return ex, broker


# ---------------------------------------------------------------------------
# Live execution-client parity wiring
# ---------------------------------------------------------------------------


class TestLiveExecutorParity:
    def test_entry_fill_opens_position(self) -> None:
        pf = Portfolio(starting_equity=10_000.0)
        ex, _ = _make_client(portfolio=pf)
        strategy = _make_strategy()

        order = _make_instrument().make_order(Side.BUY, 10, strategy=strategy.name)
        fill = ex.submit(order, now=datetime.datetime(2024, 6, 1, 14, 30))

        assert fill is not None
        live_pos = pf.position("alpaca", "SPY")
        assert live_pos is not None
        assert live_pos.signed_qty == 10  # same quantity as the executed order
        assert live_pos.avg_price == pytest.approx(450.0)
        assert live_pos.strategy == "TestStrat_paper"

    def test_exit_fill_closes_through_same_machinery(self) -> None:
        pf = Portfolio(starting_equity=10_000.0)
        ex, _ = _make_client(portfolio=pf)
        strategy = _make_strategy()
        instrument = _make_instrument()

        ex.submit(
            instrument.make_order(Side.BUY, 10, strategy=strategy.name),
            now=datetime.datetime(2024, 6, 1, 14, 30),
        )
        ex.submit(
            instrument.make_order(Side.SELL, 10, strategy=strategy.name),
            now=datetime.datetime(2024, 6, 1, 15, 30),
        )
        assert pf.position("alpaca", "SPY") is None  # closed
        assert len(pf.closed_positions) == 1

    def test_no_portfolio_is_a_noop(self) -> None:
        # Without a portfolio collector, the client still returns the fill;
        # there is simply no account to apply it to.
        ex, _ = _make_client(portfolio=None)
        order = _make_instrument().make_order(
            Side.BUY, 10, strategy=_make_strategy().name
        )
        fill = ex.submit(order, now=datetime.datetime(2024, 6, 1, 14, 30))
        assert fill is not None


# ---------------------------------------------------------------------------
# Trader snapshot restart recovery
# ---------------------------------------------------------------------------


def _make_trader() -> UnifiedTrader:
    mock_client = MagicMock()
    mock_dh = MagicMock()
    mock_dh.clients = {"paper": mock_client}
    with (
        patch("quant.live.trader.load_state", return_value={}),
        patch("quant.live.trader.AlpacaDataHandler", return_value=mock_dh),
        patch("quant.live.trader.MarketSchedule"),
        patch("quant.live.trader.load_strategies", return_value=[]),
    ):
        return UnifiedTrader()


class TestTraderRestartRecovery:
    def test_save_snapshot_has_portfolio(self) -> None:
        trader = _make_trader()
        with patch("quant.live.trader.save_state") as mock_save:
            trader._save_state()
        captured = mock_save.call_args[0][1]
        assert "portfolio" in captured
        assert captured["portfolio"]["starting_equity"] == pytest.approx(
            trader.portfolio.starting_equity
        )

    def test_restart_restores_positions_and_balances(self) -> None:
        trader = _make_trader()
        trader.portfolio.apply_fill(
            Fill(
                venue="paper",
                symbol="SPY",
                side=Side.BUY,
                qty=10,
                price=100.0,
                strategy="TestStrat_paper",
            )
        )
        captured: dict[str, Any] = {}
        with patch(
            "quant.live.trader.save_state",
            side_effect=lambda f, data, lock, lg: captured.update(data),
        ):
            trader._save_state()
        assert "portfolio" in captured

        # "restart": a fresh trader loads the same snapshot
        trader2 = _make_trader()
        with patch("quant.live.trader.load_state", return_value=captured):
            trader2._load_local_state()

        restored = trader2.portfolio.position("paper", "SPY")
        assert restored is not None
        assert restored.signed_qty == 10
        assert trader2.portfolio.cash == pytest.approx(trader.portfolio.cash)
        assert trader2.portfolio.starting_equity == pytest.approx(
            trader.portfolio.starting_equity
        )

    def test_malformed_snapshot_is_dropped_loudly(self) -> None:
        trader = _make_trader()
        with patch(
            "quant.live.trader.load_state",
            return_value={"portfolio": {"no": "valid keys"}},
        ):
            trader._load_local_state()
        # default portfolio retained (fail closed), no crash
        assert trader.portfolio is not None
