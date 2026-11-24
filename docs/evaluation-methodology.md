# Evaluation Methodology — How a Strategy Is Proven Here

*Methodology only. No performance numbers, no parameters, no alpha. The point is
to make the discipline of evaluation explicit and, therefore, auditable.*

---

## The core claim

The failure most strategies die from is not a bad signal — it is **not knowing
whether the backtest is telling the truth**. Everything here is an answer to
that question, and the answers are structured so a stranger can re-derive them.

## 1. Backtest and live are the same program

The single non-negotiable invariant across the whole system:

> There is **one** decision pipeline (`process_strategy`). Backtest and live
> drive it identically; **only the executor differs** (backtest = simulated
> fill, live = real order).

This eliminates the largest class of evaluation error: a live rewrite that
drifts from the backtest. When a backtest says something, it is the *same code*
that will run live — a reviewer can read the live path and the backtest path
and confirm they are the same decision logic.

Supporting data invariant: signals always evaluate on **adjusted** bars; raw
close is backtest-only. On the decision bar adjusted `Close == Raw_Close`
(factor 1.0 after the last corporate action), so the engine genuinely sees the
same numbers in both worlds.

## 2. The correctness sidecar (live truth)

Even with one pipeline, *data* and *order handling* could drift. The sidecar
monitor runs the backtest-reference decision live, in shadow, on the same bars,
and diffs it against what the live path did — surfacing `signal_drift`,
`manage_exit_drift`, `shadow_bar_missing`, and reconciling orders to position.
See `docs/correctness-layer.md`. A strategy is "honest" only if its shadow and
live decisions agree.

## 3. The evaluation bar (what a deployable strategy must show)

Every strategy that moves toward paper/production must come with a markdown
(single source of truth, `quant/README.md`) containing, at minimum:

- **Mathematical / statistical summary** — *why* the idea works, stated as a
  mechanism, not a curve.
- **Regime analysis** — historical behavior across bull / bear / choppy /
  crash. The explicit red flag: a strategy that is great in two regimes and
  goes to −100% in a third. The *join* between regimes matters more than the
  best regime.
- **Parameter-sensitivity analysis** — a robust *plateau* of parameters, used
  as a direct overfitting check. If results collapse off a knife-edge
  parameter, the strategy is tuned, not found.
- **Monte Carlo** — performance across many synthetic future return paths
  (adaptability check), not a single hand-picked backtest.

A deployed strategy must have an **edge over benchmark** (Sharpe / returns /
positive alpha) to even be eligible.

## 4. Guarding against leak and overfitting

- **One pipeline → no separate live reimplementation** to silently overfit.
- **Adjusted-bar discipline → no lookahead via unadjusted history.**
- **Param plateau → results must be stable, not peaked.**
- **Shadow correctness → live behavior must match reference, or it's flagged.**
- **Regime join → a strategy must not be a single-environment artifact.**
- **Monte Carlo → must survive synthetic paths, not just its realized one.**

> **Planned (on roadmap, not yet shipped):** formal **out-of-sample** reporting
> (results on an untouched hold-out) and **walk-forward** rolling evaluation. As
> of now the sensitivity/plateau + regime + shadow checks are the shipped
> discipline; OOS/walk-forward formalization is the next tightening (tracked in
> `docs/roadmap.md`).

## 5. Measure the *journey*, not just the destination

The engine records **latency as age vs duration, perpendicular** (SIP feed skew
≠ fetch slowness) and tracks fill/staleness telemetry. Two backtests that hit
the same return but one under tighter, more honest execution assumptions is the
one worth trusting. The methodology treats *how* a result was produced — drift,
leak, execution realism, regime honesty — as the primary signal, and the summary
number as secondary.

---

*This document describes the evaluation standard. It deliberately contains no
strategy performance, parameters, or private alpha — by design.*