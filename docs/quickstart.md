# Quickstart — Qi in under 5 minutes, no AWS

Goal: clone → install → run a backtest → run a paper dry-run, **without touching
AWS or entering any API keys**. Everything here runs on the reference
`example_equity` strategy against synthetic bars, so it works on a fresh
machine with nothing but Python 3.12 and `uv`.

## Prereqs

- Python 3.12 (the SDK pins exactly this minor version).
- [`uv`](https://docs.astral.sh/uv/) — the project uses one uv-managed workspace.

## 1. Clone and install

```bash
git clone https://github.com/nodeicode/qi.git
cd qi
uv sync
```

`uv sync` installs the single `quant` distribution plus the dev toolchain
(pytest, ruff, ty). The base install is brokerless — alpaca-py/boto3 are
optional extras (`quant[alpaca]`, `quant[ssm]`) and are **not** required for
anything in this guide.

> Reminder: the broker/AWS deps **are** present in this repo's own dev group so
> `uv run test` can run the live/golden suites in CI. A clean `pip install
> quant` elsewhere is truly brokerless — the library runs offline with synthetic
> bars, and live is an optional extra you never import.

## 2. Backtest the reference strategy

Q1 is a **library**: you drive the pipeline directly from a Python script or a
notebook. A zero-key offline backtest on the reference `example_equity` strategy
is a few lines — synthetic bars need no network and no API keys:

```bash
uv run python - <<'PY'
from quant.backtest.backtester import Backtester
from quant.data import SyntheticDataHandler
from quant.strategies.example_equity.strategy import (
    ExampleEquityConfig,
    ExampleEquityStrategy,
)

cfg = ExampleEquityConfig(INTERVAL="1d", MANAGE_INTERVAL="1d")
bt = Backtester(
    cfg,
    ExampleEquityStrategy(cfg),
    "2024-01-02",
    "2024-06-28",
    "SPY",
    data_handler=SyntheticDataHandler(),
)
trades = bt.run_backtest()
bt.compute_equity_curve()
print("entries:", len(trades))
print("final_equity:", round(float(bt.equity_curve.iloc[-1]), 2))
PY
```

You get a deterministic result on any machine (synthetic bars, same inputs →
same trades/metrics). Drop `data_handler=SyntheticDataHandler()` for a real venue
and `Backtester` runs the same pipeline against live bars — only the executor
changes (see `docs/deployment.md`).

## 3. Paper dry-run (zero-AWS dev mode)

A keyless dev dry-run is just the runtime-config path: poll a local config file
through `FileConfigSource` + `ConfigSync` and resolve the named strategy. No
boto3, no Alpaca, no execution:

```bash
uv run python - <<'PY'
import json
from pathlib import Path

from quant.config import ConfigSync, FileConfigSource
from quant.strategies import get_strategy
from quant.strategies.example_equity.strategy import ExampleEquityConfig

default_cfg = ExampleEquityConfig(INTERVAL="1d", MANAGE_INTERVAL="1d")

path = Path("paper_config.json")
if not path.exists():
    path.write_text(json.dumps({"strategies": [{"name": "example_equity"}]}))

sync = ConfigSync(FileConfigSource(path))
for tick in range(3):
    cfg = sync.poll()
    if cfg is not None:
        for entry in cfg.get("strategies", []):
            strat = get_strategy(entry["name"])(default_cfg)
            print(f"tick {tick}: {strat.name} ready (paper)")
    else:
        print(f"tick {tick}: config unchanged")
print("paper dry-run complete; exiting cleanly")
PY
```

The full paper/live trader turns this poller into a run loop — see
`docs/deployment.md` for the runnable `UnifiedTrader` snippet (a script or
systemd unit).

## 4. Verify the full suite (optional)

```bash
uv run test
```

Runs the whole suite (engine + data + backtest + live goldens) under the same
environment. Expect ~830 tests, all green.

## Next steps

- `docs/extensibility.md` — add your own strategy / data source / venue / executor.
- `docs/architecture.md` — how the one pipeline stays parity-safe.
- `docs/deployment.md` — paper is the default; the SSM-backed prod path.

## Notes

- **No AWS:** no IMDS, no boto3, no credentials — every offline path here is
  fully keyless via synthetic bars.
- **Deterministic:** synthetic bars mean the same invocation yields the same
  result on any machine.