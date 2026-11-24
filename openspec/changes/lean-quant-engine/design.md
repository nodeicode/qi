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
repo named **quant-engine**.

## Goals / Non-Goals

**Goals:**
- `quant-engine` as a foundation SDK: research/backtest strategies **and
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
Public product name **quant-engine**; distribution stays `quant` (imports under
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
tick/lot/settlement metadata) at `quant.instruments`, with typed subclasses
(`EquityInstrument`, `OptionInstrument` reserved for later). Strategy/protocol
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

### D8. Alpha extraction + equity cut (unchanged from prior plan)
`example_equity` → private superset (`quant-alpha`), registered via `quant.strategies`
entry points; public ships `example_equity`. Options/IC/futures modules cut
(prefetchers, futures/ic executors, options/futures/vix/news/dhan handlers,
options/bs/ic/regime utils, live `ic_executor.py`); CA-Lib/`cpp_native` behind
pure-pandas fallback + optional extras. Offline synthetic equity fixtures under
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
  live trading** → acceptable and declared: quant-engine targets strategy
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

- **Public repo name: reuse `quant-engine`.** The private superset renames to
  `quant-alpha` the day of launch (OIDC trust uses immutable repo IDs, so the
  rename is safe).
- **Portfolio is unversioned.** Ship the primitives as specified and grow/shrink
  functionality incrementally; no v1/v2 gates. Scope discipline stays: fill-driven
  PnL + equity first, commissions/splits added when needed rather than promised.
- **Agent docs live at `agents/`** (repo root), agent-first discoverability.

## Open Questions

- None — all previously-open items resolved (above).
