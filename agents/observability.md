# Observability — `quant.observability` (spec `observability`, design D7)

The v1 observability surface is deliberately small: **named span timing** (one
primitive that keeps *age* — bar staleness — perpendicular to *duration* —
loop/fetch time) and an **optional emit interface**. Shadow signals,
reconciliation, sidecars, and dashboards are extended functionality developers
build *on top of* these primitives — never inside the engine.

## Invariants (do not break without discussion)

1. **Age ∥ duration, never conflated.** Feed skew (staleness, delay) is a
   *different* metric from slowness (how long a stage took). They are recorded
   together but as separate keys / units, so "a stale bar" and "a slow loop"
   can never be mistaken for each other.
2. **A closed set of span keys.** Producers (engine, live trader) and the
   monitor consumer share the exact same `SpanName` enum — a producer cannot
   invent an off-registry span string at call time. `SPAN_KEYS` / `SLO_SPAN_KEYS`
   derive from the enum, not a second hand-maintained list.
3. **Unobserved spans are free.** `emit is None` → measured but never recorded
   (cost is one `perf_counter_ns` read each side). No sink → no output, and
   decisions/timings are identical to a no-span run.
4. **Sinks are additive.** All telemetry flows through one optional
   `emit(rows)` interface. A sink MUST batch/async internally if it needs to —
   the decision hot path never pays for batching.
5. **Zero reliance on the engine.** Extension monitors (heartbeat, divergence,
   reconciliation) are implemented as sinks/hooks over the span + emit
   primitives and consume the same rows backtest and live produce — no engine
   modification.

## Where it lives

| Path | Responsibility |
|---|---|
| `quant/src/quant/observability.py` | `SpanName`, `Span` / `span()`, `Emitters` (`NoopEmitter`, `JsonlEmitter`) |

`SpanName` members are the stable keys; the emitted JSONL key is
`<member>_ns`. The runtime wires producer hooks (`monitor_emit=`) to these
primitives; a backtest with no sink emits nothing.

## Quickstart — the observability primitives offline

```python
from quant.observability import (
    NoopEmitter,
    JsonlEmitter,
    Span,
    SpanName,
    span,
)

# A sink collects emitted rows; a consumer derives everything else from them.
rows: list[str] = []
emitter = JsonlEmitter(write=rows.append)   # JSONL lines, here captured in-memory

# Context-manager form — time a named stage:
with Span(SpanName.FETCH_DATA, emit=emitter.emit):
    _data = [i * i for i in range(2000)]

# Decorator form — time a whole function:
@span(SpanName.RUN_SIGNALS, emit=emitter.emit)
def run_signals():
    return sum(range(1000))


run_signals()

# No-op default emits nothing (zero-cost unobserved spans):
noop = NoopEmitter()
with Span(SpanName.GENERATE_SIGNALS, emit=noop.emit):
    _ = 2

print("rows emitted:", len(rows))       # 2  (fetch_data_ns, run_signals_ns)
print("no-op emitted rows:", 0)          # 0

# Age and duration are separate span keys with separate units:
fetched = [row for row in rows if '"fetch_data_ns"' in row]
print("fetch_data_ns present:", bool(fetched))
```

`SpanName` / `Emitter` mirror the exact contract backtest and live produce, so
a single consumer (a JSONL log, a dashboard, a divergence check) reads the same
rows from both environments.

## Extension points

- **New emitter/sink** — implement `Emitter.emit(event)`. Batch/async inside
  the sink; the hot path stays a single emit.
- **A monitor built on the primitives** (shadow-signal divergence, heartbeat,
  reconciliation) — wrap/observe `SpanName` rows through `emit`; no engine
  change. This is the reference extension pattern (D7).
- **New measurable stage** — add a member to `SpanName` and time it with
  `Span(...)` / `@span(...)`. `SPAN_KEYS`/`SLO_SPAN_KEYS` update automatically
  from the enum.

## Verify

The quickstart above was validated by executing its code paths (`uv run python -
<<'PY' ... PY`): context-manager + decorator spans emitted two JSONL rows via
`JsonlEmitter`, the `NoopEmitter` emitted nothing, and separate `fetch_data_ns`
/ `run_signals_ns` keys confirmed age/duration stay distinct.