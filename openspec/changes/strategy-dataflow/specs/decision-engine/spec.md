## Purpose

The engine reshapes in place from a signal-string driver into the
determinism/replay/parity harness: it feeds the strategy's declared typed
subscriptions as a typed event stream, drives the author body, records a
deterministic decision transcript, and guarantees the venue is the only
environment difference. Gates and sizing leave the engine for the author
body.

## ADDED Requirements

### Requirement: Decision transcript

`process_strategy` MUST record a deterministic decision transcript of the fed
event stream, the author body's decisions, and the venue's effects (orders
submitted, fills reported), so replay of (event stream + strategy body +
venue) reproduces identical decisions.

#### Scenario: Transcript replays identically
GIVEN the recorded transcript of a run
WHEN replayed with the same body and venue
THEN decisions reproduce byte-identically

## MODIFIED Requirements

### Requirement: One deterministic decision entry point

All trading decisions MUST flow through `process_strategy(strategy, now,
executor)` — reshaped so the engine resolves the strategy's declared
subscriptions, feeds the typed event stream to the author body, and submits
the returned typed `Order`s to the venue. Backtest and live MUST invoke the
same transformed code path; the venue is the only divergence.

#### Scenario: Backtest and live drive the same function
GIVEN a backtest run and a live run of the same strategy
WHEN each bar/tick is processed
THEN both invoke the same reshaped `process_strategy` path over the same typed event stream, and any divergence between simulated and live outcomes is attributable only to the venue

#### Scenario: Deterministic rerun
GIVEN the same strategy, declarations, and event stream
WHEN processed twice in different processes
THEN the recorded decision sequences (entries, exits, sizes, orders) are identical

### Requirement: Bar-driven scheduling

The engine MUST gate signal generation and manage decisions by the
per-subscription cadence of the strategy's declared subscriptions on closed
bars only. `INTERVAL`/`MANAGE_INTERVAL` config gates, `executor.should_run` /
`record_run`, and the engine's embedded phasing MUST be deleted; no data
fetches or ad-hoc clocks inside the decision path.

#### Scenario: Manage-only bar skips signal work
GIVEN a manage-cadence tick for a strategy with open positions
WHEN the engine processes it
THEN signal generation and signal data fetches are skipped and only the manage path runs

#### Scenario: Signal bar runs full pipeline
GIVEN a signal-cadence tick
WHEN the engine processes it
THEN the author body runs over the closed bars of the due subscription only and its decisions are recorded

### Requirement: Adjusted-bar invariant

Decision frames MUST be built from adjusted bars only in both environments,
binding per datatype as datatypes ship on the subscription port. Raw-close
pricing MUST be confined to backtest-only execution (tradable-space exits)
and MUST NOT be readable by the author body on the live path.

#### Scenario: Raw close not available to decisions
GIVEN the live path
WHEN the author body reads a decision frame
THEN no raw-close column is present or readable

#### Scenario: Backtest prices exits in tradable space
GIVEN a backtest run with `Raw_Close` attached to frames
WHEN fills are simulated
THEN execution is priced on the raw close while the author body runs on adjusted bars

### Requirement: Entry gating and sizing

The engine MUST NOT own entry gating or sizing. `check_entry_gate` /
`should_enter` (`quant/core/engine.py`, `quant/core/_utils.py`) and the
`SIZING_STRATEGIES` / `size_for_signal` dispatch (`quant/core/sizing.py`)
MUST be removed from the decision path; the author body decides entries and
sizes from instrument contract facts plus the injected read-only account
projection, returning a target expressed as a typed `Order`.

#### Scenario: Entry outside gate window blocked
GIVEN an entry the author body evaluates outside its own gate
WHEN processed
THEN no order is produced for that instrument (the body's own gate runs — no engine-side gate is consulted)

#### Scenario: Author owns the gate
GIVEN the author body runs on a due subscription
WHEN it evaluates an entry
THEN the gate decision is the body's own expression over typed data and account state

#### Scenario: No sizing registry on the path
GIVEN an entry decision
WHEN a quantity is produced
THEN it comes from the author body's sizing over instrument facts + projection, and `size_for_signal`/`SIZING_STRATEGIES` are absent

#### Scenario: Canonical position request
GIVEN an order produced by the author body
WHEN it is submitted
THEN it follows the canonical typed-`Order` schema shared with execution and portfolio

### Requirement: Fixed-size position and manage contracts

The engine MUST call the author body with stable contracts over typed data:
manage steps consume only closed data of the declared cadence; the author
receives the declared subscription frames it asked for (features are its own
computation); CLOSED positions are never managed; a pre-fill bar never
triggers an exit. Author body errors MUST fail that tick loudly (logged +
monitored), not silently.

#### Scenario: Manage receives injected indicators
GIVEN a strategy declaring its manage-cadence subscription
WHEN the manage step is invoked
THEN the passed frame contains the closed bars of that cadence, computed from the buffers the strategy declared

#### Scenario: Strategy error is loud and contained
GIVEN the author body raises during processing
WHEN the engine processes a tick
THEN the error is logged and surfaced through telemetry, and unrelated strategies continue

#### Scenario: Closed position not managed
GIVEN a position whose state machine is CLOSED
WHEN the manage step runs
THEN it is skipped

#### Scenario: Manage exit requires the fill to be dated
GIVEN a backtest position whose fill time is after the manage frame end
WHEN managed
THEN no exit fires before the fill is dated

### Requirement: Signal cache and warm start

`cache_signals`/`_signal_cache` MUST reshape into a per-subscription
frame/event buffer keyed by `(instrument, datatype, cadence)`; warmup
(`TradingEngine.warmup`) MUST reshape to seed those buffers off the decision
path. Cache invalidation MUST occur on config change so post-reload decisions
never mix pre/post-change state.

#### Scenario: Warm startup
GIVEN an engine constructed before market open
WHEN warmup runs with `now`
THEN lookback frames for the declared subscriptions populate the buffers (off the decision path, no monitor events) so the first live bar is served from buffer

#### Scenario: Config change invalidates cache
GIVEN a config update that changes a strategy's parameters
WHEN the update is applied
THEN cached subscription buffers for that strategy are invalidated and next decisions use the new config
