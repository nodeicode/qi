# Design

## Context

Two repos, one runtime: the patterns worth keeping (one engine pipeline, one
executor seam, MemBars fast path, version-checked config polling, calendar clock,
backtest driver) are entangled with example_equity-shaped opinions (see proposal.md
"Why"). The design task: rebuild them as general primitives — **venue, portfolio,
typed instruments, deterministic state machines** — informed by Nautilus Trader's
shapes (venue-bound execution clients, positions per instrument+venue, typed
instruments, event/state-machine semantics) while staying a lean SDK, not a
platform. Unify on a `sdk-cut` branch of the private repo; export a clean public
repo named **`Qi`** (`nodeicode/qi`).

## Goals / Non-Goals

**Goals:**
- `Qi` as a foundation SDK: research/backtest strategies **and
  portfolios**; research → paper → live parity via deterministic structure +
  explicit state machines.
- Nautilus-informed typing (Protocols first, `TypedDict`/dataclass boundaries,
  typed instruments) without nautilus's framework: no event-loop kernel, no
  internal message bus, no async-first core — plain Python, pandas at the edges.
- One opinionated thing (the decision engine) and replaceable everything else:
  venues, data sources, config backends, emit sinks are Protocols with sensible
  defaults (Alpaca venue, local-first config, no-op emitter).
- Pure-pandas baseline install (no TA-Lib, C++ kernel, or boto3 in default
  `uv sync`); equity-only v1; agent context as a first-class deliverable.

**Non-Goals:**
- Not a Nautilus clone: no `TradingNode` kernel, no `MessageBus`, no async event
  loops, no FIX adapter surface in v1.
- Not re-architecting the decision pipeline semantics (they encode parity); the
  engine is re-skinned for determinism/state machines, not rewritten.
- Not shipping sidecar/shadow/reconcile as core in v1 (observability is
  primitives; extended monitors are dev-built, the private superset keeps its
  implementation).
- Not touching the private production path until the export commit.

## Decisions

### D1. Identity & package shape
Public product name **`Qi`**; distribution stays `quant` (imports under
one root). `python/` folds into `src/quant/live/`; CLI entry points
`quant-backtest` / `quant-paper` / `quant-live` / `quant-portfolio`.
*Alternatives:* two-package uv workspace — two import roots contradict "one
foundation SDK". Rejected.

### D2. Venue over MarketSession (the broker/session fusion)
Replace the loose `MarketSchedule`/session concept with a venue primitive (spec
`venue`): identity + session/calendar state + `DataSourceProtocol` + execution
client in one object. Rationale (nautilus-informed): nautilus binds instruments
and execution clients to venues and gates trading on venue status — that shape
naturally absorbs "broker" (AlpacaVenue = calendar + REST data + trading client)
and "session" (venue open/close/forced-close) into one DX-friendly, swappable
unit. Defaults: `AlpacaVenue`, `BacktestVenue` (simulation timeline). The
canonical naive-UTC boundary rule moves into venue adapters.
*Alternatives:* keep a separate `MarketSchedule` (too loose, scattered);
pure-broker pattern (loses session semantics); nautilus raw venue objects (too
abstract for a lean SDK). Rejected.

### D3. Typed instruments
A minimal `Instrument` model (symbol, venue, asset class, contract specs,
tick/lot/settlement metadata) at `quant.instruments`, seeded as a typed
hierarchy (D14): an `instrument_class` discriminator plus an `underlying`
reference to another instrument, so equity, futures, options, and
options-on-futures are representable by construction (an option's `underlying`
points at a future). `EquityInstrument` ships in v1; `OptionInstrument` /
`FutureInstrument` are defined as thin, registrable types so signatures compose,
with their accounting/execution semantics deferred (see D14). Strategy/protocol
surfaces take/return typed instruments where they today take strings, keeping
`str` ticker conveniences as sugar. Rationale: nautilus's typed instruments are
the backbone of correct execution and portfolio accounting; a minimal version
costs little now and makes futures/options extension a pure addition.
*Trade-off:* churn across engine/data signatures — bounded by keeping ticker
strings legal everywhere and adding instrument objects at primary seams first
(strategy config, position schema, portfolio).

### D4. Portfolio primitives (research-first)
`quant.portfolio`: `Position` (state machine per instrument+venue: opened →
adjusting → closed; fills are the only mutators), `Portfolio` (aggregation,
account state, realized/unrealized PnL, per-strategy + combined metrics), shared
by backtest and live. Backtest driver gains a portfolio-collecting mode
(multi-strategy runs into one or more portfolios). The existing
`PositionDict`/`build_position_dict` schema becomes the `Position` state
representation (legacy schema names kept as aliases).
*Rationale:* the roadmap's honest-evaluation story needs portfolio-level results
(combined drawdown, allocation) without forks; nautilus's Portfolio is the
reference shape (positions per instrument+venue, fills-driven).

### D5. Determinism & state machines (the parity mechanism)
- Engine processes are pure functions of (strategy, config, bars, executor
  events): determinism enforced by a golden replay test (same inputs → same
  decision sequence).
- Position lifecycle, strategy state, and order flow are explicit state machines;
  every mutation comes from a fill/event, never side effects. Live recovery =
  replay persisted fills/snapshots through the same machines (existing
  `trader_state.json` persistence generalizes into portfolio/position snapshots).
- Sizing/entry-gate helpers stay pure; the data fetch is the only impure boundary
  and is behind `DataSourceProtocol` (deterministic in backtest by construction).

### D6. Config local-first
`ConfigSource` interface with `FileConfigSource` + `EnvConfigSource` shipped as
the default; `SSMConfigSource` behind `quant[ssm]`. `ConfigSync` becomes the
generic version-tracking/validation/apply shell (spec `runtime-config`).
Existing SSM behavior (IMDS region, version short-circuit, invalid-payload-once)
is preserved in the SSM backend, not the core.

### D7. Observability primitives
`quant.observability`: `span()` timing primitive (age ∥ duration) + `Emitter`
interface (no-op default; console/JSONL examples). Engine/telemetry wiring uses
only these. Shadow/reconcile/sidecar demoted to dev-built extensions; the
private superset keeps its sidecar implementation as the reference extension.
*Rationale:* the user's directive — observability should be primitives developers
compose, and the engine should not know about monitors.

### D8. Alpha extraction + equity cut (extreme: structure and patterns only)
The public tree keeps **structure/patterns only** — the protocol/primitive
seams (venue/executor/data/config/observability, rebuilt in this change), the
shared engine pipeline, the backtest driver shape, MemBars + cache, and the
public synthetic `example_equity` (also registering the `quant.strategies`
entry-point group). Every strategy-specific implementation is **cut, not
abstracted**: dead alpha ML (`utils/regime_model.py`, `utils/am_features.py`),
C++ kernels (`utils/cpp_native/`), options/IC math
(`utils/{options,bs_iv,bs_greeks,ic_scheduling}.py` — decouple `time.py`
first), non-equity data handlers (`futures/options/vix/news/dhan_data_handler.py`,
`option_analytics.py`, `options_calendar.py`, futures/options caches), IC/
futures backtest machinery (`backtest/executors/{ic,futures}.py`,
`backtest/prefetchers/`, IC branches in the driver/utils), live IC/MLEG
executors (`live/executors/{ic,unified}_executor.py`) and the sidecar
(`live/monitor/sidecar.py` — kept in the private superset as the reference
extension, D7), and core sidecar telemetry (`core/monitor_utils.py`,
superseded by observability spans in 3.3; IC leftovers in `core/_utils.py` go
with 5.3). The TA-Lib/Numba indicator library (`utils/indicators.py` 41K +
`utils/cpp_native/`) is cut outright — nothing on the public path imports it;
`example_equity` computes its indicators in pure pandas. Runtime deps collapse
with the code: `lightgbm`, `numba`/`llvmlite`, `ta-lib`, `nanobind`/
`setuptools` dropped entirely; `alpaca-py` → `quant[alpaca]` extra, `boto3` →
`quant[ssm]` extra. Strategy-specific test suites are deleted in the same
batches. Offline synthetic equity fixtures under
`quant/tests/data/` make goldens + quickstart fully keyless.

### D9. Agent context (the critical enablement)
Layered markdown, agent-first (spec `agent-context`):
- Root `AGENTS.md` — orientation + invariants (parity, adjusted bars,
  determinism, state-machine rules) + command map + index of deeper docs. Stays
  short; points, never duplicates.
- `agents/<subsystem>.md` — one per v1 subsystem (strategy, engine, venue, data,
  execution, portfolio, backtest, config, observability): files, local
  invariants, extension points, runnable extension example. Written *during*
  each subsystem's cut, not after (they encode what was learned).
- `docs/extensibility.md` — executable recipes (add strategy / data source /
  venue / executor / build on observability), each verified against the tests.
- Sync discipline: doc updates ride in the same commit as the code change (per
  subsystem task groups own their doc); CI checks root `AGENTS.md` links resolve.
*Investigation:* today's surface is root `AGENTS.md` + `quant/AGENTS.md` only —
no per-subsystem docs, no recipes; the submodule split forced two partial
AGENTS. The layered design fixes coverage and discoverability without a doc
monolith. Baseline now, deepen continuously.

### D10. Branch → export mechanics
Work on `sdk-cut` (private), green commit-by-commit; export = curated squashed
history into the new public repo; private superset untouched until export
verifies. Rollback = discard branch/public repo, keep trading on private.
**Superseded (history):** the export already happened (task group 10). The public
repo is **`nodeicode/qi` on `main`**; `sdk-cut` no longer exists. Remaining work
in this change proceeds directly on `main` of the public repo.

### D11. Counted primitive set (index, not rank)
Qi exposes a small, **closed, counted set** of primitives: venue, portfolio,
position, instrument, observability, config, data, engine. The ingredients are
individually industry-recognized (nautilus and every serious framework share
these nouns), but the closed, enumerated set is Qi's editorial reduction — no
framework prints a numbered primitive list. Membership is **derived by test**, not
vibes:
- **Parity-test:** if backtest and live disagree here, results lie → engine
decisions, position, portfolio, instrument, venue identity, adjusted-bar / interval
semantics.
- **Universality-test:** every strategy touches this regardless of venue/asset →
data, execution, config, observability.
- **Neither** → deliberately excluded (message bus, cache, clock, risk engine as
lean Non-Goals), an implementation detail behind a primitive (MemBars/prefetch
behind *data*), or alpha-specific (the strategy itself — never a primitive).
Numbering is for **counting / indexing** (a stable vocabulary for users and agents),
NOT ranking — it must not imply equal replaceability (D13 assigns replaceability).

### D12. Reference conveniences as concept docs, not primitives/code
Anything that is an amalgamation of existing primitives (a dynamic universe with
listing/delisting, a rotation, a hedge shape) is **not** a new primitive and is
generally shipped as a documented construct in `docs/concepts/`, in two flavors:
- **recipe** — prose + worked example, assembled on demand;
- **contract** — prose + a runnable parity/invariant check, used when the
composition is parity-sensitive (e.g. a universe must be point-in-time identical
across backtest and live — survivorship). A doc asserting parity is a claim; a
contract makes it verifiable for the user's actual implementation.
This keeps the counted set at 8 while letting the ecosystem grow through
composition. **Universe is the proof**: it fails the universality test, so it stays
a composition behind the data port, not a 9th primitive.

### D13. Usage surfaces decide the tiers (conformance per port)
Tiers are a projection of how Qi is used, not an independent choice. Three surfaces:
- **Agent / declarative (A):** assemble a run from the counted set by name
(config / registry / CLI; human or agent). No new code.
- **Author (B):** implement a strategy protocol and / or plug custom venue, data,
executor. The primary extension surface.
- **Builder (C):** reshape the loop or the pinned spine; contributes to core.
Mapping: A composes by name, B replaces ports, C touches the pinned spine. Posture:
**B is the capability floor** (any power user can hand-write), **A is the eased path**
(an agent assembles once parts exist and are registered). Hence replaceable =
venue, data, executor, instrument (guarded by conformance); **pinned** = engine
decisions + portfolio/position accounting + instrument-ontology root rules.
The mechanism that makes replacement parity-safe is a **per-port conformance /
contract suite** run against both runners — not just golden replay of the default
wiring. A custom data source or instrument type is only trustworthy for paper/live
if it passes that port's suite; Qi's defaults pass by construction. Cross-cutting
implication: a **data registry** (the sibling of `STRATEGY_REGISTRY`) is the
highest-value seam — most real asks begin with "get data from X."

### D14. Instrument ontology seed now; options/futures accounting deferred
Ship the instrument **seed as real code** — it is primitive #4, and a documented
hierarchy gives nothing to type against (position keys, venue calendars, portfolio
netting all reference it). Do not ship instruments as concept-doc only. Seed =
base + `instrument_class` discriminator + `underlying` ref (D3). Ship
`EquityInstrument` in v1; define `OptionInstrument` / `FutureInstrument` as thin
code types so composition (incl. options-on-futures) is representable. **Defer**
options/futures accounting and execution (multi-leg, exercise, margin / daily
settlement, roll) to later, as concept docs (contract flavor); v1 stays equity-only.
Boundary: option/futures accounting nudges the **pinned** portfolio/position layer,
so each deferred asset-family concept doc must draw that accounting boundary
explicitly and keep it out of the pinned spine until scoped in.

## Risks / Trade-offs

- **Re-skinning the engine risks regressions in the parity guarantees** →
  engine semantics are preserved; determinism is added as *tests* (golden
  replay, state-machine transition tests), not new code paths; every refactor
  lands behind a green full suite on the branch.
- **Venue/portfolio primitives churn live paths (trader, executors, monitor)**
  → incremental: introduce protocols/types first, port `python/` behavior onto
  them module-by-module (venue wraps existing calendar+handler+client; position
  dict → `Position` state machine), keep goldens green.
- **Bundled fixtures could mask data-format bugs** → fixtures are generated by
  the real fetch path once and reviewed; `data-access` conformance tests run
  against both synthetic and (when creds present) live shapes.
- **Agent docs rot** → every task group owns its doc in the same commit; CI link
  check on `AGENTS.md`; the launch gate requires `agents/*.md` present for all
  v1 subsystems (spec `agent-context` baseline scenario).
- **Portfolio accounting is subtle (commissions, corporate actions, splits)** →
  v1 scope is fill-driven PnL + equity; splits/dividends/complex cost basis are
  documented as later extensions, not silently half-supported.
- **Non-async, no-bus architecture hits throughput limits for high-frequency
  live trading** → acceptable and declared: Qi targets strategy
  cadence (bars > 1s); the bar-driven scheduling spec caps the live load already.

## Migration Plan

1. Branch `sdk-cut`; Phase 1 unify (submodule absorb, fold `python/` →
   `quant.live`, single pyproject/lock/CI/openspec, drop TID251, dedupe
   harness) — green commit-by-commit.
2. Phase 2 primitives: `quant.instruments` (typed instruments), `venue`
   (AlpacaVenue/BacktestVenue), `portfolio` (Position state machine +
   Portfolio), `quant.observability` (spans + emitter), `ConfigSource`
   local-first — each lands with its protocol, tests, and `agents/<s>.md` doc.
3. Phase 3 engine determinism hardening: golden replay test, state-machine
   position lifecycle, snapshot/recovery on live.
4. Phase 4 alpha extraction + `example_equity` + entry points; equity cut +
   optional deps + offline fixtures.
5. Phase 5 SDK DX: CLI, quickstart, extensibility recipes; zero-AWS dev mode.
6. Phase 6 verify on branch (full suite + reproduce-from-clone), export curated
   history to the public repo, secret re-audit, launch.

Rollback at any point: superset branch untouched; discard `sdk-cut` or the
public repo; continue trading on the current private state.

## Resolved decisions

- **Public repo name: `Qi`** (shipped at launch as `nodeicode/qi`;
  distribution stays `quant`). The private superset stays on the private side
  under its own name — no rename is needed and none is planned (OIDC trust
  uses immutable repo IDs, so its name never affected the public cut).
- **Portfolio is unversioned.** Ship the primitives as specified and grow/shrink
  functionality incrementally; no v1/v2 gates. Scope discipline stays: fill-driven
  PnL + equity first, commissions/splits added when needed rather than promised.
- **Agent docs live at `agents/`** (repo root), agent-first discoverability.
- **Counted primitive set (8).** Qi's primitives are venue, portfolio, position,
instrument, observability, config, data, engine — a closed, counted set derived
by the parity + universality tests (D11). Reference conveniences ship as concept
docs (recipe / contract), not as new primitives (D12).
- **Replaceable vs pinned set by usage surfaces.** Author surface (B) customizes
ports; agent surface (A) assembles by name; the spine (engine + accounting) stays
pinned. Replacement is made parity-safe by per-port conformance suites (D13).
- **Options/futures accounting deferred.** Representable via the instrument seed
(D14), but accounting/execution semantics ship later as concept docs; v1 stays
fully equity-only.

## Open Questions

- **Option/futures accounting scope.** Keep deferred (concept-doc boundary around
the pinned portfolio/position layer) versus pulling e.g. options into v1 — the
open trigger is real use cases like SPX-option VRP harvesting.
- **Blessed reference compositions.** How many reference constructs (universe,
data adapters) to ship as Surface-A assembly targets, versus leaving them to
users to build from the counted set.
