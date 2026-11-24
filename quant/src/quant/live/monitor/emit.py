"""Hot-path monitor emit — never blocks the trading pipeline.

Durations use ``time.perf_counter_ns()`` (monotonic ns within the process).
Wall correlation uses ``time.time_ns()``. OS scheduling jitter is typically
µs-scale; ns fields are exact counter deltas, not cross-host NTP sync.
"""

from __future__ import annotations

import contextlib
import logging
import multiprocessing
import os
import time
from multiprocessing.process import BaseProcess
from typing import Any

logger = logging.getLogger(__name__)

_DEFAULT_QUEUE_SIZE = 256

_queue: Any | None = None  # context-specific Queue
_drops: Any | None = None  # multiprocessing.Value
_process: BaseProcess | None = None
_ctx: Any | None = None


def ns() -> int:
    """Monotonic nanosecond counter for span timing."""
    return time.perf_counter_ns()


def wall_ns() -> int:
    """Wall-clock nanoseconds for correlation / queue lag."""
    return time.time_ns()


def enabled() -> bool:
    """True when a sidecar queue is live (started from the trader)."""
    return _queue is not None


def start_sidecar(
    *,
    metrics_path: str = "monitor_metrics.jsonl",
    state_path: str = "monitor_sidecar_state.json",
    queue_size: int | None = None,
) -> None:
    """Spawn the monitor sidecar process. Idempotent."""
    global _queue, _drops, _process, _ctx  # noqa: PLW0603

    if _process is not None and _process.is_alive():
        return

    size = queue_size or int(os.environ.get("MONITOR_QUEUE_SIZE", _DEFAULT_QUEUE_SIZE))
    _ctx = multiprocessing.get_context("spawn")
    _queue = _ctx.Queue(maxsize=max(8, size))
    _drops = _ctx.Value("q", 0)

    from quant.live.monitor.sidecar import run_sidecar

    _process = _ctx.Process(
        target=run_sidecar,
        args=(_queue, _drops, metrics_path, state_path),
        name="monitor-sidecar",
        daemon=True,
    )
    _process.start()
    logger.info(
        "[MONITOR] sidecar started pid=%s queue_size=%s metrics=%s",
        _process.pid,
        size,
        metrics_path,
    )


def stop_sidecar(timeout: float = 2.0) -> None:
    """Best-effort shutdown — never block trader exit for long."""
    global _queue, _drops, _process, _ctx  # noqa: PLW0603

    q, proc = _queue, _process
    _queue = None
    _drops = None
    _process = None
    _ctx = None

    if q is not None:
        with contextlib.suppress(Exception):
            q.put_nowait({"kind": "shutdown"})
    if proc is not None and proc.is_alive():
        proc.join(timeout=timeout)
        if proc.is_alive():
            proc.terminate()


def emit(event: dict[str, Any]) -> None:
    """Non-blocking enqueue. Drop-oldest-then-put when full (keep freshest)."""
    q = _queue
    if q is None:
        return

    event = dict(event)
    event.setdefault("emit_wall_ns", wall_ns())

    try:
        q.put_nowait(event)
        return
    except Exception:
        pass

    # Full (or transient error): discard one oldest, then put freshest.
    with contextlib.suppress(Exception):
        q.get_nowait()
    try:
        q.put_nowait(event)
    except Exception:
        if _drops is not None:
            with _drops.get_lock():
                _drops.value += 1
        return

    if _drops is not None:
        with _drops.get_lock():
            _drops.value += 1
