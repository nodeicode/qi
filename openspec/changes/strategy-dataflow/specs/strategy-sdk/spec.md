## Purpose

The strategy SDK reshapes, breaking by contract: the 3-method
`StrategyProtocol` and its string-signal contract are replaced by the
declarative authoring surface; `TradingEngine.__init__` validation, the
config fields, the registry/loader, and the live loader rewire to
declarations; unknown venues/strategies are rejected at construction; the
legacy 3-method protocol and the indicator-injection contract have no
compatibility shim.

## MODIFIED Requirements

### Requirement: Strategy execution protocol

`StrategyProtocol` (`quant/core/protocols.py`) MUST be replaced by the
declarative surface: `config: BaseStrategyConfig` whose declarations name
the instruments, subscriptions, and venue, plus an author body over typed
data events + the read-only account projection, returning typed `Order`s /
exit intents. The 3 method contract (`calculate_indicators` /
`generate_signals` with string `Signal`/`Confidence`/`Reason` columns /
`manage_trade`) MUST be removed. `TradingEngine.__init__` MUST stop checking
the three methods and `INDICATOR_COLUMNS`/ticker-kwarg inspection and
instead validate the declarative protocol, resolvable declarations, and a
callable author body — a strategy missing any is rejected at construction
with a readable error naming what is missing.

#### Scenario: Missing method rejected at construction
GIVEN a strategy class that omits required declarations or the author body
WHEN a `TradingEngine` is constructed with it
THEN construction raises a readable error naming what is missing

#### Scenario: Valid strategy accepted
GIVEN a strategy implementing the declarative surface with resolvable declarations
WHEN a `TradingEngine` is constructed with it
THEN construction succeeds and the strategy is callable through the shared reshaped pipeline

#### Scenario: Declarative strategy satisfies the protocol
GIVEN a strategy implementing the declarative surface
WHEN the SDK checks it against the protocol
THEN it satisfies construction-time checks with all declarations resolvable

#### Scenario: Legacy 3-method strategy clearly rejected
GIVEN a strategy exposing only `calculate_indicators`/`generate_signals`/`manage_trade`
WHEN it is loaded or constructed
THEN it is clearly rejected as legacy (no silent migration, no partial behavior)

### Requirement: Config validation

`BaseStrategyConfig` MUST drop the engine-driven fields
(`INTERVAL`, `MANAGE_INTERVAL`, `CAPITAL_PER_TRADE`, `TICKERS`,
`MIN_OPTION_DTE`, `MIN_SPREAD_WIDTH`, option-strike knobs) and add
declarative ones: `instruments: list[Instrument]`,
`subscriptions: list[Subscription]`, `venue: str`. The framework
risk/execution fields (`INITIAL_CAPITAL`, `MAX_CONCURRENT_POSITIONS`,
`STOP_LOSS_PERCENT`, `TAKE_PROFIT_PERCENT`, `USE_TRAIL_LOSS`,
`MAX_HOLDING_MINUTES`, `MIN_SIGNAL_CONFIDENCE`, `BASE_SLIPPAGE_FACTOR`,
`PRE_LOAD_DAYS`, `DATA_DELAY`) stay available to the author body.
Validation MUST reject an unknown venue/strategy **at construction** and
fail an invalid config or unresolvable declaration naming the offending
field — never silently at decision time.

#### Scenario: Wrong config type rejected
GIVEN a strategy whose `config` is not a `BaseStrategyConfig` instance
WHEN a `TradingEngine` is constructed with it
THEN construction raises a `TypeError` explaining the config contract

#### Scenario: Bad field value fails construction
GIVEN a config subclass that validates a field (e.g. cadence in an allowed set)
WHEN a strategy is constructed with an invalid value
THEN construction raises a validation error naming the field and the accepted values

#### Scenario: Unknown venue rejected at construction
GIVEN a strategy declaring a venue id that is not registered
WHEN the strategy/config is constructed
THEN construction fails with a clear message

#### Scenario: Declarative reference config resolves
GIVEN the canonical reference config for `example_equity`:
`instruments=[EquityInstrument("SPY", contract=EquityContract(price_precision=2, lot=1))]`,
`subscriptions=[Subscription("SPY", Datatype.BARS, "1h")]`,
`venue="backtest"`, plus framework fields `initial_capital=100_000.0`,
`max_concurrent_positions=1`, `pre_load_days=60`
WHEN the strategy/config is constructed
THEN the instruments and subscription resolve against the data port and `quant.venue`, and construction succeeds

#### Scenario: Unknown strategy rejected at construction
GIVEN a loader request for an unregistered strategy
WHEN it is loaded
THEN construction fails loudly

### Requirement: Strategy registry and dynamic loading

`quant/strategies/__init__.py` MUST keep its shape — `STRATEGY_REGISTRY`,
`get_strategy`, `_entry_point_strategy`, `_available_strategies`, the
`quant.strategies` entry-point group — with `STRATEGY_REGISTRY` resolved to
the rewritten declarative `example_equity` and loading validating
declarations. `quant/live/strategy_loader.py` MUST keep its per-(strategy,
client) loop, constructing through the new validation and assigning
`name`/`env`/`client_name`/venue. Unknown names raise a `ValueError` listing
available names; a live config naming an unknown strategy is rejected at
load time without stopping other strategies.

#### Scenario: Built-in strategy loads by name
GIVEN the SDK is installed with the rewritten `example_equity`
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

The indicator-injection contract MUST be deleted: `inject_indicators`,
`DEFAULT_INDICATOR_COLUMNS`/`_INDICATOR_COLUMNS`, and
`signal-cache`-column tracking (`_signal_cache_cols`) in
`quant/core/_utils.py`, plus `TradingEngine._indicator_columns` and the
`:meth:`cache_signals``-time column bookkeeping in `quant/core/engine.py`.
Indicators are the author body's own computation on the typed data it
declares, not an engine-injected column contract keyed by ticker.
