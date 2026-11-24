# Design

## Context

`PositionDict` (`quant/utils/positions.py`) is a `total=False` TypedDict of ~20
declared common fields; IC strike/direction data is forwarded onto the **root
dict** via `**strikes` in `build_ic_position` (`quant/utils/options.py:127`) and is
read back with `dict.get(..., 0)` defaults in the shared core monitor
(`core/monitor_utils.py:370`, `:699`) and in the strategy manage path. The live
layer already defines a sanctioned `LivePositionDict(PositionDict)` subclass plus
an `is_live_ic_position` TypeGuard (`python/protocols.py`), but the guard is wired
only into the close path (`ic_executor.py:1604`) — not into crash recovery, where
`trader.py:368` does an **unguarded `cast(list[LivePositionDict], positions)`**
over reloaded JSON. See proposal.md — Why for the motivation.

Key verified facts driving this design: `save_state`/`load_state` (`python/utils.py`)
are opaque `json.dump`/`json.load` — extra keys already survive the round-trip, no
stripping today. The parity invariant (AGENTS.md §2.1) is "one identical dict through
`manage_trade` backtest = live." Precedent from the archived boundary change: moves
land with **no behavior change to the decision pipeline**.

## Goals / Non-Goals

**Goals:**
- Declare the IC strike bundle once as a **shared, typed envelope** reachable by
  the strategy, both executors, and the monitor — ending the untyped `**extra`
  grab-bag.
- Guarantee a typed view of cross-cutting fields at every record boundary, so
  shared reads never fall back to `dict.get(..., 0)`.
- Validate/reject partial records at crash recovery instead of casting them in.
- Replace negative key-presence tests with round-trip and boundary invariants.

**Non-Goals:**
- **No runtime renamespacing.** IC strikes stay on the root record keys
  (`K_LP`/`K_SP`/`K_SC`/`K_LC`/`IC_Direction`) — do not nest under a `meta:` slot
  this change. Rationale in Decision 1.
- **No `Generic`/type-parameter ceremony** on `PositionDict` now (Decision 1).
- **No Pattern 1 side-table** — recorded as future evolution, not implemented
  (Decision 3).
- **No engine path, sizing, order-flow, or trade-record behavior change.** The
  runtime dicts and `trader_state.json` shape are unchanged; no data migration.

## Decisions

### 1. Flat runtime keys kept; a shared `ICMeta` envelope in `quant/utils/options.py` is the type + boundary parse

Define `ICMeta` (a TypedDict declaring `K_LP`, `K_SP`, `K_SC`, `K_LC`,
`IC_Direction`) in `quant/utils/options.py`, and a lift function/TypeGuard that
turns an untyped dict into a typed IC position by **validating** (not casting) the
required keys. Strikes remain flat on the record so `ic_executor`,
`monitor_utils`, the strategy, and `trader_state.json` keep their current read
shape — but every cross-cutting read now goes through the validated typed view
instead of `dict.get(..., 0)`.

*Why this over a true nested `meta:` slot (the "strict" Pattern 2):* nesting
changes the runtime shape of every IC position and the persisted `trader_state.json`,
ripples across `strategy.py`, `monitor_utils.py`, both IC executors, and every IC
test, and contradicts the "no behavior change" precedent. The namespace-collision /
schema-strip risks that nesting fixes are **theoretical here** — verified: serializers
pass the whole dict through verbatim and builders forward extras untouched. The
demonstrated holes (recovery unguarded cast, silent-zero reads, negative tests) are
all addressable without renamespacing.

*Why this over `PositionDict[MetaT]` (Generic):* the type parameter adds ceremony
with no runtime value while a single shared `ICMeta` + validation gives the same
safety. Adopt the generic only if a second strategy family with a genuinely
different envelope appears.

### 2. Boundary parse is the single lift; recovery goes through it, not `cast`

Reuse and broaden `is_live_ic_position` (exists in `python/protocols.py`) into the
entry validation, and route `trader.py._load_local_state` through it (or a strict
`parse_live_position`) so partial `trader_state.json` records are rejected loudly,
not `cast` into `LivePositionDict`. Same lift at any place an untyped dict enters
the engine/executors. The current `cast("list[LivePositionDict]", positions)` at
`trader.py:368` is replaced.

### 3. Pattern 1 (side-table) is recorded as future, gated on per-leg reconciliation

Document NautilusTrader-style `Position` accounting + strategy-owned state map as
the long-term target, triggered by real per-leg broker position reconciliation
(1 IC → 4 option positions) — the same tripwire discipline the archived change used
for its parked Part C. Not part of this change's implementation; noted so the
envelope leaves a clean seam (the typed `ICMeta` view is what a side-table would
read from).

## Risks / Trade-offs

- [Recovery policy ambiguity: reject one bad record vs halt the trader] → Escalate
  to an explicit policy (Decision under Open Questions); default is to fail closed
  on the malformed record (drop it + loud log) rather than trade it.
- [`is_live_ic_position` broadened everywhere becomes a chokepoint] → The guard is a
  small, pure key/type check; runs only at async boundaries (recovery, entry), not
  per-bar. No hot-path cost.
- [Tests asserting recovered extra keys assume opaque save/load] → Verified against
  `python/utils.py`; the round-trip invariant is real, not aspirational.
- [Two-repo split reds] → One `quant/` commit (envelope + monitor + tests), then one
  outer commit (submodule bump + `python/` repoint + guard); neither repo green-able
  alone mid-sequence, matching the archived change's sequencing.

## Migration Plan

1. **Quant commit:** add `ICMeta` + boundary lift to `quant/utils/options.py`;
   convert `core/monitor_utils.py` strike reads to the typed view; replace the
   negative-annotation tests in `quant/tests/test_engine.py` with round-trip /
   boundary invariants; update strategy manage-path reads. Gate: `cd quant &&
   uv run test` + `ruff check .`.
2. **Outer commit:** bump the submodule, repoint `python/` imports, wire
   `is_live_ic_position` into `trader.py` recovery and confirm the `ic_executor`
   guard, add/refresh live boundary tests. Gate: `cd python && uv run test` +
   `ruff check .`.
3. **Rollback:** revert the outer commit (or both). No data migration, no schema
   change — `trader_state.json` and config are untouched, so rollback is trivially
   safe.

## Open Questions

- **Recovery action on a malformed record:** should a partial `trader_state.json`
  IC position be (a) dropped & logged loudly while other strategies continue, or
  (b) treated as a hard startup error that halts the trader? Defers safely — it is
  an action-on-rejection detail, not a change to the specs/approach/tasks. Default
  is (a).