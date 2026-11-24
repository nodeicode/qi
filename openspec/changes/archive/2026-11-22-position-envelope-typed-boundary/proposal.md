# Proposal

## Why

The shared `PositionDict` contract is a flat, `total=False` dict: strategy/asset-specific
fields (IC strikes, direction, fills) ride the **root** namespace via `**extra`, so any
code that trusted the declared schema would silently strip them, and nothing guarantees
their presence where they are required. In practice this produced a documented rule that is
already violated (`python/protocols.py` subclasses `PositionDict` despite the "never
subclass" rule in `positions.py`), an **unguarded `cast` at live crash recovery**
(`trader.py` loads `trader_state.json` straight into `list[LivePositionDict]`), and
**silent-zero reads** in cross-cutting code (`core/monitor_utils.py` and the strategy's
manage path read `position.get("K_SP", 0)`, silently disabling proximity-exit logic when a
field is missing). The layered contract was never made explicit, so parity is only enforced
by negative tests that assert banned key names. We want the type contract to make strategy
state **namespaced and validated at the boundary** without breaking the one-dict parity
invariant, and to be **robust for the long term** without paying the cost of a full
NautilusTrader-style side-table before per-leg broker reconciliation exists.

## What Changes

- **Reconcile the subclass rule with reality.** The "never define a `*PositionDict`
  subclass" rule in `quant/utils/positions.py` contradicts the live layer's sanctioned
  `LivePositionDict(PositionDict)` subclass (explicitly allowed by the archived boundary
  design's decision #1). Make the live extension an explicit, single, sanctioned view.
- **Introduce a shared-typed envelope for strategy/options state.** Parameterize the
  position record with a type variable and define the IC strike/direction bundle as a
  **shared** typed structure in `quant/utils/options.py` (`ICMeta`), notified to the
  strategy, backtest executor, live executor, **and** the monitor/sidecar (which must read
  strikes for distance-to-strike reconciliation — this is intentionally **not** an opaque
  meta payload).
- **Parse/validate once at every record boundary.** Replace the unguarded `cast` at
  `trader.py` recovery with a strict boundary parse (reject/guard partial
  `trader_state.json` loudly instead of degrading), and apply the existing
  `is_live_ic_position` TypeGuard wherever an untyped dict enters the engine.
- **Remove silent-zero degradations.** Cross-cutting strike reads must not flip live
  behavior off because a key is missing; the guarantee is derived from the boundary parse.
- **Replace negative key-list tests with round-trip and boundary invariants.** Drop
  `"K_LP" not in PositionDict.__annotations__`-style assertions for opacity/round-trip and
  boundary-validation properties.
- **Record Pattern 1 (side-table) as the documented future evolution**, gated on real
  per-leg broker position reconciliation — not implemented now.

## Capabilities

### New Capabilities

None — the change is scoped entirely to the existing boundary/position-contract capability.

### Modified Capabilities

- `quant-boundary`: requirements change so that (a) the IC position envelope is a shared,
  typed structure in `quant.utils.options`, not an untyped `**extra` grab-bag on the root
  dict; (b) `python/protocols.LivePositionDict` is the one sanctioned live extension with a
  boundary TypeGuard; (c) every record entry point (including `trader.py` crash recovery)
  validates at the boundary rather than `cast`ing untyped data; and (d) the testing gate
  enforces round-trip opacity and boundary-validation invariants instead of negative
  key-presence assertions.

## Impact

- **`quant/` submodule:** `quant/utils/options.py` (define shared `ICMeta`), the
  `PositionDict` builder surface (`quant/utils/positions.py`),
  `quant/core/monitor_utils.py` (silent-zero reads), `quant/core/engine.py` /
  `quant/utils/options.py` `build_ic_position` signature, plus `quant/tests/`.
- **`python/`:** `protocols.py` (`LivePositionDict`, `is_live_ic_position`), `trader.py`
  recovery path, `executors/ic_executor.py` (guard already present, verify),
  `tests/unit/executors/test_ic_executor.py`.
- **Two-repo sequencing:** one `quant/` commit (envelope + monitor + tests), then one outer
  commit bumping the submodule pointer and updating `python/` — neither repo red alone.
- **Contract note:** the runtime dict keys `K_LP/K_SP/K_SC/K_LC` remain on the record
  (parity requires the data travel); what changes is *who declares and validates their
  shape*. `trader_state.json` shape is unchanged; no data migration.