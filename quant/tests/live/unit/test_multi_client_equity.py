"""
Tests for multi-client equity isolation and the related guard changes.

Covers:
  - MultiClientEquityManager.set_active: returns True on known client,
    False on unknown (new bool return)
  - MultiClientEquityManager.active_client: property exposes active name
  - Equity isolation: debit/credit on one client never affects another
  - LiveExecutor._set_client guard: skips when client not in clients dict
  - LiveExecutor._set_client guard: skips when set_client() returns False
  - LiveExecutor._set_client guard: proceeds when set_client() returns True
  - LiveExecutor._set_client guard: set_client called with the right pair
"""

from __future__ import annotations

# Alpaca is stubbed per-test by the autouse fixture in quant/tests/live/conftest.py.
# ---------------------------------------------------------------------------
# Imports under test
# ---------------------------------------------------------------------------
import logging
import sys
import threading
from typing import cast
from unittest.mock import MagicMock, patch

import pytest

from quant.live.executors.live_executor import LiveExecutor
from quant.live.utils import EquityManager, MultiClientEquityManager

_logger = logging.getLogger("test_multi_client_equity")


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_client(equity: str = "10000.0") -> MagicMock:
    account = MagicMock()
    account.equity = equity
    client = MagicMock()
    client.get_account.return_value = account
    return client


def _make_multi(
    equities: dict[str, str],
) -> MultiClientEquityManager:
    from alpaca.trading.client import TradingClient

    clients = {name: _make_client(eq) for name, eq in equities.items()}
    return MultiClientEquityManager(cast("dict[str, TradingClient]", clients), _logger)


# ===========================================================================
# MultiClientEquityManager — new bool return + active_client property
# ===========================================================================


class TestMultiClientEquityIsolation:
    def test_set_active_returns_true_for_known_client(self) -> None:
        multi = _make_multi({"paper": "10000", "prod": "20000"})
        assert multi.set_active("prod") is True

    def test_set_active_returns_false_for_unknown_client(self) -> None:
        multi = _make_multi({"paper": "10000"})
        assert multi.set_active("nonexistent") is False

    def test_set_active_unknown_does_not_change_active(self) -> None:
        multi = _make_multi({"paper": "10000", "prod": "20000"})
        multi.set_active("paper")
        multi.set_active("ghost")
        assert multi.active_client == "paper"

    def test_active_client_property_returns_current_name(self) -> None:
        multi = _make_multi({"paper": "10000", "prod": "20000"})
        multi.set_active("prod")
        assert multi.active_client == "prod"

    def test_get_returns_active_client_equity(self) -> None:
        multi = _make_multi({"paper": "10000", "prod": "20000"})
        multi._managers["prod"]._equity = 20000.0
        multi.set_active("prod")
        assert multi.get() == 20000.0

    def test_debit_on_paper1_does_not_affect_paper(self) -> None:
        multi = _make_multi({"paper": "10000", "paper1": "5000"})
        multi._managers["paper"]._equity = 10000.0
        multi._managers["paper1"]._equity = 5000.0

        multi.set_active("paper1")
        multi.debit(1000.0)

        assert multi._managers["paper"]._equity == 10000.0  # untouched
        assert multi._managers["paper1"]._equity == 4000.0

    def test_credit_on_paper1_does_not_affect_paper(self) -> None:
        multi = _make_multi({"paper": "10000", "paper1": "5000"})
        multi._managers["paper"]._equity = 10000.0
        multi._managers["paper1"]._equity = 5000.0

        multi.set_active("paper1")
        multi.credit(2000.0)

        assert multi._managers["paper"]._equity == 10000.0  # untouched
        assert multi._managers["paper1"]._equity == 7000.0

    def test_three_clients_are_fully_independent(self) -> None:
        multi = _make_multi({"paper": "10000", "paper1": "5000", "paper2": "8000"})
        multi._managers["paper"]._equity = 10000.0
        multi._managers["paper1"]._equity = 5000.0
        multi._managers["paper2"]._equity = 8000.0

        multi.set_active("paper1")
        multi.debit(1000.0)

        assert multi._managers["paper"]._equity == 10000.0
        assert multi._managers["paper1"]._equity == 4000.0
        assert multi._managers["paper2"]._equity == 8000.0

    def test_set_active_switches_back_correctly(self) -> None:
        multi = _make_multi({"paper": "10000", "paper1": "5000"})
        multi._managers["paper"]._equity = 10000.0
        multi._managers["paper1"]._equity = 5000.0

        multi.set_active("paper1")
        multi.debit(500.0)

        multi.set_active("paper")
        assert multi.get() == 10000.0  # untouched by the paper1 debit


# ===========================================================================
# LiveExecutor._set_client guard
# ===========================================================================


class TestLiveExecutorClientGuard:
    """
    Tests that LiveExecutor._set_client skips execution when the client guard
    fires, and proceeds when everything is correctly registered.

    LiveExecutor is constructed directly with a MagicMock trader context so
    we can control set_client() return values and observe enter_trade calls
    without constructing a full UnifiedTrader.
    """

    def _make_executor(self, strategy, *, set_client_return=True):
        """Return (executor, refs) with mocked client deps."""
        from collections import defaultdict
        from types import SimpleNamespace

        unified_exec = MagicMock()
        unified_exec.set_client.return_value = set_client_return
        unified_exec.enter_trade.return_value = None
        clients = {"paper": MagicMock(), "paper1": MagicMock()}
        logger = MagicMock()
        executor = LiveExecutor(
            last_update={},
            last_manage_update={},
            active_positions=defaultdict(list),
            strategies=[strategy],
            clients=clients,
            unified_executor=unified_exec,
            lock=MagicMock(),
            logger=logger,
            save_state=MagicMock(),
        )
        refs = SimpleNamespace(
            clients=clients,
            unified_executor=unified_exec,
            logger=logger,
        )
        return executor, refs

    def _make_strategy(self, name: str = "TestStrat", client_name: str = "paper"):
        strat = MagicMock()
        strat.name = name
        strat.client_name = client_name
        return strat

    def test_skips_entry_when_client_not_in_clients_dict(self) -> None:
        strat = self._make_strategy(client_name="unknown_client")
        executor, refs = self._make_executor(strat)
        refs.clients.pop("unknown_client", None)

        result = executor.enter(
            strat, "SPY", {"Signal": "BUY", "Confidence": 0.9}, MagicMock()
        )

        assert result is None
        refs.unified_executor.set_client.assert_not_called()
        refs.unified_executor.enter_trade.assert_not_called()
        refs.logger.error.assert_called()
        assert "not found in trading clients dict" in refs.logger.error.call_args[0][0]

    def test_skips_entry_when_set_client_returns_falsy(self) -> None:
        strat = self._make_strategy(client_name="paper")
        executor, refs = self._make_executor(strat, set_client_return=False)

        result = executor.enter(
            strat, "SPY", {"Signal": "BUY", "Confidence": 0.9}, MagicMock()
        )

        assert result is None
        refs.unified_executor.enter_trade.assert_not_called()
        refs.logger.error.assert_called()
        assert "not registered in EquityManager" in refs.logger.error.call_args[0][0]

    def test_proceeds_when_client_registered_and_set_client_truthy(self) -> None:
        strat = self._make_strategy(client_name="paper")
        executor, refs = self._make_executor(strat)

        executor.enter(
            strat, "SPY", {"Signal": "BUY_CALL", "Confidence": 0.9}, MagicMock()
        )

        refs.unified_executor.enter_trade.assert_called_once()

    def test_set_client_called_with_correct_pair(self) -> None:
        strat = self._make_strategy(client_name="paper1")
        executor, refs = self._make_executor(strat)

        executor.enter(
            strat, "SPY", {"Signal": "BUY_CALL", "Confidence": 0.9}, MagicMock()
        )

        call_args = refs.unified_executor.set_client.call_args
        assert call_args[0][0] == "paper1"
        assert call_args[0][1] is refs.clients["paper1"]

    def test_two_strategies_correct_clients_used(self) -> None:
        from collections import defaultdict

        strat_paper = self._make_strategy("StratPaper", "paper")
        strat_paper1 = self._make_strategy("StratPaper1", "paper1")

        unified_exec = MagicMock()
        unified_exec.set_client.return_value = True
        unified_exec.enter_trade.return_value = None
        clients = {"paper": MagicMock(), "paper1": MagicMock()}
        executor = LiveExecutor(
            last_update={},
            last_manage_update={},
            active_positions=defaultdict(list),
            strategies=[strat_paper, strat_paper1],
            clients=clients,
            unified_executor=unified_exec,
            lock=MagicMock(),
            logger=MagicMock(),
            save_state=MagicMock(),
        )

        row = {"Signal": "BUY_CALL", "Confidence": 0.9}
        executor.enter(strat_paper, "SPY", row, MagicMock())
        executor.enter(strat_paper1, "SPY", row, MagicMock())

        calls = unified_exec.set_client.call_args_list
        assert calls[0][0][0] == "paper"
        assert calls[1][0][0] == "paper1"
        assert unified_exec.enter_trade.call_count == 2


# Proves active-client selection is per-thread: threads selecting different
# clients concurrently each read their own client's equity, and no thread
# can redirect another thread's account selection.


class TestMultiClientEquityThreadLocal:
    def test_active_selection_is_thread_local(self) -> None:
        multi = _make_multi({"paper": "10000", "prod": "20000"})
        results: dict[str, object] = {}
        barrier = threading.Barrier(3)

        def worker(name: str) -> None:
            multi.set_active(name)
            barrier.wait()  # both threads armed before either reads
            results[name] = multi.active_client
            results[f"{name}_equity"] = multi.get()

        threads = [
            threading.Thread(target=worker, args=("paper",)),
            threading.Thread(target=worker, args=("prod",)),
        ]
        for t in threads:
            t.start()
        barrier.wait()
        for t in threads:
            t.join()

        assert results["paper"] == "paper"
        assert results["prod"] == "prod"
        assert results["paper_equity"] == 10000.0
        assert results["prod_equity"] == 20000.0

    def test_unknown_set_active_keeps_thread_binding(self) -> None:
        multi = _make_multi({"paper": "10000", "prod": "20000"})
        multi.set_active("paper")
        assert multi.set_active("ghost") is False
        assert multi.active_client == "paper"

    def test_fresh_thread_falls_back_to_default_active(self) -> None:
        multi = _make_multi({"paper": "10000", "prod": "20000"})
        resolved: dict[str, object] = {}

        def worker() -> None:
            resolved["active"] = multi.active_client
            resolved["equity"] = multi.get()

        t = threading.Thread(target=worker)
        t.start()
        t.join()

        assert resolved["active"] == "paper"
        assert resolved["equity"] == 10000.0


# ===========================================================================
# EquityManager thread safety — one account, many strategy threads.
# get()/debit()/credit() mutate the same _equity; without the per-manager
# RLock two threads can lose each other's update (read-modify-write race).
# ===========================================================================


class TestEquityManagerConcurrency:
    def _make_equity(self, initial: str = "10000.0") -> EquityManager:
        account = MagicMock()
        account.equity = initial
        client = MagicMock()
        client.get_account.return_value = account
        from quant.live.utils import EquityManager

        return EquityManager(client, _logger)

    def test_concurrent_credits_are_not_lost(self) -> None:
        """200 credits of 1.0 across 2 threads must sum exactly."""
        mgr = self._make_equity("10000.0")
        n_threads = 4
        per_thread = 50
        barrier = threading.Barrier(n_threads)
        errors: list[BaseException] = []

        def worker() -> None:
            try:
                barrier.wait()
                for _ in range(per_thread):
                    mgr.credit(1.0)
            except BaseException as exc:  # pragma: no cover - fail surface
                errors.append(exc)

        threads = [threading.Thread(target=worker) for _ in range(n_threads)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        assert not errors, f"concurrent credits raised: {errors}"
        assert mgr._equity == 10000.0 + n_threads * per_thread, (
            f"lost updates: {mgr._equity} != {10000.0 + n_threads * per_thread}"
        )

    def test_concurrent_debits_never_go_negative(self) -> None:
        """Concurrent debits stay exact and bounded (no negative equity)."""
        mgr = self._make_equity("10000.0")
        n_threads = 6
        barrier = threading.Barrier(n_threads)
        errors: list[BaseException] = []

        def worker() -> None:
            try:
                barrier.wait()
                for _ in range(40):
                    mgr.debit(100.0)
            except BaseException as exc:  # pragma: no cover - fail surface
                errors.append(exc)

        threads = [threading.Thread(target=worker) for _ in range(n_threads)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        assert not errors
        assert mgr._equity == max(0.0, 10000.0 - n_threads * 40 * 100.0)
