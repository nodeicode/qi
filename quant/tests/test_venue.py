"""Venue primitive tests (spec `venue`).

Covers: cohesive unit (identity/data/execution), holiday-not-open, rollover
recompute, calendar-unavailable fallback to defaults, the canonical naive-UTC
boundary, deterministic backtest timing, and registration/discovery.
"""

from __future__ import annotations

import datetime as _dt
import logging
from datetime import date, datetime, timezone

import pandas as pd
import pytest

from quant.venue import (
    AlpacaVenue,
    BacktestVenue,
    BaseVenue,
    SessionDay,
    to_utc_naive,
    venue_by_name,
)

DAY = date(2025, 1, 2)
NEXT_DAY = date(2025, 1, 3)
HOLIDAY = date(2025, 1, 1)


class FakeCalendar:
    """In-memory calendar: date -> (open_utc_naive, close_utc_naive) | None."""

    def __init__(self, windows: dict[date, tuple[datetime, datetime] | None]):
        self.windows = windows

    def open_close_for(self, on: date) -> tuple[datetime, datetime] | None:
        return self.windows.get(on)


def _d(day: date, hour: int, minute: int = 0) -> datetime:
    return datetime(day.year, day.month, day.day, hour, minute)


CAL = FakeCalendar(
    {
        DAY: (_d(DAY, 13, 30), _d(DAY, 21, 0)),
        NEXT_DAY: (_d(NEXT_DAY, 13, 30), _d(NEXT_DAY, 21, 0)),
        HOLIDAY: None,
    }
)


# ── cohesive unit ─────────────────────────────────────────────────────
def test_venue_is_cohesive_unit() -> None:
    ds = object()
    ec = object()
    venue = AlpacaVenue(data_source=ds, execution_client=ec, calendar=CAL)
    assert venue.id == "alpaca"
    assert venue.data_source is ds
    assert venue.execution_client is ec
    assert isinstance(venue.session_for(_d(DAY, 15, 0)), SessionDay)


# ── session / calendar ────────────────────────────────────────────────
def test_open_and_closed_windows() -> None:
    venue = AlpacaVenue(calendar=CAL)
    assert venue.is_open_at(_d(DAY, 15, 0)) is True
    assert venue.is_open_at(_d(DAY, 9, 0)) is False  # before open
    assert venue.is_open_at(_d(DAY, 22, 0)) is False  # after close


def test_holiday_not_open() -> None:
    venue = AlpacaVenue(calendar=CAL)
    s = venue.session_for(_d(HOLIDAY, 12, 0))
    assert s.open_on_date is False
    assert venue.is_open_at(_d(HOLIDAY, 12, 0)) is False


def test_rollover_recomputes() -> None:
    venue = AlpacaVenue(calendar=CAL)
    day1 = venue.session_for(_d(DAY, 15, 0))
    day2 = venue.session_for(_d(NEXT_DAY, 15, 0))
    assert day1.on == DAY
    assert day2.on == NEXT_DAY


def test_calendar_unavailable_falls_back_to_defaults() -> None:
    venue = AlpacaVenue(calendar=None)  # no calendar -> documented defaults
    s = venue.session_for(_d(DAY, 15, 0))
    assert s.open.hour == 13 and s.open.minute == 30
    assert s.close.hour == 20 and s.close.minute == 0
    assert s.forced_close.hour == 19 and s.forced_close.minute == 45


def test_fallback_logs_not_raises() -> None:
    class _Boom:
        def open_close_for(self, on):
            raise RuntimeError("calendar down")

    venue = AlpacaVenue(calendar=_Boom())
    s = venue.session_for(_d(DAY, 15, 0))  # must not raise
    assert isinstance(s, SessionDay)


# ── canonical naive-UTC boundary ──────────────────────────────────────
def test_to_utc_naive_boundary() -> None:
    aware = _d(DAY, 15, 0).replace(tzinfo=timezone(_dt.timedelta(hours=-5)))
    naive = to_utc_naive(aware)
    assert naive.tzinfo is None
    assert naive.hour == 20  # 15:00 ET == 20:00 UTC
    assert to_utc_naive(datetime(2025, 1, 2, 15, 0)) == datetime(2025, 1, 2, 15, 0)


def test_to_utc_naive_handles_pandas_timestamp() -> None:
    ts = pd.Timestamp("2025-01-02 20:00:00", tz="UTC")
    assert to_utc_naive(ts).tzinfo is None
    assert to_utc_naive(ts).hour == 20


# ── deterministic backtest venue ──────────────────────────────────────
def test_backtest_answers_from_simulation_now() -> None:
    venue = BacktestVenue(calendar=CAL)
    assert isinstance(venue, BaseVenue)
    # same simulated now -> same answer, no wall-clock dependency
    first = venue.is_open_at(_d(DAY, 15, 0))
    second = venue.is_open_at(_d(DAY, 15, 0))
    assert first is second is True
    assert venue.is_open_at(_d(DAY, 9, 0)) is False


def test_backtest_honors_closed_day_in_timeline() -> None:
    venue = BacktestVenue(calendar=CAL)
    assert venue.is_open_at(_d(HOLIDAY, 12, 0)) is False


def test_backtest_defaults_open_without_calendar() -> None:
    # No calendar: the simulation timeline is authoritative -> open in-hours.
    venue = BacktestVenue()
    assert venue.is_open_at(datetime(2025, 6, 3, 15, 0)) is True


# ── registration / discovery ──────────────────────────────────────────
def test_venue_by_name_resolves_builtins() -> None:
    assert isinstance(venue_by_name("backtest"), BacktestVenue)
    assert isinstance(venue_by_name("alpaca"), AlpacaVenue)


def test_unknown_venue_rejected_with_names() -> None:
    with pytest.raises(ValueError, match="Unknown venue"):
        venue_by_name("nope")


# ── driver resolution (spec `execution` Venue resolved from the strategy declaration) ──
def test_backtester_rejects_unknown_venue_at_construction() -> None:
    """The backtest driver rejects an unregistered declared venue id loudly."""
    from dataclasses import dataclass, field

    from quant.backtest.backtester import Backtester
    from quant.utils.strategy_config import BaseStrategyConfig

    @dataclass
    class _Cfg(BaseStrategyConfig):
        PRE_LOAD_DAYS: int = 5
        INITIAL_CAPITAL: float = 100_000.0
        MAX_CONCURRENT_POSITIONS: int = 1
        STOP_LOSS_PERCENT: float = 0.05
        TAKE_PROFIT_PERCENT: float = -1.0
        USE_TRAIL_LOSS: bool = False
        MAX_HOLDING_MINUTES: int = 60
        MIN_SIGNAL_CONFIDENCE: float = 0.0
        BASE_SLIPPAGE_FACTOR: float = 0.001
        TICKERS: list = field(default_factory=lambda: ["SPY"])

    class _Strategy:
        name = "s"

        def __init__(self) -> None:
            self.config = _Cfg()
            self.venue = "nope"

    with pytest.raises(ValueError, match="Unknown venue 'nope'"):
        Backtester(
            _Cfg(),
            _Strategy(),
            "2024-01-02",
            "2024-06-28",
            "SPY",
            data_handler=object(),
        )


def test_backtester_resolves_backtest_venue_with_wiring() -> None:
    """The backtest driver resolves the default to a wired ``BacktestVenue``."""
    from dataclasses import dataclass, field

    from quant.backtest.backtester import Backtester
    from quant.utils.strategy_config import BaseStrategyConfig
    from quant.venue import BacktestVenue

    @dataclass
    class _Cfg(BaseStrategyConfig):
        PRE_LOAD_DAYS: int = 5
        INITIAL_CAPITAL: float = 100_000.0
        MAX_CONCURRENT_POSITIONS: int = 1
        STOP_LOSS_PERCENT: float = 0.05
        TAKE_PROFIT_PERCENT: float = -1.0
        USE_TRAIL_LOSS: bool = False
        MAX_HOLDING_MINUTES: int = 60
        MIN_SIGNAL_CONFIDENCE: float = 0.0
        BASE_SLIPPAGE_FACTOR: float = 0.001
        TICKERS: list = field(default_factory=lambda: ["SPY"])

    strategy = type("S", (), {})()
    strategy.config = _Cfg()
    strategy.name = "s"

    bt = Backtester(
        _Cfg(),
        strategy,
        "2024-01-02",
        "2024-06-28",
        "SPY",
        data_handler=object(),
    )
    assert isinstance(bt._venue, BacktestVenue)
    assert bt.venue == "backtest"
