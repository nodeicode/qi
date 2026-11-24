# Proposal

## Why

The engine is split across two repos (`quant-engine` + `quant` submodule) for one
reason — keeping the private alpha (`quant/strategies/example_equity`) out of a public
repo. That split costs ~22% of commit history in submodule bumps, a duplicate
CI/openspec/lockfile/harness, an AST-level import fence in both trees, and it makes
the open-source goal impossible: a public repo with a private submodule cannot be
cloned by strangers, killing roadmap P4's "clone → backtest → paper → live" story.

Just as important: the current seams (Data, Execution, Config, Session,
Observability, Decisions) are **opinionated shapes carved to fit `example_equity` and
the existing strategies** — they work, but they are strategy-shaped, not
primitive-shaped. The unification + public cut is the opportunity to rebuild them
as genuinely general, robust primitives — informed by Nautilus Trader and other
quant-finance packages in typing, Protocols, and extensibility — without becoming
a heavy framework. The product position stays the opposite of Nautilus (which is
to quant what Claude Code is to Pi): a **lean, boring, forkable foundation**.

## What Changes

- **BREAKING — repo unification:** `quant` submodule absorbed into `quant-engine`
  as a real directory on a `sdk-cut` branch (private). One uv workspace / lock /
  CI / openspec / pre-commit; `python/` folded into the distribution as
  `quant.live`; `TID251` `banned-api` removed (import-layering test + `_private.py`
  naming keep discipline).
- **BREAKING — identity & goal:** the public product is **`quant-engine`**: an SDK
  / foundation of fully extensible primitives to **research and backtest
  strategies *and* portfolios**, and deploy them with **research → paper → live
  parity**. Parity is achieved through **deterministic structure and the
  state-machine nature** of the engine, not duplicated code paths.
- **Nautilus-informed primitives (new shapes):** a **`Venue`** primitive replaces
  the loose MarketSession — one cohesive object: venue identity, session/calendar
  state, market-data source, and execution client (the venue is both "broker" and
  "market session"); a minimal **typed-instrument** model (`Instrument` with
  symbol/venue/asset-class/contract specs); a **`Portfolio`** primitive (positions
  per instrument+venue, PnL, account state) shared by research, backtest, and
  live; positions and orders as explicit **state machines** with deterministic
  replay; worker/protocol conventions (Protocols, `TypedDict`/dataclass
  boundaries) that downstream packages can extend.
- **Opinionated-by-default, replaceable-everything:** the decision engine is the
  one place quant-engine is opinionated; Data, Execution, Venue, Config, and
  Observability ship a sensible default (Alpaca venue, local env/file config,
  emit primitives) and are swappable via Protocols.
- **BREAKING — config is local-first:** no AWS required — default config backend
  is local env/file; SSM Parameter Store becomes an optional prod backend, not the
  default.
- **BREAKING — observability as primitives:** telemetry is reduced to span timing
  + an emit interface; shadow signals / reconcile / sidecar become optional
  extended functionality devs build on the primitives (kept in the private
  superset, re-shipped later if wanted).
- **BREAKING — alpha extraction + equity scope trim:** all of
  `strategies/example_equity`, options/IC/futures machinery move to the private
  superset; public tree is equity-only with `example_equity` behind the
  `get_strategy` registry + entry points; TA-Lib/`cpp_native` become optional
  extras with pure-pandas fallback.
- **Agent context as a first-class deliverable:** layered `.md` context files for
  AI agents (root `AGENTS.md` + per-subsystem agent docs + extensibility recipes),
  because most extensibility work will be done by agents — a good baseline now,
  improved continuously.
- **SDK packaging & launch:** one distribution (`quant`) with CLI entry points
  (`quant backtest`, `quant paper`, `quant live`, `quant portfolio`); public repo
  exported from the branch with curated history; secret re-audit;
  reproduce-from-clone test as the gate.

## Capabilities

### New Capabilities

- `strategy-sdk`: authoring and injection of strategies — `StrategyProtocol`,
  `BaseStrategyConfig` validation, registry + entry points, example equity strategy.
- `decision-engine`: the shared, deterministic engine — `process_strategy`/
  `process_ticker`, bar-driven scheduling, entry gating, sizing; backtest and live
  execute the same state machine over the same decisions; raw-close confined to
  backtest execution; signal cache + warm start.
- `portfolio`: positions as state machines per (instrument, venue), fills →
  position lifecycle, realized/unrealized PnL, account state, portfolio-level
  metrics across strategies and venues.
- `backtesting`: the driver — upfront prefetch, per-bar engine loop (same
  decisions as live), fill simulation, equity curve, performance metrics,
  offline goldens/reproducibility.
- `data-access`: `DataSourceProtocol`, adjusted-bar invariant, `MemBars` fast path
  with settled-bar guarantee, persistent cache, backtest frame-view handler.
- `execution`: `ExecutorProtocol` — the environment boundary; venue-bound
  execution clients (live orders/fills) and backtest fill simulators implementing
  the same interface; asset-class routing extensible by registry.
- `venue`: the venue primitive — identity + session/calendar state + market data
  + execution client in one swappable unit (`AlpacaVenue` default,
  `BacktestVenue` deterministic); canonical naive-UTC boundary.
- `runtime-config`: passive version-checked polling with pluggable backends —
  **local env/file default**, SSM optional; live application without restarts.
- `observability`: primitives only — span timing (age ∥ duration) and an optional
  emit interface; everything else (shadow signals, reconcile, sidecar) is
  extended functionality built by developers on these primitives.
- `agent-context`: layered markdown context for AI agents — root `AGENTS.md`,
  per-subsystem agent docs, and extensibility recipes — kept authoritative and
  in sync with the code surface.

### Modified Capabilities

- None — this is the target contract for a new repository. The existing
  `quant-boundary` spec is superseded and archived in the new repo.

## Impact

- **Repos:** `quant` submodule → real directory on `sdk-cut` branch; new public
  repo exported after green; private superset (renamed `quant-alpha`) keeps the
  alpha + options/IC/futures + sidecar.
- **Code:** engine + data + backtest + utils retained (they encode the good
  patterns) but re-skinned behind the new primitives (venue, portfolio, typed
  instruments, state machines); live layer (`python/`) → `quant.live`; ~15k SLOC
  of options/IC/futures/alpha removed from the public tree; dual harness/CI/
  openspec collapsed.
- **APIs:** public surface = `quant.core` (engine, protocols), `quant.instruments`,
  `quant.venue`, `quant.portfolio`, `quant.data`, `quant.backtest`, `quant.live`,
  `quant.strategies`, `quant.utils`, `quant.observability` + CLI. Protocols in
  single homes; convenience re-exports.
- **Dependencies:** numpy/pandas core; `alpaca-py`, `boto3`, `ta-lib` become
  optional extras; no compiled-kernel build in the default install.
- **Operational:** prod deployment still SSM/EC2 (optional path, unchanged
  behavior); dev experience is zero-AWS by default.
- **Docs:** agent-context layer added; all existing boundary/split documentation
  rewritten for the unified shape.
