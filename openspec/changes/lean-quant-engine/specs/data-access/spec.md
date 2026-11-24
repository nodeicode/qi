## Purpose

The data layer both environments share: one `DataSourceProtocol` (`get_historical_data(ticker, start, end, interval, ...)`) implemented by a live handler (REST + persistent cache + in-memory fast path) and a backtest handler (read-only views of preloaded frames), so decisions read identical-shaped bars in research and production.

## ADDED Requirements

### Requirement: Data source protocol

The SDK MUST expose `DataSourceProtocol` with `get_historical_data(ticker, start, end, interval, use_cache, adjustment) -> DataFrame` and both live and backtest handlers MUST implement it. Handlers for instruments without adjustments MUST accept and ignore the `adjustment` argument.

#### Scenario: Live and backtest shapes agree
GIVEN the same ticker, range, and interval
WHEN queried through the live handler and the backtest handler
THEN both return DataFrames with the same column names and timestamp normalization

#### Scenario: Adjustment argument tolerated
GIVEN a handler whose instrument has no corporate-action adjustments
WHEN called with `adjustment="all"`
THEN the call succeeds and returns natural bars

### Requirement: Adjusted-bar fast path (MemBars)

The live handler MUST maintain an in-memory store keyed by (ticker, interval, adjustment) that serves decision windows without deserialization. A bucket MUST be appended only when fully settled (all its minutes arrived) — partially formed bars MUST NOT be served. The store MUST be seeded from history (REST/cache) and appended from the live stream, and MUST serve the same appended tail for both adjustment keys.

#### Scenario: Settled-bar guarantee
GIVEN a live minute stream
WHEN a bucket is still accumulating minutes
THEN reads never see that partial bar
WHEN its final minute arrives
THEN the settled bar becomes visible to reads atomically

#### Scenario: Fast-path coverage
GIVEN a seeded store covering a decision window
WHEN the engine requests that window
THEN the handler answers from the in-memory store without disk or network access

#### Scenario: Fast-path miss falls back
GIVEN a request window not covered by the in-memory store
WHEN the engine requests it
THEN the handler falls back to cache/REST and logs the miss

### Requirement: Seed and edge semantics

The live handler MUST define how the in-memory store answers pre-open and startup requests (seed from REST, edge-wait for the session-open bar before falling back) and MUST NOT serve a bar newer than the live stream has closed.

#### Scenario: Pre-open first asks served from seeds
GIVEN a request at session open for a bar not yet streamed
WHEN the handler answers
THEN it serves the seeded history or waits for the stream edge rather than fabricating a bar

### Requirement: Persistent cache

The live handler MUST cache fetched history to disk (per ticker/interval/adjustment) and serve cached windows without re-fetching when `use_cache` is true. Cache reads MUST be byte-stable with respect to the fetch one would otherwise perform.

#### Scenario: Second fetch hits cache
GIVEN a ticker range already fetched once
WHEN requested again with `use_cache=True`
THEN no network call occurs and the returned frame equals the first fetch

### Requirement: Data-source registration and discovery

Data sources MUST be registerable by name (built-in backtest and live handlers; entry-point group `quant.datasources` for extras) and resolved from strategy/runtime config, as the sibling of the strategy registry. An unknown data-source name MUST fail configuration loudly with available names listed.

#### Scenario: Data source resolved by name
GIVEN config naming a registered data source
WHEN the runtime resolves it
THEN the corresponding handler is constructed with the configured parameters

#### Scenario: External data source via entry point
GIVEN another distribution registers a data source under `quant.datasources`
WHEN `get_datasource("<that-name>")` is called
THEN the entry-point handler is returned without SDK code changes

#### Scenario: Unknown data source rejected
GIVEN config naming an unregistered data source
WHEN the runtime resolves it
THEN configuration fails with a message listing registered names

### Requirement: Data-source conformance across environments

A data source is only trustworthy for research-to-live parity if it passes the data-port conformance suite, which MUST run the same handler contract invariants against both the backtest and live roots (same column shape, timestamp normalization, settled-bar semantics). Qi's shipped defaults pass by construction; a custom handler MUST pass the suite to be treated as parity-safe.

#### Scenario: Conforming custom handler accepted
GIVEN a custom data source that satisfies the protocol contract
WHEN its conformance suite runs against both roots
THEN it passes and may back paper/live runs with the same shape guarantees

#### Scenario: Violating handler fails loudly
GIVEN a custom data source that mis-handles the `adjustment` argument or returns inconsistent shapes
WHEN its conformance suite runs
THEN it fails loudly rather than silently risking backtest/live drift

### Requirement: Backtest views

The backtest handler MUST wrap pre-loaded frames and expose named slices (no re-parse per request). Requests MUST be served by index lookup into the pre-loaded frame within the handler's valid range; out-of-range requests MUST return empty frames, not raise.

#### Scenario: Fast slice of preloaded frame
GIVEN a backtest run over a loaded frame
WHEN the engine requests a window
THEN the handler returns the window from memory without re-parsing source data
