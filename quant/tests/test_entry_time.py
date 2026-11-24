"""Tests for shared calculate_entry_time (T+I for all assets)."""

from __future__ import annotations

import zoneinfo

import pandas as pd

from quant.utils.time import calculate_entry_time, et_wall_to_utc_naive

_ET = zoneinfo.ZoneInfo("America/New_York")


def _utc_naive_from_et(year, month, day, hour, minute) -> pd.Timestamp:
    ts = pd.Timestamp(year=year, month=month, day=day, hour=hour, minute=minute, tz=_ET)
    return ts.tz_convert("UTC").tz_localize(None)


def test_15m_entry_is_bar_close():
    signal = _utc_naive_from_et(2024, 6, 3, 10, 0)
    assert calculate_entry_time(signal, "15m") == _utc_naive_from_et(2024, 6, 3, 10, 15)


def test_1h_entry_is_bar_close():
    signal = _utc_naive_from_et(2024, 6, 3, 10, 0)
    assert calculate_entry_time(signal, "1h") == _utc_naive_from_et(2024, 6, 3, 11, 0)


def test_1d_entry_is_next_bday_market_open_edt():
    # Mon 2024-06-03 → Tue 2024-06-04 09:30 ET (EDT = UTC-4 → 13:30 UTC)
    signal = pd.Timestamp("2024-06-03")
    entry = calculate_entry_time(signal, "1d")
    assert entry == et_wall_to_utc_naive(pd.Timestamp("2024-06-04").date(), 9, 30)
    assert entry == pd.Timestamp("2024-06-04 13:30:00")


def test_1d_entry_is_next_bday_market_open_est():
    # Tue 2024-01-09 → Wed 2024-01-10 09:30 ET (EST = UTC-5 → 14:30 UTC)
    signal = pd.Timestamp("2024-01-09")
    entry = calculate_entry_time(signal, "1d")
    assert entry == et_wall_to_utc_naive(pd.Timestamp("2024-01-10").date(), 9, 30)
    assert entry == pd.Timestamp("2024-01-10 14:30:00")


def test_1d_skips_weekend_to_monday_open():
    # Fri 2024-06-07 → Mon 2024-06-10 09:30 ET
    signal = pd.Timestamp("2024-06-07")
    entry = calculate_entry_time(signal, "1d")
    assert entry == et_wall_to_utc_naive(pd.Timestamp("2024-06-10").date(), 9, 30)
