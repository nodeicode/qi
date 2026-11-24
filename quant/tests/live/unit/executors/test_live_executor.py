"""
Direct unit tests for python/executors/live_executor.py.

Covers:
  - data_end_dt: returns now - delay_minutes
  - get_precomputed_signal: always None
  - active_positions: returns list from injected state; empty list for unknown
  - after_process_ticker: no-op returns None
  - enter: full success path; delegates to _set_client + enter_trade
  - exit: full success path; delegates to _set_client + exit_trade
  - exit: returns False when strategy not found
  - after_entry: appends position to active_positions and calls save_state
  - after_exit: removes position from active_positions and calls save_state
  - after_exit: absent position still triggers save_state
  - record_run: stamps last_update only when ran_signal=True
  - record_run: always stamps last_manage_update
  - should_run (hourly/30m): modulo-alignment fires at correct minute, not others
  - should_run (de-dup): recent last_run suppresses signal; old enough allows it;
      no prior run always fires; naive timestamp handled correctly
  - should_run (daily): fires after market open (UTC), not before, not at exactly open
  - should_run (daily + IC_ENTRY_*): IC_ENTRY config is ignored; still post-open
  - should_run (manage): mirrors signal when intervals equal; fires independently
      on its own modulo when different; manage de-dup suppresses/allows correctly
"""

from __future__ import annotations

import datetime
import sys
from collections import defaultdict
from types import SimpleNamespace
from unittest.mock import MagicMock

# Alpaca is stubbed per-test by the autouse fixture in quant/tests/live/conftest.py.
import pytest

from quant.live.executors.live_executor import LiveExecutor

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_strategy(name: str = "Strat_paper", client_name: str = "paper") -> MagicMock:
    strat = MagicMock()
    strat.name = name
    strat.client_name = client_name
    return strat


def _make_ctx(
    strategy=None,
    *,
    set_client_return: bool = True,
    delay_minutes: int = 15,
) -> SimpleNamespace:
    """Build a LiveExecutor with all state injected explicitly.

    Returns a SimpleNamespace with:
      .executor         — the LiveExecutor under test
      .active_positions — shared dict (mutations visible here)
      .last_update      — shared dict
      .last_manage_update — shared dict
      .save_state       — MagicMock callable (assert calls on it)
      .unified_executor — MagicMock sub-component
      .logger           — MagicMock logger
      .clients          — clients dict
    """
    unified_exec = MagicMock()
    unified_exec.set_client.return_value = set_client_return
    unified_exec.enter_trade.return_value = None
    unified_exec.exit_trade.return_value = False

    active_positions: dict = defaultdict(list)
    last_update: dict = {}
    last_manage_update: dict = {}
    save_state = MagicMock()
    logger = MagicMock()
    clients = {"paper": MagicMock(), "paper1": MagicMock()}

    executor = LiveExecutor(
        last_update=last_update,
        last_manage_update=last_manage_update,
        active_positions=active_positions,
        strategies=[strategy] if strategy else [],
        clients=clients,
        unified_executor=unified_exec,
        lock=MagicMock(),
        logger=logger,
        save_state=save_state,
        delay_minutes=delay_minutes,
    )
    return SimpleNamespace(
        executor=executor,
        active_positions=active_positions,
        last_update=last_update,
        last_manage_update=last_manage_update,
        save_state=save_state,
        unified_executor=unified_exec,
        logger=logger,
        clients=clients,
    )


def _make_strat_config(
    name: str = "Strat_test",
    client_name: str = "paper",
    interval: str = "1h",
    manage_interval: str | None = None,
    **extra_config,
) -> MagicMock:
    """Strategy mock with a SimpleNamespace config for should_run tests."""
    strat = MagicMock()
    strat.name = name
    strat.client_name = client_name
    strat.config = SimpleNamespace(
        INTERVAL=interval,
        MANAGE_INTERVAL=manage_interval if manage_interval is not None else interval,
        **extra_config,
    )
    return strat


# ===========================================================================
# data_end_dt
# ===========================================================================


class TestDataEndDt:
    def test_returns_now_minus_delay_minutes(self) -> None:
        ctx = _make_ctx(delay_minutes=15)
        now = datetime.datetime(2024, 6, 1, 12, 0, 0, tzinfo=datetime.UTC)

        result = ctx.executor.data_end_dt(now)

        assert result == now - datetime.timedelta(minutes=15)

    def test_custom_delay(self) -> None:
        ctx = _make_ctx(delay_minutes=5)
        now = datetime.datetime(2024, 6, 1, 12, 0, 0, tzinfo=datetime.UTC)

        assert ctx.executor.data_end_dt(now) == now - datetime.timedelta(minutes=5)


# ===========================================================================
# get_precomputed_signal
# ===========================================================================


class TestGetPrecomputedSignal:
    def test_always_returns_none(self) -> None:
        ctx = _make_ctx()
        assert ctx.executor.get_precomputed_signal("SPY", None) is None


# ===========================================================================
# active_positions
# ===========================================================================


class TestActivePositions:
    def test_returns_list_for_known_strategy(self) -> None:
        ctx = _make_ctx()
        pos = {"Ticker": "SPY"}
        ctx.active_positions["Strat_paper"] = [pos]

        assert ctx.executor.active_positions("Strat_paper") == [pos]

    def test_returns_empty_list_for_unknown_strategy(self) -> None:
        ctx = _make_ctx()

        assert ctx.executor.active_positions("NoSuchStrat") == []


# ===========================================================================
# after_process_ticker
# ===========================================================================


class TestAfterProcessTicker:
    def test_returns_none(self) -> None:
        ctx = _make_ctx()
        assert ctx.executor.after_process_ticker("Strat", "SPY", []) is None


# ===========================================================================
# enter
# ===========================================================================


class TestEnter:
    def test_full_success_path_returns_position(self) -> None:
        strat = _make_strategy()
        ctx = _make_ctx(strat)
        fake_pos = {"Ticker": "SPY", "OrderId": "abc"}
        ctx.unified_executor.enter_trade.return_value = fake_pos

        result = ctx.executor.enter(
            strat, "SPY", {"Signal": "BUY", "Confidence": 0.8}, None
        )

        assert result is fake_pos
        ctx.unified_executor.enter_trade.assert_called_once_with(
            strat, "SPY", {"Signal": "BUY", "Confidence": 0.8}, engine=None
        )

    def test_passes_engine_to_enter_trade(self) -> None:
        strat = _make_strategy()
        ctx = _make_ctx(strat)
        mock_engine = MagicMock()

        ctx.executor.enter(
            strat, "SPY", {"Signal": "BUY", "Confidence": 0.5}, mock_engine
        )

        call_kwargs = ctx.unified_executor.enter_trade.call_args[1]
        assert call_kwargs["engine"] is mock_engine

    def test_returns_none_when_enter_trade_returns_none(self) -> None:
        strat = _make_strategy()
        ctx = _make_ctx(strat)
        ctx.unified_executor.enter_trade.return_value = None

        result = ctx.executor.enter(
            strat, "SPY", {"Signal": "BUY", "Confidence": 0.5}, None
        )

        assert result is None


# ===========================================================================
# exit
# ===========================================================================


class TestExit:
    def test_full_success_path_returns_true(self) -> None:
        strat = _make_strategy()
        ctx = _make_ctx(strat)
        ctx.unified_executor.exit_trade.return_value = True
        pos = {"Ticker": "SPY", "OrderId": "xyz", "Qty": 2}

        result = ctx.executor.exit(strat.name, pos, {"reason": "test"})

        assert result is True
        ctx.unified_executor.exit_trade.assert_called_once_with(pos, 2.0)

    def test_returns_false_when_strategy_not_found(self) -> None:
        ctx = _make_ctx()  # no strategies
        pos = {"Ticker": "SPY", "OrderId": "xyz", "Qty": 1}

        result = ctx.executor.exit("NonExistentStrat", pos, {})

        assert result is False
        ctx.unified_executor.exit_trade.assert_not_called()

    def test_returns_false_when_set_client_fails(self) -> None:
        strat = _make_strategy()
        ctx = _make_ctx(strat, set_client_return=False)
        ctx.unified_executor.exit_trade.return_value = True
        pos = {"Ticker": "SPY", "OrderId": "xyz", "Qty": 1}

        result = ctx.executor.exit(strat.name, pos, {})

        assert result is False
        ctx.unified_executor.exit_trade.assert_not_called()

    def test_exit_failure_returns_false(self) -> None:
        strat = _make_strategy()
        ctx = _make_ctx(strat)
        ctx.unified_executor.exit_trade.return_value = False
        pos = {"Ticker": "SPY", "OrderId": "xyz", "Qty": 1}

        result = ctx.executor.exit(strat.name, pos, {})

        assert result is False


# ===========================================================================
# after_entry
# ===========================================================================


class TestAfterEntry:
    def test_appends_position_and_saves_state(self) -> None:
        ctx = _make_ctx()
        ctx.active_positions["Strat_paper"] = []
        pos = {"Ticker": "SPY", "OrderId": "new-1"}

        ctx.executor.after_entry("Strat_paper", pos)

        assert pos in ctx.active_positions["Strat_paper"]
        ctx.save_state.assert_called_once()

    def test_multiple_after_entry_calls_append_all(self) -> None:
        ctx = _make_ctx()
        ctx.active_positions["Strat_paper"] = []

        ctx.executor.after_entry("Strat_paper", {"OrderId": "1"})
        ctx.executor.after_entry("Strat_paper", {"OrderId": "2"})

        assert len(ctx.active_positions["Strat_paper"]) == 2


# ===========================================================================
# after_exit
# ===========================================================================


class TestAfterExit:
    def test_removes_position_and_saves_state(self) -> None:
        ctx = _make_ctx()
        pos = {"Ticker": "SPY", "OrderId": "del-1"}
        ctx.active_positions["Strat_paper"] = [pos]

        ctx.executor.after_exit("Strat_paper", pos)

        assert pos not in ctx.active_positions["Strat_paper"]
        ctx.save_state.assert_called_once()

    def test_absent_position_still_saves_state(self) -> None:
        """after_exit with a position not in the list still calls save_state."""
        ctx = _make_ctx()
        ctx.active_positions["Strat_paper"] = []
        pos = {"Ticker": "SPY", "OrderId": "ghost"}

        ctx.executor.after_exit("Strat_paper", pos)

        ctx.save_state.assert_called_once()

    def test_other_positions_not_removed(self) -> None:
        ctx = _make_ctx()
        pos_a = {"OrderId": "a"}
        pos_b = {"OrderId": "b"}
        ctx.active_positions["Strat_paper"] = [pos_a, pos_b]

        ctx.executor.after_exit("Strat_paper", pos_a)

        assert pos_b in ctx.active_positions["Strat_paper"]
        assert pos_a not in ctx.active_positions["Strat_paper"]


# ===========================================================================
# record_run
# ===========================================================================


class TestRecordRun:
    def test_stamps_last_update_when_ran_signal_true(self) -> None:
        ctx = _make_ctx()
        now = datetime.datetime(2024, 6, 1, 12, 0, tzinfo=datetime.UTC)

        ctx.executor.record_run("Strat_paper", now, ran_signal=True)

        assert ctx.last_update["Strat_paper"] == now

    def test_skips_last_update_when_ran_signal_false(self) -> None:
        ctx = _make_ctx()
        now = datetime.datetime(2024, 6, 1, 12, 0, tzinfo=datetime.UTC)

        ctx.executor.record_run("Strat_paper", now, ran_signal=False)

        assert "Strat_paper" not in ctx.last_update

    def test_always_stamps_last_manage_update(self) -> None:
        ctx = _make_ctx()
        now = datetime.datetime(2024, 6, 1, 12, 0, tzinfo=datetime.UTC)

        ctx.executor.record_run("Strat_paper", now, ran_signal=False)

        assert ctx.last_manage_update["Strat_paper"] == now

    def test_both_stamped_when_ran_signal_true(self) -> None:
        ctx = _make_ctx()
        now = datetime.datetime(2024, 6, 1, 12, 0, tzinfo=datetime.UTC)

        ctx.executor.record_run("Strat_paper", now, ran_signal=True)

        assert ctx.last_update["Strat_paper"] == now
        assert ctx.last_manage_update["Strat_paper"] == now


# ===========================================================================
# should_run — bar-driven (closed_bar_asof advances)
# ===========================================================================


class TestShouldRunBarDriven:
    """Fire when closed_bar_asof advances past the last recorded run's bar."""

    def test_fires_on_first_tick_with_no_prior_run(self) -> None:
        strat = _make_strat_config(interval="15m")
        ctx = _make_ctx(strat, delay_minutes=0)
        now = datetime.datetime(2024, 6, 1, 14, 20, 0, tzinfo=datetime.UTC)

        should_signal, should_manage = ctx.executor.should_run(strat, now)

        assert should_signal is True
        assert should_manage is True

    def test_suppresses_when_same_closed_bar(self) -> None:
        strat = _make_strat_config(interval="15m")
        ctx = _make_ctx(strat, delay_minutes=0)
        # Last run during the 14:00 bar window (closed at 14:15; still current at 14:20)
        now = datetime.datetime(2024, 6, 1, 14, 20, 0, tzinfo=datetime.UTC)
        ctx.last_update[strat.name] = datetime.datetime(
            2024, 6, 1, 14, 16, 0, tzinfo=datetime.UTC
        )

        should_signal, _ = ctx.executor.should_run(strat, now)

        assert should_signal is False

    def test_fires_when_new_bar_closes(self) -> None:
        strat = _make_strat_config(interval="15m")
        ctx = _make_ctx(strat, delay_minutes=0)
        # Prior run saw 14:00 bar; now past 14:30 close → 14:15 bar is new
        now = datetime.datetime(2024, 6, 1, 14, 30, 0, tzinfo=datetime.UTC)
        ctx.last_update[strat.name] = datetime.datetime(
            2024, 6, 1, 14, 16, 0, tzinfo=datetime.UTC
        )

        should_signal, _ = ctx.executor.should_run(strat, now)

        assert should_signal is True

    def test_config_data_delay_zero_vs_fallback(self) -> None:
        """strategy.config.DATA_DELAY=0 must win over executor delay_minutes=15."""
        strat = _make_strat_config(interval="15m", DATA_DELAY=0)
        ctx = _make_ctx(strat, delay_minutes=15)
        now = datetime.datetime(2024, 6, 1, 14, 20, 0, tzinfo=datetime.UTC)

        # With DATA_DELAY=0, end_dt=now → closed bar 14:00
        # With delay 15, end_dt=14:05 → closed bar 13:45 — different.
        # First tick always fires either way; assert config is read by checking
        # suppression after a run stamped under delay=0 semantics.
        ctx.last_update[strat.name] = now - datetime.timedelta(minutes=1)
        should_signal, _ = ctx.executor.should_run(strat, now)
        assert should_signal is False  # same closed bar under DATA_DELAY=0

    def test_naive_last_run_handled(self) -> None:
        strat = _make_strat_config(interval="15m")
        ctx = _make_ctx(strat, delay_minutes=0)
        now = datetime.datetime(2024, 6, 1, 14, 20, 0, tzinfo=datetime.UTC)
        ctx.last_update[strat.name] = datetime.datetime(2024, 6, 1, 14, 16, 0)

        should_signal, _ = ctx.executor.should_run(strat, now)

        assert should_signal is False


class TestShouldRunDailyBarDriven:
    """Daily strategies use closed_bar_asof (completed session), not IC_ENTRY_*."""

    def test_fires_when_prior_day_bar_newly_available(self) -> None:
        strat = _make_strat_config(interval="1d", DATA_DELAY=0)
        ctx = _make_ctx(strat, delay_minutes=0)
        # Mid-session: closed daily bar is prior calendar day
        now = datetime.datetime(2024, 6, 3, 15, 0, 0, tzinfo=datetime.UTC)

        should_signal, _ = ctx.executor.should_run(strat, now)

        assert should_signal is True

    def test_ic_entry_ignored(self) -> None:
        strat = _make_strat_config(
            interval="1d",
            DATA_DELAY=0,
            IC_ENTRY_HOUR_ET=15,
            IC_ENTRY_MINUTE_ET=45,
        )
        ctx = _make_ctx(strat, delay_minutes=0)
        now = datetime.datetime(2024, 6, 3, 14, 0, 0, tzinfo=datetime.UTC)

        should_signal, _ = ctx.executor.should_run(strat, now)

        assert should_signal is True


class TestShouldRunManageInterval:
    """Manage-interval bar-driven scheduling."""

    def test_manage_mirrors_signal_when_intervals_equal(self) -> None:
        strat = _make_strat_config(interval="15m", manage_interval="15m")
        ctx = _make_ctx(strat, delay_minutes=0)
        now = datetime.datetime(2024, 6, 1, 14, 20, 0, tzinfo=datetime.UTC)

        should_signal, should_manage = ctx.executor.should_run(strat, now)

        assert should_signal == should_manage

    def test_manage_fires_independently_on_finer_bar(self) -> None:
        """5m manage can advance while 1h signal bar is unchanged."""
        strat = _make_strat_config(interval="1h", manage_interval="5m", DATA_DELAY=0)
        ctx = _make_ctx(strat, delay_minutes=0)
        now = datetime.datetime(2024, 6, 1, 14, 25, 0, tzinfo=datetime.UTC)
        # Last signal run saw the 13:00 1h bar; still current at 14:25
        # (1h bar 14:00 closes at 15:00).
        ctx.last_update[strat.name] = datetime.datetime(
            2024, 6, 1, 14, 5, 0, tzinfo=datetime.UTC
        )
        # Last manage saw 14:15 5m bar; at 14:25 the 14:20 bar is new.
        ctx.last_manage_update[strat.name] = datetime.datetime(
            2024, 6, 1, 14, 16, 0, tzinfo=datetime.UTC
        )

        should_signal, should_manage = ctx.executor.should_run(strat, now)

        assert should_signal is False
        assert should_manage is True

    def test_manage_dedup_same_bar(self) -> None:
        strat = _make_strat_config(interval="1h", manage_interval="5m", DATA_DELAY=0)
        ctx = _make_ctx(strat, delay_minutes=0)
        now = datetime.datetime(2024, 6, 1, 14, 22, 0, tzinfo=datetime.UTC)
        ctx.last_update[strat.name] = now - datetime.timedelta(minutes=10)
        ctx.last_manage_update[strat.name] = now - datetime.timedelta(minutes=1)

        _, should_manage = ctx.executor.should_run(strat, now)

        assert should_manage is False
