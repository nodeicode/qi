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

- [x] 1.1 Add the typed `Order` primitive (`quant/order.py`: side, quantity, instrument, venue, price context, timestamps) with canonical `to_schema`/`from_schema`; verify a schema round-trip test (spec `execution` `Typed order primitive`)
- [x] 1.2 Put contract facts (multiplier, tick, lot, price precision, margin) and `make_order(side, qty, ...) -> Order` on the typed instruments (`quant/instruments/base.py`, equity/future/option); fold in `ContractSpec` data+math from `quant/core/_utils.py`; delete `ContractSpec`/`ProductSpecRegistry`/`PRODUCT_SPECS` so contract facts have one source (spec `instruments` `Typed instrument seed`)
- [x] 1.3 Extend `InstrumentConformance` to the order-construction surface; verify it accepts `EquityInstrument` and rejects an instrument missing `make_order`/contract facts (spec `instruments` `Instrument conformance`)
- [ ] 1.4 Remove signal-string routing in place: replace `TradingEngine.route`/`direction` and `SIGNAL_REGISTRY`/`signal_asset_class`/`signal_direction` usage with typed-instrument resolution (spec `execution` `Asset-class routing` REMOVED); verify `OptionInstrument` on `FutureInstrument` underlying sizes/orders against both with no asset-class inference (spec `instruments`)

## 2. Data port — typed subscriptions, bars first, live streaming

- [x] 2.1 Add the per-`(instrument, datatype, cadence)` subscription surface to the data port (`quant/core/protocols.py`, `quant/backtest/backtest_data_handler.py`, `quant/data/synthetic_data_handler.py`, live handler/cache); ship the `bars` datatype; verify a bars subscription resolves on both backtest and live roots with identical typed shape (spec `data-access` `Data source protocol`)
- [x] 2.2 Implement backtest bars delivery as replay of pre-loaded frames (`Backtester._prefetch_data` feeds subscription buffers; per-bar loop consumes the buffer — no re-parse); verify out-of-range requests return empty streams without raising (spec `data-access` `Backtest views`)
- [x] 2.3 Generalize registration/discovery + conformance (`quant/data/_registry.py`, `quant/tests/extensions/test_conformance_data.py`) to the typed offer/fulfillment contract; verify a conforming custom source passes against both roots and a violating one fails loudly (spec `data-access` `Data-source registration and discovery` / `Data-source conformance across environments`)
- [x] 2.4 Implement live subscription streaming into the engine buffers: `quant/data/ws/aggregator.py` + `quant/data/ws/streams/*` append settled bars into the per-subscription `_buffers` (no per-bar re-fetch/parse); verify settle/edge/seed semantics and that stream + replay fulfill the same buffer with identical typed shape (spec `data-access` `Live subscription streaming to buffers`)

## 3. Strategy authoring surface

- [x] 3.1 Replace `StrategyProtocol` (`quant/core/protocols.py`) with the declarative surface: single author body `decide(events, portfolio)` that BOTH generates entries and evaluates the portfolio's existing positions for exits (folding the `generate_signals` + `manage_trade`/`evaluate_manage_exit` roles); reshape `TradingEngine.__init__` validation (drop the 3-method check, `INDICATOR_COLUMNS`, ticker-kwarg inspection); verify a bar-driven strategy is constructed and declarations resolve (spec `strategy-authoring` `Declarative strategy surface`)
- [x] 3.2 Implement strategy-owned sizing + risk exits in the author body: delete `SIZING_STRATEGIES`/`SizingRegistry`/`size_for_signal`/`size_for_asset` (`quant/core/sizing.py`) and their call sites (`quant/backtest/executors/equity.py`, `quant/live/executors/unified_executor.py`); fold sizing into `decide` from instrument contract facts + the read-only Portfolio, and fold stop/take/max-hold evaluation (from the config risk fields) into `decide`'s manage pass; verify a futures strategy sizes from margin/multiplier and risk exits fire on the same conditions as the old engine (spec `strategy-authoring` `Sizing is delegated` / `Reference strategy`)
- [x] 3.3 Build typed orders through `instrument.make_order` and submit to the venue via `decide`'s return; verify venue fills feed the Portfolio (spec `strategy-authoring` `Venue is the order seam`)
- [ ] 3.4 Rewrite the strategy registry/loader + config validation (`quant/strategies/__init__.py`, `quant/live/strategy_loader.py`, `quant/utils/strategy_config.py`): drop `INTERVAL`/`MANAGE_INTERVAL`/`CAPITAL_PER_TRADE`/`TICKERS`/option-strike knobs; add `instruments`/`subscriptions`/`venue` declarations (keep risk/execution fields for the author); verify an unknown venue/strategy is rejected at construction and a legacy 3-method strategy is clearly rejected — no migration shim (spec `strategy-sdk` `Config validation` / `Strategy registry and dynamic loading`)

## 4. Venue as the execution seam (incl. resolution, forced-close, metrics)

- [ ] 4.1 Reshape backtest fills into the backtest venue's execution client (`quant/venue/backtest.py`), absorbing `ExecutorRouter`/`EquityExecutor`/`BacktestExecContext` (`quant/backtest/executors/*`): fills on closed/tradable data, gated; end-of-run forced-close replaces `ExecutorRouter.force_close_remaining`; the run ledger keeps feeding `Backtester.compute_equity_curve`/`calculate_performance_metrics` (spec `execution` `Backtest fills`)
- [ ] 4.2 Reshape the live path into the live venue's execution client (`quant/live/executors/*`, `quant/live/trader.py` loop); verify gated submission and partial-fill tracking into the position schema, with fills feeding the Portfolio (spec `execution` `Live orders through venue-bound clients`)
- [ ] 4.3 Implement venue resolution from `config.venue`: backtester/trader resolve the declared id to a wired `Venue` (`quant/venue/_registry.py`, `BacktestVenue`/live venue with `data_source` + `execution_client`) and pass it to `process_strategy`; unknown id rejected at construction (spec `execution` `Venue resolved from the strategy declaration`)
- [ ] 4.4 Delete `ExecutorProtocol` (`should_run`, `data_end_dt`, `get_precomputed_signal`, `enter`, `exit`, lifecycle hooks, `record_run`; `quant/core/protocols.py`) and the `ExecutorRouter` asset-class dispatch; move conformance (`test_conformance_executor.py`, `test_conformance_venue.py`) onto the venue seam against both roots (spec `execution` `Executor protocol contract` / `Executor conformance for custom implementations`)

## 5. Engine harness — determinism/replay/parity

- [ ] 5.1 Reshape `process_strategy` (class name `TradingEngine` unchanged; `executor` param becomes the venue): delete `run_signals`/`latest_signal`/`cache_signals`/`route`/`direction`/`is_actionable`/`should_enter`/`build_position`; replace `_signal_cache` with per-subscription `_buffers`; feed the typed event stream + read-only Portfolio to `decide` and record the deterministic decision transcript; verify backtest and live drive the same transformed path and only the venue differs (spec `decision-engine` `TradingEngine reshaped in place` / `One deterministic decision entry point` / `Decision transcript`)
- [ ] 5.2 Delete `INTERVAL`/`MANAGE_INTERVAL` gates, `executor.should_run`/`record_run`/`data_end_dt`/`get_precomputed_signal`, the `fetch_data`/`fetch_manage_data` calls, and the engine phasing; drive scheduling by per-subscription cadence on closed data PLUS the venue's session calendar (`is_open_at`/forced-close); verify manage-only steps skip signal work, signal steps run the body, and closed sessions gate runs (spec `decision-engine` `Bar-driven scheduling`)
- [ ] 5.3 Inject the `Portfolio` itself as read-only into `decide` (no projection class); verify sizing reads equity/margin/exposure AND the author evaluates its open positions for exits, and the strategy cannot mutate the portfolio (spec `decision-engine` + `portfolio` `Portfolio injected read-only into the author body`)
- [ ] 5.4 Re-pin `quant/tests/backtest/_replay_runner.py`, `test_determinism_two_process.py`, `test_golden_regression.py` onto the (event stream + strategy body + venue) transcript of the transformed path; verify two-process determinism and golden regression pass on the new transcription (spec `decision-engine` `Decision transcript`)
- [ ] 5.5 Update the conformance suites (`quant/tests/extensions/test_conformance_*.py`) to the new seams; verify data, venue, and instrument suites run against both backtest and live roots (spec `data-access` / `execution` / `instruments`)
- [ ] 5.6 Re-define the engine telemetry span set for the author body: `DECIDE`/submission spans through `quant/observability.py`, removing dangling references to the deleted `RUN_SIGNALS`/`PROCESS_TICKER`/`EXECUTION_DISPATCH` spans; verify parity-telemetry neutrality (identical run with/without an emit hook) (spec `decision-engine` `Author-body observability`)

## 6. Migrate the reference strategy and land internals

- [ ] 6.1 Rewrite `example_equity` against the declarative surface exactly as scoped in `strategy-authoring` `Reference strategy`: declared `EquityInstrument("SPY", contract=EquityContract(price_precision=2, lot=1))`, `(SPY, BARS, "1h")` subscription, a venue, `decide` computing the 10/30 MA cross, evaluating its portfolio positions for stop/max-hold/cross-down exits, and sizing on the instrument lot + `portfolio` buying power (not `CAPITAL_PER_TRADE`/fraction); verify it runs in backtest through the reshaped pipeline and the entry sequence matches the pre-change `example_equity` golden (spec `strategy-authoring` `Reference strategy`)
- [ ] 6.2 Land the wiring + enumerated legacy teardown: update `quant/strategies` registry/loader, `quant/backtest/backtester.py`, executors, and `quant/live/trader.py` (venue resolution included); DELETE/rewrite the load-bearing legacy tests in the same commits — `test_engine.py`, `test_sizing.py`, `test_bar_selection.py`, `test_breach_cbbo_partial.py`, live unit tests on `LiveExecutor`/`UnifiedExecutor` (`quant/tests/live/unit/executors/*`), `test_group9_equity_e2e.py`, `test_conformance_executor.py`, and the `SIGNAL_REGISTRY`/`inject_indicators` fixtures in `test_deps.py`/`test_utils.py` — removing the indicator-injection contract (spec `strategy-sdk` `Indicator injection contract` REMOVED); verify `uv run test` green, `uv run ruff check .` clean
- [ ] 6.3 Keep specs and internals the source of truth: run `openspec validate strategy-dataflow`; verify the change's spec deltas match the implemented behavior — no docs-first drift (user-facing correctness docs stay deferred to the roadmap)
- [ ] 6.4 Ship the end-to-end parity capstone: upgrade `quant/tests/backtest/test_all_primitives_parity.py` from the legacy 3-method engine path to the declarative path — run the **same** rewritten `example_equity` through the backtest venue AND a live venue (offline, same synthetic fixture) via `TradingEngine.process_strategy`; assert an identical decision transcript (only venue fills differ) and freeze/regenerate the golden artifact (spec `decision-engine` `End-to-end parity proof`)

## Workflow follow-up

- Re-derive the user-facing correctness material (whitepaper, evaluation methodology) in a later roadmap task once the seams are stable (tracked in `docs/roadmap.md`).
- Archive this change after review; verify the archived deltas merged into the main specs.