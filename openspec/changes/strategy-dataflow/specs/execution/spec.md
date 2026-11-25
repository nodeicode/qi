## Purpose

Execution is the venue seam and only environment difference. The strategy
produces a typed `Order`; the venue's execution client accepts it — backtest
simulates fills, live routes to the broker. The signal-string /
asset-class executor dispatch and the engine-centric `ExecutorProtocol` are
removed.

## ADDED Requirements

### Requirement: Typed order primitive

`quant/order.py` MUST define a typed `Order` carrying side, quantity, the
instrument, the venue, price context (limit/tag, price), and timestamps, with
canonical `to_schema`/`from_schema` round-tripping so orders cross the
backtest/live seam byte-identically.

#### Scenario: Order round-trips through the canonical schema
GIVEN a typed `Order` constructed for a strategy
WHEN it is serialized and deserialized
THEN the round-tripped order equals the original, and the venue/portfolio seams accept the canonical shape

### Requirement: Venue resolved from the strategy declaration

The driver (backtester on the backtest root; trader on the live root) MUST
resolve `config.venue` (a registered venue id) to a `Venue` instance wired
with its `data_source` and `execution_client` (`quant/venue/base.py` +
`quant/venue/_registry.py`), and pass that venue to `process_strategy`.
Backtest resolves the backtest venue (deterministic fills); live resolves
the registered live venue (broker-bound). An unknown venue id MUST fail
construction loudly.

#### Scenario: Backtest resolves the backtest venue
GIVEN a strategy declaring `venue="backtest"`
WHEN the backtester constructs the run
THEN it resolves the backtest venue (deterministic simulated fills) and passes it to `process_strategy`

#### Scenario: Live resolves the registered live venue
GIVEN a strategy declaring a registered live venue id
WHEN the trader constructs the run
THEN it resolves that venue's execution client (broker-bound, gated) and passes it to `process_strategy`

#### Scenario: Unknown venue id rejected
GIVEN a strategy declaring a venue id that is not registered
WHEN the driver resolves it
THEN construction fails loudly with the available venue ids

## REMOVED Requirements

### Requirement: Asset-class routing

The signal-string → asset-class routing chain MUST be deleted from the
decision path: `TradingEngine.route`/`direction` (`quant/core/engine.py`),
`SIGNAL_REGISTRY` and `signal_asset_class`/`signal_direction`
(`quant/core/_utils.py`), and `ExecutorRouter._asset_class_for_signal` /
`_asset_class_for_position` with the per-asset-class child dispatch
(`quant/backtest/executors/router.py`). Routing MUST start from the typed
instrument the strategy holds; no signal string determines who executes.

## MODIFIED Requirements

### Requirement: Executor protocol contract

The engine-centric `ExecutorProtocol` surface (`should_run`, `data_end_dt`,
`get_precomputed_signal`, `enter`, `exit`, lifecycle hooks, `record_run`;
`quant/core/protocols.py`) MUST be deleted. Backtest and live MUST each
expose a venue execution client that accepts typed `Order`s and reports
fills; scheduling comes from the strategy's declared subscriptions; the
engine loop records the decision transcript.

#### Scenario: Engine decoupled from broker APIs
GIVEN engine decision code
WHEN it needs scheduling, positions, or orders
THEN it talks only to the venue (its execution client and data source) — never to a broker SDK directly

#### Scenario: Both environments implement the protocol
GIVEN the backtest venue and the live venue
WHEN the execution-client methods are enumerated
THEN both implement each one with environment-appropriate semantics over the typed-order contract

#### Scenario: Engine never sees an executor
GIVEN the reshaped `process_strategy`
WHEN a decision produces an order
THEN the engine submits it to the venue's execution client — no `ExecutorProtocol` exists on the path

### Requirement: Backtest fills

Backtest fills MUST be simulated inside the backtest venue's execution
client (`quant/venue/backtest.py`), reshaping the machinery from
`ExecutorRouter`/`EquityExecutor`/`BacktestExecContext`
(`quant/backtest/executors/*`): fills price on closed tradable bars, honor
entry/exit timing rules, are gated (entry window, direction, session), and
record into the run ledger from which the equity curve and performance
metrics (`Backtester.compute_equity_curve` /
`calculate_performance_metrics`) are computed. At end of run the backtest
venue MUST force-close any remaining open positions (replacing
`ExecutorRouter.force_close_remaining`).

#### Scenario: Fill prices in tradable space
GIVEN a backtest order for a signal bar
WHEN the fill is simulated
THEN it prices on the tradable close of the closed signal bar while the author body ran on adjusted bars

#### Scenario: Gated order not filled
GIVEN an order outside the gate (entry window, direction, or session)
WHEN submitted to the backtest venue
THEN no fill is simulated and the decision transcript records the rejection

### Requirement: Live orders through venue-bound clients

The live path MUST reshape `LiveExecutor`/`UnifiedExecutor`
(`quant/live/executors/*`) and the `trader.py` loop into the live venue's
execution client: order submission is gated (venue session state + strategy
gates), fills and partial fills are tracked into the position schema, and
failures surface as errors rather than silent no-ops. No strategy talks to a
broker client directly.

#### Scenario: Gated submission
GIVEN a typed order from the author body
WHEN the live venue client is about to submit
THEN it verifies gates (e.g. market open, entry window) before placing the order

#### Scenario: Partial fill tracked
GIVEN a live order that fills partially
WHEN the venue client reconciles
THEN remaining quantity and status are tracked and reported in the position schema, and the Portfolio the strategy reads reflects the fill

### Requirement: Parity contract

Backtest and live MUST agree on decision-relevant semantics: both run the
identical transformed `process_strategy` path, interpret the same typed
events and orders, and share the same position schema. Where fills
legitimately differ (slippage, partial fills), the divergence MUST be
attributable to the venue's execution client, not to decisions, and
observable (reported) rather than silent.

#### Scenario: Divergence attributable to execution
GIVEN the same strategy in backtest and live
WHEN outcomes differ
THEN the difference is explainable by the venue's fill simulation vs real order flow, and the monitor (when enabled) can surface it as execution-side, not decision-side

#### Scenario: Only the venue differs
GIVEN the same strategy, config, and event stream
WHEN processed in backtest and live
THEN both drive the same body through the same entry point, and outcomes differ only by the venue's fills

### Requirement: Executor conformance for custom implementations

Conformance MUST move to the venue seam: a custom venue's execution client is
only parity-safe if it passes the execution-port suite against both the
backtest and live roots (typed-order contract, fill-ledger invariants,
position surface). Qi's shipped venues pass by construction; a replacement
that does not pass MUST NOT be treated as trustworthy for paper/live.

#### Scenario: Conforming custom executor accepted
GIVEN a custom venue execution client that honors the typed-order contract
WHEN its conformance suite runs against both roots
THEN it passes and may run a strategy across backtest and live

#### Scenario: Contract-violating executor fails loudly
GIVEN a custom venue execution client that slips scheduling or fill semantics
WHEN its conformance suite runs
THEN it fails loudly rather than silently risking drift between research and production
