# Strategy Module Standards

This document outlines the requirements and standards for creating strategy modules within the `quant/strategies/` directory. Strict adherence to these standards is required to ensure compatibility with `TradingEngine` (`quant/core/engine.py`), `trader.py`, and the backtester.

> **Timestamp contract:** strategies never touch timezone logic. Indexes are
> canonical tz-naive UTC (handlers enforce it via `data/_times.py`); a strategy
> only *reads* `df.index` stamps and may use the pure calendar helpers in
> `quant/core/_utils.py` (`closed_bar_asof`, `bar_close_utc`, session clocks).

> **Architecture note:** All trading decisions flow through `TradingEngine`.
> Both `trader.py` (live) and the backtester instantiate one engine per
> strategy. The engine calls your strategy's `calculate_indicators`,
> `generate_signals`, and `manage_trade` — your strategy never needs to
> know whether it's running live or in backtest. See
> [`quant/core/README.md`](../core/README.md) for the full pipeline diagram.
>
> **Supported live strategy shape:** `INTERVAL == MANAGE_INTERVAL`;
> `DATA_DELAY` on strategy config; optional entry bar gate (`IC_ENTRY_*`)
> inside `generate_signals` only (not an orchestration clock); no internal
> data fetches in `generate_signals` / `calculate_indicators`. Reference:
> `example_equity`. Dual-cadence (`1d` signal / `5m` manage) is legacy — do not
> invest in new dual-cadence designs.

## 1. Directory Structure

Each strategy must be a self-contained Python package (directory) with an `__init__.py` and a main implementation file (e.g., `strategy.py`).

```text
quant/strategies/
└── my_strategy_name/
    ├── __init__.py      # Exports the Strategy class
    ├── strategy.py      # Contains logic
    └── config.json      # Optional: default configs
```

## 2. Class Interface

Your strategy class must implement the following interface. While explicit inheritance from a base class is optional, duck typing is used.

### `__init__(self)`

- Should initialize internal state.
- **Must** have a `self.config` object (can be an Empty Class or DictWrapper) to hold parameters. `trader.py` will inject overrides here.

### `generate_signals(self, df: pd.DataFrame) -> pd.DataFrame`

- **Input**:
  - `df`: A Pandas DataFrame containing historical data (OHLCV).
- **Output**:
  - Returns `pd.DataFrame` with generated 'Signal', 'Confidence', 'Reason' columns for **every bar** in `df`.
- **Logic**:
  - Calculate indicators and generate 'Signal' columns for the entire history.

## 3. How `TradingEngine` Calls Your Strategy

`TradingEngine` drives a 4-step pipeline for every ticker, every cycle (live) or every bar (backtest). Your strategy participates in steps 2 and 3:

```
1. Data        engine.fetch_data(...)           → DataFrame (OHLCV)
2. Signals     engine.run_signals(df, ticker)   → calls YOUR calculate_indicators + generate_signals
3. Manage      engine.call_manage_trade(pos, df) → calls YOUR manage_trade
4. Sizing      engine.size_*(...)                → engine-internal, not strategy code
```

The unified entry point is `engine.process_ticker(df, manage_df, ticker, active_positions)` which returns a list of `EXIT` and `ENTRY` action dicts. Both `trader.py` and the backtester call this same method.

**Indicator injection:** When `MANAGE_INTERVAL` differs from `INTERVAL` (e.g., signals on `1d`, manage on `1h`), the engine uses `merge_asof` to forward-fill your signal-resolution indicators onto the manage-resolution bars before calling `manage_trade`. Daily indicators are shifted by 1 period to prevent lookahead.

## 4. High-Performance Requirements

For strategies requiring accelerated logic:

### High-Performance Backtesting (Numba)

The engine requires your strategy to implement the following structure:

1. **`manage_trade` (Public Wrapper)**: Prepares data and calls the static core. This is used by the Live Trader.
2. **`numba_logic` (Static Core)**: The JIT-compiled logic used by both Backtester and Live Trader.

#### 1. Public Wrapper

```python
def manage_trade(self, position: Dict, df: pd.DataFrame) -> int:
    """
    Public wrapper called by trader.py.
    Responsible for:
    1. Extracting/Preparing Numpy arrays from DataFrame.
    2. Constructing the exit_params vector.
    3. Calling self.numba_logic().
    """
    # ... prepare arrays ...
    return self.numba_logic(..., exit_params, ...)
```

> **CRITICAL — Backtest / Live Alignment**
>
> `TradingEngine.call_manage_trade` always injects indicators before
> calling `manage_trade`. In backtest, the engine pre-slices data to
> `[entry_time, entry_time + MAX_HOLDING_MINUTES]`. In live, the full
> data cache is passed instead.
>
> If the wrapper does not account for this, three things break:
>
> | Problem | Why |
> |---|---|
> | `entry_idx` points to wrong bar | `searchsorted` maps a Friday entry to Tuesday's first bar after a weekend gap |
> | `entry_price` is wrong | `mids[entry_idx]` picks up Tuesday's price, not the actual fill |
> | Max-hold never fires | Numba uses `times[entry_idx]` as the reference — only hours elapsed, not days |
>
> **Required pattern** (see `simple_swing/strategy.py` for reference):
>
> 1. **Slice from entry to end.** Filter `df` to `df.index >= actual_entry_ts`
>    before extracting arrays. This makes `entry_idx = 0`, matching backtest.
>
> 2. **Use stored entry price.** Read `UnderlyingEntryPrice` (or `EntryBasePrice`)
>    from the position dict. Do not derive it from `mids[0]` of the sliced data —
>    the first bar after a gap is not the actual fill price.
>
> 3. **Post-numba max-hold check.** If numba returns `-1`, compare
>    `last_bar_timestamp - actual_entry_time` against `MAX_HOLDING_MINUTES`.
>    Return the last index to force exit. This mirrors what the backtests do:
>    ```python
>    if exit_idx == -1:
>        final_exit_idx = len(df_slice) - 1  # forced exit
>    ```
>    Numba itself cannot detect max-hold over market gaps (weekends/holidays)
>    because it derives `entry_time` from `times[entry_idx]`, which is the first
>    **bar** timestamp — not the actual calendar entry time.

#### 2. Static Numba Core

```python
from numba import njit
import numpy as np

@staticmethod
@njit(cache=True, fastmath=True)
def numba_logic(
    mid_arr, bid_arr, ask_arr, vol_arr, ts_ns_arr,
    df_values,
    entry_idx, entry_price,
    exit_params, # Named Vector (np.array) containing [SL, TP, TRAIL, MAX_HOLD, BE, SPREAD, DIR]
    _df_times, _col_map
):
    """
    Scans the price arrays starting from entry_idx to find the exit point.

    Args:
        mid_arr (np.array): Mid price array (Option or Underlying).
        ts_ns_arr (np.array): Timestamp array (nanoseconds).
        df_values (np.array): 2D float array of the generated signals DataFrame.
        entry_idx (int): Index where trade opened.
        entry_price (float): Fill price.
        exit_params (np.array): Float64 array of exit parameters (Stop Loss, Take Profit, etc.)
                                Indexed using constants from quant.utils.constants

    Returns:
        int: The index where the trade should close.
             If end of data reached without exit, returns -1.
    """
    # Loop from entry_idx + 1 to end
    # Track peaks for trailing stops
    # Check stops/targets
    # ...
    return -1
```

This method is **MANDATORY**. The legacy Python event loop fallback has been removed.

## 7. Option Strategy Protocol

When implementing strategies that trade options (i.e., `MIN_OPTION_DTE > 0`), the `manage_trade` method must be designed to make decisions based on the **Underlying Asset's Technical Structure**, not the Option's Premium chart.

### The "Proxy" Pattern

The `OptionBacktester` and Live Trader will pass a **Proxy DataFrame** to `manage_trade` that represents the **Underlying Asset**, even though the trade is technically an option contract.

1. **DataFrame (`df`)**:
   - `Close`, `Open`, `High`, `Low`: Contain the **Underlying Asset** prices (e.g., SPY, AAPL).
   - Indicators (RSI, ADX, ATR): Calculated on the **Underlying Asset**.
   - `Raw_Close`: Identical to `Close` (Underlying).

2. **Position (`position`)**:
   - `EntryPrice`: The **Underlying Price** at the moment of entry.
   - `EntryBasePrice`: Same as `EntryPrice`.

   _Note: The actual Option Fill Price and PnL are handled externally by the Backtester/Trader. Your strategy logic should not concern itself with the Option Premium unless you explicitly request option-specific fields._

### Implication for `numba_logic`

When writing your Numba core:

- `mid_arr` will contain **Underlying Prices**.
- `entry_price` will be the **Underlying Entry Price**.
- Your stop loss distances (e.g., `2.0 * ATR`) will naturally align because `ATR` is in Underlying points and `mid_arr` is in Underlying points.

This ensures that technical exits (Trend Break, Moving Average Cross, ATR Trail) are respected based on the chart you are actually analyzing.

## 8. Equity Mode

To trade the underlying asset instead of options:

1.  Set `MIN_OPTION_DTE = -1` in your strategy config.
2.  `trader.py` will skip the option chain lookup and trade the Ticker directly.

## 6. Type Checking

- Use Python Type Hints (`typing.List`, `typing.Dict`) for all public methods.
- Ensure rigorous error handling. If an indicator fails, return an empty signal or neutral state, do not crash.
