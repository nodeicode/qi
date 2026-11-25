## Purpose

Instruments become the routing and sizing spine of the pipeline: contract
facts live on the typed instrument (single source — folded in from
`quant/core/_utils.py` `ContractSpec`/`ProductSpecRegistry`), instruments
construct typed `Order`s via `make_order`, and routing resolves from the
instrument instead of a signal-string registry. Options resolve their
`underlying` with no asset-class inference.

## MODIFIED Requirements

### Requirement: Typed instrument seed

`quant.instruments` MUST expose an `Instrument` base (symbol, venue, asset
class, contract specs, tick/lot/settlement metadata) with an
`instrument_class` discriminator and an `underlying` reference. Contract
facts MUST live on the typed instrument as the single source, absorbing
`ContractSpec`'s data+math (`round_price`, `pnl`). Each instrument MUST
expose `make_order(side, qty, ...) -> Order` producing a typed `Order`
(`quant/order.py`) bound to the instrument and its venue.

#### Scenario: Equity instrument constructed
GIVEN an `EquityInstrument` constructed with symbol, venue, and contract metadata
WHEN a strategy calls `make_order("BUY", qty)`
THEN a typed `Order` is produced carrying side, quantity, the instrument, its venue, price context, and timestamps, and the instrument is usable at the strategy and venue seams

#### Scenario: Option references its future underlying
GIVEN an `OptionInstrument` whose `underlying` is a `FutureInstrument`
WHEN composed
THEN the option resolves its underlying instrument object (an option-on-futures) with no asset-class inference, and `make_order` builds typed orders against either instrument

#### Scenario: Futures contract facts come from the instrument alone
GIVEN a `FutureInstrument` with multiplier, tick, and margin facts folded in from the legacy `ContractSpec`
WHEN the strategy sizes against it
THEN it reads margin/multiplier from the instrument — there is no separate contract-facts registry

#### Scenario: Contract math single-sourced on the instrument
GIVEN the legacy `ContractSpec.pnl` / `round_price` math
WHEN folded into the instrument
THEN fills and P&L compute through the instrument's own methods, and `ContractSpec`/`ProductSpecRegistry` are deleted from the codebase

### Requirement: v1 asset coverage and deferred semantics

v1 MUST ship `EquityInstrument` with full live behavior, and
`FutureInstrument` / `OptionInstrument` MUST be definable typed types whose
contract facts and `make_order` compose (including options-on-futures).
Accounting/execution semantics beyond what the venue seam and typed orders
cover MUST remain explicitly deferred rather than silently approximated.

#### Scenario: Equity-only runtime behavior
GIVEN the v1 base install
WHEN a strategy trades equity
THEN fills, positions, and portfolio accounting are fully supported through the venue seam and typed orders

#### Scenario: Option on a future resolves and orders against both
GIVEN an `OptionInstrument` whose `underlying` is a `FutureInstrument`
WHEN a strategy sizes/orders against the option or its underlying
THEN the underlying object resolves by reference with no asset-class inference, and typed orders build against either instrument

#### Scenario: Non-equity representable, not half-accounted
GIVEN a `FutureInstrument`/`OptionInstrument` present in v1
WHEN portfolio accounting touches semantics outside the shipped venues
THEN behavior is explicitly deferred (documented) rather than silently approximated

### Requirement: Instrument conformance

Instruments MUST expose the fields the venue/data/portfolio seams depend on,
including the order-construction surface. A conformance check MUST assert an
instrument's invariants (identity uniqueness at a venue, valid contract
facts, `make_order` present and producing a typed `Order`) when it is
introduced by an external definition (e.g. an agent or a custom data
source).

#### Scenario: Equity instrument passes instrument conformance
GIVEN an `EquityInstrument` with complete contract facts and `make_order`
WHEN `InstrumentConformance` is run
THEN it passes, and `make_order` yields a schema-valid `Order`

#### Scenario: Bad instrument rejected at boundary
GIVEN an instrument definition missing the order-construction surface or required contract fields
WHEN it is used at a seam
THEN the violation is caught by the instrument conformance check rather than surfacing mid-run
