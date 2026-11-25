"""Observability primitives — spans and an emitter interface.

The v1 observability surface is deliberately small (see the ``observability``
capability): one span-timing primitive that keeps **age** (bar staleness)
perpendicular to **duration** (loop/fetch time), and one optional emit
interface. Everything else — shadow signals, reconciliation, sidecars,
dashboards — is extended functionality developers build on top of these.

Producers (the engine, the live trader) and the monitor consumer share the
exact same closed set of span keys, so a producer can never invent an
off-registry string at call time.
"""

from __future__ import annotations

import functools
import json
import sys
import time
from collections.abc import Callable
from datetime import UTC, datetime
from enum import StrEnum
from typing import Any, Protocol, runtime_checkable

MonitorEvent = dict[str, Any]
MonitorEmit = Callable[[MonitorEvent], None]


class SpanName(StrEnum):
    """Every measurable hot-path span.

    The member is the stable key — the emitted JSONL key is ``value + "_ns"``.
    A ``StrEnum`` gives type-checking + autocomplete, so a producer can never
    invent an off-registry string at call time. ``age`` spans (staleness,
    delay) are deliberately *separate* from ``duration`` spans so feed skew is
    never conflated with slowness.
    """

    # ── durations (how long work took) ───────────────────────────────────
    # The reshaped declarative path (strategy-dataflow) measures the author
    # body and its order submission; the legacy signal/manage-span set
    # (RUN_SIGNALS / PROCESS_TICKER / EXECUTION_DISPATCH and the fetch spans)
    # is gone with the methods it timed.
    DECIDE = "decide"
    DECISION_SUBMIT = "decision_submit"
    TICK_ELAPSED = "tick_elapsed"
    TICK_DISPATCH = "tick_dispatch"
    MAIN_LOOP_LAG = "main_loop_lag"
    QUEUE_LAG = "queue_lag"
    WARMUP_TOTAL = "warmup_total"

    # ── age (how stale the input is) — keep perpendicular to durations ───


def span_key(name: SpanName) -> str:
    """A span's emitted JSONL key: ``<enum value>_ns``."""
    return f"{name!s}_ns"


class Span:
    """Time a named block or function with ``time.perf_counter_ns()``.

    On exit, records ``{name_ns: elapsed}`` into *collect* if given; else, when
    *event* is given, emits ``{**event, name_ns: elapsed}``; else emits just
    ``{name_ns: elapsed}`` via *emit* (if any). ``emit is None`` → zero-cost
    no-op. Mutate the *event* dict inside the block to add dynamic fields (e.g.
    ``accepted``) — they are merged at exit::

        with Span(SpanName.DECIDE, emit=self._monitor_emit):
            decisions = strategy.decide(...)

        @span(SpanName.DECISION_SUBMIT)    # decorator form
        def submit_decisions(...): ...

    ``emit is None`` → measured but never recorded (cost is one
    ``perf_counter_ns`` read each side).
    """

    def __init__(
        self,
        name: SpanName,
        *,
        emit: MonitorEmit | None = None,
        collect: dict[str, int] | None = None,
        event: dict[str, Any] | None = None,
    ) -> None:
        self._key = span_key(name)
        self._emit = emit
        self._collect = collect
        self._event = event
        self._t0: int | None = None

    def __enter__(self) -> Span:
        self._t0 = time.perf_counter_ns()
        return self

    def __exit__(self, *_exc: object) -> None:
        elapsed = self.elapsed_ns()
        if self._collect is not None:
            self._collect[self._key] = elapsed
        elif self._emit is not None:
            if self._event is not None:
                self._emit({**self._event, self._key: elapsed})
            else:
                self._emit({self._key: elapsed})

    def elapsed_ns(self) -> int:
        return (time.perf_counter_ns() - self._t0) if self._t0 is not None else 0

    def __call__(self, fn: Callable) -> Callable:
        @functools.wraps(fn)
        def _wrap(*args, **kwargs):
            with self:
                return fn(*args, **kwargs)

        return _wrap


def span(name: SpanName, *, emit: MonitorEmit | None = None) -> Span:
    """Decorator shorthand: ``@span(SpanName.DECIDE)``."""
    return Span(name, emit=emit)


# The full registry is DERIVED from the enum — producers and consumers share
# the exact same closed set. The latency-SLO rollup set is the same registry
# minus the event-local (non-SLO) spans, derived (no second list).
SPAN_KEYS: frozenset[str] = frozenset(span_key(n) for n in SpanName)
_NON_SLO_SPANS: frozenset[str] = frozenset(
    {span_key(SpanName.DECISION_SUBMIT), span_key(SpanName.WARMUP_TOTAL)}
)
SLO_SPAN_KEYS: frozenset[str] = SPAN_KEYS - _NON_SLO_SPANS


@runtime_checkable
class Emitter(Protocol):
    """Sink for telemetry rows. Everything above this is developer-built.

    A sink MUST be additive — consumers wishing to batch/async MUST do so in
    the sink, never in the hot path.
    """

    def emit(self, event: MonitorEvent) -> None: ...


class NoopEmitter:
    """Adopt as the default: emit nothing, cost nothing."""

    def emit(self, event: MonitorEvent) -> None:
        del event  # unused


class JsonlEmitter:
    """Write JSON lines to a stream (default stdout). Example sink.

    ``emit`` is cheap (one json.dumps + write); callers that need batching
    should build their own Emitter with an internal queue — the hot path must
    not pay for it.
    """

    def __init__(self, write: Callable[[str], object] | None = None) -> None:
        # default sink is stdout, flushed per line
        self._write = write or (
            lambda line: (sys.stdout.write(line + "\n"), sys.stdout.flush())
        )

    @staticmethod
    def _stamp(event: MonitorEvent) -> MonitorEvent:
        event = dict(event)
        event.setdefault("ts", datetime.now(UTC).isoformat())
        return event

    def emit(self, event: MonitorEvent) -> None:
        self._write(json.dumps(self._stamp(event)))


__all__ = [
    "SLO_SPAN_KEYS",
    "SPAN_KEYS",
    "Emitter",
    "JsonlEmitter",
    "MonitorEmit",
    "MonitorEvent",
    "NoopEmitter",
    "Span",
    "SpanName",
    "span",
    "span_key",
]
