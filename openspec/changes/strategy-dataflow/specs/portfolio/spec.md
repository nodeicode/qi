## Purpose

The portfolio/account surface is injected into the author body as a
read-only projection for sizing. The strategy sizes against current equity,
available margin, and open exposure but cannot mutate the portfolio — fills
remain venue-owned.

## ADDED Requirements

### Requirement: Read-only account projection

The engine MUST inject a read-only portfolio/account projection into the
author body: current equity, available margin/capital, and open exposure,
built from the existing `quant/portfolio/*` primitives. The strategy MUST be
able to read the projection for sizing and MUST NOT be able to mutate the
portfolio through it.

#### Scenario: Sizing reads current equity and exposure
GIVEN an author body sizing an entry
WHEN it reads the injected projection
THEN it sees current equity, available margin, and open exposure, and sizes from them

#### Scenario: Strategy cannot mutate the portfolio
GIVEN an author body holding the injected projection
WHEN it attempts to change portfolio state
THEN the mutation is blocked (the projection exposes no mutating surface; fills are venue-owned)
