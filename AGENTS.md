# AGENTS.md — Qi working agreement

For human contributors **and** AI agents. Read this before changing anything.

## 0. What this repo is

**Qi** (`nodeicode/qi`, public, MIT) is a lean, boring-Python SDK for
quantitative research → backtest → paper → live. One decision pipeline
(`quant.core.engine.process_strategy`) runs byte-for-byte identically in
backtest and live — only the executor differs — so research results are
truthful previews of live behavior. Qi is the public foundation cut from a
private production superset; the public story is `README.md`.

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
- Q1 is a **library**, not a CLI — primitives and engine are imported directly
  (offline backtest: `Backtester` + `quant.data.SyntheticDataHandler`; live: a
  runnable `UnifiedTrader` snippet / script). See `docs/quickstart.md` and
  `docs/deployment.md`.

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
| `docs/` | primitives index (§5) / quickstart / extensibility / architecture / deployment / evaluation-methodology / whitepaper-backtest-live-drift |
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
The public story is `README.md`. `docs/extensibility.md` holds the runnable
add-a-recipe examples.

## 7. Comment hygiene (self-contained comments)

Every comment, docstring, and inline `#` note in this repo must be fully
**self-contained**: a reader should understand it from the words on the line
alone, with no outside context required. Concretely:

- **No plan references.** Never cite design letters, ticket numbers, or task
  ids (e.g. "see D11", "per plan P4", "task 11.x") as the explanation for
  code. If a design choice matters, state *what* it is and *why* in the
  comment itself.
- **No private names.** No references to internal team/project names, docs
  outside this tree, or assumptions a stranger to the project would not hold.
- **State the reason, not the number.** A link to `docs/…` / `agents/…` may
  *supplement* an explanation but must never *replace* it — the comment alone
  must carry the meaning.
- **Links must resolve.** Any doc/agent reference must point to an existing
  file in this tree.

The goal: comment as if each line were read in isolation by someone who has
never seen the plan or the surrounding project. Every comment must be
understandable **end-to-end from its own words alone** — it must never
reference plans, tickets, ports, or any other context a reader would have to
go look up to understand; the meaning has to be fully carried by the comment
itself.

## 8. Where to look next

- `README.md` — quickstart + CLI (`uv run quant-backtest`).
- `docs/quickstart.md` — <5-minute clone → backtest → paper, no AWS.
- `openspec/changes/lean-quant-engine/` — specs + tasks for the current rebuild.
- Per-subsystem agent docs (`agents/*.md`) — index in §6.

## 9. Definition of done

- [ ] Fits the OpenSpec plan; does not break an invariant in §1.
- [ ] `uv run test` green; `ruff check .` / `ruff format .` clean.
- [ ] No secrets added; no tracked secret paths touched.
- [ ] README/docs updated where behavior or CLI changed.
- [ ] Touches the live path? A test guards it, and the monitor would catch
      divergence (see `docs/whitepaper-backtest-live-drift.md`).
