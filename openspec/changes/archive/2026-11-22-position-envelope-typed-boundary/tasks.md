# Tasks

**Note on recovery policy (Open Question in design.md):** the tasks below default to
"drop a malformed recovered IC position + loud log, fail closed on that record, and
continue other strategies." If a hard trader-halt on any malformed record is desired,
adjust Task 3.2's acceptance accordingly — it does not change specs or the other tasks.

## Group 1 — Shared `ICMeta` envelope in quant/ (quant commit part 1)

- [x] 1.1 Define the shared `ICMeta` TypedDict (declaring `K_LP`, `K_SP`, `K_SC`,
  `K_LC`, `IC_Direction`) in `quant/utils/options.py`, re-exported from
  `quant/core/protocols.py`. Verify: `cd quant && ruff check .` and a unit test
  asserting the declared keys match what `build_ic_position` forwards.
- [x] 1.2 Add a boundary lift / TypeGuard in `quant/utils/options.py` that converts an
  untyped `PositionDict` into a typed IC position by **validating** required keys
  (rejecting, not `cast`, on missing fields). Verify: unit test — a dict missing
  `K_LP` returns `False`/raises; a complete one lifts with typed strike access.
- [x] 1.3 Convert `quant/core/monitor_utils.py` strike reads (lines ~370, ~699 —
  re-locate by symbol) from `dict.get("K_SP", 0)` / `.get(...)` to the validated
  typed IC view; a missing required strike raises/guards instead of silently
  returning 0. Verify: ruff clean + unit test that a partial manage position no
  longer computes with default-0 strikes.
- [x] 1.4 Convert the strategy manage-path strike reads
  (`quant/strategies/example_equity/strategy.py` lines ~788-859, the
  `float(position.get("K_*", 0))` block) to the typed IC view, keeping the
  `strikes_valid` guard but deriving it from validated fields. Verify: `cd quant &&
  uv run test -k example_equity` green; manage proximity math unchanged for valid
  positions.
- [x] 1.5 Update the `quant/utils/positions.py` docstring: reconcile the "never
  subclass `*PositionDict`" rule with the sanctioned `LivePositionDict` live
  extension, and document `ICMeta` as the shared envelope home. Verify: doc reads
  consistently; no doc-vs-code contradiction remains.

## Group 2 — quant tests & invariants (quant commit part 2)

- [x] 2.1 Replace the negative key-presence assertions in `quant/tests/test_engine.py`
  (lines ~686, ~714-715, asserting `"K_LP" not in PositionDict.__annotations__`)
  with round-trip opacity and boundary-validation invariants (extra keys survive a
  serialize/reload; a partial record is rejected by the guard). Verify: updated tests
  pass; the old negative assertions are gone.
- [x] 2.2 Retire the private `_ICPositionFields` TypedDict + `cast` in
  `quant/tests/test_engine.py` (line ~1358-1384) in favor of the shared `ICMeta`;
  IC test fixtures use the typed envelope. Verify: `cd quant && uv run test` green,
  no standalone private position TypedDict remains in tests.

## Group 3 — outer repo: recovery guard + repoint (outer commit)

- [x] 3.1 Bump the `quant` submodule pointer and repoint `python/` imports to any moved
  symbols (`quant.utils.options` ICMeta/lift), keeping the AST boundary bans green.
  Verify: `cd python && uv run ruff check .` and `grep` over `python/executors/*.py`
  still shows only `positions` / `time` / `options` from `quant.utils`.
- [x] 3.2 Route `python/trader.py` `_load_local_state` (line ~368) through the boundary
  validation, replacing `cast(list[LivePositionDict], positions)` — a partial
  recovered record is dropped + logged (fail closed), never traded as valid.
  Verify: unit test — a `trader_state.json` fixture with a position missing an IC
  /`OccSymbol` key is not loaded into `active_positions`; a valid one survives intact.
- [x] 3.3 Confirm `python/executors/ic_executor.py` applies `is_live_ic_position` (line
  ~1604) at entry and is consistent with the new lift; add a live boundary test that
  a partial IC position cannot reach close logic. Verify: `cd python && uv run test
  -k ic_executor` green.
- [x] 3.4 Reconcile the `LivePositionDict` rule in `python/protocols.py` (document it as
  the single sanctioned live extension paired with `is_live_ic_position`), and update
  any README/boundary doc that mentions the old "never-hold-strategy-keys-on-root"
  wording. Verify: docs and code state the same contract.

## Group 4 — Integration verification

- [x] 4.1 Full verification: `cd quant && uv run test` + `ruff check .` and `cd python &&
  uv run test` + `ruff check .` all green; import-layering gates
  (`test_import_layering.py`, AST boundary bans) and the round-trip/boundary invariants
  pass in both repos. Verify: both CI suites green with no new fixtures broken.