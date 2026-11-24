# Data access — `quant.data` + `DataSourceProtocol` (spec `data-access`, design D2/D14)

Data is the primitive every strategy touches regardless of venue or asset. The
SDK exposes one protocol — `DataSourceProtocol.get_historical_data(ticker,
start, end, interval, ...) -> DataFrame` — implemented by the live handler
(REST + persistent cache + in-memory fast path) and by the backtest handler
(read-only views of preloaded frames), so decisions read **identical-shaped
bars** in research and production. `DataSourceProtocol` lives in
`quant.venue.base` (the venue owns its data source); any handler that
implements it drops into a venue and an engine unchanged.

## Invariants (do not break without discussion)

1. **One shape for live and backtest.** Same ticker, range, and interval →
   both handlers return frames with the same column names and timestamp
   normalization (naive-UTC indexes). A handler for an instrument with no
   corporate-action adjustments accepts and ignores the `adjustment` argument.
2. **Adjusted bars are the decision surface.** Signals run on adjusted bars
   only; `Raw_Close` is backtest-only execution, never readable on the live
   decision path.
3. **Only fully-settled bars are served.** The live in-memory store appends a
   bucket only when all its minutes have arrived — a partially formed bar is
   never visible to a read. Reads never get a bar newer than the live stream
   has closed.
4. **Answers come from memory when covered, else cache/REST with a log.** A
   request covered by the seeded in-memory store is answered without disk or
   network; a miss falls back to cache/REST and logs the miss.
5. **Persistent cache is byte-stable.** Fetched history is cached to disk per
   (ticker/interval/adjustment); `use_cache=True` serves cached windows without
   re-fetching and the cached frame equals the one a fetch would return.

## Where it lives

| Path | Responsibility |
|---|---|
| `quant/src/quant/venue/base.py` | `DataSourceProtocol` (the data primitive's contract) |
| `quant/src/quant/data/data_handler.py` | `AlpacaDataHandler` — live handler: REST + persistent cache + MemBars fast path |
| `quant/src/quant/data/cache/` | `cache/core.py` — arrow/frame codec + on-disk cache |
| `quant/src/quant/data/_times.py` | canonical tz-naive UTC timestamp normalization |
| `quant/src/quant/data/ws/` | live stream admission into the fast-path buckets |

## Quickstart — the data primitive offline

```python
import pandas as pd

from quant.venue import BacktestVenue
from quant.venue.base import DataSourceProtocol


# Any object implementing the protocol is a data source. This in-memory one is
# the shape both the live and backtest handlers implement (here: no network).
class MemoryDataSource:
    def get_historical_data(self, ticker, start, end, interval,
                            use_cache=True, adjustment=None):
        idx = pd.date_range("2024-01-02 14:00", periods=3, freq="1h",
                            tz="UTC").tz_localize(None)
        return pd.DataFrame({"Open": 100.0, "High": 101.0, "Low": 99.0,
                             "Close": 100.5, "Volume": 1000}, index=idx)


source = MemoryDataSource()
assert isinstance(source, DataSourceProtocol)   # runtime-checkable

# A venue owns its data source; the engine reads bars through the same object.
venue = BacktestVenue(data_source=source)
df = venue.data_source.get_historical_data("SPY", None, None, "1h",
                                           adjustment="all")
print(df.columns.tolist())          # Open/High/Low/Close/Volume
print(df.Close.iloc[-1])            # 100.5
```

The `adjustment` argument is tolerated even for instruments with no corporate
actions (returns natural bars), and the naive-UTC index matches backtest and
live output shape for the same ticker/range/interval.

## Extension points

- **New data source** (a provider, a local parquet store, an event replay):
  implement `DataSourceProtocol` and drop it into a venue / engine. The engine
  stays deterministic as long as bars are adjusted + bar-driven.
- **Another live fast path / admission policy** — build on the cache + in-memory
  store behind the protocol; only the handler changes.
- **Registration/discovery** — as the sibling of the strategy registry, data
  sources resolve by name; a `quant.datasources` entry-point group lets an
  external distribution register one with no SDK change (mirrors task 6.3).

## Verify

The quickstart above was validated by executing its code paths (`uv run python -
<<'PY' ... PY`): the `DataSourceProtocol` runtime check, the venue-owned
`data_source.get_historical_data(..., adjustment="all")` call, and the column /
close-value assertions all matched.