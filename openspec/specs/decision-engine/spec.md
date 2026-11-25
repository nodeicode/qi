# Decision Engine Specification

## Purpose

The shared, deterministic decision engine: backtest and live execute the same state machine over the same decisions, so research-to-live parity is structural, not duplicated. The engine is the one place quant-engine is opinionated; everything else is replaceable via Protocols.

## Requirements

### Requirement: TradingEngine reshaped in place (identity preserved)

The class MUST keep the name `TradingEngine` at
`quant.core.engine.TradingEngine`, its import path, and its public entry
points `process_strategy(strategy, now, venue)` and `warmup(now)`. The
string-pipeline internals (`run_signals`, `latest_signal`, `cache_signals`,
`route`, `direction`, `is_actionable`, `should_enter`, `build_position`,
`position_from_request`, and the `_supports_ticker` inspection) MUST be
deleted; `__init__`, `process_strategy`, and `warmup` MUST be reshaped to
subscriptions, the author body, and the decision transcript, with
`_signal_cache` becoming per-subscription `_buffers`. No other module
defines a strategy-driving engine class; `trader.py` and `backtester.py`
keep constructing and calling `TradingEngine`.

#### Scenario: Engine identity unchanged for callers
GIVEN `quant/core/engine.py::TradingEngine`
WHEN backtester and live trader construct and call it
THEN the class name, import path, and entry points are unchanged — only the pipeline internals differ

#### Scenario: String-pipeline methods are gone
GIVEN the reshaped `TradingEngine`
WHEN its public surface is inspected
THEN `run_signals`/`latest_signal`/`cache_signals`/`route`/`direction`/`is_actionable`/`should_enter`/`build_position` are absent

### Requirement: Decision transcript

`process_strategy` MUST record a deterministic decision transcript of the fed
event stream, the author body's decisions, and the venue's effects (orders
submitted, fills reported), so replay of (event stream + strategy body +
venue) reproduces identical decisions.

#### Scenario: Transcript replays identically
GIVEN the recorded transcript of a run
WHEN replayed with the same body and venue
THEN decisions reproduce byte-identically

#### Scenario: Golden suite re-pinned on the transcript
GIVEN the reshaped `TradingEngine`
WHEN `quant/tests/backtest/_replay_runner.py`, `test_determinism_two_process.py`, and `test_golden_regression.py` run
THEN they replay the transcript of the transformed path and pass (no legacy engine branch is pinned)

### Requirement: End-to-end parity proof (declarative capstone)

The change MUST ship an end-to-end parity test that drives the **same
declarative reference strategy** (`example_equity`, from `strategy-authoring`
`Reference strategy`) through the backtest venue AND a live venue (offline,
same deterministic synthetic fixture) via the one reshaped
`TradingEngine.process_strategy`, asserting: each venue's path is
deterministic across reruns; both venues run the same decision body and
trade; and the recorded decisions are identical **until the first
venue-reconciled divergence** (a fill priced by the venue feed). Because the
author body sizes and evaluates risk against the injected read-only
Portfolio, once fills price differently the Portfolio — and thus later
decisions — legitimately diverge; the divergence is therefore attributable
to the venue's fills, never to a difference in the decision pipeline. This
MUST upgrade `quant/tests/backtest/test_all_primitives_parity.py` from the
legacy 3-method engine path (`TradingEngine.process_strategy` via
`ExecutorRouter`) to the reshaped declarative path (`process_strategy` via
venue), and freeze the reference strategy's output to a golden artifact a
fresh run reproduces exactly.

#### Scenario: Same declarative strategy through both venues
GIVEN one declarative `example_equity` and the same synthetic fixture
WHEN it runs through the backtest venue and through a live venue
THEN both run the same reshaped `process_strategy`, both record decision transcripts and trade, and the decisions match for every tick before the first venue-reconciled fill diverges

#### Scenario: Each venue path is deterministic
GIVEN the live path (and the backtest path)
WHEN it runs twice over the same fixture
THEN the recorded decision transcript and portfolio metrics reproduce byte-identically

#### Scenario: Golden artifact reproduces exactly
GIVEN the frozen parity golden artifact
WHEN the parity test runs again on the same fixture
THEN the backtest decision sequence and portfolio metrics reproduce exactly

#### Scenario: Parity failure points at a seam, not a decision
GIVEN a divergence between the two venues in the parity test
WHEN it is inspected
THEN the divergence is attributable to the venue execution client (fill simulation vs real order flow) feeding the read-only portfolio — never to a difference in the shared decision pipeline

### Requirement: One deterministic decision entry point

All trading decisions MUST flow through `process_strategy(strategy, now,
venue)` — the `executor: ExecutorProtocol` parameter becomes the venue.
The reshaped loop resolves the declared subscriptions, serves closed bars
from `self._buffers` (backtest replay or live `data_source`), drives the
author body with (events, portfolio), submits returned typed `Order`s to the
venue's execution client, and appends the transcript. Backtest and live MUST
invoke the same transformed path; the venue is the only divergence.

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
bars only, **plus the venue's session calendar** (`is_open_at` / forced-close
from `quant/venue/base.py`): the strategy never runs when the venue says the
market is closed, and the venue's forced-close bounds exits. `INTERVAL`/
`MANAGE_INTERVAL` config gates, `executor.should_run` / `record_run` /
`data_end_dt` / `get_precomputed_signal`, and the engine's embedded phasing
in `process_strategy` MUST be deleted; no data fetches
(`fetch_data`/`fetch_manage_data` calls) or ad-hoc clocks inside the decision
path.

#### Scenario: Manage-only bar skips signal work
GIVEN a manage-cadence tick for a strategy with open positions
WHEN the engine processes it
THEN the author body's manage pass runs (evaluating portfolio positions) and the signal pass is skipped (no signal work, no signal fetches)

#### Scenario: Closed session gates the run
GIVEN a tick outside the venue's session calendar (or at forced-close)
WHEN the engine would run
THEN the body does not run new entries and exits are bounded by the venue's forced-close

#### Scenario: Signal bar runs full pipeline
GIVEN a signal-cadence tick
WHEN the engine processes it
THEN the author body runs over the closed bars of the due subscription only, its decisions are recorded and submitted

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

The engine MUST NOT own entry gating or sizing. `TradingEngine.should_enter`
and `check_entry_gate` (quant/core/_utils.py) MUST be deleted from the
decision path; `SIZING_STRATEGIES`/`SizingRegistry`/`size_for_asset`/
`size_for_signal` (quant/core/sizing.py) and their call sites
(`quant/backtest/executors/equity.py`, `quant/live/executors/unified_executor.py`)
MUST be removed. The author body decides entries and sizes from instrument
contract facts plus the injected read-only Portfolio (equity, available
margin, open exposure), returning a target expressed as a typed `Order`.

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
THEN it comes from the author body's sizing over instrument facts + the Portfolio, and `size_for_signal`/`SIZING_STRATEGIES` are absent from `quant/core/`

#### Scenario: Canonical position request
GIVEN an order produced by the author body
WHEN it is submitted
THEN it follows the canonical typed-`Order` schema (`quant/order.py`) shared with execution and portfolio — no `build_position_dict` request schema is emitted

### Requirement: Fixed-size position and manage contracts

The engine MUST call a single author body (`decide(events, portfolio)`) with
stable contracts over typed data: manage steps consume only closed data of
the declared cadence and the author evaluates the portfolio's existing open
positions for exits (engine-side `evaluate_manage_exit`/`call_manage_trade`
replaced by the author's exit intents), the author reads the framework risk
fields (`STOP_LOSS_PERCENT`/`TAKE_PROFIT_PERCENT`/`MAX_HOLDING_MINUTES`/
`USE_TRAIL_LOSS`) from its config to drive those exits, features are the
author's own computation, CLOSED positions (`quant/portfolio/*`
`PositionState`) are never managed, and a pre-fill bar never triggers an
exit. Author body errors MUST fail that tick loudly (logged + monitored),
not silently.

#### Scenario: Manage receives injected indicators
GIVEN a strategy declaring its manage-cadence subscription
WHEN the manage step is invoked
THEN the passed events contain the closed bars of that cadence from `self._buffers` and the portfolio holds the open positions — the author computes its own features and evaluates exits, `evaluate_manage_exit` is gone

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

`cache_signals`/`_signal_cache` (per-ticker LRU) MUST become
`self._buffers` — a bounded map keyed by `(instrument, datatype, cadence)`
feeding the author body's reads. `warmup(now)` MUST seed those buffers for
the declared subscriptions, off the decision path, and cache invalidation
MUST occur on config change so post-reload decisions never mix pre/post-
change state.

#### Scenario: Warm startup
GIVEN an engine constructed before market open
WHEN warmup runs with `now`
THEN the declared subscriptions' lookback frames populate `_buffers` (off the decision path, no monitor events) so the first live bar is served from buffer

#### Scenario: Config change invalidates cache
GIVEN a config update that changes a strategy's parameters
WHEN the update is applied
THEN the per-subscription buffers for that strategy are invalidated and next decisions use the new config

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

### Requirement: Author-body observability

The reshaped engine MUST keep the telemetry neutrality contract: it runs
identically with or without an emit hook. The span set MUST be redefined for
the author body — e.g. a per-tick `DECIDE` span (plus submission/venue effect
spans) replacing the deleted `RUN_SIGNALS`/`PROCESS_TICKER`/`EXECUTION_DISPATCH`
spans — recorded through the shared span primitive (`quant/observability.py`)
so backtest and live report comparable numbers. The removed engine spans
MUST NOT leave dangling telemetry references in `quant/core/engine.py`.

#### Scenario: Backtest runs without an emit hook
GIVEN no emit hook is provided
WHEN the reshaped pipeline runs in backtest
THEN it produces identical decisions and emits nothing

#### Scenario: Author-body spans recorded with a hook
GIVEN an emit hook is wired
WHEN the reshaped pipeline runs
THEN `DECIDE` and submission spans are recorded through the shared telemetry primitive