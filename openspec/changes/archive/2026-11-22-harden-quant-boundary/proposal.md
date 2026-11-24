# Proposal

## Why

The live layer (`python/`) reaches into `quant.backtest` (entry-time math), the
**private alpha** `quant.strategies` package (IC strike snap), and private
`quant.core._utils` / `quant.data._times` modules. That couples live behavior
to backtest semantics and to strategy internals that AGENTS.md declares private,
and the only guard today is convention (the old checklist's grep rules) — not
enforcement. This change moves the shared helpers to a public `quant.utils.*`
home, points the live layer at public surfaces only, and enforces the boundary
at the AST level so it cannot silently rot.

## What Changes

- **BREAKING (within-repo):** `PositionDict` definition and `build_position_dict`
  move from `quant/core/_utils.py` to `quant/utils/positions.py`.
- **BREAKING (within-repo):** `calculate_entry_time` moves from
  `quant/backtest/utils.py` to `quant/utils/time.py` (`closed_bar_asof` joins it
  from `quant/core/_utils.py`).
- **BREAKING (within-repo):** `snap_ic_strikes`, `check_ic_credit_gates`,
  `select_strike`, `calc_spread_width`, and `build_ic_position` consolidate from
  `quant/core/_utils.py`, `quant/core/engine.py`, and
  `quant/strategies/example_equity/utils.py` into `quant/utils/options.py`.
- **BREAKING (within-repo):** `quant/core/protocols.py` becomes the single home
  for `PositionDict` / `StrategyProtocol` / `ExecutorProtocol`; `python/`
  imports them from there; `quant/core/engine.py` gets `__all__ = ["TradingEngine"]`,
  `from __future__ import annotations`, and `TYPE_CHECKING`-scoped protocol
  imports so runtime re-export from the engine is physically impossible.
- New `TID251` banned-API block in `python/pyproject.toml` banning
  `quant.backtest`, `quant.strategies`, `quant.core._utils`, `quant.data._times`,
  and engine protocol re-exports, with an inline `# noqa: TID251` on the single
  dynamic strategy-load line in `python/strategy_loader.py`.
- Two-repo execution: one `quant/` commit (moves + internal callers + engine
  lock), then an outer-repo commit (submodule bump + `python/` import updates +
  TID251), so neither repo is ever red alone.
- **Quant-side enforcement:** mirror `TID251` `banned-api` bans inside
  `quant/pyproject.toml` for `quant.backtest` (core/utils/data/strategies never
  import the simulator) and `quant.strategies` (private alpha), plus the engine
  protocol re-export triple — with per-file exemptions limited to
  `src/quant/backtest/**`, `src/quant/strategies/**`, and `tests/**`. Relocate
  `check_ic_spread_gate` → `quant/utils/options.py` and `SESSION_CLOCKS` →
  `quant/utils/time.py` so `quant.core._utils` can be strictly private, and add
  an AST import-layering test gate for the `quant/utils/` leaf rule.

## Capabilities

### New Capabilities

- `quant-boundary`: the durable import/module boundary between the live layer
  (`python/`) and `quant` submodule internals — which modules the live layer may
  import, where shared helpers and protocol definitions live, and how the
  boundary is enforced (AST-level bans that must not rot).

### Modified Capabilities

None — no capabilities exist yet (`openspec list --specs` is empty); this
change introduces the first.

## Impact

- **`quant/` repo:** `src/quant/core/_utils.py` (sheds PositionDict + helpers),
  `src/quant/core/engine.py` (`build_ic_position` out, `TYPE_CHECKING` lock),
  `src/quant/core/protocols.py` (re-export `PositionDict`), new
  `src/quant/utils/{positions,time,options}.py`, `src/quant/backtest/*`
  (prefetchers, executors, utils callers), `src/quant/strategies/example_equity/`
  (leaf caller), `quant/pyproject.toml` (`TID251` + `banned-api`),
  `quant/tests/*` (incl. new `test_import_layering.py`).
- **`quant-engine/python/`:** `executors/ic_executor.py`,
  `executors/unified_executor.py`, `executors/live_executor.py`,
  `protocols.py`, `trader.py`, `strategy_loader.py` (inline `noqa`),
  `pyproject.toml` (TID251), `tests/unit/executors/test_ic_executor.py`.
- **Checklist closure:** the old `quant_infrastructure_refactoring_checklist.md`
  was absorbed into this change (proposal/design/spec/tasks). Its Part B
  verification item (trader_state.json durability) is carried as task 3.2; its
  Part C tripwires are explicitly out of scope (recorded in the spec).
