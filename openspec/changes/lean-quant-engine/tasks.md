# Tasks

## 1. Branch & Repo Unification (private, `sdk-cut`)

- [x] 1.1 Create branch: `git switch -c sdk-cut` from current main; verify `git branch --show-current` reports `sdk-cut`
- [x] 1.2 Absorb submodule: `git rm --cached quant`, delete `.gitmodules` and `quant/.git` marker, stage the real tree; verify `git submodule status` is empty and `git ls-files quant/` lists tracked sources
- [x] 1.3 Root uv workspace: root `pyproject.toml` with `[tool.uv.workspace] members = ["quant", "python"]` and root `uv lock`; verify one root `uv.lock` and `uv sync` installs both packages
- [x] 1.4 Dedupe harness: single `tests/_runner.py` + `conftest.py`; remove `quant.egg-info` / `python/*.egg-info` and caches; verify `uv run test` at root runs both suites green
- [x] 1.5 Drop `TID251` `banned-api` from both pyprojects; keep import-layering test; add `_private.py` naming rule to AGENTS.md; verify `ruff check .` clean and the layering test passes

## 2. Single-Distribution Fold (`python/` → `quant.live`)

- [x] 2.1 Move `python/{trader,config_sync,market_schedule,protocols}.py`, `executors/`, `monitor/` into `src/quant/live/`; rewrite `from python.*` to `from quant.live.*`; verify `uv run python -c "import quant.live.trader"` and ruff clean
- [x] 2.2 Collapse packaging: delete `python/pyproject.toml`; root `pyproject.toml` is the single `[project]` named `quant`; verify `uv sync` installs exactly one distribution
- [x] 2.3 Move telemetry primitives to `quant.observability` (see group 3.3) and update all importers; verify engine + monitor span tests pass
- [x] 2.4 Folder `python/tests/` into `quant/tests/live/`; verify single `uv run test` green

## 3. Primitive Seams (venue, config, observability — design D2/D6/D7)

- [ ] 3.1 `quant.instruments`: `Instrument` base + `EquityInstrument`; ticker-string conveniences preserved; verify instrument tests + strategy configs construct typed instruments
- [ ] 3.2 `venue` module: `Venue` protocol (identity + session state + data source + execution client), `AlpacaVenue` wrapping existing calendar/handler/client, `BacktestVenue` driven by simulation timeline; verify venue session tests (holiday, rollover, fallback, naive-UTC boundary) and a deterministic-backtest-time test
- [ ] 3.3 `quant.observability`: `span()` primitive keeping age ∥ duration distinct + `Emitter` protocol with no-op default and JSONL example; rewire engine/live telemetry through it; verify no-sink backtest emits nothing and a sink receives typed rows
- [ ] 3.4 `ConfigSource` + `FileConfigSource`/`EnvConfigSource` local-first with `ConfigSync` as the version/validation/apply shell; `SSMConfigSource` behind `quant[ssm]` preserving IMDS + version short-circuit + invalid-payload-once; verify zero-AWS startup, unchanged-version short-circuit, backend switch by config
- [ ] 3.5 Write `agents/venue.md`, `agents/data-access.md`, `agents/config.md`, `agents/observability.md` (invariants + extension points + runnable example each); verify each doc's example runs against the branch

## 4. Portfolio Primitives (design D4)

- [ ] 4.1 `quant.portfolio.Position`: state machine per (instrument, venue) — opened → adjusting → closed, fills-only mutation; port `PositionDict` schema as the state representation with legacy aliases; verify fill-driven transition tests + parity with existing position schema consumers
- [ ] 4.2 `quant.portfolio.Portfolio`: aggregation across strategies/venues, account state (cash, equity), realized/unrealized PnL, per-strategy + combined metrics; verify totals track positions and closed positions settle
- [ ] 4.3 Backtest portfolio mode: multiple strategies into one or more portfolios; verify combined portfolio metrics test (spec `portfolio` scenario)
- [ ] 4.4 Live parity + snapshot/recovery: live portfolio derives from venue fill events; snapshot persisted and recovered on restart; verify restart-recovery test
- [ ] 4.5 Write `agents/portfolio.md`; verify its example runs

## 5. Engine Determinism & State Machines (design D5)

- [x] 5.1 Golden replay test: same (strategy, config, bars, executor events) → identical decision sequence in two processes; verify the test passes and is wired into CI
- [ ] 5.2 Position lifecycle refactor on the engine path to route through `Position` state (group 4), keeping the canonical request schema; verify engine + existing position tests green
- [ ] 5.3 Trim engine IC-specific branches (`build_ic_position`, ICMeta read paths); re-add only what the shared pipeline truly needs, documented; verify engine tests + new equity golden pass
- [ ] 5.4 Write `agents/decision-engine.md` (determinism rules, adjusted-bar invariant, scheduling contract); verify its example runs

## 6. Alpha Extraction & Public Example

- [ ] 6.1 Remove `quant/strategies/example_equity/` from the branch (moves to private superset); verify no `example_equity` references remain in the export tree
- [x] 6.2 Add `quant/strategies/example_equity/` (synthetic MA-cross, `StrategyProtocol` + `BaseStrategyConfig` subclass); verify `get_strategy("example_equity")` returns the class and an engine constructs it
- [ ] 6.3 Register the `quant.strategies` entry-point group; verify a stub plugin distribution in tests resolves via `get_strategy` with no SDK changes
- [ ] 6.4 Write `agents/strategy-sdk.md`; verify its add-a-strategy example runs against tests

## 7. Equity Scope Trim & Baseline Install

- [ ] 7.1 Cut the design-D8 module list from the export tree (prefetchers, futures/ic backtest executors, options/futures/vix/news/dhan handlers, options/bs/ic/regime utils, `live/executors/ic_executor.py`); verify import-layering and full suite green after each deletion batch
- [ ] 7.2 Indicators: pure-pandas vectorized fallback with `quant[ta]` / `quant[native]` extras; verify indicator correctness tests pass with and without TA-Lib
- [ ] 7.3 Offline-fixture goldens: stable synthetic equity bars under `quant/tests/data/`; golden regression for `example_equity`; verify `uv run pytest -k golden` green in a fresh venv with no API keys
- [ ] 7.4 Optional extras `quant[alpaca]`, `quant[ssm]`, `quant[ta]`, `quant[native]`; verify base `uv sync` installs without them and unit suites pass brokerless

## 8. SDK Developer Experience

- [ ] 8.1 CLI entry points `quant-backtest` / `quant-paper` / `quant-live` / `quant-portfolio` (argparse, stdlib); verify `quant-backtest --help` and an offline example backtest run end-to-end
- [ ] 8.2 Zero-AWS dev mode: local config file + env keys drive a `--paper` dry run with no `boto3` installed; verify it starts, polls file config, and exits cleanly
- [ ] 8.3 `docs/quickstart.md`: clone → `uv sync` → backtest → paper in under 5 minutes, no AWS; verify commands run verbatim in a temp clone
- [ ] 8.4 `docs/extensibility.md`: recipes — add strategy / data source / venue / executor / build on observability primitives; verify each recipe runs against the installed SDK
- [ ] 8.5 `docs/architecture.md` + `docs/deployment.md` (paper default, SSM prod path); verify documented commands match reality
- [ ] 8.6 Public root AGENTS.md restructure + full `agents/*.md` index; verify the root doc references resolve (CI link check) and every v1 subsystem is covered

## 9. Integration Verification (branch)

- [ ] 9.1 Full branch suite: `uv run test`, `ruff check .`, `ruff format --check .`, pre-commit — all green on `sdk-cut`
- [ ] 9.2 Reproduce-from-clone: temp dir clone of `sdk-cut` → `uv sync` → `uv run test` green; record quickstart wall time
- [ ] 9.3 Equity e2e: backtest → paper smoke on `example_equity` with an emitter sink; verify span rows emit and portfolio state recovers across restart (spec `portfolio` recovery scenario)

## 10. Public Export & Launch

- [x] 10.1 Public repo created as **`nodeicode/qi`** (product name **`Qi`** — resolved at launch; distribution stays `quant`) and curated squashed history pushed (2 commits on `main`). Verified: no `.gitmodules`, no `.env`, no `*.pem`/`*.key`/`*.p12` in the tree or history. `example_equity` ships in the public tree **by design** (synthetic reference strategy, task 6.2 / D8) — the private alpha does not. Private-superset rename to `quant-alpha` remains pending — see 10.5.
- [x] 10.2 Secret re-audit of the entire history (paths + blob content of both export commits): zero credential hits. `gitleaks` not installed; manual scan covered all blobs (no AKIA/token/private-key patterns). Only doc references to env-var *names* were found (stale `quant/AGENTS.md`) — removed in this task.
- [x] 10.3 Repo furniture: **LICENSE is now MIT** (file + `pyproject.toml` metadata align with the GitHub-declared MIT license; duplicate proprietary `quant/LICENSE` removed); README has WHY/WHAT/quickstart; root `AGENTS.md` rewritten for the public repo (orientation / invariants / command map / layout / index — no private-repo or submodule claims); `SECURITY.md` added. ⚠ **`agents/*.md` per-subsystem docs are NOT shipped yet** — blocked on task groups 3/4/5/6, which own and verify each `agents/<s>.md` with its subsystem (spec `agent-context` baseline scenario).
- [x] 10.4 Public CI: single workflow (`.github/workflows/ci.yml`) runs uv sync + ruff lint + ruff format + the one `uv run test` suite (runner includes `tests/test_import_layering.py` and the golden two-process / golden-regression tests), on `main` push + every PR, green at export. ⚠ "Verify a fork PR passes" not literally executed — forks gate through the same `pull_request` workflow.
- [ ] 10.5 **Blocked (private side):** the private superset (`quant-alpha`) does not exist yet — no `nodeicode/quant-alpha` (or renamed superset). Installing the alpha's strategy distribution into the `quant.strategies` entry-point group against the public SDK, re-running CI, and confirming `get_strategy("<alpha>")` loads live cannot be verified from this repo. Action: rename/create the superset repo, wire its entry points against Qi, re-verify.

## Workflow follow-up

- Open Question "public repo name" resolved by action at launch: **`Qi`** (`nodeicode/qi`), not `quant-engine`; distribution stays `quant`. The private-superset rename to `quant-alpha` is still pending (task 10.5, blocked on the private side).
- Archive this change after all task groups land and the public export is re-verified.
- Verify the archived result (specs synced into the new repo's own openspec if desired).
