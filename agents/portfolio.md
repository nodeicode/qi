# Portfolio — `quant.portfolio` (spec `portfolio`, design D4)

The v1 portfolio surface is deliberately small and research-first: one
`Position` state machine per (venue, symbol) driven only by fills, and one
`Portfolio` that aggregates those positions with account state and
per-strategy + combined metrics. The **same classes and the same semantic
functions** run in backtest (fills from fill simulation) and live (fills from
venue execution), so portfolio results are comparable across environments by
construction — no forks.

## Invariants (do not break without discussion)

1. **Fills are the only mutator.** A `Position` transitions
   `opened -> adjusting -> closed` exclusively via `Position.apply(fill)` /
   `Portfolio.apply_fill(fill)`. Nothing on a position is changed by side
   effects; any code that writes a position field directly is a bug.
2. **Positions are keyed by `(venue, symbol)`.** Two positions on the same
   instrument at different venues are distinct objects. A typed
   `quant.instruments.Instrument` keeps the key source of truth; ticker strings
   stay legal as sugar.
3. **Parity by construction.** Both environments convert their execution event
   (a simulated backtest fill, or a live venue order->position) into a
   `quant.portfolio.Fill` and feed the same `Portfolio.apply_fill`. Metric
   computations (realized / unrealized / equity / total) are pure functions of
   position state + a pricing source.
4. **Accounts settle.** When a position closes, realized PnL is recorded on the
   position and the portfolio folds the proceeds into its cash ledger:
   `equity - starting_equity == realized + unrealized`.
5. **Live state is recoverable.** The trader persists a portfolio snapshot
   (open + settled positions and account state) in `trader_state.json` and
   restores it on restart via `Portfolio.from_snapshot` — no manual re-derivation.

## Where it lives

| Path | Responsibility |
|---|---|
| `quant/src/quant/portfolio/base.py` | `Fill`, `Side`, `PositionState`, `Position` (the state machine) |
| `quant/src/quant/portfolio/portfolio.py` | `Portfolio` (aggregation, PnL, metrics, snapshot) |
| `quant/src/quant/portfolio/__init__.py` | public surface (`Fill`, `Position`, `Portfolio`, `Side`, `PositionState`) |

### Wire-up points (backtest + live parity)

- **Backtest**: `Backtester(..., portfolio=Portfolio(), venue="backtest")` —
  the equity executors stream simulated fills into the optional portfolio
  through `BacktestExecContext.portfolio`.
- **Live**: `UnifiedExecutor(..., portfolio=portfolio)` emits fills from venue
  entry/exit execution; `UnifiedTrader` owns a `Portfolio(name="live")` and
  persists/restores its snapshot with the trader state.

## Quickstart — the position lifecycle

```python
from quant.portfolio import Portfolio, Fill, Side

pf = Portfolio(starting_equity=10_000.0)

# research / backtest / live all feed the SAME function below:
def trade(pf, symbol, side, qty, price, strategy):
    return pf.apply_fill(
        Fill(venue="paper", symbol=symbol, side=side,
             qty=qty, price=price, strategy=strategy)
    )

trade(pf, "AAPL", Side.BUY, 10, 150.0, "mom")            # opened
trade(pf, "AAPL", Side.BUY, 10, 150.0, "mom")            # adjusting (weighted avg)
trade(pf, "AAPL", Side.SELL, 20, 160.0, "mom")           # closed

print(pf.realized_pnl())        # (160-150)*20 = 200.0
print(pf.equity({"AAPL": 160.0}))  # settle at the exit price
```

The exact same `Portfolio.apply_fill` sees backtest simulation fills and live
venue fills, so `pf.equity(prices)` / `pf.realized_pnl()` / `pf.metrics(prices)`
mean the same thing in research, backtest, and live.

## Extension points

- **New fill source** (a new data provider, a broker that reports true fill
  prices, an event replay): build a `quant.portfolio.Fill` and call
  `portfolio.apply_fill`. The position/portfolio semantics never change.
- **Live fill prices**: this SDK's live path books a close at the entry price
  (mirroring the existing equity-manager credit convention). A venue that
  reports actual fill prices extends by constructing `Fill(price=<reported>)` —
  the PnL accounting is already identical to backtest.
- **Per-strategy/combined research** (allocation, correlation, combined
  drawdown): run several strategies into one shared `Portfolio`, or one
  `Portfolio` per strategy, then read `portfolio.metrics(prices)` — a single
  combined view plus a `per_strategy` breakdown.
- **Snapshot/recovery**: `portfolio.to_snapshot()` / `Portfolio.from_snapshot()`
  are the persistence seam; the live trader already calls them around
  `trader_state.json`.