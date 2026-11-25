# Data Access Specification

## Purpose

The data layer both environments share: one `DataSourceProtocol` (`get_historical_data(ticker, start, end, interval, ...)`) implemented by a live handler (REST + persistent cache + in-memory fast path) and a backtest handler (read-only views of preloaded frames), so decisions read identical-shaped bars in research and production.

## Requirements

### Requirement: Live subscription streaming to buffers

On the live path, each declared subscription MUST be fulfilled by a stream
(`quant/data/ws/aggregator.py` + `quant/data/ws/streams/*`) that appends
**settled bars** (fully-formed buckets only) into the same per-subscription
`_buffers` the engine reads — no per-bar re-fetch or re-parse. Backtest
(replay of pre-loaded frames) and live (stream) MUST fulfill the same
`(instrument, datatype, cadence)` buffer with an identical typed shape, and
seed/edge semantics (seed from REST/cache on startup/pre-open, wait for the
stream edge rather than fabricating a bar) MUST apply per subscription.
Stream gaps MUST fall back to seed/edge without serving a bar newer than the
stream has closed.

#### Scenario: Stream appends settled bars to the subscription buffer
GIVEN a live stream for a bars subscription
WHEN a minute bucket fully settles
THEN the settled bar is appended to the subscription buffer atomically and reads see it — partial bars are never served

#### Scenario: Backtest replay and live stream fulfill the same buffer
GIVEN the same `(instrument, "bars", cadence)` subscription
WHEN fulfilled on the backtest root (replay) and the live root (stream)
THEN both append the identical typed shape to the subscription buffer, and the engine feeds `decide` from that buffer in both

#### Scenario: Stream gap falls back without fabricating
GIVEN a stream gap for a subscription
WHEN a read crosses the gap on the live path
THEN the handler falls back to seed/cache or waits for the stream edge — it never serves a bar newer than the stream has closed

### Requirement: Data source protocol

`DataSourceProtocol.get_historical_data(ticker, start, end, interval,
...)` MUST be replaced by a subscription surface keyed by
`(instrument, datatype, cadence)` that a strategy declares and the port
fulfills in both environments; `bars` is the shipped datatype. Handlers
reshape in place: `BacktestDataHandler`
(`quant/backtest/backtest_data_handler.py`), `SyntheticDataHandler`
(`quant/data/synthetic_data_handler.py`), and the live handler/cache path
(`quant/data/*`).

#### Scenario: Live and backtest shapes agree
GIVEN the same `(instrument, "bars", cadence)` subscription on the live root and the backtest root
WHEN it is resolved
THEN both handlers return events with the same column names and timestamp normalization

#### Scenario: Adjustment argument tolerated
GIVEN a handler whose instrument has no corporate-action adjustments
WHEN it fulfills a bars subscription with `adjustment="all"` requested
THEN the call succeeds and returns natural bars

### Requirement: Adjusted-bar fast path (MemBars)

The live handler MUST maintain the in-memory store keyed by
`(instrument, datatype, cadence)` instead of (ticker, interval, adjustment)
and MUST keep its invariants: a bucket is appended only when fully settled,
partially formed bars are never served, the store is seeded from history and
appended from the live stream, and the appended tail is served identically
for both adjustment keys.

#### Scenario: Settled-bar guarantee
GIVEN a live minute stream for a bars subscription
WHEN a bucket is still accumulating minutes
THEN reads never see that partial bar
WHEN its final minute arrives
THEN the settled bar becomes visible to reads atomically

#### Scenario: Fast-path coverage
GIVEN a seeded store covering a subscription's decision window
WHEN the engine requests that window
THEN the handler answers from the in-memory store without disk or network access

#### Scenario: Fast-path miss falls back
GIVEN a request window not covered by the in-memory store
WHEN the engine requests it
THEN the handler falls back to cache/REST and logs the miss

### Requirement: Seed and edge semantics

The live handler MUST define how the in-memory store answers pre-open and
startup requests (seed from REST, edge-wait for the session-open bar before
falling back) per subscription, and MUST NOT serve a bar newer than the live
stream has closed. The seed boundary (`seed_end = end_dt + cadence`) MUST
hold per subscription so a pre-open warmup never truncates freshly banked
decision bars.

#### Scenario: Pre-open first asks served from seeds
GIVEN a pre-open request for a subscription's bar not yet streamed
WHEN the handler answers
THEN it serves the seeded history or waits for the stream edge rather than fabricating a bar

#### Scenario: Pre-open seed never truncates the decision edge
GIVEN a pre-open warmup for a subscription
WHEN the first decision bars are banked
THEN the seed tags the buffer through the next decision bar and the stream supplies the edge

### Requirement: Persistent cache

The live handler MUST cache fetched history to disk keyed by the
subscription's instrument/datatype/cadence span (superseding ticker/interval
keys) and serve cached windows without re-fetching when `use_cache` is true.
Cache reads MUST be byte-stable with respect to the fetch one would
otherwise perform.

#### Scenario: Second fetch hits cache
GIVEN a subscription span already fetched once
WHEN requested again with `use_cache=True`
THEN no network call occurs and the returned frame equals the first fetch

#### Scenario: Cache keyed by subscription span
GIVEN a bars subscription over a span
WHEN fetched twice
THEN both fetches resolve to the same cache entry keyed by instrument/datatype/cadence

### Requirement: Data-source registration and discovery

Data sources MUST be registerable by name (built-in backtest and live
handlers; entry-point group `quant.datasources` for extras) and resolved
from strategy/runtime config, as the sibling of the strategy registry. A
source MUST declare the `(instrument, datatype, cadence)` subscriptions it
offers and be discovered by that capability. An unknown data-source name
MUST fail configuration loudly with available names listed.

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

#### Scenario: Custom source discovered by its offers
GIVEN a data source declaring bars offers for specific instruments/cadences
WHEN the port enumerates sources for a subscription
THEN it finds the source by its typed offers

### Requirement: Data-source conformance across environments

A data source is only trustworthy for research-to-live parity if it passes
the data-port conformance suite, which MUST run the typed offer/fulfillment
contract invariants against both the backtest and live roots (same column
shape, timestamp normalization, settled-bar semantics). Qi's shipped defaults
pass by construction; a custom handler MUST pass the suite to be treated as
parity-safe.

#### Scenario: Conforming custom handler accepted
GIVEN a custom data source that satisfies the typed fulfillment contract
WHEN its conformance suite runs against both roots
THEN it passes and may back paper/live runs with the same shape guarantees

#### Scenario: Violating handler fails loudly
GIVEN a custom data source that mis-handles the fulfillment contract or returns inconsistent shapes
WHEN its conformance suite runs
THEN it fails loudly rather than silently risking backtest/live drift

### Requirement: Backtest views

The backtest handler MUST fulfill subscriptions by replaying pre-loaded
frames on the event stream (`Backtester._prefetch_data` feeds subscription
buffers once; the per-bar loop consumes the buffer — no re-parse, no
re-fetch per bar). Requests MUST be served by index lookup into the
pre-loaded frame within the handler's valid range; out-of-range requests
MUST return empty streams, not raise.

#### Scenario: Fast slice of preloaded frame
GIVEN a backtest run over a loaded subscription
WHEN a subscription window is requested
THEN the handler returns the window from the pre-loaded buffer without re-parsing source data

#### Scenario: Pre-loaded frames replay without re-parse
GIVEN a backtest run over a declared bars subscription
WHEN bars are consumed per bar
THEN they come from the pre-loaded buffer on the event stream and no per-bar data fetch/parse occurs

#### Scenario: Empty view does not raise
GIVEN a backtest bar outside the pre-loaded window
WHEN the loop requests it
THEN an empty stream is returned and the loop proceeds without raising