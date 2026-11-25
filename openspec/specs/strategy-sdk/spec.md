# Strategy SDK Specification

## Purpose

How a developer authors, configures, and registers a strategy so it runs identical in backtest and live. One-file extension: a strategy is a class implementing a protocol, plus a config subclass, plus one registry line.

## Requirements

### Requirement: Strategy execution protocol

Strategies MUST implement `StrategyProtocol`: a `config` attribute of type `BaseStrategyConfig` and three methods — `calculate_indicators(df, **kw) -> df`, `generate_signals(df) -> df`, and `manage_trade(position, df) -> int`. The engine MUST reject a strategy missing any of these at construction time with a readable `TypeError`.

#### Scenario: Missing method rejected at construction
GIVEN a strategy class that omits `manage_trade`
WHEN an engine is constructed with it
THEN construction raises a `TypeError` naming the missing method

#### Scenario: Valid strategy accepted
GIVEN a strategy implementing all three methods with a `BaseStrategyConfig` config
WHEN an engine is constructed with it
THEN construction succeeds and the strategy is callable through the shared pipeline

### Requirement: Config validation

Strategy config MUST subclass `BaseStrategyConfig`, whose fields drive engine behavior (intervals, tickers, data delay). An invalid config (wrong type, missing required strategy-specific field) MUST fail at strategy/engine construction with a message identifying the offending field, never silently at decision time.

#### Scenario: Wrong config type rejected
GIVEN a strategy whose `config` is not a `BaseStrategyConfig` instance
WHEN an engine is constructed with it
THEN construction raises a `TypeError` explaining the config contract

#### Scenario: Bad field value fails construction
GIVEN a config subclass that validates a field (e.g. interval in an allowed set)
WHEN a strategy is constructed with an invalid value
THEN construction raises a validation error naming the field and the accepted values

### Requirement: Strategy registry and dynamic loading

Strategies MUST be loadable by name through `get_strategy(name)`, resolving from a built-in registry (the public `example_equity`) and from any registered entry-point group. Unknown names MUST raise a `ValueError` listing available names. A live config that names an unknown strategy MUST be rejected at load time without stopping other strategies.

#### Scenario: Built-in strategy loads by name
GIVEN the SDK is installed with `example_equity`
WHEN `get_strategy("example_equity")` is called
THEN the example strategy class is returned

#### Scenario: Unknown name errors
GIVEN no strategy named `nope` is registered
WHEN `get_strategy("nope")` is called
THEN a `ValueError` is raised naming the unknown strategy

#### Scenario: Strategy from an entry point loads
GIVEN another distribution registers a strategy under the SDK's strategy entry-point group
WHEN `get_strategy("<that-name>")` is called
THEN the entry-point strategy class is returned without SDK code changes

### Requirement: Indicator injection contract

The engine MUST inject the strategy's indicator columns into manage/decision frames and cache signal output per ticker. Strategies may accept an optional `ticker` kwarg in `calculate_indicators` for cross-asset logic; the engine MUST call it with the ticker only when the signature supports it.

#### Scenario: Indicator columns present on manage frame
GIVEN a strategy declares `INDICATOR_COLUMNS`
WHEN `manage_trade` is invoked
THEN the passed frame contains those columns computed from the latest bars

#### Scenario: Ticker kwarg respected
GIVEN a strategy whose `calculate_indicators` accepts `ticker`
WHEN the engine runs signals
THEN it is called with the current ticker
