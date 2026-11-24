# Extensibility — recipes for the Qi extension surface

Qi is extended through protocol seams, not forks. Everything below runs against
the installed SDK. The convention for each recipe: **author (B)** replaces a
port (strategy / data source / venue / executor) or builds on a primitive
(observability); the engine and the accounting spine stay untouched.

The listed `uv run python - <<'PY'` blocks are standalone and keyless (synthetic
bars, no AWS). Each has been run against this tree.

## 0. Shared setup

All recipes import the offline helpers:

```python
from quant.cli._common import make_equity_config, SyntheticDataHandler
```

- `make_equity_config()` → a runnable `example_equity` config on daily bars.
- `SyntheticDataHandler()` → deterministic offline bars (a `DataSourceProtocol`).

## 1. Add a strategy

A strategy is one class (protocol) + one registry line. Nothing else changes.

```bash
uv run python - <<'PY'
from quant.core.protocols import PositionDict
from quant.backtest.backtester import Backtester
from quant.cli._common import make_equity_config, SyntheticDataHandler
from quant.strategies import STRATEGY_REGISTRY

class MyStrategy:
    name = "my_strategy"
    def __init__(self, config):
        self.config = config
    def calculate_indicators(self, df, **kw):
        return df                       # no indicators yet
    def generate_signals(self, df):
        out = df.copy()
        out["Signal"] = "BUY"; out["Confidence"] = 1.0; out["Reason"] = "always-buy"
        return out
    def manage_trade(self, position: PositionDict, df):
        return -1                        # never managed-exit; end-of-run force close

STRATEGY_REGISTRY["my_strategy"] = MyStrategy   # one registry line

cfg = make_equity_config()
bt = Backtester(cfg, MyStrategy(cfg), "2024-01-02", "2024-03-29", "SPY",
                data_handler=SyntheticDataHandler())
trades = bt.run_backtest()
print("trade-log rows:", len(trades))            # 1 (entered, held, forced close)
PY
```

To ship a strategy as a plugin **without SDK changes**, register it under the
`quant.strategies` entry-point group in your own distribution (see `agents/
strategy-sdk.md` and task 6.3) — it then resolves through `get_strategy(name)`
with no fork.

## 2. Add a data source

Implement `DataSourceProtocol.get_historical_data(...) -> DataFrame` and hand it
to the backtester (or a venue); the engine stays deterministic as long as bars
are adjusted + bar-driven.

```bash
uv run python - <<'PY'
from quant.venue.base import DataSourceProtocol
from quant.cli._common import SyntheticDataHandler

src = SyntheticDataHandler()
assert isinstance(src, DataSourceProtocol)   # runtime-checkable

df = src.get_historical_data("SPY", "2024-01-02", "2024-01-10", "1d")
print("columns:", df.columns.tolist())       # Open/High/Low/Close/Volume
print("rows:", len(df))
PY
```

Weave it into a venue and an engine the same way the reference backtest does:
`Backtester(..., data_handler=your_source)` and attach it as a venue's
`data_source`.

## 3. Add a venue

Subclass `BaseVenue` (or implement the `Venue` protocol), register it by name,
and resolve it the same way the built-in `alpaca` / `backtest` venues are.

```bash
uv run python - <<'PY'
from datetime import datetime, time
from quant.venue import BaseVenue, register_venue, venue_by_name

class CryptoVenue(BaseVenue):
    id = "crypto"
    def __init__(self, **kw):
        super().__init__(**kw,
                         default_open=time(9, 0), default_close=time(17, 0),
                         default_force_close=time(16, 45),
                         treat_no_calendar_as_closed=False)

register_venue("crypto", CryptoVenue)
cv = venue_by_name("crypto")
print("open at 12:00:", cv.is_open_at(datetime(2024, 6, 3, 12, 0)))   # True
print("open at 06:00:", cv.is_open_at(datetime(2024, 6, 3, 6, 0)))    # False
PY
```

## 4. Add an executor / asset class

Execution is the one parity-sensitive seam. In backtest, an asset-class stays
behind `BaseBacktestExecutor` and registers in the router's default registry;
the router is the single `ExecutorProtocol` face the engine sees.

```bash
uv run python - <<'PY'
import logging
import pandas as pd

from quant.backtest.executors.router import ExecutorRouter, DEFAULT_REGISTRY
from quant.backtest.executors.context import BacktestExecContext
from quant.backtest.executors.equity import EquityExecutor
from quant.core.engine import TradingEngine
from quant.cli._common import make_equity_config, SyntheticDataHandler

cfg = make_equity_config()
class TrivialStrategy:
    name = "s"
    def __init__(self, c): self.config = c
    def calculate_indicators(self, df, **kw): return df
    def generate_signals(self, df):
        out = df.copy(); out["Signal"] = "NEUTRAL"; out["Confidence"] = 0.0
        out["Reason"] = ""; return out
    def manage_trade(self, p, df): return -1

engine = TradingEngine(TrivialStrategy(cfg), SyntheticDataHandler())
ctx = BacktestExecContext(config=cfg, engine=engine, ticker="SPY",
                          logger=logging.getLogger("x"),
                          df_signals=pd.DataFrame(), df_manage=pd.DataFrame())

router = ExecutorRouter(ctx, registry={**DEFAULT_REGISTRY, "CUSTOM": EquityExecutor})
print("resolved executor:", type(router._resolve("CUSTOM")).__name__)  # EquityExecutor
PY
```

The same story holds live: implement the shared signal path and let the venue
feed its fills into a `Portfolio` (see `agents/portfolio.md`). Divergences
between backtest and live are then attributable only to your executor.

## 5. Build on observability

Observability is spans + an emitter. An extended monitor is just a sink.

```bash
uv run python - <<'PY'
from quant.observability import Emitter, Span, SpanName

class HeartbeatEmitter(Emitter):
    def __init__(self):
        self.rows = []
    def emit(self, event):
        if event.get("kind") == "heartbeat":
            self.rows.append(event)

hb = HeartbeatEmitter()
hb.emit({"kind": "heartbeat", "main_loop_lag_ns": 1234})
hb.emit({"kind": "decision", "x": 1})          # not a heartbeat -> ignored

# the same Span primitive the trader uses, pointed at your sink:
with Span(SpanName.MAIN_LOOP_LAG, emit=hb.emit):
    pass

print("heartbeats:", len(hb.rows))             # 1
PY
```

`SpanName` is the closed key set — producers and consumers share it, so your
monitor reads the exact rows backtest and live produce (age ∥ duration kept
distinct). No engine modification needed.

## Where each seam lives

| Seam | Contract | Reference |
|---|---|---|
| Strategy | `StrategyProtocol` + one registry / entry-point line | `quant.strategies` |
| Data source | `DataSourceProtocol` | `quant.venue.base` |
| Venue | `Venue` / `BaseVenue`, registered by name | `quant.venue` |
| Executor | `BaseBacktestExecutor` (backtest) / live executor pair | `quant.backtest.executors`, `quant.live.executors` |
| Observability | `Span` + `Emitter` | `quant.observability` |
| Portfolio | fills-only state machine | `quant.portfolio` |
| Config | `ConfigSource` backend, registered by name | `quant.config` |

Each seam's invariants and extension points are detailed in its `agents/*.md`
doc — see the index in `AGENTS.md`.