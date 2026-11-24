# quant

core quant package (combines quant strategies and quant common utilities)

Used as a **git submodule** by the production trader repo
(`quant-engine`, `@python/` live layer). `quant/core/` is the
shared decision engine: backtest and the live trader drive the same
`TradingEngine.process_strategy` pipeline — see
`src/quant/core/README.md` for the end-to-end state machine and the
live routing table.

The package is **src-layout**: importable code lives under `src/quant/`
(`uv_build` backend), so `import quant.core.*` resolves without a flat
namespace shadow at the submodule root.

## Layout

| Path | Role |
|---|---|
| `src/quant/core/` | `TradingEngine` — decision pipeline shared by backtest and live; sizing registry (`sizing.py`), span/monitor telemetry (`monitor_utils.py`), shared `_utils.py` helpers |
| `src/quant/backtest/` | Backtest drivers + `BacktestDataHandler` (same `process_strategy`, simulated fills) |
| `src/quant/data/` | `AlpacaDataHandler` (REST) + `ws/` live bar stream (AlpacaDelayedSipStream → `MemBarsAggregator`) |
| `src/quant/strategies/` | Deployment-ready strategies, one folder per strategy with docs (§ requirements below) |
| `src/quant/utils/` | Shared helpers (`strategy_config.py`, feature utils) |

## prerequisites

`.env` file with the following keys will need to be setup everytime this repo is used as a submodule

```python
ALPACA_API_KEY=""
ALPACA_API_SECRET=""
MASSIVE_API_KEY=""

DATABENTO_API_KEY=""

DHAN_ACCESS_TOKEN=""
DHAN_CLIENT_ID=""
```

## /src/quant/utils

Has Base utilities for usage across quant projects

## Timestamp contract

Data handlers are the **only** boundary where timezone-aware time may exist.
Every handler in `data/` (and `backtest/backtest_data_handler.py`) returns and
accepts **canonical tz-naive UTC** timestamps:

* Frames carry a `DatetimeIndex` that is tz-naive UTC, `datetime64[us]` for
  intraday bars (daily bars follow the equity-session convention and may keep
  the vendor timezone for `to_et_session_midnight`-style mapping).
* Boundary helpers live in `data/_times.py` (`parse_naive_utc`,
  `assert_canonical_index`, `ts_ns` / `from_ns`, `aligned_at`). A handler that
  bypasses them fails the conformance gate (`tests/data/test_handler_conformance.py`).
* Above the boundary the engine, executors and monitor **read** stamps only —
  the decision path contains no `tz_localize` / `tz_convert` / `replace(tzinfo`
  / `.tzinfo` branching (enforced by `tests/test_tz_boundary_gate.py`).
* The one sanctioned mutating exception is `calculate_fill_time` in
  `backtest/utils.py` (the `DATA_DELAY` simulation wall, backtest-only);
  pure calendar readers (`closed_bar_asof`, `bar_close_utc`, session clocks)
  do tz math internally but always take and return canonical naive-UTC.

## /src/quant/strategies

Deployment ready quantitative strategies along with extensive documentation of inner working and robustness results.  
Ensure to **CAREFULLY** ready the below instructions for any updates to this folder!

1. Follow a strict pattern i.e for each strategy that is **paper/production ready** ensure there is a folder with the strategy code, a markdown document with a mathematical and statistical summary along with performance and robustness metrics over the past 5 years.

2. The markdown document will be the single source of truth for the strategy its for, any updates to the strategy will need a updated markdown as well. The markdown should follow this structure:

   - Strategy Name and short description (discuss type of strategy and describe its approach to the market)
   - Assets/Tickers it will run on along with any info on strategy:ticker mapping
   - Mathematical and Statistical summary, crucial to understand what is the underlying concept and why it works
   - Performance and Robustness Analysis, the standard historical backtest in bull, bear, choppy and crash senarios. note: no strategy will perform positively in all senarios its about how it handles different market environments for eg: we don't want a strategy that does well in bull and choppy preiod but just goes straight to -100% in bearish or crash senarios, that's a **RED** flag. We also need any parameter sensitivity analysis here showing robust plateau of optimal params (acts as a check for overfitting). And a standard Monte Carlo simulation using the quant_util monte Carlo package to check performance accross all possible future return paths (monte carlo is a crucial adaptability check).

3. All strategies need to have a edge over the benchmark to be deployed in paper or production. This can be in the form of a better sharpie, raw returns and/or positive jenen alpha.

4. All strategies will be deployed on paper for a arbitrary period and based on paper performance will be moved to production. All strategies in paper env **cannot** be updated, if they are updated in any way the paper account will need to be re-initialized and strategy will be run in paper env again.

## OpenSpec workflow

Changes to this repo are **spec-driven**: open a proposal in
`openspec/changes/` (`/openspec-propose`), review the design/specs/tasks, then
apply and archive (`/openspec-apply-change`, `/openspec-archive-change`).

The production trader repo (`quant-engine`) tracks this repo as a git
submodule — after changes land here, bump the submodule there.

## Import-boundary enforcement

The import boundary is enforced at the AST level (ruff `TID251` `banned-api` in
`pyproject.toml`): `quant.backtest` and `quant.strategies` are importable only
from their own packages (plus `tests/**`), and the protocol types are imported
from `quant.core.protocols` / `quant.utils.positions`, never the engine.
`quant.utils/` is a leaf package importing nothing from `quant.core`.

**Any new `quant/*/_*.py` private module MUST be added to `banned-api` in the
same commit** (quant and `python/`), so the boundary cannot silently rot as the
tree grows. `quant/data/_times.py` and `quant/core/_utils.py` are the only two
today; `quant/utils/{positions,time,options}.py` are the shared helper homes.
