## Purpose

Observability primitives, nothing more: named span timing and an optional emit interface. Shadow signals, reconciliation, sidecars, dashboards — everything else is extended functionality developers build on these primitives.

## ADDED Requirements

### Requirement: Span timing primitive

The SDK MUST provide a span primitive that records elapsed time for a named stage (signal compute, generate, manage, sizing, dispatch) and MUST keep age (bar staleness) perpendicular to duration (loop/fetch time), so feed skew is never conflated with slowness. Spans MUST cost nothing measurable when unobserved (context-manager overhead only).

#### Scenario: Named stage timing
GIVEN the engine executes signal and manage stages
WHEN the stage timings are collected
THEN each stage reports its own duration

#### Scenario: Age and duration are distinct
GIVEN a stale bar and a fast loop
WHEN both metrics are recorded
THEN bar staleness and processing duration are recorded as separate values with separate units

#### Scenario: Unobserved spans are cheap
GIVEN a run with no sink attached
WHEN the pipeline executes spans
THEN decisions and timings are identical to a no-span run with negligible overhead

### Requirement: Optional emit interface

All telemetry MUST flow through one optional emit interface (`emit(rows)`); with no sink installed the SDK MUST behave identically and emit nothing. Emitting MUST NOT block or slow the decision path (sinks MUST be additive — async/batched by the sink, never by the hot path).

#### Scenario: No sink, no output
GIVEN a backtest run without a sink
WHEN the pipeline finishes
THEN no telemetry is emitted and decisions are unaffected

#### Scenario: Sink receives rows
GIVEN a sink installed (console, JSONL, or caller-defined)
WHEN the pipeline runs
THEN typed metric rows are delivered to the sink

### Requirement: Extension surface for developer-built functionality

The primitives MUST expose enough structure (span lifecycle, row schema, hook registration) for developers to build extended functionality — heartbeat monitors, divergence checks, reconciliation, dashboards — WITHOUT modifying the engine. New extended functionality MUST be implementable as a sink/hook over the emit interface and spans.

#### Scenario: Extended monitor built on primitives
GIVEN a developer wants shadow-signal divergence monitoring
WHEN they implement it against the span + emit primitives
THEN it works without engine changes and consumes the same rows backtest and live produce
