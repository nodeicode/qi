"""
Tests for python/executors/unified_executor.py

All Alpaca API calls are mocked — no network calls.

Covers:
  - __init__: dict clients stored; single non-dict client wrapped as {'default': client}
  - set_client: updates self.client and equity_mgr.set_active
  - enter_trade: EQUITY BUY / SELL → delegates to _enter_linear_trade
  - enter_trade: IC / OPTION / SPREAD signals → skip warning, returns None
  - enter_trade: exception in routing → returns None (never raises)
  - exit_trade: no symbol in position dict → returns False
  - exit_trade: EQUITY close → submits order, credits equity, returns True
  - exit_trade: broker exception → returns False
  - _enter_linear_trade: entry_price = 0 → returns None
  - _enter_linear_trade: qty < 1 → returns None
  - _enter_linear_trade: successful BUY without engine → position dict returned
  - _enter_linear_trade: SELL signal → position dict with Signal='SELL'
  - _enter_linear_trade: equity debited after successful order
"""

from __future__ import annotations

# Alpaca is stubbed per-test by the autouse fixture in quant/tests/live/conftest.py.
# ---------------------------------------------------------------------------
# Imports under test
# ---------------------------------------------------------------------------
import logging
import sys
from typing import Any, cast
from unittest.mock import MagicMock, patch

import quant.live.executors.unified_executor as _ue_module
from quant.live.executors.unified_executor import UnifiedExecutor
from quant.live.protocols import LivePositionDict

_logger = logging.getLogger("test_unified_executor")


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_executor(equity: float = 50000.0) -> UnifiedExecutor:
    """Return a UnifiedExecutor with all external deps mocked for isolation."""
    mock_client = MagicMock()
    executor = UnifiedExecutor(
        clients={"paper": mock_client},
        data_handler=MagicMock(),
        logger=_logger,
    )
    # Swap out the equity manager with a plain mock
    executor.equity_mgr = MagicMock()
    executor.equity_mgr.get.return_value = equity
    return executor


def _size_for_signal(signal, **kw):
    """Mirror of the core sizing dispatch for test strategies/engines.

    Producers call ``size_for_signal`` (the core module dispatch). Equity path
    returns ``shares``; option/IC path returns ``n_contracts``, distinguished
    by the keyword shape.
    """
    equity = float(kw.get("equity", 0.0))
    capital = equity * 0.1 * float(kw.get("pos_size", 1.0))
    if "premium_per_contract" in kw:
        premium = float(kw["premium_per_contract"])
        n = max(1, int(capital / premium)) if premium > 0 else 0
        return {"n_contracts": n, "capital": capital}
    price = float(kw.get("entry_price", 0.0))
    shares = max(0, int(capital // price)) if price > 0 else 0
    return {"shares": shares, "capital": capital}


# Producers call quant.core.sizing.size_for_signal directly (the engine shim is
# gone). Rebind the executor module's symbol to the deterministic fake so these
# tests exercise the real producer path with stable sizing.
_ue_module.size_for_signal = _size_for_signal


def _make_engine(route: str = "EQUITY") -> MagicMock:
    """Minimal TradingEngine mock — required for enter_trade routing/sizing."""
    eng = MagicMock()
    eng.route.side_effect = lambda signal: {
        "SELL_IC": "IC",
        "BUY_IC": "IC",
        "BUY_CALL": "OPTION",
        "BUY_PUT": "OPTION",
        "BUY_CALL_SPREAD": "SPREAD",
        "BUY": "EQUITY",
        "SELL": "EQUITY",
    }.get(str(signal).upper(), route)
    eng.direction.return_value = 1

    eng.size_for_signal.side_effect = _size_for_signal
    eng.select_strike.side_effect = lambda price, opt_type: round(price)
    eng.build_position.side_effect = lambda *a, **kw: {
        "AssetType": kw.get("AssetType", "EQUITY"),
        "Ticker": kw.get("Ticker", "SPY"),
        "OrderId": kw.get("OrderId", "ord"),
        "Qty": kw.get("Qty", 10),
        "Signal": a[3] if len(a) > 3 else kw.get("Signal", "BUY"),
        "StopLoss": kw.get("StopLoss"),
        "TakeProfit": kw.get("TakeProfit"),
        **kw,
    }
    return eng


def _make_strategy(
    name: str = "TestStrat_paper",
    env: str = "paper",
    client_name: str = "paper",
) -> MagicMock:
    s = MagicMock()
    s.name = name
    s.env = env
    s.client_name = client_name
    s.config.CAPITAL_PER_TRADE = 0.1
    s.config.MIN_OPTION_DTE = 7
    s.config.ASSET_CLASS = "EQUITY"
    return s


def _client_mock(ex: UnifiedExecutor) -> MagicMock:
    """Cast ex.client to MagicMock for type-safe mock setup in tests."""
    return cast("MagicMock", ex.client)


def _make_order(order_id: str = "order-123") -> MagicMock:
    order = MagicMock()
    order.id = order_id
    return order


# ===========================================================================
# __init__
# ===========================================================================


class TestUnifiedExecutorInit:
    def test_dict_clients_stored(self) -> None:
        mock_client = MagicMock()
        ex = UnifiedExecutor(
            clients={"paper": mock_client},
            data_handler=MagicMock(),
            logger=_logger,
        )

        assert ex.client is mock_client

    def test_equity_mgr_created(self) -> None:
        mock_client = MagicMock()
        mock_account = MagicMock()
        mock_account.equity = "10000.0"
        mock_client.get_account.return_value = mock_account
        ex = UnifiedExecutor(
            clients={"paper": mock_client},
            data_handler=MagicMock(),
            logger=_logger,
        )

        assert ex.equity_mgr is not None


# ===========================================================================
# set_client
# ===========================================================================


class TestSetClient:
    def test_set_client_updates_all_references(self) -> None:
        ex = _make_executor()
        new_client = MagicMock()

        ex.set_client("prod", new_client)

        assert ex.client is new_client
        ex.equity_mgr.set_active.assert_called_once_with("prod")

    def test_set_client_can_be_called_multiple_times(self) -> None:
        ex = _make_executor()

        ex.set_client("prod", MagicMock())
        ex.set_client("paper", MagicMock())

        assert ex.equity_mgr.set_active.call_count == 2

    def test_set_client_does_not_update_clients_when_set_active_fails(self) -> None:
        ex = _make_executor()
        original_client = ex.client
        ex.equity_mgr.set_active.return_value = False

        result = ex.set_client("unknown", MagicMock())

        assert result is False
        assert ex.client is original_client


# ===========================================================================
# enter_trade — routing
# ===========================================================================


class TestEnterTradeRouting:
    def test_ic_signal_skipped_with_warning(self) -> None:
        ex = _make_executor()
        strategy = _make_strategy()

        result = ex.enter_trade(
            strategy,
            "SPY",
            {"Signal": "SELL_IC", "Close": 450.0},
            engine=_make_engine(),
        )

        assert result is None
        ex.client.submit_order.assert_not_called()

    def test_option_signal_skipped_with_warning(self) -> None:
        ex = _make_executor()
        strategy = _make_strategy()

        with patch.object(ex, "_enter_linear_trade") as mock_lin:
            result = ex.enter_trade(
                strategy,
                "SPY",
                {"Signal": "BUY_CALL", "Close": 450.0},
                engine=_make_engine(),
            )

        assert result is None
        mock_lin.assert_not_called()

    def test_spread_signal_returns_none_with_no_order(self) -> None:
        ex = _make_executor()
        strategy = _make_strategy()

        result = ex.enter_trade(
            strategy,
            "SPY",
            {"Signal": "BUY_CALL_SPREAD", "Close": 450.0},
            engine=_make_engine(),
        )

        assert result is None
        ex.client.submit_order.assert_not_called()

    def test_equity_buy_signal_delegates_to_linear_trade(self) -> None:
        ex = _make_executor()
        strategy = _make_strategy()

        with patch.object(
            ex, "_enter_linear_trade", return_value=MagicMock()
        ) as mock_lin:
            ex.enter_trade(
                strategy,
                "SPY",
                {"Signal": "BUY", "Close": 450.0},
                engine=_make_engine(),
            )

        mock_lin.assert_called_once()

    def test_equity_sell_signal_delegates_to_linear_trade(self) -> None:
        ex = _make_executor()
        strategy = _make_strategy()
        strategy.config.MIN_OPTION_DTE = -1  # force EQUITY routing

        with patch.object(
            ex, "_enter_linear_trade", return_value=MagicMock()
        ) as mock_lin:
            ex.enter_trade(
                strategy,
                "SPY",
                {"Signal": "SELL", "Close": 450.0},
                engine=_make_engine(),
            )

        mock_lin.assert_called_once()

    def test_exception_in_routing_returns_none(self) -> None:
        ex = _make_executor()
        strategy = _make_strategy()

        with patch.object(
            ex, "_enter_linear_trade", side_effect=Exception("broker crash")
        ):
            result = ex.enter_trade(
                strategy,
                "SPY",
                {"Signal": "BUY", "Close": 450.0},
                engine=_make_engine(),
            )

        assert result is None


# ===========================================================================
# exit_trade
# ===========================================================================


class TestExitTrade:
    def test_missing_symbol_returns_false(self) -> None:
        ex = _make_executor()
        pos = cast(
            "LivePositionDict", {"AssetType": "EQUITY"}
        )  # no Ticker or OccSymbol

        result = ex.exit_trade(pos, qty=1)

        assert result is False
        ex.client.submit_order.assert_not_called()

    def test_equity_exit_submits_order_returns_true(self) -> None:
        ex = _make_executor()
        _client_mock(ex).submit_order.return_value = _make_order()
        pos = cast(
            "LivePositionDict",
            {"AssetType": "EQUITY", "Ticker": "SPY", "EntryPrice": 100.0},
        )

        result = ex.exit_trade(pos, qty=10)

        assert result is True
        ex.client.submit_order.assert_called_once()

    def test_equity_exit_credits_equity(self) -> None:
        ex = _make_executor()
        _client_mock(ex).submit_order.return_value = _make_order()
        pos = cast(
            "LivePositionDict",
            {"AssetType": "EQUITY", "Ticker": "SPY", "EntryPrice": 100.0},
        )

        ex.exit_trade(pos, qty=10)

        ex.equity_mgr.credit.assert_called_once_with(10 * 100.0)

    def test_exit_uses_occ_symbol_when_present(self) -> None:
        import quant.live.executors.unified_executor as _ue_mod

        ex = _make_executor()
        _client_mock(ex).submit_order.return_value = _make_order()
        occ = "SPY250321C00450000"
        pos = cast(
            "LivePositionDict",
            {
                "AssetType": "EQUITY",
                "OccSymbol": occ,
                "Ticker": "SPY",
                "EntryPrice": 2.0,
            },
        )

        with patch.object(_ue_mod, "MarketOrderRequest") as mock_mor:
            ex.exit_trade(pos, qty=1)

        assert mock_mor.call_args.kwargs["symbol"] == occ

    def test_exit_falls_back_to_ticker_when_no_occ_symbol(self) -> None:
        import quant.live.executors.unified_executor as _ue_mod

        ex = _make_executor()
        _client_mock(ex).submit_order.return_value = _make_order()
        pos = cast(
            "LivePositionDict",
            {"AssetType": "EQUITY", "Ticker": "SPY", "EntryPrice": 450.0},
        )

        with patch.object(_ue_mod, "MarketOrderRequest") as mock_mor:
            ex.exit_trade(pos, qty=5)

        assert mock_mor.call_args.kwargs["symbol"] == "SPY"

    def test_broker_exception_returns_false(self) -> None:
        ex = _make_executor()
        _client_mock(ex).submit_order.side_effect = Exception("broker error")
        pos = cast(
            "LivePositionDict",
            {"AssetType": "EQUITY", "Ticker": "SPY", "EntryPrice": 100.0},
        )

        result = ex.exit_trade(pos, qty=5)

        assert result is False

    def test_zero_entry_price_no_credit_called(self) -> None:
        ex = _make_executor()
        _client_mock(ex).submit_order.return_value = _make_order()
        pos = cast(
            "LivePositionDict",
            {"AssetType": "EQUITY", "Ticker": "SPY", "EntryPrice": 0.0},
        )

        ex.exit_trade(pos, qty=5)

        ex.equity_mgr.credit.assert_not_called()


# ===========================================================================
# _enter_linear_trade
# ===========================================================================


class TestEnterLinearTrade:
    def test_zero_entry_price_returns_none(self) -> None:
        ex = _make_executor(equity=50000.0)
        strategy = _make_strategy()

        result = ex._enter_linear_trade(
            strategy,
            "SPY",
            {"Signal": "BUY", "Close": 0},
            "EQUITY",
            engine=_make_engine(),
        )

        assert result is None
        ex.client.submit_order.assert_not_called()

    def test_negative_entry_price_returns_none(self) -> None:
        ex = _make_executor(equity=50000.0)
        strategy = _make_strategy()

        result = ex._enter_linear_trade(
            strategy,
            "SPY",
            {"Signal": "BUY", "Close": -10.0},
            "EQUITY",
            engine=_make_engine(),
        )

        assert result is None

    def test_qty_less_than_one_returns_none(self) -> None:
        ex = _make_executor(equity=1.0)  # tiny equity → qty < 1 at high price
        strategy = _make_strategy()

        result = ex._enter_linear_trade(
            strategy,
            "SPY",
            {"Signal": "BUY", "Close": 50000.0},
            "EQUITY",
            engine=_make_engine(),
        )

        assert result is None

    def test_successful_buy_returns_position_dict(self) -> None:
        ex = _make_executor(equity=50000.0)
        strategy = _make_strategy()
        _client_mock(ex).submit_order.return_value = _make_order("ord-001")

        pos = ex._enter_linear_trade(
            strategy,
            "SPY",
            {"Signal": "BUY", "Close": 450.0},
            "EQUITY",
            engine=_make_engine(),
        )

        assert pos is not None
        assert pos["Ticker"] == "SPY"
        assert pos["Signal"] == "BUY"
        assert pos["Strategy"] == "TestStrat_paper"
        assert pos["Env"] == "paper"
        assert pos["AssetType"] == "EQUITY"

    def test_position_contains_order_id(self) -> None:
        ex = _make_executor(equity=50000.0)
        strategy = _make_strategy()
        _client_mock(ex).submit_order.return_value = _make_order("ord-xyz")

        pos = ex._enter_linear_trade(
            strategy,
            "SPY",
            {"Signal": "BUY", "Close": 450.0},
            "EQUITY",
            engine=_make_engine(),
        )

        assert pos is not None
        assert pos.get("OrderId") == "ord-xyz"

    def test_equity_debited_after_successful_order(self) -> None:
        ex = _make_executor(equity=50000.0)
        strategy = _make_strategy()
        _client_mock(ex).submit_order.return_value = _make_order()

        ex._enter_linear_trade(
            strategy,
            "SPY",
            {"Signal": "BUY", "Close": 450.0},
            "EQUITY",
            engine=_make_engine(),
        )

        ex.equity_mgr.debit.assert_called_once()

    def test_sell_signal_captured_in_position_dict(self) -> None:
        ex = _make_executor(equity=50000.0)
        strategy = _make_strategy()
        _client_mock(ex).submit_order.return_value = _make_order()

        pos = ex._enter_linear_trade(
            strategy,
            "SPY",
            {"Signal": "SELL", "Close": 450.0},
            "EQUITY",
            engine=_make_engine(),
        )

        assert pos is not None
        assert pos["Signal"] == "SELL"

    def test_stop_loss_and_take_profit_from_signal_row(self) -> None:
        ex = _make_executor(equity=50000.0)
        strategy = _make_strategy()
        _client_mock(ex).submit_order.return_value = _make_order()
        signal_row = {
            "Signal": "BUY",
            "Close": 450.0,
            "StopLoss": 440.0,
            "TakeProfit": 460.0,
        }

        pos = ex._enter_linear_trade(
            strategy, "SPY", signal_row, "EQUITY", engine=_make_engine()
        )

        assert pos is not None
        assert pos.get("StopLoss") == 440.0
        assert pos.get("TakeProfit") == 460.0

    def test_qty_calculated_from_equity_and_capital_pct(self) -> None:
        """equity=50000, CAPITAL_PER_TRADE=0.1, close=450 → qty = int(5000//450) = 11."""
        ex = _make_executor(equity=50000.0)
        strategy = _make_strategy()
        strategy.config.CAPITAL_PER_TRADE = 0.1
        _client_mock(ex).submit_order.return_value = _make_order()

        pos = ex._enter_linear_trade(
            strategy,
            "SPY",
            {"Signal": "BUY", "Close": 450.0, "PositionSize": 1.0},
            "EQUITY",
            engine=_make_engine(),
        )

        assert pos is not None
        assert pos.get("Qty") == 11


# ===========================================================================
# Concurrency — thread-local client/equity isolation (regression)
# ===========================================================================
# Proves that client/equity resolution is per-thread: when several threads
# share one UnifiedExecutor and each arms ``set_client`` before trading, no
# thread can observe or use another thread's broker account or equity.  The
# tests use the real components (no mock swaps) so the thread-local behavior
# is exercised end-to-end.
import threading


class TestConcurrentClientIsolation:
    def _make_real_executor(self) -> tuple[UnifiedExecutor, dict[str, MagicMock]]:
        """UnifiedExecutor with real MultiClientEquityManager."""
        paper_account = MagicMock()
        paper_account.equity = "10000.0"
        prod_account = MagicMock()
        prod_account.equity = "20000.0"
        paper = MagicMock()
        paper.get_account.return_value = paper_account
        prod = MagicMock()
        prod.get_account.return_value = prod_account
        ex = UnifiedExecutor(
            clients={"paper": paper, "prod": prod},
            data_handler=MagicMock(),
            logger=_logger,
        )
        return ex, {"paper": paper, "prod": prod}

    def test_set_client_binding_is_thread_local(self) -> None:
        """Interleaved set_client across threads never crosses accounts."""
        ex, clients = self._make_real_executor()
        barrier = threading.Barrier(3)
        results: dict[str, Any] = {}

        def worker(name: str) -> None:
            assert ex.set_client(name, clients[name]) is True
            barrier.wait()  # both threads armed before either reads
            results[f"{name}_client"] = ex.client
            results[f"{name}_equity"] = ex.equity_mgr.active_client

        threads = [
            threading.Thread(target=worker, args=("paper",)),
            threading.Thread(target=worker, args=("prod",)),
        ]
        for t in threads:
            t.start()
        barrier.wait()
        for t in threads:
            t.join()

        # Paper thread sees paper everywhere; prod thread sees prod everywhere.
        assert results["paper_client"] is clients["paper"]
        assert results["prod_client"] is clients["prod"]
        assert results["paper_equity"] == "paper"
        assert results["prod_equity"] == "prod"

    def test_equity_reads_and_order_client_resolve_own_account(self) -> None:
        """The exact interleave that used to flip the shared slot is safe."""
        ex, clients = self._make_real_executor()
        barrier = threading.Barrier(3)
        resolved: dict[str, Any] = {}

        def worker(name: str) -> None:
            ex.set_client(name, clients[name])
            barrier.wait()
            # Both threads read AFTER both set_client calls have run.  With
            # a non-thread-local active client, the last set_client wins and
            # one thread would read the other's account here.
            resolved[name] = {
                "order_client": ex.client,
                "sizing_equity": ex.equity_mgr.get(),
            }

        threads = [
            threading.Thread(target=worker, args=("paper",)),
            threading.Thread(target=worker, args=("prod",)),
        ]
        for t in threads:
            t.start()
        barrier.wait()
        for t in threads:
            t.join()

        assert resolved["paper"]["order_client"] is clients["paper"]
        assert resolved["prod"]["order_client"] is clients["prod"]
        # Distinct account balances: paper 10k, prod 20k — wrong-account
        # reads would cross these.
        assert resolved["paper"]["sizing_equity"] == 10000.0
        assert resolved["prod"]["sizing_equity"] == 20000.0

    def test_fresh_thread_falls_back_to_default_client(self) -> None:
        """Threads that never call set_client resolve the first client."""
        ex, clients = self._make_real_executor()

        resolved: dict[str, Any] = {}

        def worker() -> None:
            resolved["client"] = ex.client
            resolved["equity"] = ex.equity_mgr.active_client

        t = threading.Thread(target=worker)
        t.start()
        t.join()

        assert resolved["client"] is clients["paper"]
        assert resolved["equity"] == "paper"

    def test_client_set_to_none_falls_back_to_default(self) -> None:
        """Defensive: setting client to None un-arms the thread (fallback)."""
        ex, clients = self._make_real_executor()
        ex.set_client("prod", clients["prod"])
        assert ex.client is clients["prod"]

        ex.client = None
        assert ex.client is clients["paper"]
