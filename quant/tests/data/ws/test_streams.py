"""Tests for the provider stream layer: AlpacaDelayedSipStream + registry.

The alpaca StockDataStream is faked at the alpaca module attribute so no
websocket is ever opened; the rest of the path (BarWorker → aggregator) is real.
"""

from __future__ import annotations

import asyncio
import time
from types import SimpleNamespace

import pandas as pd
import pytest
from alpaca.data.enums import DataFeed

from quant.data.ws import MemBarsAggregator, build_bar_source
from quant.data.ws.streams.alpaca import AlpacaDelayedSipStream
from quant.data.ws.worker import BarWorker


class _FakeStockDataStream:
    """Stands in for alpaca.data.live.stock.StockDataStream."""

    def __init__(
        self,
        api_key=None,
        secret_key=None,
        feed=None,
        url_override=None,
        **kwargs,
    ):
        self.feed: DataFeed | None = feed
        self.url = url_override
        self.symbols = []
        self.handler = None
        self.stopped = False

    def subscribe_bars(self, handler, *symbols) -> None:
        self.handler = handler
        self.symbols = list(symbols)

    def run(self) -> None:  # never actually connects in tests
        pass

    def stop(self) -> None:
        self.stopped = True


@pytest.fixture()
def fake_stream(monkeypatch: pytest.MonkeyPatch) -> _FakeStockDataStream:
    """Patch the alpaca class so stream construction binds *this* fake."""
    fake = _FakeStockDataStream()

    def _ctor(*args, **kwargs) -> _FakeStockDataStream:
        fake.__init__(*args, **kwargs)
        return fake

    monkeypatch.setattr("alpaca.data.live.stock.StockDataStream", _ctor)
    return fake


def _fake_bar(minute: str = "2026-01-05 09:30") -> SimpleNamespace:
    return SimpleNamespace(
        symbol="SPY",
        timestamp=pd.Timestamp(minute, tz="UTC"),
        open=100.0,
        high=101.0,
        low=99.0,
        close=100.5,
        volume=1000,
        vwap=100.3,
        trade_count=42,
    )


def _drive_handler(fake: _FakeStockDataStream, minute: str) -> None:
    """Run the stream's async handler once (as the ws loop thread would)."""
    assert fake.handler is not None
    asyncio.run(fake.handler(_fake_bar(minute=minute)))


def _wait_for(pred, timeout: float = 2.0) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if pred():
            return
        time.sleep(0.01)
    raise AssertionError("timed out waiting for the worker to process minutes")


class TestAlpacaStream:
    def test_constructs_against_delayed_sip_endpoint(
        self, fake_stream: _FakeStockDataStream
    ) -> None:
        worker = BarWorker(MemBarsAggregator())
        stream = AlpacaDelayedSipStream(
            worker, api_key="k", secret_key="s", tickers=["SPY"], interval="15m"
        )
        feed = fake_stream.feed
        assert feed is not None
        # 0.43.2 ctor guard only lets IEX/SIP through; the delayed feed is reached
        # via url_override — hence `feed` is SIP and the URL the delayed endpoint.
        assert feed.value == "sip"
        assert fake_stream.url == "wss://stream.data.alpaca.markets/v2/delayed_sip"
        assert fake_stream.symbols == ["SPY"]
        assert stream._interval == "15m"

    def test_minute_routes_via_worker_to_aggregator(
        self, fake_stream: _FakeStockDataStream
    ) -> None:
        agg = MemBarsAggregator()
        worker = BarWorker(agg)
        worker.start()
        try:
            AlpacaDelayedSipStream(
                worker, api_key="k", secret_key="s", tickers=["SPY"], interval="15m"
            )
            # one full bucket (15 minutes) + rollover minute, driven through the
            # stream handler → worker thread → aggregator; bars are 14:30 UTC ==
            # 09:30 ET so they pass the RTH gate
            for i in range(15):
                minute = pd.Timestamp("2026-01-05 14:30") + pd.Timedelta(minutes=i)
                _drive_handler(fake_stream, minute.isoformat())
            _drive_handler(fake_stream, "2026-01-05 14:45")
            _wait_for(lambda: agg.get(("SPY", "15m", "all"))[0] is not None)
            frame, _ = agg.get(("SPY", "15m", "all"))
            assert frame is not None and len(frame) == 1
            assert frame.loc["2026-01-05 14:30", "Volume"] == 15000.0
        finally:
            worker.stop()

    def test_feed_param_switches_to_realtime_feed(
        self, fake_stream: _FakeStockDataStream
    ) -> None:
        """F1: the feed is configurable — real-time SIP needs no url_override."""
        worker = BarWorker(MemBarsAggregator())
        AlpacaDelayedSipStream(
            worker, api_key="k", secret_key="s", tickers=["SPY"], feed=DataFeed.SIP
        )
        assert fake_stream.url is None  # no delayed-endpoint workaround
        assert fake_stream.feed is DataFeed.SIP

    def test_stop_is_idempotent(self, fake_stream: _FakeStockDataStream) -> None:
        worker = BarWorker(MemBarsAggregator())
        stream = AlpacaDelayedSipStream(
            worker, api_key="k", secret_key="s", tickers=["SPY"]
        )
        stream.stop()
        stream.stop()
        assert fake_stream.stopped


class TestBarWorker:
    def test_request_seed_applies_on_worker_thread(self) -> None:
        agg = MemBarsAggregator()
        worker = BarWorker(agg)
        worker.start()
        try:
            idx = pd.DatetimeIndex([pd.Timestamp("2026-01-05 09:30").as_unit("us")])
            frame = pd.DataFrame(
                {
                    "Open": [1.0],
                    "High": [2.0],
                    "Low": [0.0],
                    "Close": [1.5],
                    "Volume": [10.0],
                    "symbol": ["SPY"],
                },
                index=idx,
            )
            worker.request_seed("SPY", "15m", "all", frame, seed_end=None)
            _wait_for(lambda: agg.get(("SPY", "15m", "all"))[0] is not None)
            frame_out, cov = agg.get(("SPY", "15m", "all"))
            assert frame_out is not None and len(frame_out) == 1
            assert cov[0] == pd.Timestamp("2026-01-05 09:30")
        finally:
            worker.stop()


class TestRegistry:
    def test_build_bar_source_resolves_alpaca(self) -> None:
        worker = BarWorker(MemBarsAggregator())
        src = build_bar_source(
            "alpaca", worker, api_key="k", secret_key="s", tickers=["SPY"]
        )
        assert isinstance(src, AlpacaDelayedSipStream)

    def test_unknown_provider_raises(self) -> None:
        worker = BarWorker(MemBarsAggregator())
        with pytest.raises(ValueError, match="unknown bar source provider"):
            build_bar_source("bogus", worker)
