# Strategy Authoring Specification

## Purpose

The declarative strategy surface: a strategy is declare + react. It declares
typed instruments, typed data subscriptions, and a venue, then runs an
author-owned body (`decide`) over typed data events and the injected account
projection, sizing itself from instrument contract facts and submitting
typed `Order`s (built via `instrument.make_order`) to the venue. The
collapsed post-change call chain is: `TradingEngine.process_strategy` →
resolve declared subscriptions → serve closed bars from buffers → feed
`decide(events, projection)` → strategy returns typed order/exit intents →
`instrument.make_order` → venue execution client → fill feeds the
projection.

## ADDED Requirements

### Requirement: Declarative strategy surface

A new strategy base (e.g. `quant/strategies/base.py`) MUST provide the
declarative surface: `name`, `config: BaseStrategyConfig` with declarative
fields (`instruments`, `subscriptions`, `venue`), and an author body
`decide(events, projection) -> list[DecisionItem]` (`DecisionItem` = typed
`Order` or exit intent, `Order` built by the strategy). A bar-driven
strategy MUST construct with resolvable declarations the engine can drive
over its declared subscriptions; the 3-method `StrategyProtocol` and
signal-string outputs MUST NOT appear on this surface.

#### Scenario: Bar-driven strategy constructs and resolves
GIVEN a strategy declaring an equity instrument, a bars subscription, and a venue
WHEN it is constructed
THEN the declarations resolve against the data port and `quant/venue`, and `decide` is callable over bars events

#### Scenario: Decision body runs over typed data
GIVEN a bars event delivered to the author body
WHEN `decide` evaluates
THEN it reads typed bars (adjusted only) plus the injected projection, and returns typed order/exit intents — no signal-string protocol is involved

#### Scenario: Exit intent closes a position on manage-cadence data
GIVEN an open position and its manage-cadence bars
WHEN the author body decides on manage events
THEN it may return an exit intent which the engine routes to the venue — no engine-side `evaluate_manage_exit`/`manage_trade` machinery is invoked

### Requirement: Sizing is delegated

Sizing MUST be strategy-owned, inside the author body: the strategy computes
its target quantity from instrument contract facts (multiplier, margin, lot,
price precision on the instrument) and the injected read-only account
projection (equity, available margin, open exposure). No engine- or
registry-owned sizing exists on the decision path
(`SIZING_STRATEGIES`/`size_for_signal`/`size_for_asset` are removed).

#### Scenario: Futures strategy sizes from margin and multiplier
GIVEN a futures strategy with a `FutureInstrument` carrying margin and multiplier
WHEN it sizes an entry
THEN the quantity derives from `instrument` margin/multiplier and `projection.available_margin` — not a generic capital fraction

#### Scenario: Equity strategy sizes from contract facts
GIVEN an equity strategy with a price-precision/lot instrument and the projection
WHEN it sizes
THEN the quantity respects the instrument's lot/price constraints and available capital

#### Scenario: Size zero means do not trade
GIVEN a sizing result of zero quantity
WHEN the author body finishes
THEN no order is produced for that instrument

### Requirement: Venue is the order seam

The strategy MUST build typed `Order`s through `instrument.make_order(side,
qty, ...)` and return them from `decide`; the engine MUST submit them to the
strategy's declared venue's execution client. Fill reporting from the venue
MUST feed the portfolio/account projection the strategy reads, and the
strategy MUST NOT talk to a broker client directly.

#### Scenario: Order built on the instrument and submitted to the venue
GIVEN the author body decides to act
WHEN `decide` returns
THEN the order is built via `instrument.make_order` and submitted by the engine to the declared venue's execution client

#### Scenario: Fill reporting feeds the projection
GIVEN a venue fill for a submitted order
WHEN the fill is reported
THEN the account projection the strategy reads reflects the fill (open exposure grows; filled quantity updates the position schema)

#### Scenario: No broker client in the strategy
GIVEN an author body
WHEN it inspects its imports and attributes
THEN it holds only the venue id/declaration and typed seams — no broker SDK object is reachable
