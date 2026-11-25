# Strategy Authoring Specification

## Purpose

The declarative strategy surface: a strategy is declare + react. It declares
typed instruments, typed data subscriptions, and a venue, then runs an
author-owned body over typed data events and the injected account projection,
sizing itself from instrument contract facts and submitting typed `Order`s to
the venue.

## ADDED Requirements

### Requirement: Declarative strategy surface

A strategy MUST declare typed instruments, typed data subscriptions
`(instrument, datatype, cadence)`, and a venue, and implement an author body
that runs over the typed data events of its declared subscriptions. A
bar-driven strategy MUST be constructible with resolvable declarations.

#### Scenario: Bar-driven strategy constructs and resolves
GIVEN a strategy declaring an equity instrument, a bars subscription, and a venue
WHEN it is constructed
THEN the declarations resolve against the data port and venue, and the author body is callable over bars events

#### Scenario: Decision body runs over typed data
GIVEN a bars event delivered to the body
WHEN the body evaluates
THEN it reads typed bars (adjusted only) plus the injected projection, and returns decisions — no signal-string protocol is involved

### Requirement: Sizing is delegated

Sizing MUST be strategy-owned: the strategy computes its target quantity from
instrument contract facts (multiplier, margin, lot, price precision on the
instrument) and the injected read-only account projection. No engine- or
registry-owned sizing exists on the decision path.

#### Scenario: Futures strategy sizes from margin and multiplier
GIVEN a futures strategy with a `FutureInstrument` carrying margin and multiplier
WHEN it sizes an entry
THEN the quantity derives from margin/multiplier and account state — not a generic capital fraction

#### Scenario: Equity strategy sizes from contract facts
GIVEN an equity strategy with a price-precision/lot instrument and the projection
WHEN it sizes
THEN the quantity respects the instrument's lot/price constraints and available capital

### Requirement: Venue is the order seam

The strategy MUST build typed `Order`s through `instrument.make_order` and
submit them to its declared venue. Fill reporting from the venue MUST feed
the portfolio/account projection the strategy reads.

#### Scenario: Order built on the instrument and submitted to the venue
GIVEN the author body decides to act
WHEN it constructs an order
THEN the order is built via `instrument.make_order` and submitted to the declared venue's execution client

#### Scenario: Fill reporting feeds the projection
GIVEN a venue fill for a submitted order
WHEN the fill is reported
THEN the portfolio projection the strategy reads reflects the fill (unfilled risk ends up in open exposure)
