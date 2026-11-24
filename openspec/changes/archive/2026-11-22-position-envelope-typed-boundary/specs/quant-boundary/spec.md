# Spec Delta

## ADDED Requirements

### Requirement: Iron-condor position state is carried in a shared typed envelope

The IC strike/direction bundle (`K_LP`, `K_SP`, `K_SC`, `K_LC`, `IC_Direction`)
SHALL be declared once as a shared, typed structure in `quant/utils/options.py`
and accessed through that single declaration by the engine, the strategy, both
executors, and the monitor — never grown ad hoc as an untyped `**extra`
grab-bag on the root `PositionDict`. The payload is intentionally not opaque to
core, which reads the strikes for distance-to-strike reconciliation.

#### Scenario: Both sides of the boundary use one typed declaration
- **WHEN** a position is built or a manage decision reads IC strikes
- **THEN** the strike fields resolve from the shared typed envelope in
  `quant/utils/options.py`, and adding a new strategy-specific key requires no
  edit to `PositionDict` in `quant/utils/positions.py`

#### Scenario: The sidecar reconciles on strikes
- **WHEN** the correctness sidecar computes distance-to-strike for parity
- **THEN** it reads `K_SP`/`K_SC` through the shared typed envelope, with a
  guaranteed type for the returned value rather than a `dict.get(..., 0)`

### Requirement: Live position records are validated at every boundary

Untyped position records SHALL be validated by the boundary type-guard
(`is_live_ic_position` shape check) wherever they enter live logic, and crash
recovery (`python/trader.py` loading `trader_state.json`) MUST validate or
reject partial records loudly rather than casting them in as
`LivePositionDict`.

#### Scenario: Recovery rejects a partial record
- **WHEN** `trader_state.json` holds a live IC position missing a required IC
  or `OccSymbol` key
- **THEN** recovery does not cast it in as `LivePositionDict`; it is flagged
  and fails closed instead of being treated as a valid position

#### Scenario: Untyped dict is guarded before executor use
- **WHEN** an untyped position dict reaches a live executor
- **THEN** it passes `is_live_ic_position` before being treated as
  `LivePositionDict` and used for closing/management

### Requirement: Cross-cutting strike reads never silently disable logic

Shared code that reads the shared IC envelope MUST NOT degrade behavior
silently when a required field is absent. Reads SHALL be derived from an
already-validated boundary; an absent required field MUST fail loudly or be
explicitly handled, never defaulted to `0` such that proximity-exit logic
silently turns off.

#### Scenario: Missing strike no longer flips exit logic off
- **WHEN** a manage position lacks a required strike field
- **THEN** the proximity-exit path raises or explicitly guards the missing
  field instead of substituting `0` and silently disabling the band

### Requirement: Position contract tests enforce round-trip and boundary invariants

The position-contract test gate SHALL assert round-trip opacity (extra keys
survive serialize/reload) and boundary validation (partial records are
rejected by the guard). It MUST NOT assert negative key-presence on the shared
type (e.g. `"K_LP" not in PositionDict.__annotations__`).

#### Scenario: Extra keys survive a full save/reload round-trip
- **WHEN** a live IC position with strike and `OrderIds` keys is serialized to
  JSON and reloaded
- **THEN** all extra keys survive intact and the reloaded record still passes
  `is_live_ic_position`

#### Scenario: Partial record rejected by the guard
- **WHEN** a fixture position is missing a required IC key
- **THEN** `is_live_ic_position` returns `False` and the fixture is rejected
  before any engine/manage path consumes it