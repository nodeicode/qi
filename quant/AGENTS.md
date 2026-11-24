# AGENTS.md — quant (submodule)

Read the root repo's `AGENTS.md` first — this file is the *additions* specific
to the `quant/` package. `quant/` is a git submodule of the production trader
repo (`quant/core` is the shared backtest↔live decision engine).

## What lives here

| Path | Role |
|---|---|
| `core/` | `TradingEngine` decision pipeline (`process_strategy`), sizing registry, telemetry utils |
| `backtest/` | Backtest drivers + simulated fills (same `process_strategy`) |
| `data/` | `AlpacaDataHandler` (REST) + `ws/` live bar stream → `MemBarsAggregator` |
| `strategies/` | **Private alpha** — real strategies, one folder each |
| `utils/` | Shared helpers |

## Rules specific to quant

1. **`strategies/` is the alpha.** There is a *strict* structure mandated in
   `strategies/README.md`: every deployment-ready strategy is a folder with the
   code **plus a markdown** (math/stat summary, bull/bear/choppy/crash analysis,
   parameter-sensitivity "robust plateau" check, Monte Carlo). The markdown is
   the single source of truth for that strategy. Updating a strategy requires
   updating its markdown.
2. **Live/paper protocols:** unless explicitly told, do not edit or migrate any
   strategy that is currently deployed in a paper or production environment —
   doing so forces its paper account to be re-initialized. Treat deployed
   strategies as frozen.
3. **Engine changes are high-risk.** `core/engine.py` is driven identically by
   backtest and live. Any edit here affects real execution; it must ship with a
   test that guards the *live* path.
4. **Adjusted-bar invariant** (see root `AGENTS.md`): signals run on adjusted
   bars; raw close is backtest-only.
5. Keep the submodule pointer stable in the superproject unless a deliberate
   bump. Don't create churn between the two repos.

## Local env

Requires keys in `.env` (gitignored): `ALPACA_API_KEY/SECRET`, `DATABENTO_API_KEY`,
`DHAN_*` (see `README.md`). Never commit `.env`; verify with
`git ls-files | grep -E '\.env|\.pem'`.