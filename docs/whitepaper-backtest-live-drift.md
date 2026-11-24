# Eliminating Backtest↔Live Drift

### One decision pipeline for production options execution

**Author:** nodeicode · **Status:** draft v1 · **Repo:** `quant-engine` (private)
· **Companion docs:** `docs/evaluation-methodology.md`, `docs/correctness-layer.md`

---

## Abstract

Most algorithmic strategies die not for lack of alpha but for **backtest–live
drift**: the live system is a separate, sloppier reimplementation of the idea
that was backtested, so the thing that was validated on paper never really
runs in production. This paper describes a concrete, working alternative: a
production trading engine in which the **exact same decision pipeline** executes
in backtest and live, differing only in the **executor** (which turns a signal
into a fill). The invariant is maintained by construction, not by convention, and
is enforced operationally by a sidecar that re-runs the backtest's decision
logic live and diffs it against actual decisions.

## 1. The problem

A quant strategy is two artifacts that must agree: the *idea* (a rule for when
to enter/exit, expressed in indicators) and the *plumbing* (data, scheduling,
order submission). In a typical shop these become **two codebases**:

1. A **backtest** — idealized: perfect historical bars, no latency, no partial
   fills, no live scheduling.
2. A **live system** — a rewrite that must handle real-time data, throttles,
   fills, and restarts.

Both are constantly edited. Over time they drift: the backtest uses adjusted
bars and the live path uses a different source; the backtest enters on
yesterday's signal but live schedules differently; the backtest assumes an exit
bar that live's clock never produces. Each drift is small and plausible; a bad
week can be rationalized away. The result is that the strategy you validated is
not the strategy you run, and nobody has a single artifact that says what it
*actually* does in both worlds.

## 2. The core design: one pipeline, swap the executor

The engine centralizes the decision in one function
(`process_strategy`), and both environments call it:

```
production:  trader → engine.process_strategy(tick, executor=LiveExecutor)
backtest:    driver → engine.process_strategy(tick, executor=BacktestExecutor)
```

The entire decision — manage exits, entry gate, signal evaluation, sizing — is
identical. The **executor** is a thin, injected interface: the backtest executor
returns a simulated fill; the live executor routes an actual order. Everything
the optimizer or reviewer wants to inspect lives in one place, so there is
structurally no "live version" of the logic to drift.

Two invariants make this sound rather than cosmetic:

- **Adjusted-bar discipline.** Signals evaluate on adjusted bars in both worlds;
  raw close is backtest-only. Because adjusted `Close == Raw_Close` on the
  decision bar (factor 1.0 after the last corporate action), the engine sees the
  same price in backtest and live — no lookahead from unadjusted history, and
  no "different data" excuse for divergence.
- **Bar-driven scheduling.** Both worlds are driven by bars, not by ad-hoc
  clocks or intra-signal fetches. A bar is a bar whether it comes from history
  or the feed, so the decision cadence is identical by construction.

## 3. Enforcing trust at runtime: the sidecar

"One pipeline" is a strong *structural* guarantee, but data and order handling
can still drift. So the system adds an **operational guarantee**: a sidecar
process that, live, re-runs the backtest's decision function
(`shadow_entry_signal`, `shadow_manage_exit`) on the same bars and diffs it
against what the live path decided. Divergence is surfaced as a named sentinel
(`signal_drift`, `manage_exit_drift`, `shadow_bar_missing`), and orders are
reconciled to position. (Full detail: `docs/correctness-layer.md`.)

The sidecar is deliberately **out-of-band** — its own process and GIL, fed over
an IPC queue, async and non-blocking — so the thing that watches the hot path
never perturbs the timing it was built to measure.

## 4. What this buys

- **A single artifact of truth.** One function, read once, is the whole
  strategy. Review the pipeline and you have reviewed backtest and live.
- **Drift is caught, not discovered.** When live diverges, a named sentinel
  fires, not a mysterious weekend loss.
- **Backtests are honest by construction** about *logic* (there is no second
  implementation to silently overfit); the residual honesty concerns — data,
  fills, execution realism — are addressed explicitly by the shadow/reconcile
  layer and the evaluation methodology.
- **Reviewable.** A hiring engineer or auditor can clone, run the pipeline, and
  independently confirm the live path and backtest path are the same code.

## 5. Honest limitations

- One pipeline eliminates *logic* drift, not *execution* drift: fills, slippage,
  and capacity are separate concerns. They are tracked (latency/slippage
  telemetry) but not solved by this design.
- Formal out-of-sample / walk-forward evaluation is the intended next tightening
  (roadmap) but is not yet shipped; today the regime + parameter-plateau +
  shadow checks are the shipped discipline.
- The guarantee depends on the shadow logic being kept in lockstep with the live
  decision path; drift sentinels make a lapse visible, but they do not make it
  impossible.

## 6. Conclusion

Backtest-live drift is the quiet killer of quant strategies. Rather than fight
it with discipline, this engine removes it by construction: one decision
pipeline, differing only in the executor, with a shadow sidecar that re-derives
the reference decision live and flags any divergence. The result is a system
where "what I validated" and "what I run" are the same artifact — which is the
property that makes a backtest trustworthy at all.

---

*This document is engineering methodology. It contains no strategy
performance, parameters, or private alpha.*