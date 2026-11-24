"""Single-writer plumbing for live bar ingestion.

``BarWorker`` owns the queue + worker thread that deliver minute bars and seed
jobs to a ``BarSink`` (the aggregator). Providers' socket threads only call
``publish``; the aggregator is only ever mutated by this worker thread — or, when
no worker is running (tests), by direct synchronous calls — so the store stays
single-writer while readers snapshot-slice without locks.
"""

from __future__ import annotations

import logging
import queue
import threading

from quant.data.ws.protocols import BarSink, MinuteBar

_LOGGER = logging.getLogger(__name__)


class BarWorker:
    """Thread-safe fan-in: any thread may enqueue; one worker thread consumes."""

    def __init__(self, sink: BarSink) -> None:
        self._sink = sink
        self._q: queue.Queue = queue.Queue()
        self._thread: threading.Thread | None = None
        # Set after every applied write so read-side waiters (the edge wait in
        # get_historical_data) wake on progress instead of polling blindly.
        self._settle = threading.Event()

    def publish(self, bar: MinuteBar, interval: str) -> None:
        """Called by a provider's socket thread; never runs aggregator code."""
        self._q.put(("minute", (bar, interval)))

    def request_seed(self, ticker, interval, adjustment, frame, seed_end=None) -> None:
        """Thread-safe seed request (warmup); applied by the worker thread."""
        self._q.put(("seed", (ticker, interval, adjustment, frame, seed_end)))

    def request_merge(self, ticker, interval, adjustment, frame) -> None:
        """Thread-safe extend request (head/interior heal); applied by the worker."""
        self._q.put(("merge", (ticker, interval, adjustment, frame)))

    def start(self) -> None:
        if self._thread is not None:
            return
        self._thread = threading.Thread(
            target=self._run, name="ws-bar-worker", daemon=True
        )
        self._thread.start()

    def stop(self) -> None:
        """Drain pending work, then stop the worker (idempotent)."""
        self._q.put(("stop", None))
        if self._thread is not None:
            self._thread.join(timeout=5)
            self._thread = None

    def _run(self) -> None:
        while True:
            kind, payload = self._q.get()
            if kind == "stop":
                return
            try:
                if kind == "minute":
                    self._sink.on_minute(*payload)
                elif kind == "seed":
                    self._sink.seed(*payload)
                elif kind == "merge":
                    self._sink.merge(*payload)
            except Exception:
                _LOGGER.exception("bar worker error")
            finally:
                self._settle.set()
