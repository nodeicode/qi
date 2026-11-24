## Purpose

The venue primitive: one swappable unit that is simultaneously the "broker" and the "market session" a strategy trades through — identity, session/calendar state, market-data source, and execution client. Lean and DX-friendly, inspired by the venue concept in Nautilus Trader, without its framework weight.

## ADDED Requirements

### Requirement: Venue as a cohesive unit

A venue MUST expose: an identity (`id`), current session/calendar state (open/close/forced-close for the trading date), a market-data source (implementing `DataSourceProtocol`), and an execution client (implementing the execution interface). A strategy/engine MUST address all broker-and-session concerns through its venue object, never scraping them from scattered modules.

#### Scenario: One object serves session and broker roles
GIVEN a strategy bound to a venue
WHEN it needs open/close times, historical bars, or order submission
THEN it obtains them from the same venue object via its session, data, and execution surfaces

#### Scenario: Venue type swappable
GIVEN a strategy configured with `AlpacaVenue` (live) and with `BacktestVenue` (simulation)
WHEN it runs in each environment
THEN only the venue differs; strategy and decision code are unchanged

### Requirement: Session/calendar state

A venue MUST resolve each trading date's open, close, and forced-close window from a calendar (live: broker calendar API per date, reflecting DST, holidays, early closes; backtest: the simulation timeline). It MUST recompute on date rollover and MUST fall back to documented defaults (logged) if the calendar is unavailable.

#### Scenario: Holiday not open
GIVEN a calendar with no entry for a given date
WHEN session state is queried for that date
THEN it reports the venue closed

#### Scenario: Rollover recomputes
GIVEN venue state resolved for yesterday
WHEN queried after date rollover
THEN it refreshes open/close from the calendar for the new date

#### Scenario: Calendar unavailable falls back
GIVEN a calendar fetch failure
WHEN venue state is queried
THEN it uses documented default hours and logs the fallback rather than raising

### Requirement: Canonical timestamp convention

The decision boundary MUST use one canonical timestamp form — UTC-naive instants. Timezone-aware values MUST be confined to venue/data adapters, and anything violating the convention MUST be refused at the boundary.

#### Scenario: Naive UTC across the boundary
GIVEN a live tick timestamp
WHEN it crosses into the decision path
THEN it is UTC-naive; timezone-aware values exist only inside venue adapter code

### Requirement: Deterministic backtest venue

The backtest venue MUST answer session state from the simulation timeline, not the host wall clock: the same simulated `now` always yields the same open/closed answer for a date, independent of when or where the run executes.

#### Scenario: Simulation decides openness
GIVEN a backtest bar timestamp on a holiday
WHEN the venue's session state is queried with that timestamp
THEN it reports closed exactly as the calendar says for that date, independent of the host wall clock

### Requirement: Venue registration and discovery

Venues MUST be registered by name (built-in `alpaca` and `backtest` defaults; entry-point registry for extras) and resolved from strategy/runtime config. An unknown venue name MUST fail configuration loudly with available names listed.

#### Scenario: Default venues by name
GIVEN config naming `alpaca` or `backtest`
WHEN the runtime resolves the venue
THEN the corresponding default venue is constructed with the configured credentials/parameters

#### Scenario: Unknown venue name rejected
GIVEN config naming an unregistered venue
WHEN the runtime resolves it
THEN configuration fails with a message listing registered venue names
