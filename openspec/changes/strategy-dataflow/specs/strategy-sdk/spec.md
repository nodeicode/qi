## Purpose

The strategy SDK reshapes, breaking by contract: the 3-method
`StrategyProtocol` and its string-signal contract are replaced by the
declarative authoring surface; the registry/loader and config validation
rewire to declarations; unknown venues/strategies are rejected at
construction; the legacy 3-method protocol has no compatibility shim.

## MODIFIED Requirements

### Requirement: Strategy execution protocol

`StrategyProtocol` MUST be replaced by the declarative surface: declared
instruments, subscriptions, and venue plus an author body over typed data
events and the read-only account projection, returning typed `Order`s /
exit intents. The 3-method contract (string `Signal`/`Confidence`/`Reason`
columns) MUST be removed with no compatibility shim; a strategy missing
required declarations or the author body is rejected at construction.

#### Scenario: Missing method rejected at construction
GIVEN a strategy class that omits required declarations or the author body
WHEN an engine is constructed with it
THEN construction raises a readable error naming what is missing

#### Scenario: Valid strategy accepted
GIVEN a strategy implementing the declarative surface with resolvable declarations
WHEN an engine is constructed with it
THEN construction succeeds and the strategy is callable through the shared reshaped pipeline

#### Scenario: Declarative strategy satisfies the protocol
GIVEN a strategy implementing the declarative surface
WHEN the SDK checks it against the protocol
THEN it satisfies construction-time checks with all declarations resolvable

#### Scenario: Legacy 3-method strategy clearly rejected
GIVEN a strategy exposing only `calculate_indicators`/`generate_signals`/`manage_trade`
WHEN it is loaded
THEN it is clearly rejected as legacy (no silent migration, no partial behavior)

### Requirement: Config validation

Strategy config MUST subclass `BaseStrategyConfig`, whose fields cover the
declared instruments, subscriptions, and venue. Validation MUST reject an
unknown venue and an unknown strategy **at construction**, and an invalid
config (wrong type, missing or unresolvable declaration) MUST fail at
strategy/engine construction with a message identifying the offending field,
never silently at decision time.

#### Scenario: Wrong config type rejected
GIVEN a strategy whose `config` is not a `BaseStrategyConfig` instance
WHEN an engine is constructed with it
THEN construction raises a `TypeError` explaining the config contract

#### Scenario: Bad field value fails construction
GIVEN a config subclass that validates a field (e.g. cadence in an allowed set)
WHEN a strategy is constructed with an invalid value
THEN construction raises a validation error naming the field and the accepted values

#### Scenario: Unknown venue rejected at construction
GIVEN a strategy declaring a venue id that is not registered
WHEN the strategy/config is constructed
THEN construction fails with a clear message

#### Scenario: Unknown strategy rejected at construction
GIVEN a loader request for an unregistered strategy
WHEN it is loaded
THEN construction fails loudly

### Requirement: Strategy registry and dynamic loading

Strategies MUST be loadable by name through `get_strategy(name)`, resolving
from a built-in registry (the rewritten `example_equity`) and from any
registered entry-point group; loading validates the declarations. Unknown
names MUST raise a `ValueError` listing available names. A live config that
names an unknown strategy MUST be rejected at load time without stopping
other strategies.

#### Scenario: Built-in strategy loads by name
GIVEN the SDK is installed with `example_equity`
WHEN `get_strategy("example_equity")` is called
THEN the declarative example strategy class is returned with resolvable declarations

#### Scenario: Unknown name errors
GIVEN no strategy named `nope` is registered
WHEN `get_strategy("nope")` is called
THEN a `ValueError` is raised naming the unknown strategy

#### Scenario: Strategy from an entry point loads
GIVEN another distribution registers a strategy under the SDK's strategy entry-point group
WHEN `get_strategy("<that-name>")` is called
THEN the entry-point strategy class is returned without SDK code changes

#### Scenario: Reference strategy loads through the new loader
GIVEN the rewritten `example_equity`
WHEN loaded through `quant.live.strategy_loader` / the backtest loader
THEN it constructs with resolvable declarations in both environments

## REMOVED Requirements

### Requirement: Indicator injection contract

The indicator-injection contract (`inject_indicators`, `_indicator_columns`,
signal-cache indicator binding in `quant/core/_utils.py` /
`quant/core/engine.py`) MUST be deleted: indicators are the author body's own
computation on the typed data it declares, not an engine-injected column
contract keyed by ticker.
