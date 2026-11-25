# Strategy Authoring Specification

## Purpose

The declarative strategy surface: a strategy is declare + react. It declares
typed instruments, typed data subscriptions, and a venue, then runs a single
author-owned body (`decide`) over typed data events and the injected,
read-only Portfolio. `decide` **replaces both halves of the legacy pipeline**
— it both generates entries (what `generate_signals` + sizing did) and
evaluates existing positions held in the Portfolio for exits (what
`manage_trade` + `evaluate_manage_exit` did). Sizing is strategy-owned from
instrument contract facts + the Portfolio; typed `Order`s built via
`instrument.make_order` are submitted to the venue.

The collapsed post-change call chain is: `TradingEngine.process_strategy` →
resolve declared subscriptions → serve closed bars from buffers → feed
`decide(events, portfolio)` → strategy evaluates existing positions AND
generates entries → returns typed order/exit intents →
`instrument.make_order` → venue execution client → fill feeds the Portfolio.

## ADDED Requirements

### Requirement: Declarative strategy surface

A new strategy base (e.g. `quant/strategies/base.py`) MUST provide the
declarative surface: `name`, `config: BaseStrategyConfig` with declarative
fields (`instruments`, `subscriptions`, `venue`), and a single author body
`decide(events, portfolio) -> list[DecisionItem]` where `DecisionItem` is a
typed `Order` (built by the strategy) or an exit intent for a position from
the injected `portfolio`. `decide` MUST be the one decision function: it
generates entries from the delivered events AND evaluates the portfolio's
open positions for exits. A bar-driven strategy MUST construct with
resolvable declarations the engine can drive; the 3-method contracted
(`calculate_indicators`/`generate_signals`/`manage_trade`) and its signal
strings MUST NOT appear on this surface.

#### Scenario: Bar-driven strategy constructs and resolves
GIVEN a strategy declaring an equity instrument, a bars subscription, and a venue
WHEN it is constructed
THEN the declarations resolve against the data port and `quant/venue`, and `decide` is callable over bars events

#### Scenario: Decision body runs over typed data
GIVEN a bars event delivered to the author body
WHEN `decide` evaluates
THEN it reads typed bars (adjusted only) plus the injected `portfolio`, and returns typed order/exit intents — no signal-string protocol is involved

#### Scenario: Decide evaluates existing positions for exits
GIVEN the injected `portfolio` holding open positions for this strategy
WHEN `decide` runs
THEN it evaluates those positions (entry price, qty, exposure, holding time) over manage-cadence data and may return exit intents — this is the `manage_trade`/`evaluate_manage_exit` role folded into `decide`

#### Scenario: Exit intent closes a position on manage-cadence data
GIVEN an open position and its manage-cadence bars
WHEN the author body returns an exit intent
THEN the engine routes it to the venue — no engine-side `evaluate_manage_exit`/`manage_trade` machinery is invoked

### Requirement: Sizing is delegated

Sizing MUST be strategy-owned, inside the author body: the strategy computes
its target quantity from instrument contract facts (multiplier, margin, lot,
price precision on the instrument) and the injected read-only Portfolio
(equity, available margin, open exposure, existing positions). No engine- or
registry-owned sizing exists on the decision path
(`SIZING_STRATEGIES`/`size_for_signal`/`size_for_asset` are removed).

#### Scenario: Futures strategy sizes from margin and multiplier
GIVEN a futures strategy with a `FutureInstrument` carrying margin and multiplier
WHEN it sizes an entry
THEN the quantity derives from `instrument` margin/multiplier and the Portfolio's available margin — not a generic capital fraction

#### Scenario: Equity strategy sizes from contract facts
GIVEN an equity strategy with a price-precision/lot instrument and the Portfolio
WHEN it sizes
THEN the quantity respects the instrument's lot/price constraints and available capital

#### Scenario: Size zero means do not trade
GIVEN a sizing result of zero quantity
WHEN the author body finishes
THEN no order is produced for that instrument

### Requirement: Venue is the order seam

The strategy MUST build typed `Order`s through `instrument.make_order(side,
qty, ...)` and return them from `decide`; the engine MUST submit them to the
strategy's declared venue's execution client. Fill reporting from the venue
MUST feed the Portfolio the strategy reads, and the strategy MUST NOT talk to
a broker client directly.

#### Scenario: Order built on the instrument and submitted to the venue
GIVEN the author body decides to act
WHEN `decide` returns
THEN the order is built via `instrument.make_order` and submitted by the engine to the declared venue's execution client

#### Scenario: Fill reporting feeds the portfolio
GIVEN a venue fill for a submitted order
WHEN the fill is reported
THEN the Portfolio the strategy reads reflects the fill (open exposure grows; filled quantity updates the position schema)

#### Scenario: No broker client in the strategy
GIVEN an author body
WHEN it inspects its imports and attributes
THEN it holds only the venue id/declaration and typed seams — no broker SDK object is reachable

### Requirement: Reference strategy — canonical worked example

The change MUST ship a rewritten `example_equity` as the single canonical
worked example of the declarative surface (the reference the SDK, docs, and
golden/parity tests use), preserving its pre-change entry behavior so it can
be validated exactly. Its declaration and author body MUST be spelled out
concretely:

```python
# quant/strategies/example_equity/strategy.py (declarative rewrite)
class ExampleEquityStrategy(DeclarativeStrategy):
    name = "example_equity"
    config = ExampleEquityConfig(
        instruments=[EquityInstrument(
            symbol="SPY", venue="backtest",
            # contract facts on the instrument (single source):
            contract=EquityContract(price_precision=2, lot=1),
        )],
        subscriptions=[Subscription(
            instrument="SPY", datatype=Datatype.BARS, cadence="1h")],
        venue="backtest",
        # framework risk/execution fields kept for the author to read:
        initial_capital=100_000.0, buying_power_fraction=0.95,
        max_concurrent_positions=1, pre_load_days=60, data_delay=0,
        stop_loss_percent=0.05, take_profit_percent=-1.0,
        max_holding_minutes=60*24*30,
        fast_window=10, slow_window=30,   # MA cross windows (legacy 10/30)
    )

    def decide(self, events, portfolio) -> list[DecisionItem]:
        bars = events.bars("SPY")            # adjusted Close only (invariant)
        fast = bars.close.rolling(self.config.fast_window).mean()
        slow = bars.close.rolling(self.config.slow_window).mean()
        out: list[DecisionItem] = []

        # Manage pass — the folded manage_trade/evaluate_manage_exit role.
        for pos in portfolio.open_positions(self.name):
            if exit_risk_hit(pos, self.config):      # stop/take/max-hold
                out.append(ExitIntent(self.name, position=pos))
            elif cross_down(fast, slow):
                out.append(ExitIntent(self.name, position=pos))

        # Signal pass — the folded generate_signals + sizing role.
        if cross_up(fast, slow) and len(portfolio.open_positions(self.name)) < \
                self.config.max_concurrent_positions:
            qty = size_equity_lot(
                portfolio,                    # read-only equity / buying power
                bars.close.iloc[-1],
                instrument=self.config.instruments[0],
            )
            out.append(self.config.instruments[0].make_order("BUY", qty))
        return out
```

The rewrite MUST keep the pre-change MA windows (10/30) and the ~0.95
buying-power-fraction entry sizing semantics so the golden comparison over
the same synthetic fixture produces the **same entry decision sequence** as
the legacy 3-method `ExampleEquityStrategy` — but now sized on the instrument
(lot/price precision) against the injected, read-only `portfolio`, and with
stops/max-hold evaluated by the author body (replacing the engine's
`evaluate_manage_exit`), never via `CAPITAL_PER_TRADE`/`SIZING_STRATEGIES`.

#### Scenario: Reference strategy constructs with resolvable declarations
GIVEN the rewritten `example_equity` declaration above
WHEN it is constructed on the backtest root and the live root
THEN `config.instruments`, `config.subscriptions`, and `config.venue` resolve on both roots and `decide` is callable

#### Scenario: Backtest entry behavior matches the pre-change reference
GIVEN the same synthetic fixture as the legacy golden
WHEN the rewritten strategy runs in backtest through the reshaped pipeline
THEN the entry decision sequence matches the pre-change `example_equity` (golden comparison), with sizing expressed on the instrument + portfolio and risk exits evaluated by the author body

#### Scenario: Risk exits still fire on the same conditions
GIVEN the legacy strategy's stop-loss / max-hold conditions
WHEN the author body's manage pass runs on the same positions
THEN the same exits are produced that the engine's `evaluate_manage_exit` used to produce for `example_equity`