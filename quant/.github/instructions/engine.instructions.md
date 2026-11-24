---
description: "Use when reviewing or writing TradingEngine or core pipeline code. Covers the unified engine contract, asset class routing, position schema, and sizing."
applyTo: "core/**"
---

# TradingEngine & Core Module Standards

## Architecture Principle

`TradingEngine` is the **single decision layer** — both the backtester and live trader instantiate it, eliminating duplication. All trading logic flows through its 4-step pipeline:

1. **Data** — `fetch_data()` via data handlers
2. **Signals** — `run_signals()` calls `strategy.calculate_indicators()` + `strategy.generate_signals()`
3. **Manage** — `call_manage_trade()` delegates to `strategy.manage_trade()` if implemented
4. **Sizing** — `size_equity/option/spread/ic()` + `build_position()` constructs `PositionDict`

Entry point: `process_ticker(df_signal, manage_df, ticker, active_positions)`

## StrategyProtocol Validation

The engine validates the strategy at construction time using `runtime_checkable Protocol`. Missing `calculate_indicators` or `generate_signals` raises `TypeError`.

## Signal Routing

`_SIGNAL_META[SignalType]` maps each signal to `(asset_class, direction)` for O(1) dispatch:

| Asset Class | Sizing Method   | Fill Model                          |
| ----------- | --------------- | ----------------------------------- |
| EQUITY      | `size_equity()` | Slippage $/share or %               |
| OPTION      | `size_option()` | Numba volume-impact scalar          |
| SPREAD      | `size_spread()` | Fixed $0.05/leg                     |
| IC          | `size_ic()`     | Combo slippage + residual extrinsic |

## PositionDict Schema

`PositionDict` is a `TypedDict` with `total=False`. Core fields:

- `EntryTime`, `EntryPrice`, `EntryBasePrice`, `UnderlyingEntryPrice`
- `Direction` (±1), `Signal`, `Confidence` (0–1)
- `StopLoss`, `TakeProfit`, `UseTrail`, `MaxHoldTime`
- `AssetType`, `ATR`, `Ticker`, `Qty`, `Expiration`

Strategy-specific extras are passed via `**extra` in `build_position()` — no schema changes needed for new fields.

## Config Resolution

Use `engine.cfg(key)` to read config values with canonical defaults. All config fields are UPPERCASE.

## Signal Cache

Bounded LRU cache (max 64 tickers) via `OrderedDict`. Prevents unbounded memory growth during multi-ticker backtests.

## PR Review Checks

- [ ] Changes to `TradingEngine` don't break the backtest↔live symmetry.
- [ ] New asset classes add entries to `_SIGNAL_META` and implement a `_open_*()` method in the backtester.
- [ ] `PositionDict` changes are additive (use `total=False` — never remove existing keys).
- [ ] `cfg()` defaults are sensible and documented.
- [ ] No strategy-specific logic leaks into the engine — keep it generic.
