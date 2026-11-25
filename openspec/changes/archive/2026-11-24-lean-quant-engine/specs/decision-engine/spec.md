## Purpose

The shared, deterministic decision engine: backtest and live execute the same state machine over the same decisions, so research-to-live parity is structural, not duplicated. The engine is the one place quant-engine is opinionated; everything else is replaceable via Protocols.

## ADDED Requirements

### Requirement: One deterministic decision entry point

All trading decisions MUST flow through `process_strategy(strategy, now, executor)` (and its per-ticker form) in both environments. The engine MUST be a deterministic state machine: for identical (strategy, config, bar timeline, executor behavior) inputs it MUST produce identical decisions — no dependence on wall clock, iteration order, or environment. Environment-specific concerns MUST be reachable only through the executor interface.

#### Scenario: Backtest and live drive the same function
GIVEN a backtest run and a live run of the same strategy
WHEN each bar/tick is processed
THEN both invoke the same `process_strategy` decision code path, and any divergence between simulated and live outcomes is attributable only to the executor implementation

#### Scenario: Deterministic rerun
GIVEN the same strategy, config, and bar timeline
WHEN processed twice in different processes
THEN the sequence of decisions (signals, entries, exits, sizes) is identical

### Requirement: Bar-driven scheduling

The engine MUST gate signal generation and manage decisions by the strategy's `INTERVAL` and `MANAGE_INTERVAL` configuration on closed bars only. It MUST NOT fetch data or run ad-hoc clocks inside the decision path.

#### Scenario: Manage-only bar skips signal work
GIVEN a manage-interval bar for a strategy with open positions
WHEN the engine processes it
THEN signal generation and signal data fetches are skipped and only the manage path runs

#### Scenario: Signal bar runs full pipeline
GIVEN a signal-interval bar
WHEN the engine processes it
THEN indicators, signals, entry gating, and sizing run and the result is cached per ticker

### Requirement: Adjusted-bar invariant

Decision frames MUST be built from adjusted bars only in both environments. Raw-close pricing MUST be confined to backtest-only execution (tradable-space exits) and MUST NOT be readable on the live decision path.

#### Scenario: Raw close not available to decisions
GIVEN the live path
WHEN a decision frame is assembled
THEN no raw-close column is present or readable by signal code

#### Scenario: Backtest prices exits in tradable space
GIVEN a backtest run with `Raw_Close` attached to frames
WHEN fills are simulated
THEN execution is priced on the raw close while indicators run on adjusted bars

### Requirement: Entry gating and sizing

The engine MUST apply entry gates (e.g. configurable `EntryTime` window, direction validation) and MUST size positions through the shared sizing primitive, producing a canonical position request. Sizing failures MUST abort the entry for that ticker without corrupting other state.

#### Scenario: Entry outside gate window blocked
GIVEN a signal generated outside the strategy's entry window
WHEN processed
THEN no entry is requested for that ticker

#### Scenario: Canonical position request
GIVEN an entry signal passes gating and sizing
WHEN a position request is created
THEN it follows the canonical position schema shared with execution and portfolio

### Requirement: Fixed-size position and manage contracts

The engine MUST call `calculate_indicators`, `generate_signals`, and `manage_trade` through the strategy protocol with stable signatures (strategy methods may consume injected indicator columns; `ticker` kwarg supported when declared). Strategy method errors MUST fail the tick for that strategy loudly (logged + monitored), not silently.

#### Scenario: Manage receives injected indicators
GIVEN a strategy declaring `INDICATOR_COLUMNS`
WHEN `manage_trade` is invoked
THEN the passed frame contains the declared columns computed from latest bars

#### Scenario: Strategy error is loud and contained
GIVEN a strategy method that raises during processing
WHEN the engine processes a tick
THEN the error is logged and surfaced through telemetry, and unrelated strategies continue

### Requirement: Signal cache and warm start

The engine MUST keep a bounded per-ticker signal cache (LRU) and MUST support a strategy-agnostic warmup pass (same pipeline, config-driven lookback). Cache invalidation MUST occur on config change, so post-reload decisions never mix pre/post-change state.

#### Scenario: Warm startup
GIVEN an engine constructed before market open
WHEN warmup runs with `now`
THEN lookback bars are fetched and the signal pipeline runs so the first live bar is served from cache

#### Scenario: Config change invalidates cache
GIVEN a config update that changes a strategy's parameters
WHEN the update is applied
THEN cached signals for that strategy are invalidated and next decisions use the new config

### Requirement: Parity telemetry neutrality

The engine MUST accept an optional emit hook and MUST behave identically without one. All pipeline timing MUST use the shared span primitive so backtest and live report comparable numbers.

#### Scenario: Backtest runs without monitor
GIVEN no emit hook is provided
WHEN the pipeline runs in backtest
THEN it produces identical decisions and emits nothing

#### Scenario: Spans recorded with hook
GIVEN an emit hook is wired
WHEN the pipeline runs
THEN signal/manage/sizing spans are recorded through the shared telemetry primitive
