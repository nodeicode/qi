"""Unit tests for quant.data.ws.MemBarsAggregator.

No threads, no network — the aggregator is driven synchronously, mirroring how the
worker thread drives it in production (single writer).
"""

from __future__ import annotations

import pandas as pd
import pytest

from quant.data.ws import MemBarsAggregator
from quant.data.ws.protocols import MinuteBar


def _mb(
    symbol: str,
    ts: str,
    open_: float = 100.0,
    high: float = 101.0,
    low: float = 99.0,
    close: float = 100.5,
    volume: float = 1000.0,
) -> MinuteBar:
    return MinuteBar(
        symbol=symbol,
        timestamp=pd.Timestamp(ts),
        open=open_,
        high=high,
        low=low,
        close=close,
        volume=volume,
    )


def _feed_rtf(agg: MemBarsAggregator, symbol: str, start: str, n: int) -> None:
    """Feed *n* consecutive 1-minute bars beginning at *start* (UTC wall)."""
    for i in range(n):
        ts = pd.Timestamp(start) + pd.Timedelta(minutes=i)
        agg.on_minute(_mb(symbol, ts.isoformat()), interval="15m")


def _seed_frame(stamps: list[str], base: float, symbol: str = "SPY") -> pd.DataFrame:
    idx = pd.DatetimeIndex([pd.Timestamp(t).as_unit("us") for t in stamps])
    return pd.DataFrame(
        {
            "Open": [base + i for i in range(len(stamps))],
            "High": [base + i + 1 for i in range(len(stamps))],
            "Low": [base + i - 1 for i in range(len(stamps))],
            "Close": [base + i + 0.5 for i in range(len(stamps))],
            "Volume": 1000.0,
            "symbol": symbol,
        },
        index=idx,
    )


class TestBucketRollover:
    def test_complete_bucket_appends_settled_row_to_both_keys(self) -> None:
        agg = MemBarsAggregator()
        _feed_rtf(agg, "SPY", "2026-01-05 14:30", 15)  # bucket A: 09:30..09:44
        agg.on_minute(_mb("SPY", "2026-01-05 14:45"), interval="15m")  # rollover A

        for adjustment in ("all", "raw"):
            frame, cov = agg.get(("SPY", "15m", adjustment))
            assert frame is not None
            assert len(frame) == 1
            row = frame.iloc[0]
            assert row["Open"] == 100.0  # first minute open
            assert row["High"] == 101.0  # max high
            assert row["Low"] == 99.0  # min low
            assert row["Close"] == 100.5  # last minute close
            assert row["Volume"] == 15000.0  # 15 x 1000
            assert row["symbol"] == "SPY"
            assert frame.index[0] == pd.Timestamp("2026-01-05 14:30")
            assert frame.index.dtype == "datetime64[us]"
            assert cov == (
                pd.Timestamp("2026-01-05 14:30"),
                pd.Timestamp("2026-01-05 14:30"),
            )

    def test_bucket_finalizes_on_last_minute_not_rollover(self) -> None:
        """The bucket is settled the moment its last minute arrives, so the
        engine's closed-bar request (DATA_DELAY expiry instant) is already
        banked — no +1-minute rollover lag on the live edge."""
        agg = MemBarsAggregator()
        _feed_rtf(agg, "SPY", "2026-01-05 14:30", 15)  # bucket A complete at 14:44
        for adjustment in ("all", "raw"):
            frame, cov = agg.get(("SPY", "15m", adjustment))
            assert frame is not None and len(frame) == 1  # settled pre-rollover
            assert frame.index[0] == pd.Timestamp("2026-01-05 14:30")
            assert cov[1] == pd.Timestamp("2026-01-05 14:30")
        # a late duplicate minute must not corrupt the finalized row
        agg.on_minute(_mb("SPY", "2026-01-05 14:44"), interval="15m")
        frame, _cov = agg.get(("SPY", "15m", "all"))
        assert frame is not None and len(frame) == 1
        assert frame.iloc[0]["Volume"] == 15000.0  # 15 x 1000, not 16000

    def test_missing_minute_drops_bucket(self) -> None:
        agg = MemBarsAggregator()
        _feed_rtf(agg, "SPY", "2026-01-05 14:30", 14)  # only 14/15 minutes
        agg.on_minute(_mb("SPY", "2026-01-05 14:45"), interval="15m")  # rollover
        frame, _cov = agg.get(("SPY", "15m", "all"))
        assert frame is None  # never fabricated
        assert _cov == (None, None)

    def test_stale_minute_is_ignored(self) -> None:
        agg = MemBarsAggregator()
        _feed_rtf(agg, "SPY", "2026-01-05 14:30", 15)
        agg.on_minute(_mb("SPY", "2026-01-05 14:45"), interval="15m")  # starts bucket B
        # stale minute from bucket A arrives late — must not pollute B
        agg.on_minute(_mb("SPY", "2026-01-05 14:31"), interval="15m")
        _feed_rtf(agg, "SPY", "2026-01-05 14:46", 14)
        agg.on_minute(_mb("SPY", "2026-01-05 15:00"), interval="15m")  # rollover B

        frame, _cov = agg.get(("SPY", "15m", "all"))
        assert frame is not None and len(frame) == 2
        b_row = frame.iloc[-1]
        assert b_row["Volume"] == 15000.0  # stale 09:31 excluded
        assert frame.index[-1] == pd.Timestamp("2026-01-05 14:45")

    def test_extended_hours_bucket_not_stored(self) -> None:
        agg = MemBarsAggregator()
        # 11:00-11:14 UTC = 06:00 ET (winter) — before RTH → dropped
        _feed_rtf(agg, "SPY", "2026-01-05 11:00", 15)
        agg.on_minute(_mb("SPY", "2026-01-05 11:15"), interval="15m")
        frame, _cov = agg.get(("SPY", "15m", "all"))
        assert frame is None


class TestSeed:
    def test_seed_overlap_truncation_rest_wins(self) -> None:
        agg = MemBarsAggregator()
        # ws tail has settled rows at 14:30 (bucket A) and 14:45 (bucket B)
        _feed_rtf(agg, "SPY", "2026-01-05 14:30", 15)  # A
        agg.on_minute(
            _mb("SPY", "2026-01-05 14:45"), interval="15m"
        )  # rollover A, starts B
        _feed_rtf(agg, "SPY", "2026-01-05 14:46", 14)  # B: 14:46..14:59
        agg.on_minute(_mb("SPY", "2026-01-05 15:00"), interval="15m")  # rollover B

        # REST seed owns stamps <= seed_end (14:35 UTC = 09:35 ET); ws owns newer
        seed_frame = _seed_frame(["2026-01-05 14:30", "2026-01-05 14:45"], base=500.0)
        agg.seed("SPY", "15m", "all", seed_frame, seed_end="2026-01-05 14:35")

        frame, _cov = agg.get(("SPY", "15m", "all"))
        assert frame is not None and len(frame) == 2
        # 09:30 comes from the REST seed (base 500), 09:45 from the ws tail (open 100)
        assert frame.loc["2026-01-05 14:30", "Open"] == 500.0
        assert frame.loc["2026-01-05 14:45", "Open"] == 100.0
        assert _cov == (
            pd.Timestamp("2026-01-05 14:30"),
            pd.Timestamp("2026-01-05 14:45"),
        )

    def test_coverage_envelope_advances_only_on_append(self) -> None:
        agg = MemBarsAggregator()
        agg.seed("SPY", "15m", "all", _seed_frame(["2026-01-05 14:30"], 100.0))
        # complete bucket 10:00
        _feed_rtf(agg, "SPY", "2026-01-05 15:00", 15)
        agg.on_minute(_mb("SPY", "2026-01-05 15:15"), interval="15m")  # rollover
        # partial bucket 10:15 (5/15 minutes) → dropped, coverage must not advance
        _feed_rtf(agg, "SPY", "2026-01-05 15:15", 5)
        agg.on_minute(_mb("SPY", "2026-01-05 15:30"), interval="15m")

        _, cov = agg.get(("SPY", "15m", "all"))
        assert cov == (
            pd.Timestamp("2026-01-05 14:30"),
            pd.Timestamp("2026-01-05 15:00"),  # frozen past the dropped bucket
        )

    def test_seed_is_idempotent(self) -> None:
        agg = MemBarsAggregator()
        agg.seed("SPY", "15m", "raw", _seed_frame(["2026-01-05 14:30"], 100.0))
        agg.seed("SPY", "15m", "raw", _seed_frame(["2026-01-05 14:30"], 200.0))
        frame, _ = agg.get(("SPY", "15m", "raw"))
        assert frame is not None and len(frame) == 1
        assert frame.loc["2026-01-05 14:30", "Open"] == 200.0
