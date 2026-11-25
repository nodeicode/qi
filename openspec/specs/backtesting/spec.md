# Backtesting Specification

## Purpose

The backtest driver: prefetch the lookback data once, then replay bars through the shared decision pipeline with an executor that simulates fills, producing trades, an equity curve, and performance metrics — identical decision code to live, so a backtest result is a truthful preview of the live path.

## Requirements

### Requirement: Driver loop

A single backtest driver MUST run the shared engine per bar over the configured range: prefetch signal/manage bars upfront, iterate bars in time order, invoke `process_strategy` with the backtest executor, and record each decision/fill into a trades ledger. The driver MUST be strategy-agnostic (driven by config + strategy protocol, no strategy-specific code).

#### Scenario: Strategy-agnostic run
GIVEN any strategy implementing the strategy protocol
WHEN the driver runs over a range
THEN it executes without importing or referencing strategy internals

#### Scenario: Ledger records every trade
GIVEN a completed backtest
WHEN the trades ledger is inspected
THEN it contains one row per entry/exit decision with fill prices, quantities, and timestamps

### Requirement: Equity curve and metrics

The driver MUST produce an equity curve from realized fills and MUST compute standard performance metrics (returns, risk-adjusted measures, drawdown) plus per-reason trade statistics. Metrics MUST be derived from the same ledger the live side reports from, so research numbers are computed the same way as monitor numbers.

#### Scenario: Equity curve from fills only
GIVEN a backtest with entries and exits
WHEN the equity curve is computed
THEN values change only when fills occur

#### Scenario: Reason statistics
GIVEN a backtest with exits tagged by reason
WHEN statistics are computed
THEN exit reasons are aggregated (counts, returns per reason)

### Requirement: Reproducibility

A backtest MUST be reproducible from the same inputs: strategy version + config + data source + date range MUST deterministically produce the same trades and metrics (no dependence on wall-clock, iteration order, or environment). A golden-regression fixture MUST exist for the example strategy and MUST fail CI on any drift in the decision path.

#### Scenario: Identical rerun
GIVEN the same strategy, config, data, and range
WHEN run twice in different processes
THEN trades and metrics are identical

#### Scenario: Golden regression guard
GIVEN a golden fixture for `example_equity`
WHEN the backtest suite runs
THEN the example's trades/metrics match the golden within tolerance, and a decision-path change that moves them fails CI

### Requirement: Research-to-live reckoning

The backtest MUST report the same per-bar ledger fields the live monitor emits (signal id, entry/exit, reason, fills) so shadow comparisons are possible. It MUST NOT silently diverge in column meaning between environments.

#### Scenario: Shadow-comparable output
GIVEN a backtest ledger and a live monitor feed for the same strategy
WHEN both are exported
THEN the shared fields have the same names and semantics
