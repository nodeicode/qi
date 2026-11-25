# Design — reshape the existing pipeline into strategy-as-dataflow

## Context

The current decision path (`quant/core/engine.py` `process_strategy` →
`process_ticker` → `route`/`should_enter` → `ExecutorRouter.enter` →
`EquityExecutor`/`LiveExecutor` → `size_for_signal`) is one shared loop with a
string-keyed routing spine (`SIGNAL_REGISTRY` → `SIZING_STRATEGIES` →
asset-class executor registry). The strategy only ever sees DataFrames and
returns strings; it cannot declare instruments, subscriptions, or account
state. This design reshapes that exact loop in place so the strategy becomes
the author of its own decisions and the venue is the only difference between
backtest and live — with no legacy branch left behind.

**Commit contract (applies to every task):** each task is one commit (or a
small coherent batch). Every commit must (a) delete the legacy mechanism it
replaces in the same commit, (b) pass the full `uv run test` suite and
`uv run ruff check .` / `uv run ruff format .` on that commit, and (c) include
the test migration for the behavior it changed. No commit may introduce a
parallel "opt-in" path, a `mode=` switch, or keep dead legacy code — the suite
itself is migrated forward per commit so it stays green without legacy
branches.

## Goals

- One pipeline, reshaped in place: `process_strategy` is still the single
  entry point in both environments; backtest and live drive the identical
  transformed code path and differ only in the venue's execution client.
- The strategy declares typed instruments, typed subscriptions, and a venue,
  and owns its decisions and sizing over typed data + a read-only account
  projection.
- Routing, contract facts, and sizing have exactly one source each: typed
  instruments (contract facts, `make_order`), the author body (sizing,
  decisions).
- Determinism is pinned by replaying a recorded (event stream + body + venue)
  decision transcript on the transformed path.

## Decisions

### D1 — Reshape `TradingEngine` in place; the class keeps its name

The class stays `TradingEngine` at `quant.core.engine.TradingEngine` with
the same import path and public entry points (`process_strategy`, `warmup`);
only its internal pipeline is rewired. No renamed or parallel engine class
is introduced — `trader.py` and `backtester.py` keep constructing this one
class. `quant/core/engine.py` keeps its classes but every step is rewired:

- The per-ticker fetch (`fetch_data`/`fetch_manage_data`) resolves the
  strategy's declared subscriptions instead of `config.TICKERS` +
  `INTERVAL`/`MANAGE_INTERVAL`.
- `run_signals`/`generate_signals` are replaced by driving the author body
  over typed data events; `cache_signals`/`_signal_cache` become the event/
  frame buffer for the declared cadences.
- `route(signal)`, `direction(signal)`, `should_enter`/`check_entry_gate`
  (engine.py) and the `SIGNAL_REGISTRY` functions they call
  (`signal_asset_class`/`signal_direction`, core/_utils.py) are deleted:
  the instrument carries the type and the author body carries the gate
  decision.
- The loop records a deterministic **decision transcript** (see D7).

Rationale: the engine's shape — a schedule, a data step, a decision step, an
execute step — is correct; the coupling is in *what keys each step*. Evolving
the same class keeps byte-identical parity reasoning and deletes registration
tables instead of adding them.

### D2 — Typed instruments become the routing and contract-facts spine

- `quant/instruments/*` (base, equity, future, option, `_registry.py`) grow
  the **contract facts** (multiplier, tick, lot, price precision, margin) and
  an order-construction surface `make_order(side, qty, ...) -> Order`
  (`quant/order.py`, new: side/quantity/instrument/venue/price context/
  timestamps with a canonical schema round-trip).
- `ContractSpec`/`ProductSpecRegistry`/`PRODUCT_SPECS`
  (quant/core/_utils.py) are folded into the typed instruments — their data
  and math (round_price, pnl) survive on the instrument; the registry module
  references are deleted so contract facts have one source.
- `ExecutorRouter._asset_class_for_signal` / `_asset_class_for_position`
  (backtest/executors/router.py) and `TradingEngine.route` are deleted;
  routing starts from the typed instrument held by the strategy.
- `OptionInstrument.underlying` resolution works with no asset-class
  inference — an option on a `FutureInstrument` sizes/orders against both.

### D3 — Sizing moves into the strategy; the sizing registry is deleted

- `SIZING_STRATEGIES` / `SizingRegistry` / `size_for_asset` /
  `size_for_signal` (`quant/core/sizing.py`) are removed from the decision
  path. The sizing math in `EquitySizing`/`FuturesSizing`/… is folded into
  the author body (strategy-authoring, `Sizing is delegated`).
- The engine/executors stop calling `size_for_signal`
  (backtest/executors/equity.py `EquityExecutor.enter`,
  live/executors/unified_executor.py). A strategy sizes from instrument
  contract facts + the injected read-only Portfolio (D6) — a futures
  strategy uses margin/multiplier, not a generic capital fraction.
- The strategy returns a target quantity; `instrument.make_order` turns it
  into a typed `Order`.
- The same author body folds in the **exit side** of sizing: evaluating the
  portfolio's existing positions against the config risk fields
  (`stop_loss`/`take_profit`/`max_hold`) replaces the engine's
  `evaluate_manage_exit`, so `decide` is the single merged
  signal-and-manage function.

### D4 — The data port reshapes to typed subscriptions (bars first)

- `DataHandlerProtocol.get_historical_data(ticker, start, end, interval)`
  (quant/core/protocols.py) is generalized to a per-`(instrument, datatype,
  cadence)` **subscription** surface; `bars` is the shipped datatype.
- Implementations reshape accordingly: `quant/backtest/backtest_data_handler.py`
  (replay of pre-loaded frames — `Backtester._prefetch_data` feeds the
  subscription buffer, no re-parse per bar), `quant/data/synthetic_data_handler.py`,
  the live Alpaca handler/cache path (`quant/data/*`).
- Live delivery is **streaming into the same subscription buffers**
  (`quant/data/ws/aggregator.py` + `quant/data/ws/streams/*` append settled
  bars), so backtest replay and live stream fulfill the same buffer with an
  identical typed shape; seed/edge semantics bind per subscription.
- Out-of-range subscription windows return an **empty stream** without
  raising (replaces implicit frame-emptiness handling in the loop).
- `SIGNAL_REGISTRY`-driven discovery/conformance
  (`quant/tests/extensions/test_conformance_data.py`) is generalized to the
  typed offer/fulfillment contract against both roots.

### D5 — The venue is the only seam; executor surface is absorbed

- The existing `Venue` primitive (`quant/venue/base.py`: `data_source` +
  `execution_client` + calendar) is what both environments plug into.
- Backtest: the fill machinery in `ExecutorRouter`/`EquityExecutor`/
  `BacktestExecContext` (`quant/backtest/executors/*`) reshapes into the
  backtest venue's execution client (`quant/venue/backtest.py`): fills
  simulated on closed/tradable data and gated.
- Live: `LiveExecutor`/`UnifiedExecutor` (`quant/live/executors/*`) and the
  `trader.py` loop reshape into the live venue's execution client: gated
  submission, partial-fill tracking into the position schema.
- The driver resolves `config.venue` to a wired `Venue`
  (`quant/venue/_registry.py`): backtest → `BacktestVenue`, live → the
  registered live venue, each with `data_source` + `execution_client`; that
  venue object is what `process_strategy` receives.
- `ExecutorProtocol` (`should_run`, `data_end_dt`, `get_precomputed_signal`,
  `enter`, `record_run`; quant/core/protocols.py) is deleted — its concerns
  land on the venue (execution + session calendar), subscriptions
  (schedule), and the engine loop (transcript). Custom-execution tests
  (`test_conformance_executor.py`, `test_conformance_venue.py`) move to the
  venue seam against both roots.
- End-of-run forced-close and the equity-curve/metrics ledger live on the
  backtest venue (replacing `ExecutorRouter.force_close_remaining`);
  `Backtester.compute_equity_curve`/`calculate_performance_metrics` keep
  reading the run ledger.

### D6 — The Portfolio is injected read-only; no projection class

- The engine passes the `Portfolio` itself (`quant/portfolio/*`), as a
  read-only view, into `decide(events, portfolio)`. The strategy reads
  equity, available margin, open exposure, and its own existing open
  positions (for sizing and for evaluating exits). No separate
  `AccountProjection` class is introduced — the handed `portfolio` exposes
  no mutating surface, so the strategy cannot mutate it; fills remain
  venue-owned (spec `portfolio` ADDED requirement).

### D7 — Determinism is pinned on the decision transcript

- `process_strategy` records a deterministic transcript of the fed event
  stream + author decisions + venue effects (new `Decision transcript`
  requirement, spec `decision-engine`).
- `quant/tests/backtest/_replay_runner.py`, `test_determinism_two_process.py`,
  and `test_golden_regression.py` re-pin on that transcript over the
  transformed path. No legacy engine branch remains to pin — the suite
  already migrated with the engine (D1).

### D8 — Strategy SDK reshapes, breaking by contract

- `StrategyProtocol` (quant/core/protocols.py) becomes the declarative
  surface: declared instruments, subscriptions, venue + an author body; the
  return values are typed `Order`/exit intents, not signal strings.
- `BaseStrategyConfig` (quant/utils/strategy_config.py) drops
  `TICKERS`/`INTERVAL`/`MANAGE_INTERVAL`/`CAPITAL_PER_TRADE`-style flattened
  knobs in favor of the declarations; validation rejects unknown venues and
  unknown strategies **at construction**, and a legacy 3-method strategy is
  clearly rejected (there is no migration shim).
- `quant/strategies/__init__.py` registry/loader, `quant/live/strategy_loader.py`,
  and `quant/live/trader.py` construction paths reshape to the declarative
  surface; `example_equity` is rewritten as the reference strategy and must
  run in backtest with entry behavior matching the pre-change reference
  (golden comparison), while the deterministic guarantee now comes from the
  transcript.

## Risks / Trade-offs

- **[R] Free-form author bodies weaken determinism** → Mitigated by the
  transcript + two-process replay on the transformed path; cadence comes from
  subscriptions, not ad-hoc clocks.
- **[R] No-compat means every consumer must move at once** → Accepted, per
  the change's contract; the suite moves with each commit so the tree is
  never red and there is no co-existence burden.
- **[R] Reshaping live order submission touches a guarded surface** → Live
  client behavior (gated submission, partial fills) keeps dedicated tests
  (`quant/tests/live/unit/executors/*`) migrated against the venue client.
- **[R] Adjusted-bar / no-lookahead discipline** → Retains force: signals
  still run on adjusted bars only; `Raw_Close` stays backtest-only execution;
  the invariant binds per datatype as datatypes are added.

## Migration plan (order of work)

1. Instrument spine: `Order` primitive; contract facts + `make_order` on the
   instruments (fold in `ContractSpec`); order-surface conformance; then the
   in-place routing swap (delete `SIGNAL_REGISTRY` from the engine/router).
2. Data port: typed subscriptions (bars), backtest replay views,
   offer/fulfillment conformance.
3. Strategy authoring: declarative surface, strategy-owned sizing, order
   path to the venue, registry/loader + validation rewrite.
4. Venue seam: backtest fill venue, live venue client, conformance on the
   venue seam; `ExecutorProtocol` deleted.
5. Engine harness: event-stream feeding + transcript, per-subscription
   cadence scheduling, read-only projection; goldens and conformance
   re-pinned on the transformed path.
6. Land: `example_equity` rewritten + runnable; wiring (registry/loader,
   backtester, live trader) green; `openspec validate` coherent.
