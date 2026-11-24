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


class TestProcessStrategy(unittest.TestCase):
    def setUp(self) -> None:
        self.trader = _make_trader()
        # Give the market_schedule mock predictable UTC open time (14:30 UTC)
        self.trader.market_schedule.market_open_time.hour = 14
        self.trader.market_schedule.market_open_time.minute = 30

    def _make_strategy(
        self,
        interval: str = "60m",
        manage_interval: str | None = None,
        ic_entry_hour: int | None = None,
    ) -> MagicMock:
        s = MagicMock()
        s.name = "CoreStrategy_paper"
        s.config.INTERVAL = interval
        s.config.MANAGE_INTERVAL = manage_interval or interval
        s.config.IC_ENTRY_HOUR_ET = ic_entry_hour
        return s

    def test_1d_no_ic_entry_hour_before_market_open_returns_empty(self) -> None:
        """Lines 397-402: else-path when IC_ENTRY_HOUR_ET is None.
        now is 09:00 UTC, market opens at 14:30 UTC → should_signal=False."""
        strategy = self._make_strategy("1d", "1d")
        now = datetime.datetime(2024, 6, 1, 9, 0, tzinfo=datetime.UTC)
        result = self.trader.process_strategy(strategy, now)
        self.assertEqual(result, [])

    def test_zero_interval_returns_empty(self) -> None:
        """Line 409: else branch when interval_minutes == 0."""
        strategy = self._make_strategy("0m", "0m")
        now = datetime.datetime(2024, 6, 1, 9, 0, tzinfo=datetime.UTC)
        result = self.trader.process_strategy(strategy, now)
        self.assertEqual(result, [])

    def test_dedup_naive_last_run_sets_should_signal_false(self) -> None:
        """Lines 415-418: last_run has no tzinfo → gets UTC added, then
        now - last_run < delta - tolerance → should_signal=False → returns []."""
        # INTERVAL=60m, delta=60min, tolerance=30s
        # (current_minutes - 16) % 60 == 0 when current_minutes = 76 → 01:16 UTC
        strategy = self._make_strategy("60m", "60m")
        now = datetime.datetime(2024, 6, 1, 1, 16, tzinfo=datetime.UTC)
        # last_run was 1 minute ago (naive) — within 59.5 min window → dedup fires
        self.trader.last_update["CoreStrategy_paper"] = datetime.datetime(
            2024, 6, 1, 1, 15
        )
        result = self.trader.process_strategy(strategy, now)
        self.assertEqual(result, [])

    def test_dedup_aware_last_run_old_enough_passes(self) -> None:
        """Lines 415->417, 417->422: last_run is tz-aware AND old enough that
        now - last_run >= delta - tolerance → should_signal stays True."""
        import pandas as pd

        strategy = self._make_strategy("60m", "60m")
        strategy.config.TICKERS = ["SPY"]
        strategy.config.PRE_LOAD_DAYS = 5
        strategy.config.DAILY_START = None
        # (76-16)%60=0 → should_signal=True at 01:16 UTC
        now = datetime.datetime(2024, 6, 1, 1, 16, tzinfo=datetime.UTC)
        # last_run 65 minutes ago (tz-aware) — outside 59.5 min window → dedup not triggered
        self.trader.last_update["CoreStrategy_paper"] = now - datetime.timedelta(
            minutes=65
        )

        fake_df = pd.DataFrame(
            {"Close": [100.0]},
            index=pd.DatetimeIndex(
                [pd.Timestamp("2024-01-01", tz="UTC")], name="timestamp"
            ),
        )
        self.trader.data_handler.get_historical_data.return_value = fake_df

        engine = self.trader.engines["CoreStrategy_paper"]
        with patch.object(engine, "process_ticker", return_value=[]) as mock_pt:
            result = self.trader.process_strategy(strategy, now)

        self.assertEqual(result, [])
        mock_pt.assert_called_once()

    def test_manage_only_cycle_no_positions_returns_empty(self) -> None:
        """Line 449: manage-only cycle skips when no open positions exist.

        INTERVAL=60m → should_signal=False at 09:21 UTC (current_minutes=561,
        (561-16)%60=5≠0). MANAGE_INTERVAL=5m → (561-16)%5=0 → should_manage=True.
        No open positions → early return [].
        """
        strategy = self._make_strategy("60m", "5m")
        now = datetime.datetime(2024, 6, 1, 9, 21, tzinfo=datetime.UTC)
        # Ensure no open positions for this strategy
        self.trader.active_positions.pop("CoreStrategy_paper", None)
        result = self.trader.process_strategy(strategy, now)
        self.assertEqual(result, [])

    def test_manage_dedup_naive_last_manage_sets_should_manage_false(self) -> None:
        """Lines 434-438: manage dedup — naive last_manage gets UTC, then
        now - last_manage < manage_delta - tolerance → should_manage=False.
        With should_signal also False → returns []."""
        # INTERVAL=60m should_signal=False (not on schedule)
        # MANAGE_INTERVAL=5m should_manage=True at 09:21 UTC (545%5==0)
        strategy = self._make_strategy("60m", "5m")
        now = datetime.datetime(2024, 6, 1, 9, 21, tzinfo=datetime.UTC)
        # last_manage_update 2 minutes ago (naive) — within 4.5 min window → dedup
        self.trader.last_manage_update["CoreStrategy_paper"] = datetime.datetime(
            2024, 6, 1, 9, 19
        )
        # Ensure no open positions (won't get to manage-only check anyway since dedup fires first)
        self.trader.active_positions.pop("CoreStrategy_paper", None)
        result = self.trader.process_strategy(strategy, now)
        self.assertEqual(result, [])

    def test_manage_dedup_aware_last_manage_too_recent(self) -> None:
        """Line 434+435: last_manage is tz-aware AND too recent → should_manage=False."""
        strategy = self._make_strategy("60m", "5m")
        now = datetime.datetime(2024, 6, 1, 9, 21, tzinfo=datetime.UTC)
        # last_manage_update 2 minutes ago (tz-aware) — within 4.5 min window → dedup
        self.trader.last_manage_update["CoreStrategy_paper"] = now - datetime.timedelta(
            minutes=2
        )
        self.trader.active_positions.pop("CoreStrategy_paper", None)
        result = self.trader.process_strategy(strategy, now)
        self.assertEqual(result, [])

    def test_manage_dedup_old_enough_passes_through(self) -> None:
        """Line 435->440: last_manage is old enough → dedup does NOT fire,
        should_manage stays True but no positions → manage-only early return []."""
        strategy = self._make_strategy("60m", "5m")
        now = datetime.datetime(2024, 6, 1, 9, 21, tzinfo=datetime.UTC)
        # last_manage_update 10 minutes ago (tz-aware) — outside 4.5 min window
        self.trader.last_manage_update["CoreStrategy_paper"] = now - datetime.timedelta(
            minutes=10
        )
        # No open positions → manage-only cycle early return
        self.trader.active_positions.pop("CoreStrategy_paper", None)
        result = self.trader.process_strategy(strategy, now)
        self.assertEqual(result, [])

    def test_no_tickers_returns_empty_after_entering_try_block(self) -> None:
        """Line 459: should_signal=True but strategy has no tickers → return []."""
        strategy = self._make_strategy("60m", "60m")
        strategy.config.TICKERS = []
        # INTERVAL=60m, (76-16)%60=0 → should_signal=True at 01:16 UTC
        now = datetime.datetime(2024, 6, 1, 1, 16, tzinfo=datetime.UTC)
        result = self.trader.process_strategy(strategy, now)
        self.assertEqual(result, [])

    def test_with_tickers_and_engine_runs_process_ticker(self) -> None:
        """With tickers + live data, engine.process_ticker is called."""
        import pandas as pd

        strategy = self._make_strategy("60m", "60m")
        strategy.config.TICKERS = ["SPY"]
        strategy.config.PRE_LOAD_DAYS = 5
        strategy.config.DAILY_START = None
        now = datetime.datetime(2024, 6, 1, 1, 16, tzinfo=datetime.UTC)

        fake_df = pd.DataFrame(
            {"Close": [100.0]},
            index=pd.DatetimeIndex(
                [pd.Timestamp("2024-01-01", tz="UTC")], name="timestamp"
            ),
        )

        self.trader.data_handler.get_historical_data.return_value = fake_df

        engine = self.trader.engines["CoreStrategy_paper"]
        with patch.object(engine, "process_ticker", return_value=[]) as mock_pt:
            result = self.trader.process_strategy(strategy, now)

        # process_strategy now returns [] (execution is inline via LiveExecutor)
        self.assertEqual(result, [])
        mock_pt.assert_called_once()

    def test_signal_only_phase_string_is_set(self) -> None:
        """Line 459: should_signal=True but should_manage=False → signal-only phase."""
        import pandas as pd

        strategy = self._make_strategy("60m", "7m")
        strategy.config.TICKERS = ["SPY"]
        strategy.config.PRE_LOAD_DAYS = 5
        strategy.config.DAILY_START = None
        # At 01:16 UTC: (76-16)%60=0 → should_signal=True
        # (76-16)%7=60%7=4≠0 → should_manage=False → signal-only phase (line 459)
        now = datetime.datetime(2024, 6, 1, 1, 16, tzinfo=datetime.UTC)

        fake_df = pd.DataFrame(
            {"Close": [100.0]},
            index=pd.DatetimeIndex(
                [pd.Timestamp("2024-01-01", tz="UTC")], name="timestamp"
            ),
        )
        self.trader.data_handler.get_historical_data.return_value = fake_df

        engine = self.trader.engines["CoreStrategy_paper"]
        with patch.object(engine, "process_ticker", return_value=[]) as mock_pt:
            result = self.trader.process_strategy(strategy, now)

        self.assertEqual(result, [])
        mock_pt.assert_called_once()

    def test_1d_with_daily_start_uses_lookback_from_start(self) -> None:
        """Line 479: INTERVAL=1d with DAILY_START set → lookback computed from start date."""
        import pandas as pd

        strategy = self._make_strategy("1d", "1d")
        strategy.config.IC_ENTRY_HOUR_ET = None
        strategy.config.TICKERS = ["SPY"]
        strategy.config.PRE_LOAD_DAYS = 5
        strategy.config.DAILY_START = "2024-01-01"
        # At 15:00 UTC → current_minutes_utc=900 > market_open=870 (14:30) → should_signal=True
        now = datetime.datetime(2024, 6, 1, 15, 0, tzinfo=datetime.UTC)

        fake_df = pd.DataFrame(
            {"Close": [100.0]},
            index=pd.DatetimeIndex(
                [pd.Timestamp("2024-01-01", tz="UTC")], name="timestamp"
            ),
        )
        self.trader.data_handler.get_historical_data.return_value = fake_df

        engine = self.trader.engines["CoreStrategy_paper"]
        with patch.object(engine, "process_ticker", return_value=[]) as mock_pt:
            result = self.trader.process_strategy(strategy, now)

        self.assertEqual(result, [])
        mock_pt.assert_called_once()

    def test_empty_df_from_cache_skips_ticker(self) -> None:
        """Line 518: when data fetch returns empty df → continue skips ticker."""
        import pandas as pd

        strategy = self._make_strategy("60m", "60m")
        strategy.config.TICKERS = ["SPY"]
        strategy.config.PRE_LOAD_DAYS = 5
        strategy.config.DAILY_START = None
        now = datetime.datetime(2024, 6, 1, 1, 16, tzinfo=datetime.UTC)

        # Return empty df → engine skips ticker loop
        self.trader.data_handler.get_historical_data.return_value = pd.DataFrame()

        engine = self.trader.engines["CoreStrategy_paper"]
        with patch.object(engine, "process_ticker", return_value=[]) as mock_pt:
            result = self.trader.process_strategy(strategy, now)

        self.assertEqual(result, [])
        mock_pt.assert_not_called()

    def test_ticker_exception_is_caught_and_logged(self) -> None:
        """Lines 537-538: exception during ticker processing is caught and logged."""
        import pandas as pd

        strategy = self._make_strategy("60m", "60m")
        strategy.config.TICKERS = ["SPY"]
        strategy.config.PRE_LOAD_DAYS = 5
        strategy.config.DAILY_START = None
        now = datetime.datetime(2024, 6, 1, 1, 16, tzinfo=datetime.UTC)

        fake_df = pd.DataFrame(
            {"Close": [100.0]},
            index=pd.DatetimeIndex(
                [pd.Timestamp("2024-01-01", tz="UTC")], name="timestamp"
            ),
        )
        self.trader.data_handler.get_historical_data.return_value = fake_df

        engine = self.trader.engines["CoreStrategy_paper"]
        with patch.object(
            engine, "process_ticker", side_effect=RuntimeError("bad ticker")
        ):
            # Must not raise — engine catches per-ticker exceptions
            result = self.trader.process_strategy(strategy, now)

        self.assertEqual(result, [])

    def test_outer_exception_is_caught_and_returns_empty(self) -> None:
        """Lines 542-544: exception outside inner ticker loop is caught."""
        strategy = self._make_strategy("60m", "60m")
        # Make strategy.config raise on TICKERS access → triggers outer except
        bad_config = MagicMock()
        bad_config.INTERVAL = "60m"
        bad_config.MANAGE_INTERVAL = "60m"
        bad_config.IC_ENTRY_HOUR_ET = None
        type(bad_config).TICKERS = property(
            lambda self: (_ for _ in ()).throw(RuntimeError("cfg explode"))
        )
        strategy.config = bad_config
        now = datetime.datetime(2024, 6, 1, 1, 16, tzinfo=datetime.UTC)

        result = self.trader.process_strategy(strategy, now)
        self.assertEqual(result, [])

    def test_zero_manage_interval_falls_through_to_else_branch(self) -> None:
        """Line 438: manage_interval='0m' → manage_interval_minutes==0 → else branch."""
        strategy = self._make_strategy("60m", "0m")
        # At 01:16 UTC: should_signal=True (schedule hit)
        # manage_interval_minutes=0 → else → should_manage=should_signal=True
        # But strategy has no tickers → returns [] from try block
        strategy.config.TICKERS = []
        now = datetime.datetime(2024, 6, 1, 1, 16, tzinfo=datetime.UTC)
        result = self.trader.process_strategy(strategy, now)
        self.assertEqual(result, [])


# ===========================================================================
# cleanup() — module-level atexit handler
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
