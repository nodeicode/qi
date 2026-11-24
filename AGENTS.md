# AGENTS.md — Working Agreement

For human contributors **and** AI coding agents working in this repository.
If you are an agent, read this before changing anything. It exists because this
repo runs **real money paths** — some moves read like refactors but change live
behavior.

---

## 0. What this repo is (10-second orientation)

A production quant trading engine with a **single decision pipeline shared by
backtest and live** (`quant/core/engine.py::process_strategy`). Live execution
lives in `python/` (`trader.py`, `executors/`), the engine/backtest/data layer
in `quant/`, infra in `infrastructure/` (OpenTofu), CI/CD in `.github/`.

- `quant/` is currently a **git submodule** (remote `nodeicode/quant`).
- `quant/strategies/**` is **private alpha** — do not publish, rename, or bind
  the submodule URL to anything that would expose it.
- Repos are **private** on GitHub. Do not broaden visibility.

The authoritative plan/why is `docs/roadmap.md`.

---

## 1. Security rules (non-negotiable)

- `.env`, `*.pem`, `*.key`, `*.p12` are **gitignored** — keep it that way. Never
  `git add -f` them. Rotate any secret that touches a public/committed surface.
- Real secrets (Alpaca, Databento, Polygon, deploy key) live in **AWS SSM
  Parameter Store**, fetched at runtime (`config_sync.py`, `bootstrap.sh`).
  Never hardcode them; never print them to logs.
- `git log --all` must contain **no** credential, token, .pem, or `.env` path.
- Commit history of the **public** scaffold must contain **no strategy files**
  (`quant/strategies/**`) and no strategy parameters. Use `git filter-repo` if
  ever needed before publishing.
- The `.tfvars` only hold non-secret values (region, instance type, key-pair
  name). If a real value ever lands in a `.tfvars`, move it to SSM and scrub
  history.

---

## 2. Architecture invariants (do not break without discussion)

1. **One pipeline.** Backtest and live both go through
   `process_strategy`/`process_ticker`. A change to the decision path must be a
   change that lives in `quant/core/engine.py`, not a parallel rethink in
   `python/`. Live and backtest differ **only** in the executor.
2. **Adjusted-bar invariant.** Signals run on **adjusted** bars only. If you
   introduce a raw-close read on the live path, that is a bug — `Raw_Close` is
   backtest-only.
3. **Bar-driven scheduling.** `INTERVAL` (signal) and `MANAGE_INTERVAL`
   (manage) gates drive the tick. Do not introduce intra-`generate_signals`
   data fetches or ad-hoc clocks.
4. **Live config is SSM.** Strategy config is reconciled at runtime from SSM; no
   local config file is the live source of truth.
5. **Order flow is gated and controlled.** Multi-leg (IC) execution uses the
   adaptive fill-loop path. Any change to order submission must preserve
   fill-feedback handling and never bypass stop-before-submit-style guards.
6. **Monitoring is part of the system.** The sidecar (`python/monitor/`) is the
   correctness layer — metrics, shadow signals, reconcile. New execution paths
   must emit and reconcile, not just trade.
7. **Import layering is enforced by a test, not a linter rule.** The old
   `TID251` banned-api fence is gone. Keep the one-way layering
   (`quant.utils` is a leaf; `quant.backtest` / `quant.strategies` are terminal
   leaves) via `quant/tests/test_import_layering.py`, and give any
   package-internal module a `_private.py` suffix instead of relying on a linter.
   A sanctioned cross-leaf import carries an inline `# layering-ok` on its line.

---

## 3. Commandments / dev workflow

- Package manager is **uv**. Python versions pinned via `.python-version`.
- Tests: `uv run test` at the repo root runs the full workspace (quant + python
suites). `-m data|backtest|utils|strategies|unit|e2e` scopes a module (single
entry point `quant/tests/_runner.py`); `-k <filter>` passes through to pytest.
- Lint/format: **ruff** (pre-commit present in `python/` and `quant/`). Let the
  pre-commit hook run; `ruff check .` + `ruff format .` locally.
- Coverage reports (`htmlcov/`, `.coverage`) are gitignored artifacts.
- Docs live in Markdown next to the code they describe; update the README and
  the relevant `*README.md` when behavior changes. Strategy docs follow the
  structure mandated in `quant/README.md` (math/stat summary, bull/bear/choppy/
  crash, param sensitivity, Monte Carlo) — every deployed strategy has one.

### Agent-specific:
- Explore before editing; do not trust a stale README over the code.
- Prefer small, reviewable commits/PRs (the public commit history *is* the
  story — see `docs/roadmap.md` §5).
- When you make a change that touches the decision pipeline or executors, add
  or update a test that would catch a regression in **live behavior**, not just
  unit shape.
- Never run, submit, or scrape live trading actions from a sandbox agent turn
  unless explicitly asked and gated.

---

## 4. Layout cheat-sheet

| Path | Responsibility |
|---|---|
| `quant/core/` | `TradingEngine` decision pipeline, sizing, telemetry utils |
| `quant/backtest/` | Backtest drivers, `BacktestDataHandler`, simulated fills |
| `quant/data/` | `AlpacaDataHandler` (REST) + `ws/` live bar stream → `MemBarsAggregator` |
| `quant/strategies/` | **Private alpha** — strategy packages, one folder each |
| `python/trader.py` | Live orchestration: heartbeat, thread fan-out, state recovery |
| `python/executors/` | Routing + single-leg + IC 4-leg (MLEG) execution |
| `python/config_sync.py` | SSM live config poller (stateless, version-tracked) |
| `python/monitor/` | Correctness sidecar: metrics, shadow signals, reconcile |
| `infrastructure/` | OpenTofu: OIDC, EC2, EventBridge schedule, SSM (staging/prod) |
| `.github/` | `ci.yml`, `deploy.yml`, `actions/ssm-deploy` |
| `docs/roadmap.md` | The why + build plan. **Read first.** |

---

## 5. Definition of done

- [ ] Fits the blueprint (`docs/roadmap.md`); does not break invariants in §2.
- [ ] Tests pass (`uv run test`), ruff clean, pre-commit green.
- [ ] No secrets added; no tracked secret paths touched.
- [ ] README/docs updated where behavior or CLI changed.
- [ ] If it touches live execution: a test guards the live path, and the sidecar
      would catch a divergence.