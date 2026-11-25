# Decision engine — `quant.core.engine.TradingEngine` (spec `decision-engine`, design D5)

The engine is the one place Qi is opinionated: every trading decision — signal
generation, entry gating, sizing, indicator injection, `manage_trade`, and the
canonical position request — flows through `TradingEngine.process_strategy`
(and its per-ticker form `process_ticker`), **shared byte-for-byte by backtest
and live**. Environment-specific behavior lives only behind the
`ExecutorProtocol` boundary. This is the parity guarantee: research results are
truthful previews of live behavior.

## Invariants (do not break without discussion)

1. **One deterministic entry point.** All decisions flow through
   `process_strategy(strategy, now, executor)` in both environments. For
   identical (strategy, config, bar timeline, executor behavior) the engine
   MUST produce identical decisions — no wall clock, iteration order, or
   environment dependence. Guarded by the two-process golden replay /
   golden-regression tests and the deterministic backtest fixture.
2. **Bar-driven scheduling.** `INTERVAL` (signal) and `MANAGE_INTERVAL`
   (manage) gates drive the tick. No intra-`generate_signals` data fetches, no
   ad-hoc clocks in the decision path. A manage-only bar skips the signal
   pipeline entirely; a signal bar runs indicators → signals → gate → sizing.
3. **Adjusted-bar invariant.** Decision frames are built from adjusted bars in
   both environments. `Raw_Close` is backtest-only execution (tradable-space
   exits); it is never readable on the live decision path.
4. **Pure helpers, one impure boundary.** Sizing / entry-gate / manage-exit
   helpers are pure functions; the only impure boundary is the data fetch,
   behind `DataSourceProtocol` (deterministic in backtest by construction).
5. **Explicit state machines.** Position lifecycle, strategy state, and order
   flow are explicit state machines mutated only by fills/events, never side
   effects. On the engine path, positions are represented through the group-4
   `Position` primitive (via `position_from_request`); a CLOSED position state
   machine is never managed.
6. **Canonical request schema.** `build_position` emits the canonical
   `PositionDict` request byte-identical (executors/strategies see the same
   dict as ever) — routing the lifecycle through `Position` does not change the
   schema.

## Equity-only scope (5.3)

The shared pipeline is **equity-only**: `build_ic_position` and the `ICMeta`
read paths are gone, and the backtest/live executors refuse non-equity
signals loudly. What the shared pipeline still needs:

- `SIGNAL_REGISTRY` and `AssetClass` (in `quant.core._utils`) — retained by
  design: signal routing consumes them, and they keep the futures/options
  extension a pure addition.
- Sizing is strategy-owned: the author body sizes from instrument contract
  facts + the portfolio (`quant.strategies` helpers); the engine no longer
  dispatches sizing (`quant.core.sizing` is deleted). The executor fill
  paths size inline from the strategy's capital settings until venue fills
  take quantities from submitted orders.

To re-add an asset class, the seam is a registered signal + an
`ExecutorProtocol` implementor — the engine pipeline itself does not change.

## Where decisions live

| Concern | Home |
|---|---|
| Signal pipeline (indicators → signals → cache) | `run_signals` / `cache_signals` (engine) |
| Manage exits (`manage_trade` + injected indicators) | `evaluate_manage_exit` (`core._utils`) |
| Entry gating / canonical request | `check_entry_gate` / `build_position` |
| Position lifecycle (group-4 primitive) | `position_from_request` → `quant.portfolio.Position` |
| Environment boundary | `ExecutorProtocol` (live trader / backtest router) |

## Quickstart — the decision path offline

```python
import datetime
import pandas as pd
from types import SimpleNamespace

from quant.core.engine import TradingEngine
from quant.core.protocols import PositionDict
from quant.utils.strategy_config import BaseStrategyConfig

class FakeDataHandler:
    def get_historical_data(self, ticker, start, end, interval, use_cache=True):
        idx = pd.date_range("2024-01-02 14:00", periods=5, freq="1h", tz="UTC")
        return pd.DataFrame({"Open": 100.0, "High": 101.0, "Low": 99.0,
                             "Close": 100.5, "Volume": 1000}, index=idx)

def make_config() -> BaseStrategyConfig:
    return BaseStrategyConfig(
        INTERVAL="1h", MANAGE_INTERVAL="1h", PRE_LOAD_DAYS=1,
        INITIAL_CAPITAL=10_000.0, CAPITAL_PER_TRADE=0.1,
        MAX_CONCURRENT_POSITIONS=2,
        STOP_LOSS_PERCENT=0.02, TAKE_PROFIT_PERCENT=0.05, USE_TRAIL_LOSS=False,
        MAX_HOLDING_MINUTES=480, MIN_SIGNAL_CONFIDENCE=0.0,
        BASE_SLIPPAGE_FACTOR=0.0, TICKERS=["SPY"], DATA_DELAY=0,
    )

class Strategy:
    name = "demo"
    def __init__(self): self.config = make_config()
    def calculate_indicators(self, df, **kw): return df
    def generate_signals(self, df):
        out = df.copy(); out["Signal"] = "BUY"; out["Confidence"] = 0.9
        return out
    def manage_trade(self, position: PositionDict, df): return -1

strategy = Strategy()
engine = TradingEngine(strategy, FakeDataHandler())

# canonical position request (unchanged schema) -> routed through `Position`
req = engine.build_position(datetime.datetime(2024, 6, 1, 12, 0), 150.0, 1,
                            "BUY", 0.75, Ticker="SPY", Qty=10)
lifecycle = engine.position_from_request(req)          # group-4 Position
assert lifecycle.state.value == "opened" and lifecycle.direction == 1
assert engine.build_position(datetime.datetime(2024, 6, 1, 12, 0), 150.0, 1,
                             "BUY", 0.75)["EntryPrice"] == 150.0
print("decision path OK: canonical request", req["EntryPrice"], req["Direction"])
```

The same `process_strategy` drives the backtester (`quant.backtest.Backtester`)
and the live trader (`quant.live.UnifiedTrader`); only the executor differs.

## Extension points

- **New strategy**: implement `StrategyProtocol` (`calculate_indicators`,
  `generate_signals`, `manage_trade` + a `BaseStrategyConfig`). Nothing else
  changes; the entry gate and manage contract already hold.
- **New signal / asset class**: register it in `SIGNAL_REGISTRY` and provide an
  `ExecutorProtocol` implementor for its asset class (equity-only today).
- **Data source**: implement `DataSourceProtocol` for `get_historical_data`;
  the engine stays deterministic as long as bars are adjusted + bar-driven.
- **Observability**: pass `monitor_emit=` — the pipeline is telemetry-neutral
  (identical decisions, emits nothing) with no hook.
- **New executor**: implement `ExecutorProtocol`; divergences between backtest
  and live are then attributable only to your executor, not the engine.