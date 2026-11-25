# Proposal

## Why

Q1 is a lean, boring-Python SDK for quantitative research → backtest → paper → live, and the product is the shared decision pipeline plus the 8 closed primitives — an API surface, not a flag grammar. The four CLI entry points (`quant-backtest`, `quant-paper`, `quant-live`, `quant-portfolio`) are a second-class, leaky view of that surface: they try to express every primitive knob through argparse flags, they carry the only ad-hoc cross-leaf import carve-out in the tree, and they have zero test coverage. Qi should be consumed as a library — primitives and engine directly in notebooks/scripts, and live as a documented snippet (script / systemd service) — not through a CLI that can never keep pace.

## What Changes

- **BREAKING** Remove `quant/src/quant/cli/` entirely (backtest/paper/live/portfolio/`_common`/`__init__`).
- **BREAKING** Remove the four console scripts from `[project.scripts]` in `pyproject.toml` (`quant-backtest` / `quant-paper` / `quant-live` / `quant-portfolio`). The `test` script stays.
- Rehome `SyntheticDataHandler` (the deterministic, offline, oscillating bar source) out of `quant/cli/_common.py` into the library (`quant.data`), kept solely so a demo notebook can run a zero-key backtest. No `make_equity_config` / `resolve_strategy` / `run_offline_backtest` conveniences — those die with the CLI.
- **BREAKING** Drop the CLI's three `# layering-ok` cross-leaf imports (`quant.backtest`, `quant.strategies`). Keep the principled registry edge (`quant/data/_registry.py` → `BacktestDataHandler`) and the live `strategy_loader` dynamic line, and **keep** `test_import_layering.py` (invariant §1.5).
- Update CLI-era documentation to the library API: `README.md` quickstart, `docs/quickstart.md`, `docs/deployment.md`, `docs/architecture.md`, `docs/extensibility.md`, and `AGENTS.md` §3 command map — replace removed commands with concise inline library calls.
- Remove the CLI-specific `T201` per-file ruff ignore in `pyproject.toml`.

## Capabilities

### New Capabilities

None — the 8 closed primitives and their specs are unchanged (this is a surface/tooling/docs layer, not behavior).

### Modified Capabilities

None at spec level. The removal touches packaging, imports, and docs only; no primitive's requirement text or behavior changes. Change opts out of specs via `skip_specs: true`.

## Impact

- **Code:** deletes `quant/src/quant/cli/**`; rehomes `SyntheticDataHandler`; touches `pyproject.toml` (`[project.scripts]`, ruff per-file ignores); removes `# layering-ok` on three imports.
- **Docs:** `README.md`, `docs/{quickstart,deployment,architecture,extensibility}.md`, `AGENTS.md` §3.
- **Tests:** no CLI tests exist (verified); `test_import_layering.py` stays and its carve-out mechanism remains for the registry/loader edges.
- **Behavior:** end users using the CLI lose it (breaking); library API is unchanged.