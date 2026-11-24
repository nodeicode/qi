"""IC CBBO exact-timestamp policy for realistic fills."""

from __future__ import annotations

import datetime
from unittest.mock import MagicMock

import pandas as pd
import pytest


@pytest.fixture(autouse=True)
def _stub_env(monkeypatch):
    monkeypatch.setenv("DATABENTO_API_KEY", "test_databento_key")


@pytest.fixture()
def handler(monkeypatch, tmp_path):
    monkeypatch.setattr("quant.data.options_data_handler.db.Historical", MagicMock)
    from quant.data.options_data_handler import OptionDataHandler

    return OptionDataHandler(cache_dir=str(tmp_path / "opt_cache"))


def test_post_target_only_cache_returns_missing_leg(handler, monkeypatch):
    """A future-only quote is never used and leaves the leg unavailable."""
    idx = pd.date_range("2025-06-20 14:01", periods=3, freq="1min", tz="UTC")
    cache = pd.DataFrame(
        {
            "bid": [1.0, 1.01, 1.02],
            "ask": [1.20, 1.21, 1.22],
            "mid": [1.10, 1.11, 1.12],
        },
        index=idx,
    )
    monkeypatch.setattr(handler._cache, "get_superset", lambda *a, **k: cache)
    at = datetime.datetime(2025, 6, 20, 14, 0, tzinfo=datetime.UTC)
    out = handler.prefetch_ic_cbbo(
        [
            {
                "ticker": "SPY",
                "strike": 500.0,
                "expiration": "2025-06-20",
                "opt_type": "call",
                "at_time": at,
                "leg_key": "sc",
            }
        ],
    )
    assert out == {"sc": None}


def test_at_or_before_quote_used(handler, monkeypatch):
    idx = pd.date_range("2025-06-20 13:58", periods=4, freq="1min", tz="UTC")
    cache = pd.DataFrame(
        {
            "bid": [0.9, 0.95, 1.0, 1.05],
            "ask": [1.1, 1.15, 1.2, 1.25],
            "mid": [1.0] * 4,
        },
        index=idx,
    )
    monkeypatch.setattr(handler._cache, "get_superset", lambda *a, **k: cache)
    at = datetime.datetime(2025, 6, 20, 14, 0, tzinfo=datetime.UTC)
    out = handler.prefetch_ic_cbbo(
        [
            {
                "ticker": "SPY",
                "strike": 500.0,
                "expiration": "2025-06-20",
                "opt_type": "put",
                "at_time": at,
                "leg_key": "sp",
            }
        ],
    )
    assert out["sp"] is not None
    # Latest causal row is the fill-clock row.
    assert float(out["sp"]["bid"]) == pytest.approx(1.0)
    assert out["sp"]["source"] == "cache"
    assert out["sp"]["quote_age_seconds"] == pytest.approx(0.0)


def test_bounded_causal_cache_quote_avoids_provider_refetch(handler, monkeypatch):
    """A quote within the allowed causal age is a valid cache hit."""
    at = datetime.datetime(2025, 6, 20, 14, 0, tzinfo=datetime.UTC)
    stale = pd.DataFrame(
        {"bid": [0.9], "ask": [1.1], "mid": [1.0]},
        index=pd.DatetimeIndex([at - datetime.timedelta(seconds=1)]),
    )
    raw = pd.DataFrame(
        {
            "symbol": ["SPY   250620P00500000"],
            "bid_px_00": [1.0],
            "ask_px_00": [1.2],
        },
        index=pd.DatetimeIndex([at]),
    )
    store = MagicMock()
    store.to_df.return_value = raw
    handler.databento_client.timeseries.get_range.return_value = store
    monkeypatch.setattr(handler._cache, "get_superset", lambda *a, **k: stale)

    out = handler.prefetch_ic_cbbo(
        [
            {
                "ticker": "SPY",
                "strike": 500.0,
                "expiration": "2025-06-20",
                "opt_type": "put",
                "at_time": at,
                "leg_key": "sp",
            }
        ]
    )

    assert float(out["sp"]["bid"]) == pytest.approx(0.9)
    assert out["sp"]["quote_age_seconds"] == pytest.approx(1.0)
    handler.databento_client.timeseries.get_range.assert_not_called()


def test_prefetch_keeps_all_boundary_rows_and_uses_the_latest(handler, monkeypatch):
    """The start and target minute can both be returned by one causal query."""
    at = datetime.datetime(2025, 6, 20, 14, 0, tzinfo=datetime.UTC)
    raw = pd.DataFrame(
        {
            "bid_px_00": [0.9, 1.0],
            "ask_px_00": [1.1, 1.2],
        },
        index=pd.DatetimeIndex(
            [at - datetime.timedelta(seconds=60), at],
        ),
    )
    store = MagicMock()
    store.to_df.return_value = raw
    handler.databento_client.timeseries.get_range.return_value = store
    monkeypatch.setattr(handler._cache, "get_superset", lambda *a, **k: None)

    hydrated = handler.prefetch_ic_cbbo(
        [
            {
                "ticker": "SPY",
                "strike": 500.0,
                "expiration": "2025-06-20",
                "opt_type": "put",
                "at_time": at,
                "leg_key": "put",
            }
        ]
    )

    assert hydrated["put"] is not None
    assert hydrated["put"]["mid"] == pytest.approx(1.1)
    assert "limit" not in handler.databento_client.timeseries.get_range.call_args.kwargs


def test_legacy_sentinel_is_treated_as_cached_no_quote(handler, monkeypatch):
    """Old ``-1`` rows never price a leg or trigger a replacement fetch."""
    at = datetime.datetime(2022, 1, 4, 15, 0, tzinfo=datetime.UTC)
    legacy = pd.DataFrame(
        {"bid": [-1.0], "ask": [-1.0], "mid": [-1.0]},
        index=pd.DatetimeIndex([at]),
    )
    raw = pd.DataFrame(
        {
            "symbol": ["SPY   220104P00473000"],
            "bid_px_00": [1.0],
            "ask_px_00": [1.2],
        },
        index=pd.DatetimeIndex([at]),
    )
    store = MagicMock()
    store.to_df.return_value = raw
    handler.databento_client.timeseries.get_range.return_value = store
    monkeypatch.setattr(handler._cache, "get_superset", lambda *a, **k: legacy)

    out = handler.prefetch_ic_cbbo(
        [
            {
                "ticker": "SPY",
                "strike": 473.0,
                "expiration": "2022-01-04",
                "opt_type": "put",
                "at_time": at,
                "leg_key": "lp",
            }
        ]
    )

    assert out == {"lp": None}
    handler.databento_client.timeseries.get_range.assert_not_called()


def test_non_strict_mode_returns_missing_leg_for_whole_entry_skip(handler, monkeypatch):
    """Research mode can skip an incomplete IC without inventing a fill."""
    at = datetime.datetime(2022, 1, 4, 15, 0, tzinfo=datetime.UTC)
    monkeypatch.setattr(handler._cache, "get_superset", lambda *a, **k: None)
    store = MagicMock()
    store.to_df.return_value = pd.DataFrame()
    handler.databento_client.timeseries.get_range.return_value = store

    out = handler.prefetch_ic_cbbo(
        [
            {
                "ticker": "SPY",
                "strike": 473.0,
                "expiration": "2022-01-04",
                "opt_type": "put",
                "at_time": at,
                "leg_key": "lp",
            }
        ],
    )

    assert out["lp"] is None
    assert handler._last_cbbo_prefetch_stats["missing_exact_legs"] == 1


def test_cache_only_mode_never_calls_databento_for_a_cache_miss(handler, monkeypatch):
    """A budget-safe rerun skips an incomplete entry without a provider call."""
    at = datetime.datetime(2025, 6, 20, 14, 0, tzinfo=datetime.UTC)
    monkeypatch.setattr(handler._cache, "get_superset", lambda *a, **k: None)

    out = handler.prefetch_ic_cbbo(
        [
            {
                "ticker": "SPY",
                "strike": 500.0,
                "expiration": "2025-06-20",
                "opt_type": "put",
                "at_time": at,
                "leg_key": "lp",
            }
        ],
        allow_network=False,
    )

    assert out == {"lp": None}
    handler.databento_client.timeseries.get_range.assert_not_called()
    assert handler._last_cbbo_prefetch_stats["api_fetches"] == 0


def test_budget_error_stops_later_cbbo_requests(handler, monkeypatch):
    """One 402 trips the handler-wide circuit breaker for the backtest."""
    at = datetime.datetime(2025, 6, 20, 14, 0, tzinfo=datetime.UTC)
    monkeypatch.setattr(handler._cache, "get_superset", lambda *a, **k: None)
    handler.databento_client.timeseries.get_range.side_effect = RuntimeError(
        "402 account_insufficient_funds"
    )
    request = {
        "ticker": "SPY",
        "strike": 500.0,
        "expiration": "2025-06-20",
        "opt_type": "put",
        "at_time": at,
        "leg_key": "lp",
    }

    assert handler.prefetch_ic_cbbo([request]) == {"lp": None}
    assert handler._cbbo_budget_exhausted.is_set()

    request["leg_key"] = "lp_later"
    assert handler.prefetch_ic_cbbo([request]) == {"lp_later": None}
    assert handler.databento_client.timeseries.get_range.call_count == 1


def test_ic_cbbo_batches_only_identical_bounded_causal_windows(handler, monkeypatch):
    """Separate fill clocks must not widen a CBBO request to an hourly span."""
    at_a = datetime.datetime(2025, 6, 20, 14, 0, tzinfo=datetime.UTC)
    at_b = datetime.datetime(2025, 6, 20, 14, 30, tzinfo=datetime.UTC)
    monkeypatch.setattr(handler._cache, "get_superset", lambda *a, **k: None)
    calls = []

    def _empty_batch(**kwargs):
        calls.append((kwargs["start"], kwargs["end"], kwargs["symbols"]))
        return pd.DataFrame()

    monkeypatch.setattr(handler, "_batched_get_range_with_recovery", _empty_batch)
    handler.prefetch_ic_cbbo(
        [
            {
                "ticker": "SPY",
                "strike": 500.0,
                "expiration": "2025-06-20",
                "opt_type": "put",
                "at_time": at_a,
                "leg_key": "a",
            },
            {
                "ticker": "SPY",
                "strike": 501.0,
                "expiration": "2025-06-20",
                "opt_type": "call",
                "at_time": at_b,
                "leg_key": "b",
            },
        ],
    )

    assert {(start, end) for start, end, _ in calls} == {
        (
            at_a - datetime.timedelta(seconds=1),
            at_a + datetime.timedelta(milliseconds=1),
        ),
        (
            at_b - datetime.timedelta(seconds=1),
            at_b + datetime.timedelta(milliseconds=1),
        ),
    }
    assert all(len(symbols) == 1 for _, _, symbols in calls)


def test_ic_cbbo_batch_does_not_cap_total_rows_by_symbol_count(handler, monkeypatch):
    """A global row limit can omit a requested symbol with multiple CBBO rows."""
    at = datetime.datetime(2025, 6, 20, 14, 0, tzinfo=datetime.UTC)
    monkeypatch.setattr(handler._cache, "get_superset", lambda *a, **k: None)
    symbols = [
        "SPY   250620P00500000",
        "SPY   250620C00510000",
    ]
    raw = pd.DataFrame(
        {
            "symbol": [symbols[0], symbols[0], symbols[1]],
            "bid_px_00": [1.0, 1.01, 0.9],
            "ask_px_00": [1.2, 1.21, 1.1],
        },
        index=pd.DatetimeIndex(
            [
                at - datetime.timedelta(seconds=1),
                at,
                at,
            ]
        ),
    )
    store = MagicMock()
    store.to_df.return_value = raw
    handler.databento_client.timeseries.get_range.return_value = store

    out = handler.prefetch_ic_cbbo(
        [
            {
                "ticker": "SPY",
                "strike": 500.0,
                "expiration": "2025-06-20",
                "opt_type": "put",
                "at_time": at,
                "leg_key": "put",
            },
            {
                "ticker": "SPY",
                "strike": 510.0,
                "expiration": "2025-06-20",
                "opt_type": "call",
                "at_time": at,
                "leg_key": "call",
            },
        ]
    )

    assert out["put"] is not None
    assert out["call"] is not None
    assert "limit" not in handler.databento_client.timeseries.get_range.call_args.kwargs


def test_missing_zero_dte_expiration_is_not_fabricated_from_calendar(
    handler, monkeypatch
):
    """A pre-launch SPY Tuesday must be skipped, not assigned a fake expiry."""
    prefetch = MagicMock()
    monkeypatch.setattr(handler, "prefetch_expirations", prefetch)

    out = handler.build_expiration_cache(
        "SPY",
        [datetime.date(2022, 1, 4)],
        min_dte=0,
        max_dte=0,
        repair_max_days=0,
    )

    assert out == {}
    prefetch.assert_not_called()


def test_spy_zero_dte_schedule_is_authoritative(handler, monkeypatch):
    """Listed SPY 0-DTE dates do not depend on incomplete definition snapshots."""
    prefetch = MagicMock()
    monkeypatch.setattr(handler, "prefetch_expirations", prefetch)

    out = handler.build_expiration_cache(
        "SPY",
        [datetime.date(2023, 4, 12)],
        min_dte=0,
        max_dte=0,
        repair_max_days=0,
    )

    assert out == {"2023-04-12": "2023-04-12"}
    prefetch.assert_not_called()


def test_non_spy_zero_dte_uses_conservative_friday_calendar(handler):
    """No definition request is made for non-SPY products."""
    out = handler.build_expiration_cache(
        "QQQ",
        [datetime.date(2023, 4, 14)],
        min_dte=0,
        max_dte=0,
        repair_max_days=0,
    )

    assert out == {"2023-04-14": "2023-04-14"}
    handler.databento_client.timeseries.get_range.assert_not_called()


@pytest.mark.parametrize(
    ("day", "expected"),
    [
        (datetime.date(2022, 11, 21), "2022-11-21"),  # existing Monday series
        (datetime.date(2022, 11, 22), "2022-11-22"),  # first Tuesday expiry
        (datetime.date(2022, 12, 1), "2022-12-01"),  # first Thursday expiry
    ],
)
def test_spy_zero_dte_schedule_uses_actual_listing_starts(handler, day, expected):
    out = handler.build_expiration_cache(
        "SPY", [day], min_dte=0, max_dte=0, repair_max_days=0
    )

    assert out == {day.isoformat(): expected}


def test_spy_zero_dte_schedule_accepts_listed_session_without_definition(handler):
    assert handler.build_expiration_cache(
        "SPY", [datetime.date(2023, 4, 12)], min_dte=0, max_dte=0, repair_max_days=0
    ) == {"2023-04-12": "2023-04-12"}
