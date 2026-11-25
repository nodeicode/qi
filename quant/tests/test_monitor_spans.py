"""Contract coverage for the shared Span timing primitives.

Guards: (a) ``Span`` records monotonic ns and is a no-op when ``emit=None``;
(b) ``collect`` buckets nested spans deterministically; (c) the ``@span``
decorator wraps without masking exceptions; (d) the determinism guard — every
``SpanName`` maps into ``SPAN_KEYS`` and the latency-SLO rollup set
``SLO_SPAN_KEYS ⊆ SPAN_KEYS`` with exactly the two non-SLO spans excluded.

The span set is the reshaped declarative path's set (``DECIDE`` /
``DECISION_SUBMIT`` for the author body + its submission, the loop/latency
series, and the off-path ``WARMUP_TOTAL``). The legacy spans the deleted
signal/manage machinery timed (``RUN_SIGNALS`` / ``PROCESS_TICKER`` /
``EXECUTION_DISPATCH`` and the fetch spans) are gone with those methods.
"""

from __future__ import annotations

import time

import pytest

from quant.observability import (
    SLO_SPAN_KEYS,
    SPAN_KEYS,
    Span,
    SpanName,
    span,
    span_key,
)

_NON_SLO_KEYS = {"decision_submit_ns", "warmup_total_ns"}


def test_span_key_matches_enum_values() -> None:
    assert span_key(SpanName.DECIDE) == "decide_ns"
    assert span_key(SpanName.DECISION_SUBMIT) == "decision_submit_ns"
    assert span_key(SpanName.WARMUP_TOTAL) == "warmup_total_ns"


def test_registry_is_exact_closed_set() -> None:
    assert frozenset(span_key(n) for n in SpanName) == SPAN_KEYS
    assert (
        frozenset(
            {
                "decide_ns",
                "decision_submit_ns",
                "tick_elapsed_ns",
                "tick_dispatch_ns",
                "main_loop_lag_ns",
                "queue_lag_ns",
                "warmup_total_ns",
            }
        )
        == SPAN_KEYS
    )


def test_determinism_guard_every_span_name_in_registry() -> None:
    for name in SpanName:
        assert span_key(name) in SPAN_KEYS


def test_slo_keys_subset_and_exact_non_slo_exclusion() -> None:
    assert SLO_SPAN_KEYS <= SPAN_KEYS
    assert SPAN_KEYS - SLO_SPAN_KEYS == _NON_SLO_KEYS
    assert _NON_SLO_KEYS <= SPAN_KEYS


def test_span_event_merges_span_key_into_payload_on_exit() -> None:
    """``event`` base-dict form: emitted payload is ``{**event, name_ns}`` with
    dynamic fields mutated inside the block merged at exit (decision_submit)."""
    events: list[dict] = []

    def emit(event: dict) -> None:
        events.append(event)

    evt = {"kind": "decision_submit", "accepted": False}
    with Span(SpanName.DECISION_SUBMIT, emit=emit, event=evt):
        new_fill = True
        evt["accepted"] = bool(new_fill)

    assert len(events) == 1
    (payload,) = events
    assert payload["kind"] == "decision_submit"
    assert payload["accepted"] is True
    assert "decision_submit_ns" in payload
    assert payload["decision_submit_ns"] >= 0
    # caller's dict is not mutated with the span key (fresh merge at exit)
    assert "decision_submit_ns" not in evt


def test_span_records_monotonic_ns() -> None:
    events: list[dict[str, int]] = []

    def emit(event: dict[str, int]) -> None:
        events.append(event)

    with Span(SpanName.DECIDE, emit=emit) as sp:
        elapsed = sp.elapsed_ns()
        assert elapsed >= 0
        start = time.perf_counter_ns()
        while time.perf_counter_ns() - start < 1_000_000:  # ~1ms busy wait
            pass  # ensure a measurable, strictly positive elapsed

    assert len(events) == 1
    (event,) = events
    assert set(event) == {"decide_ns"}
    assert event["decide_ns"] > 0
    # emitted elapsed equals what was observed inside the block
    assert event["decide_ns"] > elapsed


def test_span_emit_none_is_noop() -> None:
    # No exception, nothing emitted, no collect bucket.
    with Span(SpanName.DECIDE, emit=None):
        pass
    with Span(SpanName.DECIDE):
        pass
    # Reuse across multiple entries stays safe.
    sp = Span(SpanName.DECISION_SUBMIT)
    with sp:
        pass
    with sp:
        pass


def test_span_elapsed_zero_when_not_entered() -> None:
    sp = Span(SpanName.DECIDE)
    assert sp.elapsed_ns() == 0


def test_collect_buckets_nested_spans_deterministically() -> None:
    bucket: dict[str, int] = {}
    with Span(SpanName.TICK_ELAPSED, collect=bucket):
        with Span(SpanName.DECIDE, collect=bucket):
            time.sleep(0.001)
        with Span(SpanName.DECISION_SUBMIT, collect=bucket):
            time.sleep(0.001)
    assert set(bucket) == {
        "tick_elapsed_ns",
        "decide_ns",
        "decision_submit_ns",
    }
    # Outer span strictly dominates each nested span even with 1ms sleeps.
    assert bucket["tick_elapsed_ns"] > bucket["decide_ns"]
    assert bucket["tick_elapsed_ns"] > bucket["decision_submit_ns"]
    # Repeated entry with the same bucket overwrites deterministically.
    with Span(SpanName.DECIDE, collect=bucket):
        pass
    assert bucket["decide_ns"] >= 0


def test_decorator_wraps_and_preserves_metadata() -> None:
    events: list[dict[str, int]] = []

    def emit(event: dict[str, int]) -> None:
        events.append(event)

    @span(SpanName.DECISION_SUBMIT, emit=emit)
    def run(scale: int) -> int:
        """Docstring must survive wrapping."""
        return scale * 2

    assert run.__name__ == "run"
    assert run.__doc__ == "Docstring must survive wrapping."
    assert run(21) == 42
    assert len(events) == 1
    assert set(events[0]) == {"decision_submit_ns"}
    assert events[0]["decision_submit_ns"] > 0


def test_decorator_does_not_mask_exceptions() -> None:
    events: list[dict[str, int]] = []

    def emit(event: dict[str, int]) -> None:
        events.append(event)

    @span(SpanName.DECIDE, emit=emit)
    def boom() -> None:
        raise ValueError("boom")

    with pytest.raises(ValueError, match="boom"):
        boom()
    # The span still emitted before the exception propagated.
    assert len(events) == 1
    assert set(events[0]) == {"decide_ns"}
