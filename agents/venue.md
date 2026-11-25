# Venue — `quant.venue` (spec `venue`, design D2)

The venue is the single, swappable unit a strategy trades through: **identity +
session/calendar state + market-data source + execution client** in one object.
A strategy/engine addresses every broker-and-session concern through its venue
— it never scrapes calendars, bars, or order plumbing from scattered modules.
`AlpacaVenue` (live) and `BacktestVenue` (simulation) ship; extra venues
register by name, and swapping one for the other leaves strategy and decision
code unchanged.

## Invariants (do not break without discussion)

1. **One object serves session + broker roles.** Open/close times, historical
   bars (`data_source`), and order submission (`execution_client`) all come
   from the venue object. There is no supported path that reads these from
   elsewhere.
2. **Naive-UTC decision boundary.** Timezone-aware values live *inside* venue
   adapters; everything crossing into the decision path is UTC-naive. The only
   sanctioned conversion is `to_utc_naive`. A value that violates this is a
   bug at the boundary, not policy.
3. **Determinism is sealed in the backtest venue.** `BacktestVenue` answers
   session state from the injected simulation `now` + its configured window —
   never the host wall clock. The same simulated timestamp always yields the
   same open/closed answer, on any machine, at any wall time (guarded by the
   deterministic-backtest tests).
4. **Session fallback is logged, never fatal.** If the calendar is unavailable
   or returns a closure, the venue falls back to documented default hours with
   a log. Live venues treat an unknown day as closed-and-defaults; the backtest
   venue's simulation timeline is open-by-config.
5. **The order seam is typed orders in, fills out.** ``execution_client``
   accepts the typed ``Order`` the author body builds via
   ``instrument.make_order`` (``ExecutionClientProtocol.submit``) and reports
   a ``Fill``; a reported fill feeds the Portfolio the strategy reads. The
   strategy never holds a broker object — only the venue declaration and its
   typed seams.
5. **Rollover recomputes.** Session state re-resolves per date; a query after
   date rollover refreshes open/close/forced-close from the calendar rather
   than reusing a cached previous-day window.

## Where it lives

| Path | Responsibility |
|---|---|
| `quant/src/quant/venue/base.py` | `Venue` / `CalendarProtocol` / `DataSourceProtocol` / `ExecutionClientProtocol` protocols, `BaseVenue` (session resolution), `to_utc_naive`, `SessionDay` |
| `quant/src/quant/venue/backtest.py` | `BacktestVenue` — deterministic simulation-timeline venue; `BacktestExecutionClient` — typed-`Order` → simulated-fill seam |
| `quant/src/quant/venue/alpaca.py` | `AlpacaCalendar` + `AlpacaVenue` — live NY→UTC calendar + injectable data/execution |
| `quant/src/quant/venue/_registry.py` | `register_venue` / `venue_by_name` (built-ins `alpaca`, `backtest`) |

## Quickstart — the venue primitive offline

```python
from datetime import datetime

import pandas as pd

from quant.venue import BacktestVenue, to_utc_naive, venue_by_name
from quant.venue.base import DataSourceProtocol

# Deterministic backtest venue: answers session state from the simulated
# timestamp, never the wall clock. Defaults: 13:30–20:00 UTC equity window.
venue = BacktestVenue()
print(venue.id)                       # "backtest"

print(venue.is_open_at(datetime(2024, 6, 3, 15, 0)))   # True   (in window)
print(venue.is_open_at(datetime(2024, 6, 3, 9, 0)))    # False  (pre-open)

# Canonical boundary: tz-naive UTC everywhere decisions run.
print(to_utc_naive(pd.Timestamp("2024-06-03 15:00", tz="UTC")))
# 2024-06-03 15:00:00


# The data source is a `DataSourceProtocol` duck-type attached to the venue.
class MemoryDataSource:
    def get_historical_data(self, ticker, start, end, interval,
                            use_cache=True, adjustment=None):
        idx = pd.date_range("2024-01-02 14:00", periods=3, freq="1h",
                            tz="UTC").tz_localize(None)
        return pd.DataFrame({"Open": 100.0, "High": 101.0, "Low": 99.0,
                             "Close": 100.5, "Volume": 1000}, index=idx)


source = MemoryDataSource()
assert isinstance(source, DataSourceProtocol)          # runtime-checkable

venue = BacktestVenue(data_source=source)
df = venue.data_source.get_historical_data("SPY", None, None, "1h")
print(df.Close.iloc[-1])                               # 100.5

# Venues also resolve by name from config.
print(venue_by_name("backtest").id)                    # "backtest"
```

The same strategy/engine code runs identically against `AlpacaVenue` (live) —
only the venue object differs; decisions and session semantics are unchanged.

## Extension points

- **New venue** (a broker, a paper simulator, a backfilled historical venue):
  implement the `Venue` protocol (or subclass `BaseVenue`) and register it —
  `register_venue("my_broker", MyVenue)` — so it becomes resolvable by name as
  a sibling of `alpaca` / `backtest`.
- **Real calendar** — implement `CalendarProtocol.open_close_for(on)` returning
  `(open_utc_naive, close_utc_naive)` (or `None` for a closure) and inject it.
  `AlpacaCalendar` is the shipped live reference.
- **Data source** — attach any `DataSourceProtocol` (`get_historical_data(...)`);
  the engine stays deterministic as long as bars are adjusted + bar-driven.
- **Execution client** — inject an execution client with your broker's fill
  semantics; only the venue's execution surface changes.

## Verify

The quickstart above was validated by executing its code paths (`uv run python -
<<'PY' ... PY`): venue identity, `is_open_at` on/off window, `to_utc_naive`, the
`DataSourceProtocol` runtime check, the `venue.data_source` read, and
`venue_by_name("backtest")` all returned the expected outputs.