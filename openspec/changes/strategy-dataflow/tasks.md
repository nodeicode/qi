# Tasks

**Commit contract — every task is one commit (or a small coherent batch):**

1. The commit **deletes the legacy mechanism it replaces** in the same commit
   (no `mode=` switches, no opt-in paths, no dead legacy code kept).
2. The **full suite and lint run green on that commit**: `uv run test`
   (60%+ coverage) and `uv run ruff check .` / `uv run ruff format .`.
3. Tests for the changed behavior are migrated **in the same commit**.
4. Commit message states what was reshaped and what was deleted.

No commit may regress the suite. If a step cannot land green in one commit,
split it — never layer a parallel path.

## 1. Instrument spine — routing and contract facts on typed instruments

- [ ] 1.1 Add the typed `Order` primitive (`quant/order.py`: side, quantity, instrument, venue, price context, timestamps) with canonical `to_schema`/`from_schema`; verify a schema round-trip test (spec `execution` `Typed order primitive`)
- [ ] 1.2 Put contract facts (multiplier, tick, lot, price precision, margin) and `make_order(side, qty, ...) -> Order` on the typed instruments (`quant/instruments/base.py`, equity/future/option); fold in `ContractSpec` data+math from `quant/core/_utils.py`; delete `ContractSpec`/`ProductSpecRegistry`/`PRODUCT_SPECS` so contract facts have one source (spec `instruments` `Typed instrument seed`)
- [ ] 1.3 Extend `InstrumentConformance` to the order-construction surface; verify it accepts `EquityInstrument` and rejects an instrument missing `make_order`/contract facts (spec `instruments` `Instrument conformance`)
- [ ] 1.4 Remove signal-string routing in place: replace `TradingEngine.route`/`direction` and `SIGNAL_REGISTRY`/`signal_asset_class`/`signal_direction` usage with typed-instrument resolution (spec `execution` `Asset-class routing` REMOVED); verify `OptionInstrument` on `FutureInstrument` underlying sizes/orders against both with no asset-class inference (spec `instruments`)

## 2. Data port — typed subscriptions, bars first

- [ ] 2.1 Add the per-`(instrument, datatype, cadence)` subscription surface to the data port (`quant/core/protocols.py`, `quant/backtest/backtest_data_handler.py`, `quant/data/synthetic_data_handler.py`, live handler/cache); ship the `bars` datatype; verify a bars subscription resolves on both backtest and live roots with identical typed shape (spec `data-access` `Data source protocol`)
- [ ] 2.2 Implement backtest bars delivery as replay of pre-loaded frames (`Backtester._prefetch_data` feeds subscription buffers; per-bar loop consumes the buffer — no re-parse); verify out-of-range requests return empty streams without raising (spec `data-access` `Backtest views`)
- [ ] 2.3 Generalize registration/discovery + conformance (`quant/data/_registry.py`, `quant/tests/extensions/test_conformance_data.py`) to the typed offer/fulfillment contract; verify a conforming custom source passes against both roots and a violating one fails loudly (spec `data-access` `Data-source registration and discovery` / `Data-source conformance across environments`)

## 3. Strategy authoring surface

- [ ] 3.1 Replace `StrategyProtocol` (`quant/core/protocols.py`) with the declarative surface (author body `decide(events, projection)` over declared instruments/subscriptions/venue); reshape `TradingEngine.__init__` validation (drop the 3-method check, `INDICATOR_COLUMNS`, ticker-kwarg inspection); verify a bar-driven strategy is constructed and declarations resolve (spec `strategy-authoring` `Declarative strategy surface`)
- [ ] 3.2 Implement strategy-owned sizing: delete `SIZING_STRATEGIES`/`SizingRegistry`/`size_for_signal`/`size_for_asset` (`quant/core/sizing.py`) and their call sites (`quant/backtest/executors/equity.py`, `quant/live/executors/unified_executor.py`); fold the sizing math into the author body consuming instrument contract facts + the injected read-only account projection; verify a futures strategy sizes from margin/multiplier, not a generic fraction (spec `strategy-authoring` `Sizing is delegated`)
- [ ] 3.3 Build typed orders through `instrument.make_order` and submit to the venue; verify fill reporting feeds the portfolio/account projection (spec `strategy-authoring` `Venue is the order seam`)
- [ ] 3.4 Rewrite the strategy registry/loader + config validation (`quant/strategies/__init__.py`, `quant/live/strategy_loader.py`, `quant/utils/strategy_config.py`): drop `INTERVAL`/`MANAGE_INTERVAL`/`CAPITAL_PER_TRADE`/`TICKERS`/option-strike knobs from `BaseStrategyConfig`; add `instruments`/`subscriptions`/`venue` declarations; verify an unknown venue/strategy is rejected at construction and a legacy 3-method strategy is clearly rejected — no migration shim (spec `strategy-sdk` `Config validation` / `Strategy registry and dynamic loading`)

## 4. Venue as the execution seam

- [ ] 4.1 Reshape backtest fills into the backtest venue's execution client (`quant/venue/backtest.py`), absorbing `ExecutorRouter`/`EquityExecutor`/`BacktestExecContext` (`quant/backtest/executors/*`); verify fills are simulated on closed/tradable data and gated (spec `execution` `Backtest fills`)
- [ ] 4.2 Reshape the live path into the live venue's execution client (`quant/live/executors/*`, `quant/live/trader.py` loop); verify gated submission and partial-fill tracking into the position schema (spec `execution` `Live orders through venue-bound clients`)
- [ ] 4.3 Delete `ExecutorProtocol` (`should_run`, `data_end_dt`, `get_precomputed_signal`, `enter`, `record_run`; `quant/core/protocols.py`) and the `ExecutorRouter` asset-class dispatch; move conformance (`test_conformance_executor.py`, `test_conformance_venue.py`) onto the venue seam; verify a custom venue passes the execution-port suite against both roots and a contract-violating one fails (spec `execution` `Executor protocol contract` / `Executor conformance for custom implementations`)

## 5. Engine harness — determinism/replay/parity

- [ ] 5.1 Reshape `process_strategy` (class name `TradingEngine` unchanged; `executor` param becomes the venue): delete `run_signals`/`latest_signal`/`cache_signals`/`route`/`direction`/`is_actionable`/`should_enter`/`build_position`; replace `_signal_cache` with per-subscription `_buffers`; feed the typed event stream to the author body and record the deterministic decision transcript; verify backtest and live drive the same transformed path and only the venue differs (spec `decision-engine` `TradingEngine reshaped in place` / `One deterministic decision entry point` / `Decision transcript`)
- [ ] 5.2 Delete `INTERVAL`/`MANAGE_INTERVAL` gates, `executor.should_run`/`record_run`/`data_end_dt`/`get_precomputed_signal`, the `fetch_data`/`fetch_manage_data` calls, and the engine phasing; drive scheduling by per-subscription cadence on closed data; verify manage-only steps skip signal work and signal steps run the body (spec `decision-engine` `Bar-driven scheduling`)
- [ ] 5.3 Inject the read-only portfolio/account projection into the author body; verify sizing reads current equity/exposure and the strategy cannot mutate the portfolio (spec `decision-engine` + `portfolio` `Read-only account projection`)
- [ ] 5.4 Repoint `quant/tests/backtest/_replay_runner.py`, `test_determinism_two_process.py`, `test_golden_regression.py` onto the (event stream + strategy body + venue) transcript of the transformed path; verify two-process determinism and golden regression pass on the new transcription (spec `decision-engine` `Decision transcript`)
- [ ] 5.5 Update the conformance suites (`quant/tests/extensions/test_conformance_*.py`) to the new seams; verify data, venue, and instrument suites run against both backtest and live roots (spec `data-access` / `execution` / `instruments`)

## 6. Migrate the reference strategy and land internals

- [ ] 6.1 Rewrite `example_equity` against the declarative surface (declared instruments/subscriptions/venue, author body, strategy-owned sizing); verify it runs in backtest through the reshaped pipeline and matches the pre-change entry behavior via the golden comparison (spec `strategy-sdk` `Strategy registry and dynamic loading`)
- [ ] 6.2 Land the wiring: `quant/strategies` registry/loader, `quant/backtest/backtester.py`, executors, and `quant/live/trader.py` on the new seams; delete the remaining legacy path pieces (signal cache/warmup reshape, indicator-injection contract removal per spec `strategy-sdk` `Indicator injection contract` REMOVED); verify `uv run test` green, `uv run ruff check .` clean
- [ ] 6.3 Keep specs and internals the source of truth: run `openspec validate strategy-dataflow`; verify the change's spec deltas match the implemented behavior — no docs-first drift (user-facing correctness docs stay deferred to the roadmap)

## Workflow follow-up

- Re-derive the user-facing correctness material (whitepaper, evaluation methodology) in a later roadmap task once the seams are stable (tracked in `docs/roadmap.md`).
- Archive this change after review; verify the archived deltas merged into the main specs.
