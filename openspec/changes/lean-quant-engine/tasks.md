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

- [x] 3.1 `quant.instruments` (D14 seed): `Instrument` base + `instrument_class` discriminator + `underlying` reference to another instrument; ship `EquityInstrument` v1; define thin, registrable `OptionInstrument` / `FutureInstrument` types so options, futures, and options-on-futures are representable (an option's `underlying` points at a future) with their accounting/execution semantics deferred to concept docs; ticker-string conveniences preserved; verify instrument tests + strategy configs construct typed instruments (spec `instruments`)
- [x] 3.2 `venue` module: `Venue` protocol (identity + session state + data source + execution client), `AlpacaVenue` wrapping existing calendar/handler/client, `BacktestVenue` driven by simulation timeline; verify venue session tests (holiday, rollover, fallback, naive-UTC boundary) and a deterministic-backtest-time test
- [x] 3.3 `quant.observability`: `span()` primitive keeping age ∥ duration distinct + `Emitter` protocol with no-op default and JSONL example; rewire engine/live telemetry through it; verify no-sink backtest emits nothing and a sink receives typed rows
- [x] 3.4 `ConfigSource` + `FileConfigSource`/`EnvConfigSource` local-first with `ConfigSync` as the version/validation/apply shell; `SSMConfigSource` behind `quant[ssm]` preserving IMDS + version short-circuit + invalid-payload-once; verify zero-AWS startup, unchanged-version short-circuit, backend switch by config
- [ ] 3.5 Write `agents/venue.md`, `agents/data-access.md`, `agents/config.md`, `agents/observability.md` (invariants + extension points + runnable example each); verify each doc's example runs against the branch

## 4. Portfolio Primitives (design D4)

- [x] 4.1 `quant.portfolio.Position`: state machine per (instrument, venue) — opened → adjusting → closed, fills-only mutation; port `PositionDict` schema as the state representation with legacy aliases; verify fill-driven transition tests + parity with existing position schema consumers
- [x] 4.2 `quant.portfolio.Portfolio`: aggregation across strategies/venues, account state (cash, equity), realized/unrealized PnL, per-strategy + combined metrics; verify totals track positions and closed positions settle
- [x] 4.3 Backtest portfolio mode: multiple strategies into one or more portfolios; verify combined portfolio metrics test (spec `portfolio` scenario)
- [x] 4.4 Live parity + snapshot/recovery: live portfolio derives from venue fill events; snapshot persisted and recovered on restart; verify restart-recovery test
- [x] 4.5 Write `agents/portfolio.md`; verify its example runs

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

- [x] 7.1 **Extreme equity-only trim of `quant/src/quant/`** — keep structure/patterns only (protocol/primitive seams, engine pipeline, executor seam, data protocol + MemBars + cache, backtest driver shape); **cut, not abstract**, every strategy-specific implementation in per-area deletion batches, import-layering + full suite green after each. **Requires 3.3 first**: the `core/monitor_utils.py` cut below needs the engine/trader rewired to observability spans; the `ic_scheduling → time.py` decouple and stripping `ICMeta` out of kept `protocols.py`/backtest driver precede the pure deletions:
  - `utils/`: cut `regime_model.py` (dead, 37K), `am_features.py` (dead, 18K), `indicators.py` (41K TA-Lib/Numba library, imported only by cut modules + tests), `cpp_native/` (vq kernels), `bs_iv.py`, `bs_greeks.py`, `options.py`, `ic_scheduling.py` (first decouple its `et_wall_to_utc_naive` into `time.py`), `constants.py`; keep `time.py`, `positions.py`, `strategy_config.py`
  - `data/`: cut `futures/options/vix/news/dhan_data_handler.py`, `option_analytics.py`, `options_calendar.py`, `cache/futures.py` + `cache/options.py`; keep `data_handler.py` (de-strategified), `_times.py`, `cache/core.py` (arrow/frame_codec/models/stock), `ws/` live stream
  - `backtest/`: cut `executors/ic.py` + `executors/futures.py` + `prefetchers/`; strip IC/option branches from `utils.py`, `base_backtest.py`, `backtester.py`, `router.py`; keep the driver + equity fills + `backtest_data_handler.py`
  - `live/`: cut `executors/ic_executor.py` + `monitor/sidecar.py` (sidecar moves to the private superset as the reference extension, D7); de-IC `live/utils.py`, `protocols.py`; keep `trader.py`, `config_sync.py`, `market_schedule.py`, `strategy_loader.py`, `monitor/emit.py`. **Reconciled**: `executors/unified_executor.py` + `executors/live_executor.py` are **kept** and de-IC'd to equity-only — `UnifiedExecutor` is the equity live executor wired into `trader.py` and `get_executor`
  - `core/`: cut `monitor_utils.py` (engine first rewired to observability spans in 3.3) and IC leftovers in `_utils.py` (with 5.3); keep `engine.py`, `protocols.py`, `sizing.py`
  - runtime deps: drop `lightgbm`, `numba`/`llvmlite`, `ta-lib`, `nanobind`/`setuptools`; `alpaca-py` → `quant[alpaca]` extra, `boto3` → `quant[ssm]` extra (7.4)
  - tests: delete strategy-specific suites (`futures_fixtures.py`, `test_futures_validation.py`, `test_prefetchers.py`, `test_ic_cbbo_at_or_before.py`, `test_option_greeks.py`, `test_vix_data_handler.py`, `test_futures/options_data_handler.py`, `test_native_kernels.py`, `test_utils_vectorization.py`, `test_arrow_ipc_cache.py` if it depends on futures/options caches); retarget live tests off `ic_executor`

  **Closed (de-IC sweeps):** the remaining IC/option surface in the kept core/live/data
  path was cleared — engine `_utils.py` IC branch + `is_ic_position`, live schema/executor
  OccSymbol surface (`is_live_ic_position` → fail-closed `is_live_position`), option-only
  data-handler methods (`_get_occ_symbol`, `fetch_real_time_option_data`, OCC-quote
  emission), `get_occ_symbol`, and stale IC docstrings — full suite green at close
  (726 passed / 95.33% coverage). `PRODUCT_SPECS`, `SIGNAL_REGISTRY`, `AssetClass` are
  **retained by design** (`sizing.py` / signal routing consume them). Deferred to 7.4 (deps,
  not marked): `pyproject.toml` extras/dep drop; the `numba`-backed fill-slippage scalar in
  `backtest/utils.py` keeps `numba` imported until 7.4 drops it. Live tests run mocked
  in-suite (alpaca stubbed; zero skips; 290 live tests green) — no AWS-dependent tests block 7.1.
- [ ] 7.2 Indicators: no TA-Lib/Numba indicator library ships — `utils/indicators.py` + `cpp_native/` cut (nothing on the public path imports them; `example_equity` computes its MA-cross indicators in pure pandas); verify no shipped module imports `ta-lib`/`numba` and `ta-lib` is removed from deps
- [ ] 7.3 Offline-fixture goldens: stable synthetic equity bars under `quant/tests/data/`; golden regression for `example_equity`; verify `uv run pytest -k golden` green in a fresh venv with no API keys
- [ ] 7.4 Optional extras `quant[alpaca]` (alpaca-py) and `quant[ssm]` (boto3); verify base `uv sync` installs without them and unit suites pass brokerless

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

- [x] 10.1 Public repo created as **`nodeicode/qi`** (product name **`Qi`** — resolved at launch; distribution stays `quant`) and curated squashed history pushed (2 commits on `main`). Verified: no `.gitmodules`, no `.env`, no `*.pem`/`*.key`/`*.p12` in the tree or history. `example_equity` ships in the public tree **by design** (synthetic reference strategy, task 6.2 / D8) — the private alpha does not. The private superset stays on the private side under its own name; no rename is needed.
- [x] 10.2 Secret re-audit of the entire history (paths + blob content of both export commits): zero credential hits. `gitleaks` not installed; manual scan covered all blobs (no AKIA/token/private-key patterns). Only doc references to env-var *names* were found (stale `quant/AGENTS.md`) — removed in this task.
- [x] 10.3 Repo furniture: **LICENSE is now MIT** (file + `pyproject.toml` metadata align with the GitHub-declared MIT license; duplicate proprietary `quant/LICENSE` removed); README has WHY/WHAT/quickstart; root `AGENTS.md` rewritten for the public repo (orientation / invariants / command map / layout / index — no private-repo or submodule claims); `SECURITY.md` added. ⚠ **`agents/*.md` per-subsystem docs are NOT shipped yet** — blocked on task groups 3/4/5/6, which own and verify each `agents/<s>.md` with its subsystem (spec `agent-context` baseline scenario).
- [x] 10.4 Public CI: single workflow (`.github/workflows/ci.yml`) runs uv sync + ruff lint + ruff format + the one `uv run test` suite (runner includes `tests/test_import_layering.py` and the golden two-process / golden-regression tests), on `main` push + every PR, green at export. ⚠ "Verify a fork PR passes" not literally executed — forks gate through the same `pull_request` workflow.


## 11. Extensible Surface — primitives index, concept docs, registry, conformance (D11–D13)

- [ ] 11.1 Counted-primitive index (D11): document the closed 8 (venue, portfolio, position, instrument, observability, config, data, engine), each tagged with its derivation (parity-load-bearing vs universal seam) and replaceability tier (pinned vs port) in `docs/primitives.md`, mirrored into root `AGENTS.md` and the spec index; verify the index stays in sync with the specs
- [ ] 11.2 `docs/concepts/` under the recipe/contract distinction (D12; spec `agent-context`: Reference concept documents): establish the convention; ship **universe** as the reference contract-flavor concept (composition of instruments ∘ data ∘ membership rule) with a runnable point-in-time parity check across backtest/live (survivorship); verify the check runs
- [ ] 11.3 Data-source registry + entry-point group `quant.datasources` (D13; spec `data-access`: Data-source registration and discovery): `DataSourceProtocol` implementations register by name; verify `get_datasource("...")` resolves a stub plugin with no SDK changes (mirror task 6.3)
- [ ] 11.4 Per-port conformance suites (D13; spec `data-access`: Data-source conformance, `execution`: Executor conformance, `instruments`: Instrument conformance): one suite per port (venue, data, executor, config, portfolio, instrument) runnable against both backtest and live roots, each asserting that port's invariants; Qi's shipped defaults pass by construction; verify under `quant/tests/extensions/` that a conforming custom adapter passes and a violating one fails loudly

## Workflow follow-up

- Open Question "public repo name" resolved by action at launch: **`Qi`** (`nodeicode/qi`), not `quant-engine`; distribution stays `quant`. The private superset is owned and changed on the private side — the `quant-alpha` rename is moot and dropped.

## Tracked dangling work (keep until group 7 fully closed; checked against a green `main`)  

Closed with 7.1 (de-IC sweeps):
- ✅ **Live-seam de-IC (7.1 `live/`):** done — `live/protocols.py` slimmed to the equity schema
  with the fail-closed `is_live_position` boundary validator; `get_occ_symbol` dropped from
  `live/utils.py`; `unified_executor` runs Ticker-only (exit + position build); trader recovery
  and live tests repointed (fail-closed malformed-record recovery still covered).
- ✅ **core `_utils.py` IC leftovers (7.1 `core/`, pairs with 5.3):** `is_ic_position` dropped;
  `PRODUCT_SPECS`, `SIGNAL_REGISTRY`, `AssetClass` **retained by design** (still consumed by
  `sizing.py` at ~186 and signal routing).
- ✅ **base_backtest / backtest utils residual:** stale IC/option prose comments removed (backtester,
  base_backtest, backtest utils) and the dead private `_compute_realized_equity_curve` /
  `_calculate_additive_performance_metrics` scaffold deleted (zero callers); `Raw_Close` notes
  retained (sanctioned backtest-only execution).
- ✅ **Live/AWS-dependent tests:** no longer blockers — live tests run mocked in-suite (alpaca
  stubbed via conftest, zero skips, 290 live tests green).

Still open for 7.2 / 7.3 / 7.4 — `7.2`/`7.3`/`7.4` checkboxes stay `[ ]`:
- **7.2 verification:** requires the deps change below; code cut (indicators.py + cpp_native/) already landed, but `7.2` needs "no module imports ta-lib/numba + ta-lib removed from deps" verified.
- **7.3 offline-fixture goldens:** synthetic equity fixtures + `example_equity` golden — not started.
- **7.4 extras + dep drop (deps change deferred):** `pyproject.toml` untouched this pass. Need: drop `lightgbm`, `numba`/`llvmlite`, `ta-lib`, `nanobind`/`setuptools` (now that their importers are cut) and move `alpaca-py` → `quant[alpaca]` extra, `boto3` → `quant[ssm]` extra; verify base `uv sync` is brokerless and clean.
- Archive this change after all task groups land and the public export is re-verified.
- Verify the archived result (specs synced into the new repo's own openspec if desired).
