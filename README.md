<p align="center"><strong>Qi</strong> — <em>Quantitative Intelligence</em> primitives to model, research, and deploy mathematical rules in open markets.</p>

Qi is a lean, boring-Python SDK for quantitative research → backtest → paper → live, with a single decision pipeline shared byte-for-byte across environments so research results are truthful previews of live behavior *(research/live parity by construction, not by duplication)*.

It is the opposite of a heavyweight trading framework: no platform, no event-loop kernel, no internal message bus, no async-first core. Protocol-based seams — a strategy is one file + one registry line; a venue wires data + session + execution into one swappable unit; a portfolio is a position state machine — with sensible defaults (Alpaca, local config, pure-pandas) so the baseline install needs **zero** system dependencies and **zero** cloud.

## Why

The hard part of quant is not finding a signal — it is knowing your backtest tells the truth, and keeping a strategy honest between backtest and live. Qi engineers for that specifically:

- **One decision pipeline** (`process_strategy`) runs identical in backtest and live; only the executor differs. Backtest↔live drift is structurally impossible.
- **Deterministic engine** — a golden, two-process replay test proves an identical decision sequence for identical inputs.
- **Adjusted-bar invariant** — signals run on adjusted bars; raw close is backtest-only.
- **Lean primitives you extend** — `quant.observability` is spans + an emitter; shadow signals, reconcile, dashboards are things you build on top.

## Quickstart

```bash
git clone https://github.com/nodeicode/qi.git
cd qi
uv sync                      # one distribution "quant"; pure-pandas, no system libs
uv run test                  # full suite (engine + data + backtest + live)
```

Q1 is a **library**: drive the pipeline from a script or a notebook. An offline
backtest on the reference equity strategy (no keys, no AWS):

```bash
uv run python - <<'PY'
from quant.backtest.backtester import Backtester
from quant.data import SyntheticDataHandler
from quant.strategies.example_equity.strategy import (
    ExampleEquityConfig, ExampleEquityStrategy,
)
cfg = ExampleEquityConfig(INTERVAL="1d", MANAGE_INTERVAL="1d")
bt = Backtester(cfg, ExampleEquityStrategy(cfg), "2024-01-02",
                "2024-06-28", "SPY", data_handler=SyntheticDataHandler())
trades = bt.run_backtest(); bt.compute_equity_curve()
print("entries:", len(trades))
PY
```

A keyless dev dry-run polls a local config file via `FileConfigSource` + `ConfigSync`;
live is a runnable `UnifiedTrader` snippet — see `docs/quickstart.md` (full
<5-minute clone→backtest→paper guide) and `docs/deployment.md` (live/systemd).
`docs/extensibility.md` covers adding your own strategy / data source / venue.

`uv run test -m live` scopes the folded live layer; `-m data` / `-m backtest` the engine data/backtest tests.

## Layout

```
src/quant/            # the SDK — one distribution
  core/               # decision engine, protocols, sizing
  data/               # data source + MemBars fast path + cache
  backtest/           # driver, fill simulation, metrics
  strategies/         # example_equity (reference) + registry/entry-points
  live/               # trader loop, executors, monitor, config, session
  observability.py    # spans (age ∥ duration) + emitter interface
  utils/              # time, positions, indicators, config
```

## Extension model

- **Add a strategy** → one class (protocol) + one registry line.
- **Add a data source / venue** → implement the venue protocol (`AlpacaVenue` / `BacktestVenue` ship).
- **Add an executor / asset class** → one executor pair (backtest + live) behind the same interface.
- **Add a strategy pack** → register via the `quant.strategies` entry-point group; no fork.

## Status

Qi is the public foundation cut from a private production superset (the private alpha and options/IC machinery stay private). Equity-first: futures/options paths are documented seams, not shipped v1. Building in public — watch for portfolio primitives, venue backends, and the agent-context docs.

## License

MIT
