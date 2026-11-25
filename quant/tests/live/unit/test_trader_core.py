"""
Tests for python/trader.py — core UnifiedTrader methods.

Covers:
  - _cleanup_expired_positions: expired IC removed; future IC, equity, malformed expiry preserved
"""

from __future__ import annotations

import datetime
import logging
import os
import sys
import time
import unittest
from dataclasses import dataclass, field
from typing import cast
from unittest.mock import MagicMock, patch

# N.B. alpaca is stubbed per-test by the autouse fixture in tests/live/conftest.py.
sys.path.append(
    os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
)

from quant.live.trader import UnifiedTrader
from quant.utils.strategy_config import BaseStrategyConfig

# ---------------------------------------------------------------------------
# Minimal strategy/config fixtures
# ---------------------------------------------------------------------------


@dataclass
class CoreTestConfig(BaseStrategyConfig):
    INTERVAL: str = "1m"
    PRE_LOAD_DAYS: int = 1
    MANAGE_INTERVAL: str = "1m"
    INITIAL_CAPITAL: float = 100_000.0
    CAPITAL_PER_TRADE: float = 0.10
    MAX_CONCURRENT_POSITIONS: int = 5
    STOP_LOSS_PERCENT: float = 0.30
    TAKE_PROFIT_PERCENT: float = 2.0
    BREAKEVEN_PROFIT_PERCENT: float = 1.0
    USE_TRAIL_LOSS: bool = False
    MAX_HOLDING_MINUTES: int = 480
    MIN_SIGNAL_CONFIDENCE: float = 0.5
    MAX_DAY_TRADES: int = 3
    BASE_SLIPPAGE_FACTOR: float = 0.001
    TICKERS: list = field(default_factory=lambda: ["SPY"])
    MIN_OPTION_DTE: int = 0


class CoreTestStrategy:
    def __init__(self, name: str = "CoreStrategy_paper") -> None:
        self.name = name
        self.config = CoreTestConfig()
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
        return None


# ---------------------------------------------------------------------------
# Factory: build a fully-mocked UnifiedTrader
# ---------------------------------------------------------------------------


def _make_trader(strategy_name: str = "CoreStrategy_paper") -> UnifiedTrader:
    mock_dh = MagicMock()
    mock_client = MagicMock()
    mock_dh.clients = {"paper": mock_client}
    strategy = CoreTestStrategy(strategy_name)

    with (
        patch("quant.live.trader.load_state", return_value={}),
        patch("quant.live.trader.AlpacaDataHandler", return_value=mock_dh),
        patch("quant.live.trader.MarketSchedule"),
        patch("quant.live.trader.load_strategies", return_value=[strategy]),
    ):
        trader = UnifiedTrader()

    return trader


# ===========================================================================
# _cleanup_expired_positions
# ===========================================================================


class TestCleanupExpiredPositions(unittest.TestCase):
    def setUp(self) -> None:
        self.trader = _make_trader()
        self.s_name = self.trader.strategies[0].name

    def _ic_pos(self, expiration: str) -> dict:
        return {
            "Ticker": "SPY",
            "AssetType": "IC",
            "Expiration": expiration,
            "Collateral": 1000.0,
            "FillCredit": 1.0,
            "NContracts": 1,
        }

    def test_expired_ic_position_is_removed(self) -> None:
        self.trader.active_positions[self.s_name] = [self._ic_pos("2020-01-01")]

        with patch.object(self.trader, "_save_state") as mock_save:
            self.trader._cleanup_expired_positions()

        self.assertEqual(self.trader.active_positions[self.s_name], [])
        mock_save.assert_called_once()

    def test_future_ic_position_is_preserved(self) -> None:
        future_pos = self._ic_pos("2099-01-01")
        self.trader.active_positions[self.s_name] = [future_pos]

        with patch.object(self.trader, "_save_state") as mock_save:
            self.trader._cleanup_expired_positions()

        self.assertEqual(len(self.trader.active_positions[self.s_name]), 1)
        mock_save.assert_not_called()

    def test_non_ic_position_is_never_removed(self) -> None:
        equity_pos = {
            "Ticker": "SPY",
            "AssetType": "EQUITY",
            "Expiration": "2020-01-01",
        }
        self.trader.active_positions[self.s_name] = [equity_pos]

        with patch.object(self.trader, "_save_state") as mock_save:
            self.trader._cleanup_expired_positions()

        self.assertEqual(len(self.trader.active_positions[self.s_name]), 1)
        mock_save.assert_not_called()

    def test_malformed_expiry_string_does_not_crash(self) -> None:
        self.trader.active_positions[self.s_name] = [
            {"AssetType": "IC", "Expiration": "not-a-date", "Ticker": "SPY"}
        ]
        # Should not raise
        self.trader._cleanup_expired_positions()

    def test_empty_expiry_string_preserves_position(self) -> None:
        self.trader.active_positions[self.s_name] = [
            {"AssetType": "IC", "Expiration": "", "Ticker": "SPY"}
        ]
        self.trader._cleanup_expired_positions()
        self.assertEqual(len(self.trader.active_positions[self.s_name]), 1)

    def test_only_expired_positions_removed_from_mixed_list(self) -> None:
        expired = self._ic_pos("2020-01-01")
        future = self._ic_pos("2099-01-01")
        equity = {"AssetType": "EQUITY", "Expiration": "2020-01-01", "Ticker": "SPY"}
        self.trader.active_positions[self.s_name] = [expired, future, equity]

        with patch.object(self.trader, "_save_state"):
            self.trader._cleanup_expired_positions()

        remaining = self.trader.active_positions[self.s_name]
        self.assertEqual(len(remaining), 2)
        self.assertIn(future, remaining)
        self.assertIn(equity, remaining)

    def test_save_state_called_only_when_something_removed(self) -> None:
        # One expired, one future — only one save call expected
        self.trader.active_positions[self.s_name] = [
            self._ic_pos("2020-01-01"),
            self._ic_pos("2099-01-01"),
        ]
        with patch.object(self.trader, "_save_state") as mock_save:
            self.trader._cleanup_expired_positions()

        mock_save.assert_called_once()


# ===========================================================================
# _load_local_state — removed strategy in saved state
# ===========================================================================


class TestLoadLocalState(unittest.TestCase):
    def test_removed_strategy_does_not_pollute_active_positions(self) -> None:
        """Positions for a strategy that no longer exists must NOT be loaded
        into active_positions (the `pass` at line 338 means we skip them)."""
        trader = _make_trader()
        original_keys = set(trader.active_positions.keys())

        with patch(
            "quant.live.trader.load_state",
            return_value={
                "active_positions": {
                    "RemovedStrategy_paper": [{"Ticker": "SPY", "OrderId": "x"}]
                },
                "orders": {},
                "last_update": {},
            },
        ):
            trader._load_local_state()

        # "RemovedStrategy_paper" was NOT in active_positions → skipped
        assert "RemovedStrategy_paper" not in trader.active_positions
        # Keys did not grow beyond what was already there
        assert set(trader.active_positions.keys()) == original_keys


# ===========================================================================
# process_strategy — scheduling edge cases
# ===========================================================================


class TestCleanupFunction(unittest.TestCase):
    """Module-level cleanup() is registered with atexit and calls logging.shutdown."""

    def test_cleanup_calls_logging_shutdown(self) -> None:
        import quant.live.trader as trader_module

        with patch("logging.shutdown") as mock_shutdown:
            trader_module.cleanup()

        mock_shutdown.assert_called_once()


# ===========================================================================
# UnifiedTrader.run() — main loop branches
# ===========================================================================


class TestTraderRun(unittest.TestCase):
    """Exercise every branch in UnifiedTrader.run() without real I/O."""

    def setUp(self) -> None:
        self.trader = _make_trader()

    def test_run_market_closed_sleeps_60(self) -> None:
        """Market closed → _cleanup_expired_positions + sleep(60) + continue.
        Second iteration raises KeyboardInterrupt to exit the loop."""
        self.trader.market_schedule.is_market_open.side_effect = [
            False,
            KeyboardInterrupt(),
        ]
        with patch("quant.live.trader.time") as mock_time:
            self.trader.run()

        mock_time.sleep.assert_any_call(60)

    def test_run_config_update_applied_when_poll_returns_cfg(self) -> None:
        """poll() returning a config dict → _apply_config_update is called."""
        new_cfg = {"strategies": []}
        self.trader.market_schedule.is_market_open.side_effect = [
            True,
            KeyboardInterrupt(),
        ]
        with (
            patch.object(self.trader.config_sync, "poll", return_value=new_cfg),
            patch.object(self.trader, "_apply_config_update") as mock_apply,
            patch.object(self.trader, "_save_state"),
            patch("quant.live.trader.time"),
        ):
            self.trader.run()

        mock_apply.assert_called_once_with(new_cfg)

    def test_run_no_strategies_saves_state(self) -> None:
        """No strategies → skip thread pool; _save_state is still called."""
        self.trader.strategies = []
        self.trader.market_schedule.is_market_open.side_effect = [
            True,
            KeyboardInterrupt(),
        ]
        with (
            patch.object(self.trader.config_sync, "poll", return_value=None),
            patch.object(self.trader, "_save_state") as mock_save,
            patch("quant.live.trader.time"),
        ):
            self.trader.run()

        mock_save.assert_called()

    def test_run_strategies_executed_in_threadpool(self) -> None:
        """With strategies present, process_strategy is called for each."""
        self.trader.market_schedule.is_market_open.side_effect = [
            True,
            KeyboardInterrupt(),
        ]
        with (
            patch.object(self.trader.config_sync, "poll", return_value=None),
            patch.object(self.trader, "process_strategy", return_value=[]) as mock_ps,
            patch.object(self.trader, "_save_state"),
            patch("quant.live.trader.time"),
        ):
            self.trader.run()

        mock_ps.assert_called_once()

    def test_run_exception_in_loop_logs_and_sleeps_60(self) -> None:
        """Exception inside the try block → logs error + sleeps 60 seconds."""
        self.trader.market_schedule.is_market_open.side_effect = [
            RuntimeError("network down"),
            KeyboardInterrupt(),
        ]
        with patch("quant.live.trader.time") as mock_time:
            self.trader.run()

        mock_time.sleep.assert_any_call(60)

    def test_run_future_exception_is_caught_per_future(self) -> None:
        """Per-future try/except in run() catches and logs exceptions that
        escape process_strategy (secondary safety net).

        process_strategy is patched to raise so that future.result() re-raises
        it; the loop must not crash and the logger must record the error."""
        self.trader.market_schedule.is_market_open.side_effect = [
            True,
            KeyboardInterrupt(),
        ]
        mock_logger = MagicMock()
        self.trader.logger = mock_logger
        with (
            patch.object(self.trader.config_sync, "poll", return_value=None),
            patch.object(
                self.trader,
                "process_strategy",
                side_effect=RuntimeError("strategy exploded"),
            ),
            patch.object(self.trader, "_save_state"),
            patch("quant.live.trader.time"),
        ):
            self.trader.run()  # must not raise

        mock_logger.exception.assert_called()


# ===========================================================================
# Monitor wiring (emit hook stubbed via conftest.mock_monitor)
# ===========================================================================


def test_trader_starts_monitor_and_wires_emit(mock_monitor):
    """UnifiedTrader wires the optional emit hook into engines (no sidecar)."""
    trader = _make_trader("CoreStrategy_paper")

    assert trader._monitor_enabled is True
    assert trader._monitor_emit is mock_monitor
    engine = trader.engines["CoreStrategy_paper"]
    assert engine._monitor_emit is mock_monitor


def test_trader_run_emits_heartbeat(mock_monitor):
    """Main loop emits a heartbeat each tick when the monitor is enabled."""
    from tests.live.conftest import emitted_events

    trader = _make_trader("CoreStrategy_paper")
    trader.market_schedule.is_market_open.side_effect = [True, KeyboardInterrupt()]
    mock_monitor.reset_mock()

    with (
        patch.object(trader.config_sync, "poll", return_value=None),
        patch.object(trader, "process_strategy"),
        patch.object(trader, "_save_state"),
        patch("quant.live.trader.time"),
    ):
        trader.run()

    heartbeats = emitted_events(mock_monitor, kind="heartbeat")
    assert len(heartbeats) == 1
    # Heartbeat carries the main-loop cycle drift series.
    assert isinstance(heartbeats[0].get("main_loop_lag_ns"), int)

    # Worker wrapper publishes dispatch jitter to the monitor plane.
    from quant.core._utils import tick_dispatch_ns

    captured: dict[str, int] = {}
    trader_inst = _make_trader("CoreStrategy_paper")
    trader_inst._monitor_emit = mock_monitor

    def _fake_process_strategy(strategy, now):
        captured["dispatch"] = tick_dispatch_ns()
        return []

    trader_inst.process_strategy = _fake_process_strategy
    submit_ns = time.perf_counter_ns() - 7_000_000  # pretend 7 ms of queue wait
    from quant.live.protocols import LiveStrategyProtocol

    trader_inst._process_strategy_timed(
        cast("LiveStrategyProtocol", object()),
        datetime.datetime.now(datetime.UTC),
        submit_ns,
    )
    assert captured["dispatch"] >= 7_000_000 - 1_000_000
    assert tick_dispatch_ns() == 0, "wrapper must restore the contextvar"


if __name__ == "__main__":
    unittest.main()
