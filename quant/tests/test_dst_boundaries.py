"""DST regression tests for the canonical entry/fill clock.

Guards the end-to-end invariant: an entry/signal wall at a fixed ET time
must map to the correct naive-UTC stamp across the US DST boundary.

* ``utils.time.et_wall_to_utc_naive`` — pure ET-wall → naive-UTC mapping
  (10:15 ET → 14:15 UTC EDT / 15:15 UTC EST).
* ``backtest.utils.calculate_fill_time`` — the backtest fill clock
  (``T + I + DATA_DELAY``) on the 09:45 ET signal bar → 10:15 ET fill:
  13:45 → 14:15 UTC (EDT) and 14:45 → 15:15 UTC (EST).
"""

from __future__ import annotations

import datetime

import pandas as pd
import pytest

from quant.backtest.utils import calculate_fill_time
from quant.utils.time import et_wall_to_utc_naive

# US DST boundaries in 2023: starts Mar 12 02:00, ends Nov 05 02:00.
_EST_DATE = datetime.date(2023, 1, 10)
_EDT_DATE = datetime.date(2023, 6, 1)
_DST_START_PRE = datetime.date(2023, 3, 10)  # EST
_DST_START_POST = datetime.date(2023, 3, 13)  # EDT
_DST_END_PRE = datetime.date(2023, 11, 3)  # EDT
_DST_END_POST = datetime.date(2023, 11, 6)  # EST


@pytest.mark.parametrize(
    ("day", "expected_utc"),
    [
        (_EST_DATE, "2023-01-10 15:15:00"),  # 10:15 ET = 15:15 UTC (EST)
        (_EDT_DATE, "2023-06-01 14:15:00"),  # 10:15 ET = 14:15 UTC (EDT)
        (_DST_START_PRE, "2023-03-10 15:15:00"),
        (_DST_START_POST, "2023-03-13 14:15:00"),
        (_DST_END_PRE, "2023-11-03 14:15:00"),
        (_DST_END_POST, "2023-11-06 15:15:00"),
    ],
)
def test_et_wall_to_utc_naive_across_dst(day, expected_utc):
    out = et_wall_to_utc_naive(day, hour=10, minute=15)
    assert out == pd.Timestamp(expected_utc)
    assert out.tzinfo is None  # canonical naive-UTC


@pytest.mark.parametrize(
    ("bar_open_utc", "expected_fill_utc"),
    [
        # 09:45 ET signal bar, 15m interval, DATA_DELAY=15 → fill 10:15 ET.
        ("2023-01-10 14:45:00", "2023-01-10 15:15:00"),  # EST
        ("2023-06-01 13:45:00", "2023-06-01 14:15:00"),  # EDT
        ("2023-03-10 14:45:00", "2023-03-10 15:15:00"),
        ("2023-03-13 13:45:00", "2023-03-13 14:15:00"),
        ("2023-11-03 13:45:00", "2023-11-03 14:15:00"),
        ("2023-11-06 14:45:00", "2023-11-06 15:15:00"),
    ],
)
def test_calculate_fill_time_across_dst(bar_open_utc, expected_fill_utc):
    out = calculate_fill_time(pd.Timestamp(bar_open_utc), "15m", 15)
    assert out == pd.Timestamp(expected_fill_utc)
    assert out.tzinfo is None


def test_calculate_fill_time_no_delay_is_entry_time():
    out = calculate_fill_time(pd.Timestamp("2023-06-01 13:45:00"), "15m", 0)
    assert out == pd.Timestamp("2023-06-01 14:00:00")
    assert out.tzinfo is None
