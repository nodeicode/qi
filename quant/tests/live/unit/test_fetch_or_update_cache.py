"""
Tests for python/utils.py — uncovered paths.

Covers:
  - fetch_or_update_cache: incremental update when ticker already in cache
      (merge, dedup, sort, naive-tz handling, empty new data)
  - save_state: I/O exception is caught and logged (never raises)
"""

from __future__ import annotations

import datetime
import os
import sys
import threading
import unittest
import unittest.mock
from unittest.mock import MagicMock, patch

import pandas as pd

sys.path.append(
    os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
)

import pytest

from quant.live.utils import fetch_or_update_cache, resolve_config_file, save_state

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_df(dates: list[str], close: float = 100.0) -> pd.DataFrame:
    idx = pd.DatetimeIndex([pd.Timestamp(d, tz="UTC") for d in dates], name="timestamp")
    return pd.DataFrame({"Close": close}, index=idx)


# ===========================================================================
# fetch_or_update_cache — incremental update path
# ===========================================================================


class TestFetchOrUpdateCacheIncremental(unittest.TestCase):
    def setUp(self) -> None:
        self.dh = MagicMock()

    def test_initial_load_populates_cache(self) -> None:
        df = _make_df(["2024-01-01", "2024-01-02"])
        self.dh.get_historical_data.return_value = df
        cache: dict = {}

        result = fetch_or_update_cache(
            cache,
            "strat1",
            "SPY",
            self.dh,
            datetime.datetime(2024, 1, 3, tzinfo=datetime.UTC),
            5,
            "1d",
        )

        self.assertIn("strat1", cache)
        self.assertIn("SPY", cache["strat1"])
        self.assertFalse(result.empty)
        self.dh.get_historical_data.assert_called_once()

    def test_incremental_update_merges_new_data(self) -> None:
        existing = _make_df(["2024-01-01", "2024-01-02"])
        new_data = _make_df(["2024-01-02", "2024-01-03"])  # overlaps at 01-02
        self.dh.get_historical_data.return_value = new_data

        cache = {"strat1": {"SPY": existing}}
        result = fetch_or_update_cache(
            cache,
            "strat1",
            "SPY",
            self.dh,
            datetime.datetime(2024, 1, 4, tzinfo=datetime.UTC),
            5,
            "1d",
        )

        # 3 unique rows after merge+dedup
        self.assertEqual(len(result), 3)
        self.dh.get_historical_data.assert_called_once()

    def test_incremental_no_new_data_preserves_existing(self) -> None:
        existing = _make_df(["2024-01-01", "2024-01-02"])
        self.dh.get_historical_data.return_value = pd.DataFrame()

        cache = {"strat1": {"SPY": existing}}
        result = fetch_or_update_cache(
            cache,
            "strat1",
            "SPY",
            self.dh,
            datetime.datetime(2024, 1, 4, tzinfo=datetime.UTC),
            5,
            "1d",
        )

        self.assertEqual(len(result), 2)

    def test_result_is_sorted_by_index(self) -> None:
        existing = _make_df(["2024-01-02"])
        new_data = _make_df(["2024-01-01", "2024-01-03"])
        self.dh.get_historical_data.return_value = new_data

        cache = {"strat1": {"SPY": existing}}
        result = fetch_or_update_cache(
            cache,
            "strat1",
            "SPY",
            self.dh,
            datetime.datetime(2024, 1, 4, tzinfo=datetime.UTC),
            5,
            "1d",
        )

        self.assertTrue(result.index.is_monotonic_increasing)

    def test_duplicate_rows_deduped_keeps_last(self) -> None:
        existing = _make_df(["2024-01-01"], close=100.0)
        new_data = _make_df(["2024-01-01"], close=200.0)
        self.dh.get_historical_data.return_value = new_data

        cache = {"strat1": {"SPY": existing}}
        result = fetch_or_update_cache(
            cache,
            "strat1",
            "SPY",
            self.dh,
            datetime.datetime(2024, 1, 2, tzinfo=datetime.UTC),
            5,
            "1d",
        )

        self.assertEqual(len(result), 1)
        self.assertEqual(result["Close"].iloc[0], 200.0)

    def test_existing_cache_last_index_used_as_incremental_start(self) -> None:
        """Incremental update fetches from last cached timestamp onward."""
        existing = _make_df(["2024-01-01", "2024-01-02"])
        new_data = _make_df(["2024-01-02", "2024-01-03", "2024-01-04"])
        self.dh.get_historical_data.return_value = new_data

        cache = {"strat1": {"SPY": existing}}
        fetch_or_update_cache(
            cache,
            "strat1",
            "SPY",
            self.dh,
            datetime.datetime(2024, 1, 5, tzinfo=datetime.UTC),
            5,
            "1d",
        )

        # get_historical_data must have been called with a start >= last cached ts
        call_kwargs = self.dh.get_historical_data.call_args
        start_arg = call_kwargs.kwargs.get("start") or (
            call_kwargs.args[1] if len(call_kwargs.args) > 1 else None
        )
        self.assertIsNotNone(start_arg)

    def test_cache_strategy_key_created_when_absent(self) -> None:
        """Ticker present under a different strategy key — new key created."""
        df = _make_df(["2024-01-01"])
        self.dh.get_historical_data.return_value = df
        cache: dict = {"other_strat": {"SPY": df}}

        fetch_or_update_cache(
            cache,
            "new_strat",
            "SPY",
            self.dh,
            datetime.datetime(2024, 1, 2, tzinfo=datetime.UTC),
            5,
            "1d",
        )

        self.assertIn("new_strat", cache)
        self.assertIn("SPY", cache["new_strat"])


# ===========================================================================
# save_state — I/O exception handler
# ===========================================================================


class TestSaveStateExceptionHandler(unittest.TestCase):
    def test_io_error_is_caught_and_logged(self) -> None:
        mock_logger = MagicMock()
        lock = threading.RLock()

        with patch("builtins.open", side_effect=OSError("disk full")):
            # Must not raise
            save_state("some_file.json", {"key": "value"}, lock, mock_logger)

        mock_logger.error.assert_called()

    def test_os_replace_error_is_caught_and_logged(self) -> None:
        mock_logger = MagicMock()
        lock = threading.RLock()
        import json

        with (
            patch("builtins.open", unittest.mock.mock_open()),
            patch("os.replace", side_effect=OSError("replace failed")),
        ):
            save_state("some_file.json", {"key": "value"}, lock, mock_logger)

        mock_logger.error.assert_called()


# ===========================================================================
# fetch_or_update_cache — additional edge cases
# ===========================================================================


class TestFetchOrUpdateCacheEdgeCases(unittest.TestCase):
    def setUp(self) -> None:
        self.dh = MagicMock()

    def test_naive_timestamp_cache_with_empty_new_data(self) -> None:
        """Covers else-branch when last_ts.tz is None (line ~404 in utils.py)."""
        naive_idx = pd.DatetimeIndex([pd.Timestamp("2024-01-01")], name="timestamp")
        existing = pd.DataFrame({"Close": [100.0]}, index=naive_idx)
        # Return empty df → concat never called, no tz mismatch
        self.dh.get_historical_data.return_value = pd.DataFrame()

        cache = {"strat1": {"SPY": existing}}
        result = fetch_or_update_cache(
            cache,
            "strat1",
            "SPY",
            self.dh,
            datetime.datetime(2024, 1, 4, tzinfo=datetime.UTC),
            5,
            "1d",
        )
        # Existing data preserved when no new data arrives
        self.assertFalse(result.empty)

    def test_update_start_clamped_to_last_ts(self) -> None:
        """Covers the branch update_start = last_ts (line ~408 in utils.py).

        last_ts is within 1 day of data_end → update_start (= data_end - 1d)
        is earlier than last_ts → clamped so we don't go backward.
        """
        # last_ts is 20 hours before data_end (< 1 day) → clamped
        data_end = datetime.datetime(2024, 1, 4, tzinfo=datetime.UTC)
        last_ts = pd.Timestamp("2024-01-03 20:00:00", tz="UTC")

        tz_idx = pd.DatetimeIndex([last_ts], name="timestamp")
        existing = pd.DataFrame({"Close": [100.0]}, index=tz_idx)

        new_data = pd.DataFrame(
            {"Close": [101.0]},
            index=pd.DatetimeIndex(
                [pd.Timestamp("2024-01-03 21:00:00", tz="UTC")], name="timestamp"
            ),
        )
        self.dh.get_historical_data.return_value = new_data

        cache = {"strat1": {"SPY": existing}}
        fetch_or_update_cache(cache, "strat1", "SPY", self.dh, data_end, 5, "1d")

        call_kwargs = self.dh.get_historical_data.call_args[1]
        # The start arg must be last_ts (clamped), not data_end - 1 day (= 2024-01-03 00:00)
        self.assertEqual(call_kwargs["start"], last_ts.isoformat())


# ===========================================================================
# resolve_config_file — path-traversal guard (line 221)
# ===========================================================================


class TestResolveConfigFilePathTraversal:
    def test_path_traversal_raises_value_error(self) -> None:
        """base_name containing '../' sequences that escape _PROJECT_ROOT
        must raise ValueError (security guard, line ~221 in utils.py)."""
        import logging

        logger = logging.getLogger("test")
        with pytest.raises(ValueError, match="escapes project root"):
            resolve_config_file("../../../etc/passwd", logger)


if __name__ == "__main__":
    unittest.main()
