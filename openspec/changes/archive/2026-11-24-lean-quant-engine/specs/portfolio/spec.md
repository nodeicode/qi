## Purpose

The portfolio primitive: positions as explicit state machines per (instrument, venue), aggregated from fills into a portfolio with realized/unrealized PnL and account state — identical in research, backtest, and live, so portfolio-level results are directly comparable across environments.

## ADDED Requirements

### Requirement: Position as a state machine

A position MUST be created from a fill/order on an instrument at a venue, MUST transition through explicit states (e.g. opened → adjusting → closed) as fills arrive, and MUST record quantity, average price, cost basis, and realized PnL through its lifecycle. A position MUST NOT be mutated by side effects — only by fills/events.

#### Scenario: Fills drive transitions
GIVEN an open position
WHEN an additional fill arrives
THEN the position transitions to the adjusted state with updated quantity and average price
WHEN a closing fill arrives
THEN the position transitions to closed and records realized PnL

#### Scenario: Position keyed by instrument and venue
GIVEN two positions on the same instrument at different venues
WHEN inspected
THEN they are distinct position objects with venue-scoped state

### Requirement: Portfolio aggregation

The portfolio MUST aggregate all positions under its care (across strategies and venues), track account state (cash, equity, buying power where the venue supplies it), and compute portfolio-level unrealized/realized PnL from position state plus a pricing source.

#### Scenario: Portfolio totals track positions
GIVEN a portfolio containing open positions
WHEN its unrealized PnL and equity are computed
THEN they reflect the sum of position state and current prices

#### Scenario: Closed positions settle
GIVEN positions that have closed during a run
WHEN portfolio realized PnL is computed
THEN it equals the sum of the closed positions' realized PnL

### Requirement: Environment parity

The same portfolio primitives MUST run in backtest (fills from simulation) and live (fills from venue execution clients), with the same position/portfolio semantics and the same metric computations. Portfolio metrics MUST derive from the same ledger fields the backtest reports, so research numbers equal live numbers by construction.

#### Scenario: Same metrics both environments
GIVEN a portfolio in backtest and a portfolio in live over equivalent fills
WHEN PnL and equity metrics are computed
THEN the computations are the same functions over the same position semantics

### Requirement: Portfolio research across strategies

The backtest MUST support running multiple strategies/instruments into one or more portfolios and reporting per-strategy and combined metrics, so portfolio-level research (allocation, correlation, combined drawdown) is possible without forking the engine.

#### Scenario: Combined portfolio metrics
GIVEN a backtest running two strategies into one portfolio
WHEN combined metrics are requested
THEN they are computed across both strategies' fills, and per-strategy metrics remain available separately

### Requirement: Persistence and recovery

Live portfolio state MUST be persistable (snapshot) and recoverable on restart; after recovery the portfolio MUST re-derive position state from persisted fills/snapshots without manual intervention.

#### Scenario: Restart recovery
GIVEN a live portfolio snapshot taken before a restart
WHEN the runtime restarts and loads the snapshot
THEN positions and balances are restored to match pre-restart state
