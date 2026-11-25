# Tasks

## 1. Tear out the CLI package and wiring

- [x] 1.1 Rehome `SyntheticDataHandler` into `quant/data/synthetic_data_handler.py` (with its query params / docstring), and add `from quant.data.synthetic_data_handler import SyntheticDataHandler` to `quant/data/__init__.py` so it is importable as `quant.data.SyntheticDataHandler`. Verify: import works and the class exposes `get_historical_data(ticker, start, end, interval, use_cache, adjustment)`.
- [x] 1.2 Delete `quant/src/quant/cli/` entirely (`__init__.py`, `_common.py`, `backtest.py`, `paper.py`, `live.py`, `portfolio.py`). Verify: `find quant/src/quant/cli` is empty and no file imports `quant.cli`.
- [x] 1.3 Remove the four console scripts from `pyproject.toml` `[project.scripts]` (`quant-backtest`, `quant-paper`, `quant-live`, `quant-portfolio`); keep `test`. Verify: `[project.scripts]` lists only `test`.
- [x] 1.4 Remove the CLI `T201` per-file ruff ignore (`"quant/src/quant/cli/**"` line) from `pyproject.toml`. Verify: `grep quant/cli pyproject.toml` returns nothing.

## 2. Preserve the layering contract

- [x] 2.1 Confirm the only remaining `# layering-ok` edges are the principled ones: `quant/data/_registry.py` → `BacktestDataHandler` and `quant/live/strategy_loader.py`. Verify: `grep -rn "layering-ok" quant/src` shows zero CLI-front-end markers.

## 3. Docs to library-first

- [x] 3.1 Rewrite `docs/extensibility.md` CLI imports (`quant.cli._common`) to the rehomed `quant.data.SyntheticDataHandler` (and direct primitive construction where those helpers were used). Verify: no `quant.cli` reference in `docs/`.
- [x] 3.2 Update `README.md`, `docs/quickstart.md`, `docs/deployment.md`, `docs/architecture.md` to library calls (backtest via `Backtester` + `SyntheticDataHandler`; live via a `Trader` run snippet; metrics via `Portfolio`), removing all four command invocations. Verify: `grep -rn "quant-backtest\|quant-paper\|quant-live\|quant-portfolio" --include="*.md" README.md docs` returns nothing.
- [x] 3.3 Update `AGENTS.md` §3 command map to remove the four SDK CLI commands, noting the library-first posture (live = documented snippet). Verify: §3 no longer cites `quant-*` console scripts.

## 4. Verification & integration

- [x] 4.1 Run `uv run test` (full suite incl. `test_import_layering.py`) — all green. Verify: suite passes.
- [x] 4.2 Run `uv run ruff check .` and `uv run ruff format --check .` — clean. Verify: no ruff findings.
- [x] 4.3 Run `uv run ty check` — clean. Verify: type check passes.
- [x] 4.4 Full-repo sweep for stray CLI references (src, tests, docs, pyproject, AGENTS.md, CI). Verify: `grep -rn "quant.cli\|quant-backtest\|quant-paper\|quant-live\|quant-portfolio" .` returns only `.git`/`.venv` noise.

## 5. Docs verification follow-up (not tracked for apply/archive)

- Per the project's DoD: no secrets added; no tracked secret paths touched.
- Then archive the change (`openspec archive cli-tearout`) and commit.

## Workflow follow-up

- Archive: `openspec archive cli-tearout`.
- Commit the change + implementation, then note the CLI's public exit.