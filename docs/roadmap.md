# Roadmap — Making This Repo Speak for Itself

> **Status:** living plan. Everything in here is executable by one person
> (me) in a sequence, with each commit telling part of the story.
>
> **Audience:** future employer (quant fund / AI lab) and future-me.
> The goal is not "a bigger codebase" — it is *a claim about how I engineer
> trading systems, backed by a repo a stranger can clone, run, and re-verify.*

---

## 1. WHY this exists (the one-paragraph story)

Most trading systems are two different programs: a backtest that never
survives contact with live markets (because the live path is a separate,
sloppier rewrite), or a live bot with no measurement, no correctness checks,
and uncontrolled order flow.

This repo is the counter-example. It is a production trading engine built so
that **the exact same decision pipeline runs in backtest and live**, with
honest latency/age measurement, a correctness *sidecar* that watches the trader
in real time, real multi-leg options execution that survives partial fills, and
clean, sanitized infrastructure. It exists to prove, not to claim: an edge can
go from idea → backtest → paper → live **without drift, without latency
blindness, and without falling apart on real order flow.**

If an interviewer asks "why did you build this?", the answer is:

> Because the hard part of quant is not finding a signal — it is knowing your
> backtest is telling the truth, and keeping a strategy honest and stable
> between backtest and live. I engineered for that specifically.

---

## 2. WHAT this system is (the map)

| Layer | Piece | What it actually does |
|---|---|---|
| Decision engine | `quant/core/engine.py` | **Single** `process_strategy` pipeline shared byte-for-byte by backtest and live; only the *executor* differs. Kills backtest↔live drift by construction. |
| Data | `quant/data/` (+ `ws/`) | REST handler + live stream → `MemBarsAggregator`. **Adjusted-bar invariant**: signals always run on adjusted bars; raw close is backtest-only. |
| Execution | `python/executors/` | Asset-class routing; single-leg options/equity/futures; **4-leg IC (MLEG)** executor with **adaptive fill loops** that handle partial/replacement fills (`fix-ic-fill-feedback-loop`). |
| Orchestration | `python/trader.py` + `config_sync.py` | 1-min heartbeat, per-strategy thread fan-out, live config via **SSM** (no restarts), state persistence, market-hours scheduling. |
| Correctness | `python/monitor/` (`sidecar.py`/`emit.py`) | Async sidecar consuming an IPC queue: metrics, **shadow signals**, **position reconcile**. This is the correctness layer. |
| Measurement | latency metric dictionary | **Age vs duration kept perpendicular** (`bar_staleness_*` vs `*_ns`) — separates SIP feed skew from fetch slowness. |
| Infra | `infrastructure/` (OpenTofu) | OIDC GitHub Actions → AWS, EC2 auto-started/stopped by EventBridge for market hours, secrets in SSM (none checked in), staging + prod. |
| CI | `.github/workflows` | `ci.yml` tests + `deploy.yml` via SSM RunCommand to the instances. |

**Alpha boundary:** `quant/strategies/**` (the real signal rules, params,
thresholds, feature config) is **private**. The public repo publishes the
engine, executors, data layer, monitor, IaC, and a *synthetic* example
strategy that implements the same interface — never the alpha's numbers.

---

## 3. Principles (the guardrails everything follows)

1. **Interface over params.** Publish the strategy *protocol*
   (`calculate_indicators` / `generate_signals` / `manage_trade`); keep all
   real parameters, thresholds, and feature weights private.
2. **Methodology over returns.** Publish how I measure (latency, fills,
   drift); never publish performance numbers that invite "prove the alpha."
3. **Reproducibility before publishing.** A stranger's fresh clone must run
   the same backtest and get the same report. No result is publishable until
   `git clone && uv sync && uv run test` reproduces it.
4. **Honesty as the differentiator.** Backtests report out-of-sample /
   walk-forward results, not in-sample; the sidecar's reconcile output is real
   and shown, gaps included.
5. **Never scatter secrets.** `.env`/`*.pem`/keys stay gitignored; SSM is the
   source of truth; commit history may contain **no** credential.

---

## 4. The build pillars (with status)

### P1 — Correctness layer *(EXISTS via sidecar — sharpen, don't invent)*
Already: async sidecar, IPC queue, metrics, shadow signals, reconcile.
- [x] Document it prominently → **`docs/correctness-layer.md`** (name, JSONL rows, drift sentinels).
- [x] **shadow-signal divergence check — already shipped** (`signal_drift`, `manage_exit_drift`, `shadow_bar_missing`, order↔position reconcile). Verified against `python/monitor/sidecar.py` + `quant/core/monitor_utils.py`; now documented.
 
### P2 — Evaluation discipline *(partially exists — complete)*
`quant/src/quant/strategies/README.md` already requires per-strategy markdown with param-sensitivity
and Monte Carlo. Lift the bar:
- [ ] (Take as TODO for later, build in public) Enforce **out-of-sample**: backtests report results on an untouched hold-out
      window; document the train/test split rule.
- [ ] (Take as TODO for later, build in public) Add **walk-forward** to the backtest driver (small re-use of existing
      `base_backtest.py` machinery).
- [ ] Publish (public) a *methodology* note: "how this engine prevents leak /
      overfitting," generic, no alpha numbers.

### P3 — Diagnostic metrics *(EXISTS in latency dict — broaden slightly)*
Latency age/duration is already world-class. Add:
- [ ] (Take as TODO for later, build in public)  **Fill quality**: fill-vs-mid slippage (bps) vs spread, per asset class —
      the honesty a fund actually screens for.
- [ ] (Take as TODO for later, build in public)  **Capacity estimate**: how far fills stay within spread before impact;
      a ceiling, not a vanity number.

### P4 — One-command reproducibility (This whole idea can be a TODO but its not viable to build right now high effort and high chances of breaking existing logic)

### P5 — IaC hardening story *(EXISTS — write it up)*
- [ ] `infrastructure/README.md` is already strong (OIDC, EventBridge schedule,
      SSM). Add the "why" paragraph (no checked-in keys; zero-config instance
      bootstrap) so the story reads as intent, not accident.

### P6 — Whitepaper *(NEW — de-templated to the real system)*
2–3 pages on **one** real engineering bottleneck here. Best candidates:
- "Eliminating backtest↔live drift: one decision pipeline for options execution"
  *(strongest, matches the code).*
- "Adaptive fill-loop management for multi-leg iron-condor orders under partial
  fills" *(execution grit, impossible to fake).*
Published to `lohitaryan.dev`, pointing back at the public repo.

---

## 5. Public narrative through commits

Because strategies stay private, the **public repo's commit history is the
story**. So the work is sequenced as small, well-labeled commits/PRs that read
as an arc:

```
docs(roadmap): state the why + boundaries          ← you are here
docs(readme): lead with WHY/WHAT, alpha boundary
docs(agents): working agreement for contributors & AI
feat(correctness): shadow-signal divergence sentinel
feat(eval): out-of-sample + walk-forward driver
feat(report): one-command reproducible system report
docs(whitepaper): eliminate backtest⇄live drift
chore(repo): rename this repo (see §7)
```

Each item is a reviewable PR, not a monolith.

---

## 6. Sequencing vs Oct 2027

1. **Lock down** (done): repos private, secret audit clean.
2. **Sharpen correctness + eval + metrics** (P1–P3): the actual lift, ~now.
3. **Run live through 2026–27**, collecting real telemetry (fills, slippage,
   latency, reconcile) via CloudWatch/sidecar. This is authentic proof money
   can't fake.
4. **Reproducibility + whitepaper** (P4, P6) once the system is stable.
5. **Apply** with the repro artifact and live telemetry in hand.

Order = *make it correct → make it live → publish what it proves.*

---

## 7. Repo identity — **DECIDED: `quant-engine`**

Renamed `nodeicode/alpaca-options-prod` → **`nodeicode/quant-engine`** (private)
on GitHub; local remote, IaC `repo_url`, OIDC IAM condition, bootstrap script,
and submodule reference all updated to `quant-engine`. References to the old
name are gone. The `quant` submodule keeps its own name.

Naming intent: it is an engine (engine + executors + data + monitor + IaC),
not just an options strategy — and it generalizes across asset classes.