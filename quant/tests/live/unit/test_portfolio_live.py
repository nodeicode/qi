"""Live portfolio wiring + snapshot restart recovery (spec `portfolio`, type 4.4).

Covers:
  - the live executor streams venue fill events through the same Portfolio /
    Position machinery as backtest (entry opens, close settles);
  - the live trader persists a portfolio snapshot in trader_state.json and
    restores positions + balances on restart (spec persistence scenario).
"""

from __future__ import annotations

import logging
from typing import Any
from unittest.mock import MagicMock, patch

import pytest

from quant.live.executors.unified_executor import UnifiedExecutor
from quant.live.trader import UnifiedTrader
from quant.portfolio import Fill, Portfolio, Side

_logger = logging.getLogger("test_portfolio_live")


# ---------------------------------------------------------------------------
# Helpers (mirror test_unified_executor patterns)
# ---------------------------------------------------------------------------


def _size_for_signal(signal, **kw):
    equity = float(kw.get("equity", 0.0))
    capital = equity * 0.1 * float(kw.get("pos_size", 1.0))
    entry_price = float(kw.get("entry_price", 0.0))
    shares = int(capital / entry_price) if entry_price > 0 else 0
    return {"shares": shares, "effective_capital": capital}


def _make_engine() -> MagicMock:
    eng = MagicMock()
    eng.route.return_value = "EQUITY"
    eng.direction.return_value = 1
    eng.size_for_signal.side_effect = _size_for_signal
    eng.build_position.side_effect = lambda *a, **kw: {
        "AssetType": kw.get("AssetType", "EQUITY"),
        "Ticker": kw.get("Ticker", "SPY"),
        "OrderId": kw.get("OrderId", "ord"),
        "Qty": kw.get("Qty", 10),
        "Signal": a[3] if len(a) > 3 else kw.get("Signal", "BUY"),
        "EntryPrice": a[2] if len(a) > 2 else kw.get("EntryPrice", 0.0),
        "StopLoss": kw.get("StopLoss"),
        "TakeProfit": kw.get("TakeProfit"),
        **kw,
    }
    return eng


def _make_strategy(name: str = "TestStrat_paper") -> MagicMock:
    s = MagicMock()
    s.name = name
    s.env = "paper"
    s.client_name = "paper"
    s.config.CAPITAL_PER_TRADE = 0.1
    return s


def _make_executor(equity: float = 50000.0, portfolio: Portfolio | None = None):
    mock_client = MagicMock()
    dh = MagicMock()
    dh.clients = {"paper": mock_client}
    ex = UnifiedExecutor(
        clients={"paper": mock_client},
        data_handler=dh,
        logger=_logger,
        portfolio=portfolio,
    )
    ex.equity_mgr = MagicMock()
    ex.equity_mgr.get.return_value = equity
    return ex, mock_client, dh


# ---------------------------------------------------------------------------
# Live executor parity wiring
# ---------------------------------------------------------------------------


class TestLiveExecutorParity:
    def test_entry_fill_opens_position(self) -> None:
        pf = Portfolio(starting_equity=10_000.0)
        ex, client, _ = _make_executor(portfolio=pf)
        client.submit_order.return_value = MagicMock(id="ord-001")
        strategy = _make_strategy()

        pos = ex._enter_linear_trade(
            strategy,
            "SPY",
            {"Signal": "BUY", "Close": 450.0},
            "EQUITY",
            engine=_make_engine(),
        )

        assert pos is not None
        live_pos = pf.position("paper", "SPY")
        assert live_pos is not None
        assert live_pos.signed_qty > 0  # same sizing as the executed order
        assert live_pos.avg_price == pytest.approx(450.0)
        assert live_pos.strategy == "TestStrat_paper"

    def test_exit_fill_closes_through_same_machinery(self) -> None:
        pf = Portfolio(starting_equity=10_000.0)
        ex, client, _ = _make_executor(portfolio=pf)
        client.submit_order.return_value = MagicMock(id="ord-001")
        strategy = _make_strategy()

        pos = ex._enter_linear_trade(
            strategy,
            "SPY",
            {"Signal": "BUY", "Close": 450.0},
            "EQUITY",
            engine=_make_engine(),
        )
        qty = pos["Qty"]
        assert ex.exit_trade(pos, qty) is True
        assert pf.position("paper", "SPY") is None  # closed
        assert len(pf.closed_positions) == 1

    def test_no_portfolio_is_a_noop(self) -> None:
        # Backwards compatible: without a portfolio, fills are simply not emitted.
        ex, client, _ = _make_executor(portfolio=None)
        client.submit_order.return_value = MagicMock(id="ord-001")
        pos = ex._enter_linear_trade(
            _make_strategy(),
            "SPY",
            {"Signal": "BUY", "Close": 450.0},
            "EQUITY",
            engine=_make_engine(),
        )
        assert pos is not None


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
        assert trader.portfolio.position("paper", "SPY") is None
