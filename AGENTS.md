# AGENTS.md — Qi working agreement

For human contributors **and** AI agents. Read this before changing anything.

## 0. What this repo is

**Qi** (`nodeicode/qi`, public, MIT) is a lean, boring-Python SDK for
quantitative research → backtest → paper → live. One decision pipeline
(`quant.core.engine.process_strategy`) runs byte-for-byte identically in
backtest and live — only the executor differs — so research results are
truthful previews of live behavior. Qi is the public foundation cut from a
private production superset; the plan/why is `docs/roadmap.md`, the public
story is `README.md`.

This repo is mid-rebuild: the active contract is the OpenSpec change
`openspec/changes/lean-quant-engine/` (primitives: venue, portfolio,
observability, runtime-config; agent-context docs). Its specs are the contract
and `tasks.md` is the checklist — read both before working on the SDK shape.

## 1. Invariants (do not break without discussion)

1. **One pipeline.** Backtest and live both run `process_strategy` /
   `process_ticker`; live and backtest differ **only** in the executor.
2. **Adjusted-bar invariant.** Signals run on adjusted bars only; `Raw_Close`
   is backtest-only execution.
3. **Bar-driven scheduling.** `INTERVAL` (signal) and `MANAGE_INTERVAL`
   (manage) gates drive the tick; no intra-`generate_signals` data fetches or
   ad-hoc clocks.
4. **Determinism.** Engine decisions are a pure function of (strategy,
   config, bars, executor events), guarded by two-process golden replay tests
   (`quant/tests/backtest/test_determinism_two_process.py`,
   `test_golden_regression.py`).
5. **Import layering is enforced by a test** (`quant/tests/test_import_layering.py`),
   not a linter: `quant.utils` is a leaf; `quant.backtest` / `quant.strategies`
   are terminal leaves. Package-internal modules get a `_private.py` suffix; a
   sanctioned cross-leaf import carries an inline `# layering-ok` on its line.

## 2. Security rules (non-negotiable)

- `.env`, `*.pem`, `*.key`, `*.p12` are gitignored — keep it that way. Never
  `git add -f` them.
- No credentials in code, docs, or commit messages. Rotate anything that
  touched a committed surface.
- This public repo contains no secrets by construction; real credentials run
  in the private ops path only (AWS SSM, fetched at runtime, never inline).
- `git log --all` must stay free of credential/token/.pem/.env paths.
- Report vulnerabilities via `SECURITY.md` (GitHub private security advisory).

## 3. Command map

- `uv run test` — full suite via `quant/tests/_runner.py` (`-m data|backtest|
  strategies|live|unit` scopes a module; `-k` passes through to pytest).
- `uv run ruff check .` / `uv run ruff format .` — lint/format (pre-commit
  config: `quant/.pre-commit-config.yaml`).
- `uv run ty check` — strict type checking (`quant/ty.toml`).
- CI (`.github/workflows/ci.yml`) runs uv sync + ruff + the one suite on every
  push/PR to `main`.
- SDK CLI (entry points under `[project.scripts]`):
  - `uv run quant-backtest` — offline deterministic backtest (no keys).
  - `uv run quant-paper` — zero-AWS dev dry-run, polls a local config file.
  - `uv run quant-portfolio` — print portfolio metrics from a snapshot.
  - `uv run quant-live` — precondition-checked live/paper trader launcher (`quant[alpaca]`).
  See `docs/quickstart.md` and `docs/deployment.md`.

## 4. Layout

| Path | Responsibility |
|---|---|
| `quant/src/quant/core/` | Decision engine, protocols, sizing |
| `quant/src/quant/data/` | Data source + MemBars fast path + cache |
| `quant/src/quant/backtest/` | Backtest driver, fill simulation, metrics |
| `quant/src/quant/strategies/` | `example_equity` + `get_strategy` registry / entry points |
| `quant/src/quant/live/` | Trader loop, executors, monitor, config, session |
| `quant/src/quant/utils/` | Time, positions, indicators, config helpers |
| `quant/src/quant/observability.py` | Spans (age ∥ duration) + emitter interface |
| `quant/tests/` | The one test suite (incl. import-layering + goldens) |
| `agents/` | Per-subsystem agent docs (invariants + extension points — see §6) |
| `docs/` | primitives index (§5) / quickstart / extensibility / architecture / deployment / roadmap (the why) |
| `openspec/` | Specs + the active change `openspec/changes/lean-quant-engine/` |

## 5. Closed primitives (counted set — design D11)

The SDK exposes a closed, counted set of **8 primitives** (full index with
derivation + tier tags: `docs/primitives.md`; mirrored from the OpenSpec design
D11). Keep all three in sync when the set changes:

| # | Primitive | Derivation | Tier |
|---|---|---|---|
| 1 | venue (`quant.venue`) | parity-load-bearing | port |
| 2 | portfolio (`quant.portfolio`) | parity-load-bearing | pinned |
| 3 | position (`quant.portfolio`) | parity-load-bearing | pinned |
| 4 | instrument (`quant.instruments`) | parity-load-bearing | port (type) / pinned (ontology root) |
| 5 | observability (`quant.observability`) | universal-seam | port (emitter) / pinned (span keys) |
| 6 | config (`quant.config`) | universal-seam | port |
| 7 | data (`quant.data`) | universal-seam | port |
| 8 | engine (`quant.core.engine`) | parity-load-bearing | pinned |

**Parity-load-bearing** = backtest/live disagreement would lie; **universal-seam**
= every strategy touches it. **Pinned** = the spine (least replaceable);
**port** = replaceable behind per-port conformance (see
`quant/tests/extensions/test_conformance_*.py`). A composed convenience (e.g. a
universe) is a concept (`docs/concepts/`), never a 9th primitive.

## 6. Per-subsystem agent docs (index)

Each v1 subsystem has a dedicated agent-facing doc under `agents/` capturing
what lives there, its local invariants, and its extension points. Read the one
for the area you are changing. Links reference files in this tree (resolve-checked):

| Doc | Subsystem | Covers |
|---|---|---|
| [`agents/decision-engine.md`](agents/decision-engine.md) | engine | one pipeline, determinism, bar-driven scheduling |
| [`agents/portfolio.md`](agents/portfolio.md) | portfolio | fills-only Position, aggregation, snapshot/recovery |
| [`agents/venue.md`](agents/venue.md) | venue | broker+session unit, naive-UTC, backtest determinism |
| [`agents/data-access.md`](agents/data-access.md) | data | `DataSourceProtocol`, settled-bar rule, offline example |
| [`agents/config.md`](agents/config.md) | config | version-checked poll, local-first, plugin backends |
| [`agents/observability.md`](agents/observability.md) | observability | spans (age ∥ duration), emitter, extension |
| [`agents/strategy-sdk.md`](agents/strategy-sdk.md) | strategy authoring | `StrategyProtocol`, one registry line, add-a-strategy example |

**Scheduled under their owning task groups** (referenced, but not yet shipped):
per-port conformance/extension docs (instruments, execution, backtest) land
with their conformance suites (11.x).
The public story is `README.md`; the why is `docs/roadmap.md`. `docs/
extensibility.md` holds the runnable add-a-recipe examples.

## 7. Where to look next

- `README.md` — quickstart + CLI (`uv run quant-backtest`).
- `docs/quickstart.md` — <5-minute clone → backtest → paper, no AWS.
- `docs/roadmap.md` — the story/plan. **Read first.**
- `openspec/changes/lean-quant-engine/` — specs + tasks for the current rebuild.
- Per-subsystem agent docs (`agents/*.md`) — index in §6.

## 8. Definition of done

- [ ] Fits the OpenSpec plan; does not break an invariant in §1.
- [ ] `uv run test` green; `ruff check .` / `ruff format .` clean.
- [ ] No secrets added; no tracked secret paths touched.
- [ ] README/docs updated where behavior or CLI changed.
- [ ] Touches the live path? A test guards it, and the monitor would catch
      divergence (see `docs/correctness-layer.md`).
