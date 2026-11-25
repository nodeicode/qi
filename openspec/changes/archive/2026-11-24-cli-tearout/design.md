# Design

## Context

See proposal.md — Why. The current state that shapes this: `quant/cli/**` holds four console-entry adapter modules plus `_common.py`, which carries the only ad-hoc cross-leaf imports (`quant.backtest`, `quant.strategies` marked `# layering-ok (CLI front-end)`) and three useful-but-unshipped helpers (`SyntheticDataHandler`, `make_equity_config`, `resolve_strategy`, `run_offline_backtest`). The tree already has *principled* cross-leaf edges that MUST survive: `quant/data/_registry.py` lazily resolving the registered `"backtest"` data source, and `quant/live/strategy_loader.py`'s single dynamic strategy import. `test_import_layering.py` enforces invariant §1.5 and keeps a `# layering-ok` carve-out mechanism.

## Goals / Non-Goals

**Goals:**
- Qi becomes strictly library-first: no shipped console scripts beyond `test`; primitives + engine imported directly.
- Keep an offline, deterministic, zero-key demo path for notebooks (rehome `SyntheticDataHandler`).
- Preserve the layering contract and its principled edges (registry, strategy loader) intact.

**Non-Goals:**
- No docs-site/generated web UI (deferred — too complex at this stage).
- No new convenience wrappers (`make_equity_config`, `resolve_strategy`, `run_offline_backtest` die with the CLI — notebooks compose the primitives directly).
- No change to the 8 primitives or the decision pipeline behavior.
- No re-home of the CLI's argparse/precondition logic (live is a documented snippet, not a launcher).

## Decisions

**D1 — Delete, don't demote.** Remove the whole `quant/cli` package and its `[project.scripts]` lines rather than refactoring the CLI onto the library. Rationale: the CLI is the very thing being abandoned; a rework preserves a surface with zero tests and a leaky flag grammar. Alternative considered: keep `quant-cli` as a thin `python -m` shim — rejected, it re-introduces the burden being removed.

**D2 — Rehome only `SyntheticDataHandler`, not the runnable conveniences.** Move the class to `quant/data/synthetic_data_handler.py`; delete `make_equity_config` / `resolve_strategy` / `run_offline_backtest`. Rationale: the demo handler is a genuine data-source (a `get_historical_data` provider, layering-safe — it imports nothing from backtest/strategies), so it belongs with the data layer and needs no conveniences to be useful in a notebook. The config/run helpers were CLI-scaffolding an agent of the notebook should write itself.

**D3 — Keep the layering test and its principled edges.** Do not delete `test_import_layering.py` (that would break invariant §1.5 and ban the registry's legit lazy import). Remove only the CLI's three `# layering-ok` imports. The test's carve-out mechanism is retained for `data/_registry.py` and `live/strategy_loader.py`. Alternative considered: gut the test — rejected, it fights the "no one-off patterns" goal.

**D4 — Docs reflect the library, minimally.** Update the CLI references in `README.md`, `docs/{quickstart,deployment,architecture,extensibility}.md`, and `AGENTS.md` §3 to concise inline library calls (a `Backtester` + demo handler call, a `Trader` run snippet for live, `Portfolio` snapshot reads for metrics). No site generation, no docs architecture change.

## Risks / Trade-offs

- [Offline on-ramp regression] → Mitigation: D2 keeps `SyntheticDataHandler` in the library, so a notebook backtest stays keyless; docs show the 5-line pattern.
- [Docs drift after command removal] → Mitigation: docs are edited in the same change as the code; `grep quant-backtest|quant-paper|quant-live|quant-portfolio` must be clean at archive.
- [Someone re-introduces CLI under a different form] → Mitigation: `pyproject.toml` `[project.scripts]` has only `test`; `AGENTS.md` §3 documents the library-first posture.

## Migration Plan

Single commit: delete CLI + rehome handler in the same change; docs updated atomically. No data migration; no runtime state. Rollback = revert the commit (restores `quant/cli` and console scripts verbatim).

## Open Questions

None.