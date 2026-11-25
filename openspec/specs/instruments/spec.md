# Instruments Specification

## Purpose

The instrument primitive: the smallest typed unit both environments reason over. A single correctly-seeded typed hierarchy — base + `instrument_class` discriminator + `underlying` reference — keeps equity, futures, options, and options-on-futures representable by construction, so asset-class growth is a pure addition instead of an abstraction re-architecture (design D14). v1 ships equity behavior; options/futures are representable but their accounting/execution semantics are deferred.

## Requirements

### Requirement: Typed instrument seed

`quant.instruments` MUST expose an `Instrument` base (symbol, venue, asset class, contract specs, tick/lot/settlement metadata) and a typed hierarchy seeded with an `instrument_class` discriminator and an `underlying` reference to another Instrument, so any instrument can reference another (e.g. an option's underlying). Strategy/protocol surfaces MUST take/return typed instruments where they today take strings, keeping ticker-string conveniences as sugar.

#### Scenario: Equity instrument constructed
GIVEN a symbol, venue, and asset class
WHEN an `EquityInstrument` is constructed
THEN it carries identity, venue, and contract metadata and is usable at the strategy and venue seams

#### Scenario: Option references its future underlying
GIVEN an `OptionInstrument` whose `underlying` is a `FutureInstrument`
WHEN composed
THEN the option resolves its underlying instrument object (representing an option-on-futures) with no extra casing

### Requirement: v1 asset coverage and deferred semantics

v1 MUST ship `EquityInstrument` with full live behavior. `OptionInstrument` and `FutureInstrument` MUST be definable as thin, registrable types so signatures compose (including options-on-futures), but their accounting and execution semantics MUST be deferred to documented concept docs rather than half-supported in v1.

#### Scenario: Equity-only runtime behavior
GIVEN the v1 base install
WHEN a strategy trades equity
THEN fills, positions, and portfolio accounting are fully supported

#### Scenario: Non-equity representable, not half-accounted
GIVEN an `OptionInstrument` present in v1
WHEN portfolio accounting touches it
THEN behavior is explicitly deferred (documented) rather than silently approximated

### Requirement: Instrument conformance

Instruments MUST expose the fields the venue/data/portfolio seams depend on. A conformance check MUST assert an instrument's invariants (identity uniqueness at a venue, valid contract specs) when it is introduced by an external definition (e.g. an agent or a custom data source).

#### Scenario: Bad instrument rejected at boundary
GIVEN an instrument definition missing required contract fields
WHEN it is used at a seam
THEN the violation is caught by the instrument conformance check rather than surfacing mid-run
