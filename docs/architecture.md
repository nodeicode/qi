# Architecture — the one pipeline, and the primitives behind it

Qi's core claim is **parity by construction**: the same decision pipeline runs
byte-for-byte in backtest and live, so research results are truthful previews
of live behavior. This doc explains the shape that makes that true — the one
pipeline, the environment seam, and the closed set of primitives it composes.
For the deeper invariants and verification detail, see
`docs/whitepaper-backtest-live-drift.md` and `docs/evaluation-methodology.md`.

## 1. The one pipeline

```
strategy ──▶ TradingEngine.process_strategy / process_ticker
                │  indicators → signals → gate → sizing → canonical position request
                ▼
             ExecutorProtocol   ◀── the ONLY environment difference
                │
        ┌───────┴────────┐
   BacktestExecutor   LiveExecutor
   (SimulatedFills)   (venue fills) ──▶ Portfolio (fills-only state machine)
```

- **Engine** (`quant.core.engine.TradingEngine`) owns every decision:
  signal generation, entry gating, sizing, `manage_trade`, and the canonical
  position request — identical in both environments.
- **Executor** (`ExecutorProtocol`) is the single environment seam. In backtest
  the executor simulates fills; live it streams real venue fills. Because the
  seam is *only* there, drift is attributable to an executor, not the engine.
- **Determinism** is enforced architecturally and by test: two-process golden
  replay (`test_determinism_two_process.py`) plus golden regression
  (`test_golden_example_equity.py` / `test_golden_regression.py`) prove the
  decision sequence is a pure function of (strategy, config, bars, executor
  events).

## 2. The invariants that hold it together

1. **One pipeline** — backtest and live both run `process_strategy` /
   `process_ticker`; only the executor differs.
2. **Adjusted bars** — signals run on adjusted bars only; `Raw_Close` is
   backtest-only execution.
3. **Bar-driven scheduling** — `INTERVAL` (signal) and `MANAGE_INTERVAL`
   (manage) gates drive the tick; no intra-`generate_signals` data fetches or
   ad-hoc clocks.
4. **Determinism** — engine decisions are a pure function of inputs, guarded by
   the golden two-process test.
5. **Import layering is enforced by a test** — `quant.utils` is a leaf;
   `quant.backtest` / `quant.strategies` are terminal leaves; a sanctioned
   cross-leaf import carries an inline `# layering-ok`.

## 3. The closed primitives (counted set)

Qi exposes a small, closed set of primitives (D11). Membership is derived by
test, not taste:

| # | Primitive | Home | Parity-sensitive |
|---|---|---|---|
| 1 | venue | `quant.venue` | yes |
| 2 | portfolio | `quant.portfolio` | yes |
| 3 | position | `quant.portfolio` | yes |
| 4 | instrument | `quant.instruments` | yes (ontology root) |
| 5 | observability | `quant.observability` | no (universal) |
| 6 | config | `quant.config` | no (universal) |
| 7 | data | `quant.data` / `DataSourceProtocol` | no (universal) |
| 8 | engine | `quant.core.engine` | yes |

**Replaceable** (guarded by conformance): venue, data source, executor,
instrument. **Pinned**: engine decisions and portfolio/position accounting —
this is where parity lives, so it is the least replaceable. Anything narrow,
non-universal, or alpha-specific (caches, clocks, the strategy itself) is
deliberately **not** a primitive.

## 4. Moving parts and where they live

| Area | Path |
|---|---|
| Decision engine, protocols, sizing | `quant/src/quant/core/` |
| Data source + MemBars fast path + cache | `quant/src/quant/data/` |
| Backtest driver, fill simulation, metrics | `quant/src/quant/backtest/` |
| Strategy registry / entry points + reference strategy | `quant/src/quant/strategies/` |
| Trader loop, executors, monitor, config, session | `quant/src/quant/live/` |
| Runtime-config backends | `quant/src/quant/config/` |
| Portfolio / position state machines | `quant/src/quant/portfolio/` |
| Venue (broker + session unit) | `quant/src/quant/venue/` |
| Instrument ontology | `quant/src/quant/instruments/` |
| Spans + emitter | `quant/src/quant/observability.py` |

## 5. Concurrency / live shape

- The live trader polls a **passive, version-checked config** once per tick
  (`ConfigSync`); applied changes land between ticks, no restarts (spec
  `runtime-config`).
- Config backends are **local-first** (file / env) — zero AWS by default — with
  SSM as an opt-in prod backend (`quant[ssm]`), resolved lazily so
  `import quant.config` never imports boto3.
- Live state (portfolio snapshot, open positions) is persisted and recovered on
  restart (`trader_state.json`).

## 6. Extending without forking

Extensibility is protocol seams + registry lines — see `docs/extensibility.md`
for runnable recipes (add strategy / data source / venue / executor / build on
observability) and the `agents/*.md` index in `AGENTS.md` for each subsystem's
invariants and extension points.

## Verification surface

- `test_determinism_two_process.py` — identical decisions across two processes.
- `test_golden_example_equity.py` / `test_golden_regression.py` — frozen
  end-to-end output for the reference strategy / a synthetic strategy.
- `test_import_layering.py` — the one-way dependency contract.
- The library's offline path (`Backtester` + `SyntheticDataHandler`) exercises the
  pipeline against synthetic bars — see `docs/quickstart.md`.