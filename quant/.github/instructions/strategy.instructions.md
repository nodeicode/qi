---
description: "Use when reviewing or writing strategy code. Covers the strategy interface contract, signal conventions, and the TradingEngine integration pattern."
applyTo: "strategies/**"
---

# Strategy Code Standards

## Required Interface (StrategyProtocol)

Every strategy **must** implement:

```python
def calculate_indicators(self, df: pd.DataFrame, **kw) -> pd.DataFrame
def generate_signals(self, df: pd.DataFrame) -> pd.DataFrame
```

Optional (for intraday exit management):

```python
def manage_trade(self, position: PositionDict, df: pd.DataFrame) -> int
```

- `calculate_indicators` receives OHLCV bars, returns df with added indicator/probability columns. Accepts optional `ticker` kwarg.
- `generate_signals` must return **every bar** with at minimum: `Signal` (str enum), `Confidence` (float 0–1).
- `manage_trade` returns the bar index to exit at, or `-1` to hold.

## Signal Types

Use `SignalType` enum values only:

| Signal                                         | Asset Class       |
| ---------------------------------------------- | ----------------- |
| `BUY`, `SELL`, `LONG`, `SHORT`                 | Equity            |
| `BUY_CALL`, `BUY_PUT`, `SELL_CALL`, `SELL_PUT` | Single-leg option |
| `BUY_CALL_MOM`, `BUY_PUT_MOM`                  | Momentum option   |
| `BUY_SPREAD`, `SELL_SPREAD`                    | Vertical spread   |
| `SELL_IC`, `BUY_IC`                            | Iron condor       |

## Config Pattern

Strategy configs extend `BaseStrategyConfig` (dataclass in `utils/strategy_config.py`). All config fields are UPPERCASE. Key required fields:

- `INTERVAL`, `PRE_LOAD_DAYS`, `MANAGE_INTERVAL`
- `INITIAL_CAPITAL`, `CAPITAL_PER_TRADE`, `MAX_CONCURRENT_POSITIONS`
- `STOP_LOSS_PERCENT`, `TAKE_PROFIT_PERCENT`, `MAX_HOLDING_MINUTES`
- `MIN_SIGNAL_CONFIDENCE`, `TICKERS`

## Strategy Registry

All strategies are registered in `strategies/__init__.py` via `STRATEGY_REGISTRY` dict. New strategies must be added there.

## Performance Rules

- Numba (`@njit`) is required for any hot-path trade management logic.
- Numba functions use a 12-element float array for exit params — see `utils/constants.py` for named indices.
- Never use Python objects or string operations inside `@njit` functions.

## Backtest/Live Alignment

The `TradingEngine` is the **single source of truth** for trading decisions. Both `backtester.py` and `trader.py` call `engine.process_ticker()` — strategy code must never contain backtester-specific or live-specific logic.

## Anti-Lookahead Checklist (PR reviewers must verify)

- [ ] Feature pruning uses only the initial walk-forward window, not the full sample.
- [ ] Labels use `shift(-1)` or forward-looking windows — features must not see their own outcome bar.
- [ ] Walk-forward embargo (`WF_EMBARGO`) separates train and test sets.
- [ ] No full-sample HMM fitting (forward-backward algorithm leaks future).
- [ ] Indicators that use future data (e.g., centered rolling windows) are forbidden.
