# Spec Delta

## Purpose

Define the durable import/module boundary between the live layer (`python/`) and
the `quant` submodule internals: which modules the live layer may import, where
shared helpers and protocol definitions live, and how the boundary is enforced
so it cannot silently rot as the codebase grows.

## ADDED Requirements

### Requirement: Live layer imports only public quant surfaces

The live layer (`python/`) MUST import `quant` only through public homes
(`quant.utils.*`, `quant.core.protocols`, and `quant.core.engine` solely for
`TradingEngine`) and MUST NOT import `quant.backtest`, `quant.strategies`,
`quant.core._utils`, `quant.data._times`, or any other `quant/*/_*.py` private
module. The `python/` lint configuration MUST ban these modules at the AST
level (ruff `TID251` `banned-api`).

#### Scenario: Live executor already imports a banned module
- **WHEN** `python/` code contains `from quant.backtest.utils import calculate_entry_time`
- **THEN** `cd python && uv run ruff check .` fails with the TID251 ban message
  for `quant.backtest`, and the import must be rewritten to
  `quant.utils.time` for CI to pass

#### Scenario: Private alpha stays out of the order path
- **WHEN** an executor imports `quant.strategies.example_equity.utils` to snap IC strikes
- **THEN** the import fails lint with the `quant.strategies` ban message, and the
  only permitted strategy import in `python/` is the single dynamic-load line in
  `python/strategy_loader.py`

#### Scenario: New private module must be adopted by the bans
- **WHEN** a new `quant/*/_*.py` private module is added in `quant/`
- **THEN** the change that adds it MUST also add a `banned-api` entry for it in
  `python/pyproject.toml` in the same commit

### Requirement: Shared helpers live in quant.utils leaf modules

`PositionDict` and `build_position_dict` MUST be defined in
`quant/utils/positions.py`; entry-time and closed-bar selection math MUST live
in `quant/utils/time.py`; options/IC helpers MUST live in `quant/utils/options.py`.
Both the live layer and the backtest package MUST import these helpers from
their `quant.utils.*` home. `quant/utils/` MUST remain a leaf package (no
imports from `quant/core/`); the dependency edge runs strictly
`quant/core` → `quant/utils`.

#### Scenario: Both sides of the boundary use the same builder
- **WHEN** `quant/backtest/executors/ic.py` and `python/executors/ic_executor.py`
  build an iron-condor position
- **THEN** both import `build_ic_position` from `quant.utils.options`, and
  `grep -n "from quant.utils.\|quant.utils import" python/executors/*.py` matches
  only `positions` / `time` / `options`

#### Scenario: utils stays a leaf package
- **WHEN** a new module in `quant/utils/` needs a shared helper
- **THEN** it imports it from another `quant.utils.*` module or the standard
  library, never from `quant.core.*`, keeping the utils → core dependency edge
  at zero

### Requirement: Protocol definitions have a single home

`PositionDict`, `StrategyProtocol`, and `ExecutorProtocol` MUST be defined and
exported from `quant/core/protocols.py` (`PositionDict` defined in
`quant/utils/positions.py` and re-exported there). `quant/core/engine.py` MUST
NOT expose these types at runtime: it declares `__all__ = ["TradingEngine"]`,
uses `from __future__ import annotations`, and imports protocols only under
`TYPE_CHECKING`. The live layer MUST import these types from
`quant.core.protocols`, never from `quant.core.engine`.

#### Scenario: Live layer imports a protocol type
- **WHEN** `python/trader.py` or `python/protocols.py` imports `ExecutorProtocol`
  or `StrategyProtocol`
- **THEN** the import statement reads `from quant.core.protocols import ...`, and
  `from quant.core.engine import ExecutorProtocol` fails lint with the
  `quant.core.engine.ExecutorProtocol` ban message

#### Scenario: Runtime re-export from the engine is impossible
- **WHEN** a new contributor writes `from quant.core.engine import PositionDict`
- **THEN** it fails at import time with `ImportError` (the binding is
  `TYPE_CHECKING`-only) and fails lint via the banned-api entry, so the type
  must be imported from `quant.utils.positions` or `quant.core.protocols`

#### Scenario: Definition moves before the boundary is repointed
- **WHEN** `python/protocols.py` subclasses `PositionDict` as `LivePositionDict`
- **THEN** the `PositionDict` definition lands in `quant/utils/positions.py` in
  the same `quant/` commit that precedes the submodule bump, so no intermediate
  state leaves the live layer importing a stale home

### Requirement: quant submodule enforces internal layering bans

The `quant/` ruff configuration MUST ban `quant.backtest` and
`quant.strategies` (importable only from their own packages and `tests/**`) and
the protocol types re-exported from `quant.core.engine`, via ruff `TID251`
`banned-api`. Per-file `TID251` exemptions MUST be limited to
`src/quant/backtest/**`, `src/quant/strategies/**`, and `tests/**`, keeping the
bans active by default in `core`, `utils`, `data`, and any new package.

#### Scenario: Core imports the simulator
- **WHEN** a module under `src/quant/core/` adds `import quant.backtest`
- **THEN** `cd quant && uv run ruff check .` fails with the `quant.backtest`
  ban message, and core must not depend on the simulator

#### Scenario: Private alpha leaves the leaf
- **WHEN** a non-strategy, non-test module in `quant` imports `quant.strategies`
- **THEN** ruff fails with the `quant.strategies` ban message; the only permitted
  importers are `src/quant/strategies/**` and `tests/**`

#### Scenario: Protocol type re-exported from the engine
- **WHEN** `quant` code does `from quant.core.engine import PositionDict`
- **THEN** ruff fails with the `quant.core.engine.PositionDict` ban message, and
  the type must be imported from `quant.core.protocols` or `quant.utils.positions`

### Requirement: quant.core._utils stays private to core

`quant/core/_utils.py` MUST be importable only from within `src/quant/core/**`
and `tests/**`. The shared helpers it hosts MUST be relocated to `quant/utils/*`
so no non-core, non-test module needs it, including `SESSION_CLOCKS` (→
`quant/utils/time.py`) and `check_ic_spread_gate` (→ `quant/utils/options.py`,)
so `src/quant/backtest/**` carries no `quant.core._utils` import and needs no
`TID251` exemption for it.

#### Scenario: Simulator reads session clocks
- **WHEN** `quant/backtest/utils.py` uses `SESSION_CLOCKS`
- **THEN** it imports it from `quant.utils.time`, not `quant.core._utils`, so the
  strict `quant.core._utils` ban requires no backtest exemption

#### Scenario: IC spread gate moves with its sibling
- **WHEN** `quant/backtest/executors/ic.py` checks `check_ic_spread_gate`
- **THEN** it imports it from `quant.utils.options` alongside
  `check_ic_credit_gates`, keeping all IC gate logic in one options home

### Requirement: quant.utils leaf is AST-enforced

`quant/utils/` MUST import only from sibling `quant.utils.*` modules and the
standard library. A test gate MUST fail if any module under `src/quant/utils/`
imports `quant.core`, `quant.data`, `quant.backtest`, or `quant.strategies`, so
the leaf invariant is enforced at CI rather than by a grep convention (an
allowlist cannot be expressed in `TID251` `banned-api`).

#### Scenario: Leaf module imports core
- **WHEN** a module under `src/quant/utils/` adds `from quant.core.sizing import ...`
- **THEN** the import-layering test fails, and the shared helper must instead be
  imported from a sibling `quant.utils.*` module
