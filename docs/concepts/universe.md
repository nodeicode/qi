# Universe — a contract-flavor concept (design D12)

A **universe** is a composition of three existing primitives, not a 9th one:
**instruments** (`quant.instruments`) ∘ **data** (a `DataSourceProtocol` view)
∘ a **membership rule** (which instruments qualify). Its one hard requirement
is **point-in-time identity**: the member set at any past date must be the same
whether computed on the backtest root or the live root. That makes the universe
parity-sensitive, so it ships as a *contract* with a runnable check (spec
`agent-context`: reference concept documents).

The classic failure is **survivorship bias**: a naive "current list" omits
instruments that have since been delisted, so a past-date universe computed
from today's list silently misses them — research results lie. The check below
detects exactly that.

## Contract — runnable point-in-time parity check

The exact block is guarded by `quant/tests/extensions/test_universe_concept.py`
(task 11.2), which runs it against both the backtest and live roots.

```python
# compose instruments ∘ data ∘ membership rule, point-in-time
from dataclasses import dataclass
from datetime import datetime


@dataclass(frozen=True)
class Listing:
    """An instrument's active window at a venue (the data source's view)."""

    symbol: str
    listed: datetime
    delisted: datetime | None = None  # None = still listed today


def active_as_of(listing: Listing, as_of: datetime) -> bool:
    """Point-in-time activity: listed before/on `as_of`, not yet delisted."""
    return not (
        as_of < listing.listed
        or (listing.delisted is not None and as_of >= listing.delisted)
    )


def pit_universe(listings, membership_rule, as_of: datetime) -> list[str]:
    """The backtest PIT universe: members alive `as_of` and passing the rule."""
    return sorted(
        listing.symbol
        for listing in listings
        if active_as_of(listing, as_of) and membership_rule(listing.symbol)
    )


def check_survivorship(pit_view, live_view, membership_rule, as_of: datetime) -> None:
    """Assert the as-of universe is identical across backtest + live views.

    `pit_view` is the backtest root's point-in-time listing history (includes
    delisted instruments). `live_view` is the live root's *daily* snapshot —
    the symbols the live data source reports for `as_of`. A live view built
    from today's list instead is survivorship-biased and fails loudly here.
    """
    expected = pit_universe(pit_view, membership_rule, as_of)
    actual = sorted(s for s in live_view if membership_rule(s))
    if actual != expected:
        missing = sorted(set(expected) - set(actual))
        raise AssertionError(
            f"point-in-time parity violated at {as_of.date()}: "
            f"survivorship bias — missing members {missing}"
        )


# a fixed, deterministic listing history (deterministic synthetic data)
t0 = datetime(2020, 1, 1)
HISTORY = [
    Listing("SPY", t0),                       # still listed
    Listing("AAPL", t0),                      # still listed
    Listing("DINO", datetime(2020, 6, 1), datetime(2021, 12, 31)),  # delisted
]
RULE = lambda sym: sym in {"SPY", "AAPL", "DINO"}  # noqa: E731 - demo filter

as_of = datetime(2021, 1, 15)  # DINO is still alive: mid-life
check_survivorship(HISTORY, {l.symbol for l in HISTORY if active_as_of(l, as_of)},
                   RULE, as_of)               # conforming live view -> passes
print("universe at", as_of.date(), ":", pit_universe(HISTORY, RULE, as_of))
```

A biased live view — built from *today's* list (`{"SPY", "AAPL"}`) — omits
`DINO`, and the same call raises:

```text
AssertionError: point-in-time parity violated at 2021-01-15:
survivorship bias — missing members ['DINO']
```

## The contract in practice

- **Backtest root**: the data source serves point-in-time listing history
  (active windows incl. delistings).
- **Live root**: the data source serves the daily snapshot (the symbols actually
  reported for each date).
- **Composition**: `members(rule, data, as_of)` — the membership rule is yours;
  the point-in-time windowing is the contract. Any implementation that passes
  the check is survivorship-safe for those dates; any that fails must not be
  used for research-to-live parity work.