# Proposal

## Why

The strategy is a passive passenger of a monolithic engine. The coupling map
is a chain of **string-keyed lookups** that no single piece can break:

1. `StrategyProtocol` (`quant/core/protocols.py`) fixes a strategy as three
   DataFrame methods — `calculate_indicators` → `generate_signals` →
   `manage_trade` — and `generate_signals` emits `"BUY"/"SELL"/"NEUTRAL"`
   strings plus a confidence.
2. `TradingEngine` (`quant/core/engine.py`) is the only driver: it reads
   `INTERVAL`/`MANAGE_INTERVAL`/`TICKERS` off a flattened `BaseStrategyConfig`
   (`quant/utils/strategy_config.py`), fetches OHLCV by ticker string through
   `DataHandlerProtocol.get_historical_data(ticker, start, end, interval)`
   (`quant/core/protocols.py`), routes the signal string to an asset class via
   `SIGNAL_REGISTRY` (`quant/core/_utils.py` `SignalRegistry`), sizes via
   `SIZING_STRATEGIES`/`size_for_signal` (`quant/core/sizing.py`), and hands
   the decision to an `ExecutorProtocol` (`should_run`, `data_end_dt`,
   `get_precomputed_signal`, `enter`, …).
3. `ExecutorRouter` (`quant/backtest/executors/router.py`) dispatches
   `enter`/`exit` by `_asset_class_for_signal` — a second pass through the
   same string registry — to per-asset-class children (`EquityExecutor`),
   each of which calls `size_for_signal` again.
4. The live path (`quant/live/trader.py` → `LiveExecutor`/`UnifiedExecutor`)
   is a second, parallel consumer of the same engine, clock-gated by
   `should_run`.

Net effect: the strategy cannot express instruments, subscriptions, margins,
premiums, or portfolio state; each asset class requires edits across four
separate registries; and every new strategy must speak the engine's string
dialect. `ContractSpec`/`ProductSpecRegistry` (`quant/core/_utils.py`) already
hold futures contract facts but only for backtest P&L — invisible to the
strategy that needs them to size. The `Venue` primitive (`quant/venue/base.py`,
already a data-source + execution-client + calendar unit) is bypassed: the
engine reaches around it through `ExecutorProtocol`.

## What this change is

A **breaking, in-place reshape** of that pipeline into data-flow. Not a new
system beside the old one: each step rewires one existing mechanism and
deletes the one it replaces in the same commit, so the suite stays green at
every commit and there is never a "legacy default" next to a "new opt-in".

Target shape:

- **Strategy is an author-owned body** that declares typed instruments, typed
  data subscriptions `(instrument, datatype, cadence)`, and a venue, then
  decides over typed data events and an injected **read-only** portfolio/
  account projection. It sizes itself from instrument contract facts.
- **Routing starts from typed instruments.** `SIGNAL_REGISTRY`,
  `SIZING_STRATEGIES`, and the executor router's asset-class dispatch are
  deleted; the instrument *is* the type (options resolve their `underlying`).
- **Contract facts live on the instrument.** `ContractSpec`'s multiplier/
  tick/margin data and math move onto the typed instruments (single source);
  `make_order(side, qty, …)` builds typed `Order`s (`quant/order.py`).
- **The data port ships bars subscriptions.** `get_historical_data(ticker,
  interval)` becomes a per-`(instrument, datatype, cadence)` subscription;
  backtest fulfills it by replaying pre-loaded frames on the event stream.
- **Venue is the only seam.** Backtest and live both run the same reshaped
  `process_strategy`; fills/orders differ only in the venue's execution
  client. `ExecutorProtocol`'s engine-centric surface is absorbed/deleted.
- **Determinism moves to a transcript.** The two-process golden suite
  (`quant/tests/backtest/_replay_runner.py`, `test_determinism_two_process.py`,
  `test_golden_regression.py`) re-pins on the recorded (event stream + body +
  venue) decision transcript of the *transformed* path.

## Capabilities

- **NEW `strategy-authoring`** — the declarative surface: declarations, the
  author body, strategy-owned sizing, venue as the order seam.
- **`decision-engine`** — `process_strategy` reshaped into the harness that
  feeds the typed event stream and records the transcript; scheduling moves
  from `INTERVAL`/`MANAGE_INTERVAL` gates to per-subscription cadence;
  gating/sizing moves out of the engine into the author body.
- **`data-access`** — typed subscription surface (bars shipped); backtest
  replay views; offer/fulfillment conformance.
- **`execution`** — asset-class routing REMOVED; backtest fills and live
  orders reshaped into venue-bound clients; conformance on the venue seam.
- **`instruments`** — contract facts + `make_order` on typed instruments;
  conformance extended to the order-construction surface.
- **`portfolio`** — ADDED read-only account projection the strategy sizes
  against (mutation blocked by construction).
- **`strategy-sdk`** — 3-method protocol and registry/loader replaced by the
  declarative surface; unknown venue/strategy rejected at construction.
- **`venue`** — unchanged primitive (its `data_source` + `execution_client`
  shape is exactly what the seams plug into).

## Impact

- **Code (deleted or reshaped):** `quant/core/engine.py`
  (`route`, `direction`, `should_enter`, `INTERVAL` gates, sizing calls),
  `quant/core/_utils.py` (`SignalRegistry`, `SIGNAL_REGISTRY`,
  `signal_asset_class`, `signal_direction`, `check_entry_gate`,
  `inject_indicators`, `ContractSpec`/`ProductSpecRegistry` migration),
  `quant/core/sizing.py` (`SIZING_STRATEGIES`, `SizingRegistry`,
  `size_for_signal` — math folded into strategies), `quant/core/protocols.py`
  (`StrategyProtocol`, `ExecutorProtocol`, `DataHandlerProtocol`),
  `quant/backtest/executors/*` (router + asset-class children reshaped into
  the backtest venue), `quant/live/executors/*` (+ `trader.py` loop),
  `quant/data/*` + `quant/backtest/backtest_data_handler.py` (subscriptions),
  `quant/strategies/*` (registry/loader + `example_equity`).
- **Specs:** strategy-authoring (new) + decision-engine, data-access,
  execution, instruments, portfolio, strategy-sdk (deltas).
- **Tests:** engine/sizing/executor/live/golden suites migrate per commit;
  two-process + golden replay re-pinned to the transcript; conformance suites
  run against both roots on the new seams.
- **Breaking:** every existing 3-method strategy and any `SIGNAL_REGISTRY` /
  `SIZING_STRATEGIES` / `ExecutorRouter` consumer is rewritten or deleted —
  there is no compatibility shim.

## Non-goals (tracked on the roadmap, not here)

- Ticks / L1-L2 order book / multi-asset subscriptions (the port is shaped to
  allow them later; bars ship now).
- Re-deriving the user-facing correctness material (whitepaper, evaluation
  methodology) — a later roadmap task; specs + internals land here.
