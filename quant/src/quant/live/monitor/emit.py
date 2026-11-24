"""Hot-path monitor emit — never blocks the trading pipeline.

Durations use ``time.perf_counter_ns()`` (monotonic ns within the process).
Wall correlation uses ``time.time_ns()``. OS scheduling jitter is typically
µs-scale; ns fields are exact counter deltas, not cross-host NTP sync.

The queue consumer (sidecar / shadow worker) is a developer-built extension
(design D7 — it moved to the private superset as the reference extension):
this module owns only the non-blocking enqueue primitive.  With no queue
wired, ``emit`` is a no-op and ``enabled()`` is False — the SDK behaves
identically and emits nothing (observability spec).
"""

from __future__ import annotations

import contextlib
import logging
import time
from typing import Any

logger = logging.getLogger(__name__)

_queue: Any | None = None  # context-specific Queue (wired by an extension)
_drops: Any | None = None  # multiprocessing.Value — dropped-event counter


def ns() -> int:
    """Monotonic nanosecond counter for span timing."""
    return time.perf_counter_ns()


def wall_ns() -> int:
    """Wall-clock nanoseconds for correlation / queue lag."""
    return time.time_ns()


def enabled() -> bool:
    """True when a consumer queue is live (wired by an extension)."""
    return _queue is not None


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
