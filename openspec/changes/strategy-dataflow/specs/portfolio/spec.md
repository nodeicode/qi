## Purpose

The engine passes the **Portfolio itself** into the author body (`decide`)
as a read-only view — no separate projection class. The strategy reads
current equity, available margin, open exposure, and its own existing open
positions (to evaluate exits) from the Portfolio; it cannot mutate it (fills
remain venue-owned).

## ADDED Requirements

### Requirement: Portfolio injected read-only into the author body

The engine MUST pass the `Portfolio` (`quant/portfolio/*`) itself — not a
projection wrapper — into `decide(events, portfolio)` as a **read-only**
view: the strategy MUST be able to read current equity, available
margin/capital, open exposure, and its existing open positions for this
strategy name, and MUST NOT be able to mutate the portfolio through it.

#### Scenario: Sizing reads current equity, margin, and exposure
GIVEN an author body sizing an entry
WHEN it reads the injected `portfolio`
THEN it sees current equity, available margin, open exposure, and sizes from them

#### Scenario: Manage evaluates existing positions from the portfolio
GIVEN the injected `portfolio` holding open positions for this strategy
WHEN `decide` runs its manage pass
THEN it reads those positions (entry price, qty, exposure, holding time) to evaluate exits — the portfolio carries the positions `evaluate_manage_exit` used to see

#### Scenario: Strategy cannot mutate the portfolio
GIVEN an author body holding the injected `portfolio`
WHEN it attempts to change portfolio state
THEN the mutation is blocked (the handed view exposes no mutating surface; fills are venue-owned)