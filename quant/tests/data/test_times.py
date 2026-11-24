"""Tests for the handler boundary contract (``quant.data._times``)."""

from __future__ import annotations

import datetime as dt

import numpy as np
import pandas as pd
import pytest

from quant.data._times import (
    aligned_at,
    assert_canonical_index,
    from_ns,
    parse_naive_utc,
    to_naive_index,
    to_naive_utc,
    ts_ns,
)


def test_parse_naive_utc_naive_is_tagged():
    s, e = parse_naive_utc("2024-01-02 09:30", "2024-01-02 16:00")
    assert s.tzinfo is None
    assert e.tzinfo is None
    # natural unit preserved (cache-coverage bookkeeping is unit-sensitive)
    assert isinstance(s, pd.Timestamp)


def test_parse_naive_utc_aware_converted_and_stripped():
    aware = pd.Timestamp("2024-06-01 14:00:00+00:00")
    s, _ = parse_naive_utc(aware, aware)
    assert s == pd.Timestamp("2024-06-01 14:00:00")
    assert s.tzinfo is None


def test_parse_naive_utc_et_converted_to_utc():
    aware = pd.Timestamp("2024-06-01 10:00:00-04:00")
    s, _ = parse_naive_utc(aware, aware)
    assert s == pd.Timestamp("2024-06-01 14:00:00")
    assert s.tzinfo is None


def test_parse_naive_utc_int_ns():
    ns = 1_704_165_000_000_000_000
    s, _ = parse_naive_utc(ns, ns)
    assert ts_ns(s) == ns


def test_parse_naive_utc_rejects_nat():
    with pytest.raises(ValueError):
        parse_naive_utc(pd.NaT, pd.Timestamp("2024-01-01"))
    with pytest.raises(ValueError):
        parse_naive_utc("2024-01-01", np.datetime64("NaT"))


def test_ts_ns_roundtrip_lossless():
    ts = pd.Timestamp("2024-01-02 09:30:00.123456789")
    assert from_ns(ts_ns(ts)) == ts
    assert from_ns(ts_ns(ts)).unit == "ns"  # ns preserved for coverage boundaries


def test_assert_canonical_index_intraday():
    df = pd.DataFrame(
        {"Close": [1.0, 2.0]},
        index=pd.DatetimeIndex(["2024-01-02 09:30", "2024-01-02 09:45"], tz="UTC")
        .tz_localize(None)
        .as_unit("us"),
    )
    assert_canonical_index(df, allow_aware_daily=False)


def test_assert_canonical_index_rejects_aware_intraday():
    df = pd.DataFrame(
        {"Close": [1.0]},
        index=pd.DatetimeIndex(["2024-01-02 09:30"], tz="UTC"),
    )
    with pytest.raises(AssertionError):
        assert_canonical_index(df, allow_aware_daily=False)


def test_assert_canonical_index_permits_aware_daily():
    df = pd.DataFrame(
        {"Close": [1.0]},
        index=pd.DatetimeIndex(["2024-01-02"], tz="UTC"),
    )
    assert_canonical_index(df, interval="1d")


def test_to_naive_utc_single_value_strips_tz():
    assert to_naive_utc(pd.Timestamp("2024-06-01 14:00:00-04:00")).tzinfo is None


def test_to_naive_index_intraday_aware_to_naive_us():
    df = pd.DataFrame(
        {"Close": [1.0, 2.0]},
        index=pd.DatetimeIndex(["2024-01-02 14:30", "2024-01-02 14:45"])
        .tz_localize("America/New_York")
        .tz_convert("UTC"),
    )
    out = to_naive_index(df)
    assert pd.DatetimeIndex(out.index).tz is None
    assert pd.DatetimeIndex(out.index).unit == "us"


def test_to_naive_index_naive_input_untouched():
    df = pd.DataFrame({"Close": [1.0]}, index=pd.DatetimeIndex(["2024-01-02 09:30"]))
    out = to_naive_index(df)
    assert pd.DatetimeIndex(out.index).tz is None
    assert out is df


def test_to_naive_index_daily_passthrough_aware():
    df = pd.DataFrame(
        {"Close": [1.0]}, index=pd.DatetimeIndex(["2024-01-02"], tz="UTC")
    )
    out = to_naive_index(df, interval="1d")
    assert pd.DatetimeIndex(out.index).tz is not None  # daily-aware, per convention


def test_aligned_at():
    idx = pd.DatetimeIndex(["2024-01-02 09:30", "2024-01-02 09:45", "2024-01-02 10:00"])
    df = pd.DataFrame({"Close": [1, 2, 3]}, index=idx)
    assert aligned_at(df, "2024-01-02 09:40") == pd.Timestamp("2024-01-02 09:30")
    assert aligned_at(df, "2024-01-02 09:45") == pd.Timestamp("2024-01-02 09:45")
    assert aligned_at(df, "2024-01-02 09:00") is None
