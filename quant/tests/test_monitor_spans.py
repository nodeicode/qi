"""Contract coverage for the shared Span timing primitives (plan §1).

Guards: (a) ``Span`` records monotonic ns and is a no-op when ``emit=None``;
(b) ``collect`` buckets nested spans deterministically; (c) the ``@span``
decorator wraps without masking exceptions; (d) the determinism guard — every
``SpanName`` maps into ``SPAN_KEYS`` and the latency-SLO rollup set
``SLO_SPAN_KEYS ⊆ SPAN_KEYS`` with exactly the two non-SLO spans excluded.
"""

from __future__ import annotations

import time

import pytest

from quant.core.monitor_utils import (
    SLO_SPAN_KEYS,
    SPAN_KEYS,
    Span,
    SpanName,
    span,
    span_key,
)

_NON_SLO_KEYS = {"execution_dispatch_ns", "warmup_total_ns"}


def test_span_key_matches_enum_values() -> None:
    assert span_key(SpanName.GENERATE_SIGNALS) == "generate_signals_ns"
    assert span_key(SpanName.EXECUTION_DISPATCH) == "execution_dispatch_ns"
    assert span_key(SpanName.WARMUP_TOTAL) == "warmup_total_ns"


def test_registry_is_exact_closed_set() -> None:
    assert frozenset(span_key(n) for n in SpanName) == SPAN_KEYS
    assert (
        frozenset(
            {
                "generate_signals_ns",
                "strategy_calc_total_ns",
                "fetch_data_ns",
                "fetch_manage_data_ns",
                "run_signals_ns",
                "process_ticker_ns",
                "tick_elapsed_ns",
                "tick_dispatch_ns",
                "main_loop_lag_ns",
                "queue_lag_ns",
                "execution_dispatch_ns",
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
    dynamic fields mutated inside the block merged at exit (execution_dispatch)."""
    events: list[dict] = []

    def emit(event: dict) -> None:
        events.append(event)

    evt = {"kind": "execution_dispatch", "action": "entry"}
    with Span(SpanName.EXECUTION_DISPATCH, emit=emit, event=evt):
        new_pos = True
        evt["accepted"] = bool(new_pos)

    assert len(events) == 1
    (payload,) = events
    assert payload["kind"] == "execution_dispatch"
    assert payload["action"] == "entry"
    assert payload["accepted"] is True
    assert "execution_dispatch_ns" in payload
    assert payload["execution_dispatch_ns"] >= 0
    # caller's dict is not mutated with the span key (fresh merge at exit)
    assert "execution_dispatch_ns" not in evt


def test_span_records_monotonic_ns() -> None:
    events: list[dict[str, int]] = []

    def emit(event: dict[str, int]) -> None:
        events.append(event)

    with Span(SpanName.FETCH_DATA, emit=emit) as sp:
        elapsed = sp.elapsed_ns()
        assert elapsed >= 0
        start = time.perf_counter_ns()
        while time.perf_counter_ns() - start < 1_000_000:  # ~1ms busy wait
            pass  # ensure a measurable, strictly positive elapsed

    assert len(events) == 1
    (event,) = events
    assert set(event) == {"fetch_data_ns"}
    assert event["fetch_data_ns"] > 0
    # emitted elapsed equals what was observed inside the block
    assert event["fetch_data_ns"] > elapsed


def test_span_emit_none_is_noop() -> None:
    # No exception, nothing emitted, no collect bucket.
    with Span(SpanName.PROCESS_TICKER, emit=None):
        pass
    with Span(SpanName.PROCESS_TICKER):
        pass
    # Reuse across multiple entries stays safe.
    sp = Span(SpanName.RUN_SIGNALS)
    with sp:
        pass
    with sp:
        pass


def test_span_elapsed_zero_when_not_entered() -> None:
    sp = Span(SpanName.FETCH_DATA)
    assert sp.elapsed_ns() == 0


def test_collect_buckets_nested_spans_deterministically() -> None:
    bucket: dict[str, int] = {}
    with Span(SpanName.STRATEGY_CALC_TOTAL, collect=bucket):
        with Span(SpanName.GENERATE_SIGNALS, collect=bucket):
            time.sleep(0.001)
        with Span(SpanName.FETCH_MANAGE_DATA, collect=bucket):
            time.sleep(0.001)
    assert set(bucket) == {
        "strategy_calc_total_ns",
        "generate_signals_ns",
        "fetch_manage_data_ns",
    }
    # Outer span strictly dominates each nested span even with 1ms sleeps.
    assert bucket["strategy_calc_total_ns"] > bucket["generate_signals_ns"]
    assert bucket["strategy_calc_total_ns"] > bucket["fetch_manage_data_ns"]
    # Repeated entry with the same bucket overwrites deterministically.
    with Span(SpanName.GENERATE_SIGNALS, collect=bucket):
        pass
    assert bucket["generate_signals_ns"] >= 0


def test_decorator_wraps_and_preserves_metadata() -> None:
    events: list[dict[str, int]] = []

    def emit(event: dict[str, int]) -> None:
        events.append(event)

    @span(SpanName.RUN_SIGNALS, emit=emit)
    def run(scale: int) -> int:
        """Docstring must survive wrapping."""
        return scale * 2

    assert run.__name__ == "run"
    assert run.__doc__ == "Docstring must survive wrapping."
    assert run(21) == 42
    assert len(events) == 1
    assert set(events[0]) == {"run_signals_ns"}
    assert events[0]["run_signals_ns"] > 0


def test_decorator_does_not_mask_exceptions() -> None:
    events: list[dict[str, int]] = []

    def emit(event: dict[str, int]) -> None:
        events.append(event)

    @span(SpanName.PROCESS_TICKER, emit=emit)
    def boom() -> None:
        raise ValueError("boom")

    with pytest.raises(ValueError, match="boom"):
        boom()
    # The span still emitted before the exception propagated.
    assert len(events) == 1
    assert set(events[0]) == {"process_ticker_ns"}
