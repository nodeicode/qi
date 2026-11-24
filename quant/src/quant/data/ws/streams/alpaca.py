"""Alpaca delayed-SIP minute-bar source.

Connects to the free 15-minute-delayed SIP feed and forwards each settled minute
bar to a ``BarWorker``. New providers live as sibling modules here and register in
``quant.data.ws`` — the aggregator and worker never change.
"""

from __future__ import annotations

import contextlib
import threading

import pandas as pd
from alpaca.data.enums import DataFeed

from quant.data.ws.protocols import MinuteBar
from quant.data.ws.worker import BarWorker

# v2 stream endpoint for the delayed SIP feed (15-min delayed SIP data).
_STREAM_URL = "wss://stream.data.alpaca.markets/v2/delayed_sip"


class AlpacaDelayedSipStream:
    """``BarSource`` backed by alpaca-py's ``StockDataStream``.

    Note on the feed: alpaca-py 0.43.2's ``StockDataStream`` ctor guard rejects
    ``DataFeed.DELAYED_SIP`` (it only allows IEX/SIP), so for the delayed feed we
    pass ``feed=SIP`` plus ``url_override`` pointing at the delayed endpoint — the
    guard passes and the endpoint is the delayed feed. ``DataStream.run()``
    auto-reconnects and exits cleanly (logs, does not raise) on an
    "insufficient subscription" auth failure for free-tier accounts.
    """

    def __init__(
        self,
        worker: BarWorker,
        *,
        api_key: str,
        secret_key: str,
        tickers: list[str],
        interval: str = "15m",
        feed: DataFeed = DataFeed.DELAYED_SIP,
    ) -> None:
        """Construct a live minute-bar stream.

        Args:
            worker: ``BarWorker`` that owns the aggregator (single writer).
            api_key / secret_key: Alpaca market-data credentials.
            tickers: Symbols to subscribe to minute bars for.
            interval: Bucket timeframe aggregated from minute bars ("15m").
            feed: Which feed to stream. Defaults to the free 15-minute-delayed
                SIP; pass ``DataFeed.SIP`` (or ``DataFeed.IEX``) for real-time.
        """
        from alpaca.data.live.stock import StockDataStream

        self._worker = worker
        self._interval = interval
        if feed == DataFeed.DELAYED_SIP:
            # alpaca-py 0.43.2 guard workaround (see class docstring).
            # TODO(feed): drop this branch and ``url_override`` once alpaca-py
            # accepts ``DataFeed.DELAYED_SIP`` directly.
            feed_arg, url_override = DataFeed.SIP, _STREAM_URL
        else:
            feed_arg, url_override = feed, None
        self._stream = StockDataStream(
            api_key=api_key,
            secret_key=secret_key,
            feed=feed_arg,
            url_override=url_override,
        )
        self._stream.subscribe_bars(self._on_minute, *tickers)
        self._thread: threading.Thread | None = None

    async def _on_minute(self, bar) -> None:  # runs on the ws loop thread
        self._worker.publish(
            MinuteBar(
                symbol=bar.symbol,
                timestamp=pd.Timestamp(bar.timestamp),
                open=bar.open,
                high=bar.high,
                low=bar.low,
                close=bar.close,
                volume=float(bar.volume),
                vwap=float(bar.vwap) if bar.vwap is not None else None,
                trade_count=(
                    float(bar.trade_count) if bar.trade_count is not None else None
                ),
            ),
            interval=self._interval,
        )

    def start(self) -> None:
        if self._thread is not None:
            return
        self._thread = threading.Thread(
            target=self._stream.run, name="ws-alpaca-delayed-sip", daemon=True
        )
        self._thread.start()

    def stop(self) -> None:
        """Stop the stream (idempotent)."""
        if self._stream is not None:
            with contextlib.suppress(Exception):
                self._stream.stop()  # thread-safe: run_coroutine_threadsafe internally
        self._thread = None
