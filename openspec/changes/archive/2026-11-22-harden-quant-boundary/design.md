# Design

## Context

The submodule boundary is a one-way contract: `quant/utils/` is already a leaf
package (zero `quant.core` imports; verified), `quant/core/_utils.py` already
imports from `quant.utils` (`_utils.py:45-46`, `engine.py:55`), and the live
layer is the only external consumer. Today `python/` still imports entry-time
math from `quant.backtest` (`unified_executor.py:16`, `ic_executor.py:62`),
private helpers from `quant.core._utils` (`unified_executor.py:17`,
`ic_executor.py:63`, `live_executor.py:36`), private-alpha strike logic from
`quant.strategies.example_equity.utils` (`ic_executor.py:1122`, test
`test_ic_executor.py:2675`, prefetcher `backtest/prefetchers/ic.py:102`), and
protocol types from `quant.core.engine` (`trader.py:42-43`,
`protocols.py:19`, `live_executor.py:37`). `quant/core/engine.py` has no
`__all__`, no `from __future__ import annotations`, and no `TYPE_CHECKING`,
so every one of those re-exported bindings is resolvable at runtime today.
`python/pyproject.toml` has a ruff section but no `banned-api` / `TID251`.

## Goals / Non-Goals

**Goals:**
- One `quant/` commit + one outer-repo commit that leave each repo green alone
  (quant has its own CI; never break history mid-PR).
- Public helper homes in `quant/utils/` with strictly one-way
  `quant.core` → `quant.utils` dependencies.
- Live layer imports only public surfaces; enforcement via AST-level
  `TID251` bans that run in CI, not grep.
- Runtime re-export of protocols from the engine made physically impossible.

**Non-Goals:**
- No behavior change to decision pipeline, sizing, or order flow — moves only.
- No deprecation re-export shims (the sole consumer is `python/` and the bump
  lands immediately; shims only needed if a second consumer appears).
- Part C of the checklist (typed models / RiskEngine / allocator, SQLite store,
  futures execution, IBKR adapter) stays parked behind its tripwires.
- `quant/data/_times.py` is NOT consolidated into `quant/utils/time.py` —
  it is the data-handler canonical-timestamp boundary, a different concern.

## Decisions

1. **`quant/utils/{positions,time,options}.py` as the three homes.** The split
   follows what the consumers do, not who the author was: position schema +
   builder (subclassed by `python/protocols.py`), time math (bar/entry clocks),
   option math (strikes, gates, IC position). Created in dependency order:
   `positions.py` → `time.py` → `options.py` (`options.py` builds on
   `build_position_dict`).
   *Alternative:* dump everything into one `quant/utils/shared.py` — rejected:
   muddies the leaf contract and grows one unbounded drawer again.
2. **`build_ic_position` leaves the engine as a free function in
   `quant/utils/options.py`.** Its two callers straddle the boundary
   (`quant/backtest/executors/ic.py:352`, `python/executors/ic_executor.py:1589`);
   a backtest home recreates the A1 live→backtest violation, a live home strands
   the backtest, duplication silently forks the `PositionDict` schema. One
   builder, one home, both callers.
3. **AST enforcement via ruff `TID251` `banned-api` in `python/pyproject.toml`.**
   RIP-8 grep rules missed `import quant.backtest` forms and false-positived on
   comments. `banned-api` matches literal prefixes — every private module is
   listed explicitly. `strategy_loader.py:6` gets an inline `# noqa: TID251`
   instead of a `per-file-ignores` entry (which would disable every ban in the
   file), and `noqa` sidesteps bare-filename matching ambiguity.
4. **Engine lock: `__all__ = ["TradingEngine"]` + `from __future__ import
   annotations` + `TYPE_CHECKING` protocol imports.** `__all__` alone only
   guards `import *`; the lock closes the runtime-import hole that lets
   `python/` keep importing protocols from the engine.
5. **Two-repo sequencing without shims.** Commit order matters: `PositionDict`
   moves **first** in `quant/` (definition + consumers), because
   `python/protocols.py:66` subclasses it at runtime — bumping the pointer
   before the definition lands would `ImportError` mid-PR. Submodule commits
   land, CI passes, then the outer repo bumps and repoints `python/` imports.
6. **Checklist reference moved into the change.** All `file:line` refs from the
   checklist were re-verified against the current tree (they match); tasks carry
   the line numbers as *starting points* with the mandate to re-locate, since
   lines drift.
7. **Quant-side `TID251` enforcement mirrors the boundary inside the submodule.**
   `quant/pyproject.toml` bans `quant.backtest` and `quant.strategies` (plus the
   engine protocol re-export triple) via `banned-api`, exempting only the two
   terminal leaves (`src/quant/backtest/**`, `src/quant/strategies/**`) and
   `tests/**`. Core, utils, data — and any new package — keep all bans active by
   default, so new code must opt into a dependency license rather than silently
   coupling.
   *Cost/benefit on the backtest exemption:* per-file-ignoring `backtest/**`
   lets the simulator re-import `strategies` without lint catching it. The plan
   already removes today's single edge (A2, `prefetchers/ic.py:102`); the two
   relocations (`check_ic_spread_gate` → `options.py`, `SESSION_CLOCKS` →
   `time.py`) additionally zero out backtest's `quant.core._utils` imports, so a
   later switch to the airtight strict form (inline `# noqa` on backtest's ~15
   self-import lines, no exemption) is a trivial follow-up.
   *Leaf rule:* `TID251` `banned-api` bans *blocked* modules and cannot express
   an allowlist, so the `quant/utils/` leaf invariant is enforced by a small AST
   test gate (`quant/tests/test_import_layering.py`) — the same house style as
   the existing `test_tz_boundary_gate.py` / handler-conformance gates.
   *R5 boundary:* banning `quant.core._utils` / `quant.data._times` globally in
   quant is deferred until the relocations land; `_times` specifically stays
   accessible to `backtest` because `backtest/backtest_data_handler.py`
   implements the data-handler canonical-timestamp contract.

## Risks / Trade-offs

- [Line drift: checklist `file:line` refs go stale] → Every task says
  "re-locate by symbol before editing"; the verification gates are
  symbol/grep-based, not line-based.
- [TID251 false hits on tests or comments] → `TID251` is AST-level, so comments
  are immune; the one known test import (`test_ic_executor.py:2675`) moves in
  the same commit as A2 or the new rule fails CI — sequenced inside the same
  outer-repo commit.
- [`live_executor.py` / `unified_executor.py` still import `DataHandlerProtocol`
  etc. from `quant.core.engine` and any missed caller breaks after the engine
  lock] → The engine lock happens in the same `quant/` commit that updates all
  `quant/` internal callers, and the TID251 ban makes a missed `python/` caller
  a lint failure, not a runtime surprise; DoD includes a grep gate over
  `python/` imports.
- [Quant CI red if tests reference moved symbols mid-commit] → Moves and their
  `quant/tests/` callers land in Commit 1; `cd quant && uv run test` +
  `ruff check .` gate the commit.
- [Submodule pointer bump without the `python/` repoint breaks `python/`
  CI] → Bump and `python/` repoint are one atomic outer-repo commit, gated by
  `cd python && uv run test` + `ruff check .`.

## Migration Plan

1. **Quant commit 1:** create `quant/utils/{positions,time,options}.py`, move
   implementations + `PositionDict` definition, update all `quant/` internal
   callers (engine, backtest utils/prefetchers/executors, example_equity leaf), lock
   the engine, update `quant/tests/`, run `cd quant && uv run test`,
   `ruff check .`, `ruff format .`. Commit to `main`.
2. **Outer commit 2:** bump the submodule pointer, repoint `python/` imports
   (`quant.utils.{positions,time,options}`, `quant.core.protocols`,
   `quant.core.engine` → `TradingEngine` only), add the A5 `TID251` block +
   inline `noqa` on `strategy_loader.py:6`, move the test's `snap_ic_strikes`
   import, update the boundary docs (`quant/README.md` line about `banned-api`),
   run `cd python && uv run test` + `ruff check .`. Commit to the current branch.
3. **Rollback:** revert the outer commit (pointer back) or revert both commits;
   no data migration, no schema change — the moves are import-statement +
   module-location changes only. `trader_state.json` / `config_sync` unaffected.

## Open Questions

None — the checklist's Part A plan is fully specified and verified against the
current tree; Part B/C handling is recorded in the proposal.
