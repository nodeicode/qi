## Purpose

The execution seam: the one interface (`ExecutorProtocol`) both environments implement, so the engine never touches broker specifics. Backtest simulates fills per asset class; live places and tracks real orders; both expose the same scheduling and position surface, and both implement the same per-asset-class contract so adding an asset class means adding one executor pair.

## ADDED Requirements

### Requirement: Executor protocol contract

The SDK MUST define `ExecutorProtocol` covering scheduling (`should_run`, `data_end_dt`), data handoff (`get_precomputed_signal`), positions (`active_positions`), entry/exit (`enter`, `exit`), lifecycle hooks (`after_entry`, `after_exit`, `after_process_ticker`, `record_run`). The engine MUST interact with the environment exclusively through this interface.

#### Scenario: Engine decoupled from broker APIs
GIVEN engine decision code
WHEN it needs scheduling, positions, or orders
THEN it calls only executor protocol methods, never a broker SDK directly

#### Scenario: Both environments implement the protocol
GIVEN the backtest executor and the live executor
WHEN the protocol methods are enumerated
THEN both implement each one with environment-appropriate semantics

### Requirement: Asset-class routing

Executors MUST route entry/exit by the signal's asset class through a registry mapping asset class → executor implementation. The default registry MUST contain equity; other classes MUST be addable by registering a class, with children constructed lazily so unused classes cost nothing.

#### Scenario: Equity signal routes to equity executor
GIVEN an equity signal
WHEN entering a position
THEN the registered equity executor handles it

#### Scenario: Registered class extends routing
GIVEN a strategy registering a custom asset class executor
WHEN a signal of that class arrives
THEN the custom executor handles entry and reports fills

### Requirement: Backtest fills

The backtest executor MUST simulate fills on closed, tradable bars (price in raw/tradable space while indicators run adjusted), honor entry/exit timing rules, and record fills into the run ledger for the equity-curve computation.

#### Scenario: Fill prices in tradable space
GIVEN a backtest entry signal
WHEN the fill is simulated
THEN it prices on the tradable close of the signal bar

### Requirement: Live orders through venue-bound clients

The live executor MUST submit orders through the execution client of the strategy's venue, track fills and partial fills, and expose positions in the canonical schema. Order submission MUST be gated (venue session state + strategy gates) and MUST surface failures as errors rather than silent no-ops.

#### Scenario: Gated submission
GIVEN an entry signal
WHEN the live executor is about to submit
THEN it verifies gates (e.g. market open, entry window) before placing the order

#### Scenario: Partial fill tracked
GIVEN a live order that fills partially
WHEN the executor reconciles
THEN remaining quantity and status are tracked and reported in the position schema

### Requirement: Parity contract

Backtest and live MUST agree on decision-relevant semantics: same entry/exit signal interpretation, same position schema, same scheduling. Where fills legitimately differ (slippage, partial fills), the divergence MUST be attributable to execution, not decisions, and observable (reported) rather than silent.

#### Scenario: Divergence attributable to execution
GIVEN the same strategy in backtest and live
WHEN outcomes differ
THEN the difference is explainable by fill simulation vs real order flow, and the monitor (when enabled) can surface it as execution-side, not decision-side
