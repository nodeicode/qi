"""
Tests for python/market_schedule.py

All Alpaca API calls are mocked — no network calls.
Real pandas is used for timezone conversion (pure computation, no I/O).

Covers:
  - __init__: default market hours and flags
  - update_market_hours: no client → early return, state unchanged
  - update_market_hours: same date → skips API call, state unchanged
  - update_market_hours: empty calendar → is_market_open_today = False
  - update_market_hours: calendar with time objects → UTC hours updated
  - update_market_hours: calendar with datetime objects → .time() extracted
  - update_market_hours: force_close_time is 15 min before close
  - update_market_hours: current_trading_date updated after success
  - update_market_hours: API exception → state unchanged
  - is_market_open: market not open today → False
  - is_market_open: now within hours → True
  - is_market_open: now before open → False
  - is_market_open: now after close → False
  - is_market_open: boundary conditions (exactly at open / close)
  - is_market_open: no explicit now → uses real UTC clock
"""

from __future__ import annotations

# Alpaca is stubbed per-test by the autouse fixture in quant/tests/live/conftest.py.
# ---------------------------------------------------------------------------
# Imports under test
# ---------------------------------------------------------------------------
import datetime
import logging
import sys
from types import SimpleNamespace
from unittest.mock import MagicMock

import pandas as pd

from quant.live.market_schedule import MarketSchedule

_logger = logging.getLogger("test_market_schedule")


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_calendar(
    date: datetime.date,
    open_time: datetime.time = datetime.time(9, 30),
    close_time: datetime.time = datetime.time(16, 0),
) -> SimpleNamespace:
    """Return a fake Alpaca Calendar entry mimicking the SDK's datetime fields."""
    return SimpleNamespace(
        date=date,
        open=datetime.datetime.combine(date, open_time),
        close=datetime.datetime.combine(date, close_time),
    )


def _mock_client_returning(calendars: list) -> MagicMock:
    client = MagicMock()
    client.get_calendar.return_value = calendars
    return client


# ===========================================================================
# __init__
# ===========================================================================


class TestMarketScheduleInit:
    def test_default_market_open_time(self) -> None:
        ms = MarketSchedule()

        assert ms.market_open_time == datetime.time(13, 30)

    def test_default_market_close_time(self) -> None:
        ms = MarketSchedule()

        assert ms.market_close_time == datetime.time(20, 0)

    def test_default_force_close_time(self) -> None:
        ms = MarketSchedule()

        assert ms.force_close_time == datetime.time(19, 45)

    def test_not_open_today_by_default(self) -> None:
        ms = MarketSchedule()

        assert ms.is_market_open_today is False

    def test_current_trading_date_none_by_default(self) -> None:
        ms = MarketSchedule()

        assert ms.current_trading_date is None

    def test_custom_logger_stored(self) -> None:
        custom = logging.getLogger("custom_test")
        ms = MarketSchedule(logger=custom)

        assert ms.logger is custom

    def test_default_logger_created_when_none(self) -> None:
        ms = MarketSchedule()

        assert ms.logger is not None
        assert isinstance(ms.logger, logging.Logger)


# ===========================================================================
# update_market_hours
# ===========================================================================


class TestUpdateMarketHours:
    def test_no_client_returns_early_state_unchanged(self) -> None:
        ms = MarketSchedule()
        ms.update_market_hours(None)

        assert ms.is_market_open_today is False
        assert ms.current_trading_date is None
        assert ms.market_open_time == datetime.time(13, 30)

    def test_same_date_skips_api_call(self) -> None:
        today = pd.Timestamp.now(tz="America/New_York").date()
        ms = MarketSchedule()
        ms.current_trading_date = today
        ms.is_market_open_today = True
        mock_client = _mock_client_returning([])

        ms.update_market_hours(mock_client)

        mock_client.get_calendar.assert_not_called()
        assert ms.is_market_open_today is True  # unchanged

    def test_empty_calendar_marks_closed(self) -> None:
        ms = MarketSchedule()
        ms.current_trading_date = datetime.date(2000, 1, 1)

        ms.update_market_hours(_mock_client_returning([]))

        assert ms.is_market_open_today is False

    def test_valid_calendar_updates_hours(self) -> None:
        ms = MarketSchedule()
        ms.current_trading_date = datetime.date(2000, 1, 1)
        cal = _make_calendar(datetime.date(2025, 1, 15))

        ms.update_market_hours(_mock_client_returning([cal]))

        assert ms.is_market_open_today is True
        assert isinstance(ms.market_open_time, datetime.time)
        assert isinstance(ms.market_close_time, datetime.time)
        # 9:30 ET (EST = UTC-5) → 14:30 UTC; EDT = UTC-4 → 13:30 UTC
        assert datetime.time(13, 0) <= ms.market_open_time <= datetime.time(15, 0)

    def test_valid_calendar_datetime_objects_extracts_time(self) -> None:
        """Calendar.open/close are always datetime in the Alpaca SDK."""
        ms = MarketSchedule()
        ms.current_trading_date = datetime.date(2000, 1, 1)
        cal = _make_calendar(datetime.date(2025, 1, 15))

        ms.update_market_hours(_mock_client_returning([cal]))

        assert ms.is_market_open_today is True
        assert isinstance(ms.market_open_time, datetime.time)

    def test_force_close_is_15_min_before_market_close(self) -> None:
        ms = MarketSchedule()
        ms.current_trading_date = datetime.date(2000, 1, 1)
        cal = _make_calendar(datetime.date(2025, 1, 15))

        ms.update_market_hours(_mock_client_returning([cal]))

        close_dt = datetime.datetime.combine(
            datetime.date.today(), ms.market_close_time
        )
        force_dt = datetime.datetime.combine(datetime.date.today(), ms.force_close_time)
        assert close_dt - force_dt == datetime.timedelta(minutes=15)

    def test_current_trading_date_updated_after_successful_call(self) -> None:
        ms = MarketSchedule()
        ms.current_trading_date = datetime.date(2000, 1, 1)
        cal = _make_calendar(datetime.date(2025, 1, 15))

        ms.update_market_hours(_mock_client_returning([cal]))

        # Should now equal today's NY date
        expected_today = pd.Timestamp.now(tz="America/New_York").date()
        assert ms.current_trading_date == expected_today

    def test_api_exception_leaves_state_unchanged(self) -> None:
        ms = MarketSchedule()
        original_open = ms.market_open_time
        mock_client = MagicMock()
        mock_client.get_calendar.side_effect = Exception("API error")

        ms.update_market_hours(mock_client)

        assert ms.market_open_time == original_open
        assert ms.is_market_open_today is False

    def test_different_date_triggers_new_api_call(self) -> None:
        yesterday = pd.Timestamp.now(tz="America/New_York").date() - datetime.timedelta(
            days=1
        )
        ms = MarketSchedule()
        ms.current_trading_date = yesterday
        cal = _make_calendar(datetime.date(2025, 1, 15))
        mock_client = _mock_client_returning([cal])

        ms.update_market_hours(mock_client)

        mock_client.get_calendar.assert_called_once()

    def test_empty_calendar_after_prior_open_day_marks_closed(self) -> None:
        """A holiday following a trading day: is_market_open_today flips to False."""
        ms = MarketSchedule()
        ms.current_trading_date = datetime.date(2000, 1, 1)
        ms.is_market_open_today = True

        ms.update_market_hours(_mock_client_returning([]))

        assert ms.is_market_open_today is False


# ===========================================================================
# is_market_open
# ===========================================================================


class TestIsMarketOpen:
    def _open_schedule(self) -> MarketSchedule:
        """Return a MarketSchedule pre-set as open today so update is skipped."""
        ms = MarketSchedule()
        ms.current_trading_date = pd.Timestamp.now(tz="America/New_York").date()
        ms.is_market_open_today = True
        ms.market_open_time = datetime.time(13, 30)  # 13:30 UTC
        ms.market_close_time = datetime.time(20, 0)  # 20:00 UTC
        return ms

    def test_market_not_open_today_returns_false(self) -> None:
        ms = MarketSchedule()
        ms.current_trading_date = pd.Timestamp.now(tz="America/New_York").date()
        ms.is_market_open_today = False
        now = datetime.datetime(2025, 1, 15, 15, 0, tzinfo=datetime.UTC)

        assert ms.is_market_open(MagicMock(), now=now) is False

    def test_now_within_hours_returns_true(self) -> None:
        ms = self._open_schedule()
        now = datetime.datetime(2025, 1, 15, 15, 0, tzinfo=datetime.UTC)  # midday

        assert ms.is_market_open(MagicMock(), now=now) is True

    def test_now_before_open_returns_false(self) -> None:
        ms = self._open_schedule()
        now = datetime.datetime(2025, 1, 15, 12, 0, tzinfo=datetime.UTC)  # before 13:30

        assert ms.is_market_open(MagicMock(), now=now) is False

    def test_now_after_close_returns_false(self) -> None:
        ms = self._open_schedule()
        now = datetime.datetime(2025, 1, 15, 21, 0, tzinfo=datetime.UTC)  # after 20:00

        assert ms.is_market_open(MagicMock(), now=now) is False

    def test_exactly_at_open_returns_true(self) -> None:
        ms = self._open_schedule()
        now = datetime.datetime(2025, 1, 15, 13, 30, 0, tzinfo=datetime.UTC)

        assert ms.is_market_open(MagicMock(), now=now) is True

    def test_exactly_at_close_returns_true(self) -> None:
        ms = self._open_schedule()
        now = datetime.datetime(2025, 1, 15, 20, 0, 0, tzinfo=datetime.UTC)

        assert ms.is_market_open(MagicMock(), now=now) is True

    def test_one_second_before_open_returns_false(self) -> None:
        ms = self._open_schedule()
        now = datetime.datetime(2025, 1, 15, 13, 29, 59, tzinfo=datetime.UTC)

        assert ms.is_market_open(MagicMock(), now=now) is False

    def test_one_second_after_close_returns_false(self) -> None:
        ms = self._open_schedule()
        now = datetime.datetime(2025, 1, 15, 20, 0, 1, tzinfo=datetime.UTC)

        assert ms.is_market_open(MagicMock(), now=now) is False

    def test_no_explicit_now_uses_real_clock(self) -> None:
        ms = self._open_schedule()

        result = ms.is_market_open(MagicMock())

        assert isinstance(result, bool)
