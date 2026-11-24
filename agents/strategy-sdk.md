# Strategy authoring — `StrategyProtocol` + the `quant.strategies` registry (spec `strategy-sdk`, design D8)

A strategy is **one class + one registry line**, and it runs byte-for-byte
identical in backtest and live because it only ever faces the shared engine in
`quant.core.engine.TradingEngine`. You never write a backtest loop or a live
loop; you implement the protocol, and the engine + executor seam do the rest.
The public reference is `example_equity` (`quant.strategies.example_equity`).

## Invariants (do not break without discussion)

1. **Implement the protocol exactly.** A strategy MUST have a `config` that is
   a `BaseStrategyConfig` instance and three methods:
   `calculate_indicators(df, **kw) -> df`, `generate_signals(df) -> df`, and
   `manage_trade(position, df) -> int`. The engine REJECTS a strategy missing
   any of these at construction with a `TypeError` naming the missing method
   (spec scenario).
2. **Config is the contract.** `config` MUST subclass `BaseStrategyConfig`; an
   invalid config (wrong type, or a bad field value) fails at strategy/engine
   construction — never silently at decision time (spec scenario).
3. **Signals drive decisions; you never touch clocks.** `generate_signals`
   returns `Signal` / `Confidence` / `Reason` columns for every bar. Scheduling
   (`INTERVAL`, `MANAGE_INTERVAL`) and `DATA_DELAY` come from config; no
   intra-`generate_signals` data fetches, no ad-hoc clocks.
4. **Adjusted bars only.** Decision frames are adjusted bars in both
   environments. `Raw_Close` is backtest-only execution, never readable on the
   live decision path.
5. **Indicators are injected, not re-fetched.** Columns you declare in
   `INDICATOR_COLUMNS` are computed once and injected into manage frames; the
   engine caches signal output per ticker.

## Where it lives

| Path | Responsibility |
|---|---|
| `quant/src/quant/strategies/__init__.py` | `get_strategy(name)` — resolves built-ins then the `quant.strategies` entry-point group; `STRATEGY_REGISTRY` |
| `quant/src/quant/strategies/example_equity/` | the reference strategy (protocol reference + config subclass) |
| `quant/src/quant/utils/strategy_config.py` | `BaseStrategyConfig` (field validation) |
| `quant/src/quant/core/engine.py` | `TradingEngine` — protocol enforcement + signal pipeline |
| `quant/tests/extensions/test_strategy_entry_points.py` | stub-plugin distribution resolves via entry points with no SDK change |

Registering under the `quant.strategies` **entry-point group** (in your own
distribution's `pyproject.toml`) makes a strategy resolvable by name with **no
SDK change** — the same mechanism task 6.3 verifies.

## Quickstart — add a strategy (run against the tests)

The exact block below is reproduced in `quant/tests/extensions/
test_strategy_sdk_example.py` (task 6.4), so it runs against the suite. This is
a fast>slow moving-average strategy:

```bash
uv run python - <<'PY'
import pandas as pd
from dataclasses import dataclass, field

from quant.backtest.backtester import Backtester
from quant.core.engine import TradingEngine
from quant.core.protocols import PositionDict
from quant.strategies import STRATEGY_REGISTRY, get_strategy
from quant.utils.strategy_config import BaseStrategyConfig


# 1. Config — subclass BaseStrategyConfig and give every required field a value.
@dataclass
class DemoMAConfig(BaseStrategyConfig):
    INTERVAL: str = "1d"
    MANAGE_INTERVAL: str = "1d"
    PRE_LOAD_DAYS: int = 60
    INITIAL_CAPITAL: float = 100_000.0
    CAPITAL_PER_TRADE: float = 0.5
    MAX_CONCURRENT_POSITIONS: int = 1
    STOP_LOSS_PERCENT: float = 0.05
    TAKE_PROFIT_PERCENT: float = -1.0
    USE_TRAIL_LOSS: bool = False
    MAX_HOLDING_MINUTES: int = 60 * 24 * 30
    MIN_SIGNAL_CONFIDENCE: float = 0.0
    BASE_SLIPPAGE_FACTOR: float = 0.001
    TICKERS: list = field(default_factory=lambda: ["SPY"])
    DATA_DELAY: int = 0
    FAST_WINDOW: int = 5
    SLOW_WINDOW: int = 20


# 2. The strategy — the protocol + the contained logic.
class DemoMAStrategy:
    name = "demo_ma"
    INDICATOR_COLUMNS = ["FAST", "SLOW"]

    def __init__(self, config=None):
        self.config = config or DemoMAConfig()

    def calculate_indicators(self, df, **kw):
        out = df.copy()
        out["FAST"] = out["Close"].rolling(self.config.FAST_WINDOW).mean()
        out["SLOW"] = out["Close"].rolling(self.config.SLOW_WINDOW).mean()
        return out

    def generate_signals(self, df):
        out = df.copy()
        out["Signal"] = "NEUTRAL"
        out["Confidence"] = 0.0
        out["Reason"] = ""
        if out.empty or "SLOW" not in out.columns:
            return out
        mode = out["FAST"] > out["SLOW"]
        out.loc[mode, "Signal"] = "BUY"
        out.loc[mode, "Confidence"] = 1.0
        out.loc[mode, "Reason"] = "fast>slow"
        return out

    def manage_trade(self, position: PositionDict, df):
        return -1


# 3. One registry line → resolvable by name (and via an entry point, too).
STRATEGY_REGISTRY["demo_ma"] = DemoMAStrategy
assert get_strategy("demo_ma") is DemoMAStrategy

# 4. A deterministic, offline bar source (no keys / network) to drive it.
class FakeDataHandler:
    def get_historical_data(self, ticker, start, end, interval,
                            use_cache=True, adjustment=None):
        import numpy as np
        idx = pd.date_range("2023-06-01", periods=300, freq="B")
        close = 100.0 + 25.0 * np.sin(np.arange(len(idx)) / 12.0)
        return pd.DataFrame({"Open": close - 0.5, "High": close + 1.0,
                             "Low": close - 1.0, "Close": close,
                             "Volume": [1_000_000] * len(idx)}, index=idx)

# 5. Run it through the shared engine → and an offline end-to-end backtest.
engine = TradingEngine(DemoMAStrategy(), FakeDataHandler())
_cfg = DemoMAConfig()
bt = Backtester(_cfg, DemoMAStrategy(_cfg), "2024-01-02", "2024-06-28",
                "SPY", data_handler=FakeDataHandler())
trades = bt.run_backtest()
bt.compute_equity_curve()
assert len(trades) >= 1 and not bt.equity_curve.empty
print("demo_ma tradable, trades:", len(trades))
PY
```

(The `import pandas as pd` is assumed at the top of the block; the snippet is a
condensation — the full, runnable version mirroring this is
`test_strategy_sdk_example.py`, which also guards the spec scenarios.)

A missing `manage_trade`, or a `config` that is not a `BaseStrategyConfig`,
raises a `TypeError` naming the offending member — verified by that test file.

## Extension points

- **New strategy** — implement the protocol + a `BaseStrategyConfig` subclass
  and add one registry line (or an entry point under `quant.strategies`).
  Nothing else changes; entry gating, sizing, and the manage contract already
  hold.
- **Cross-asset signals** — accept an optional `ticker` kwarg in
  `calculate_indicators`; the engine calls it with the ticker automatically.
- **Indicator injection** — declare `INDICATOR_COLUMNS`; the engine computes
  them once and binds them into manage frames.
- **Distribution/extras** — for a *new asset class* (beyond equity), the seam
  is a registered signal + an `ExecutorProtocol` implementor; the strategy path
  itself is unchanged.

## Verify

- `uv run test` — the reference + the add-a-strategy example tests are part of
  the single suite.
- `uv run pytest quant/tests/extensions/test_strategy_sdk_example.py -q` — the
  doc example's guarded tests pass (engine construction, signal pipeline,
  registry, offline backtest round-trip).