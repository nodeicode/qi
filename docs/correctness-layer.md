# The Correctness Layer (sidecar monitor)

The engine is not trusted to police itself. A **sidecar process**
(`python/monitor/sidecar.py`) runs beside the live trader, consumes everything
the engine emits over an IPC queue, and independently re-derives what the
system **should** have decided. Divergence between what the live path decided
and what the backtest-reference logic `would` have decided is the drift signal
this whole system is built to catch.

> This is what "knowing your backtest is telling the truth" means operationally:
> the backtest's decision logic is run live, in shadow, on the same bars — and
> the two are diffed continuously.

---

## Why it exists

In this system the live path and the backtest path share **one** decision
pipeline (`quant/core/engine.py::process_strategy`). That removes the whole
class of bugs where the live rewrite drifts from the backtest. But "one
pipeline" still leaves the possibility that *data* or *order handling* drifts.
The sidecar closes that gap by running shadow logic against the live bars and
reconciling orders to position.

## The three jobs

| Job | What it records | Category |
|---|---|---|
| **Metrics** | latency/staleness rows (`signal_evaluated`, `manage_evaluated`) | measurement |
| **Shadow signals** | what the backtest-reference logic *would* have decided | drift detection |
| **Reconcile** | does the broker position match what we think we opened/closed | correctness |

## The drift sentinels

Each of these is an **alarm** — not just a log line. When one fires, the system
is telling you the live path and its reference diverged, which is exactly the
failure mode that silently kills retail strategies.

| Sentinel | Means |
|---|---|
| `signal_drift` | live entry `ShadowSignalFeed` differs from backtest-reference shadow signal on the same bar |
| `manage_exit_drift` | live manage/exit decision differs from the shadow manage decision |
| `shadow_bar_missing` | the shadow could not run because the reference bar was missing — a **blind spot**, flagged instead of silently skipped |
| `signal_without_trade` | engine produced a signal but no order was recorded for it |
| `trade_without_signal` | an order was recorded with no corresponding signal |
| `bar_staleness_breach` | resolved bar age exceeded the freshness SLO (10 ms excess budget) |
| `manage parity blind-spot guard` | `manage_evaluated` events stop arriving entirely → the manage parity plane would go silently dead without it |
| `[MONITOR][LATENCY]` queue drops | monitor is falling behind → reported rather than silently dropped |

## What the JSONL looks like

Everything appends to `monitor_metrics.jsonl` as one JSON object per line. A
shadow-manage row, condensed:

```json
{
  "kind": "shadow_manage_result",
  "strategy": "example_equity",
  "ticker": "SPY",
  "entry_bar_ts": "2025-06-10T18:30:00Z",
  "manage_bar_ts": "2025-06-10T18:35:00Z",
  "shadow_exit": "STOP",
  "shadow_exit_bar": 35,
  "shadow_exit_idx": 35,
  "shadow_manage_ns": 128000,
  "tf": "2025-06-10T18:35:03Z"
}
```

When the live decision and the shadow disagree, a drift event is also emitted
(here: live chose `STOP` on bar 2, shadow chose `STOP` on bar 35 — a mismatch in
*when* to exit):

```
[MONITOR] manage_exit_drift strategy=example_equity ticker=SPY entry=STOP live=STOP/2 shadow=STOP/35
```

## Key design principles

1. **Out-of-band.** The sidecar runs in its own process/GIL and is drained from
   a queue — it never blocks or slows the trading hot path (`emit` is async and
   non-blocking by construction).
2. **Same bars, reference logic.** The shadow re-runs the *backtest* decision
   function (`shadow_entry_signal`, `shadow_manage_exit`) so drift means "live
   behaved differently from our own reference," not "live is different from an
   idealized model."
3. **Blind spots are loud.** When the reference can't be computed, the system
   flags it (`shadow_bar_missing`) rather than silently passing.
4. **Reporting is durable.** JSONL via a single persistent stdlib fd — no
   per-event open/close, so the monitor never perturbs the timing it exists to
   measure.

## Where it lives

| Piece | Path |
|---|---|
| Emitter (async, hot path) | `python/monitor/emit.py` |
| Sidecar (consumer, own process) | `python/monitor/sidecar.py` |
| Staleness + telemetry helpers | `quant/core/_utils.py` (shadow/sidecar moved to the private superset) |
| Metric dictionary | see root `README.md` → Latency metric dictionary |

**CloudWatch:** the instances stream these logs (`python/trader.log`, sidecar
metrics) so the drift sentinels are queryable in production long after the fact.