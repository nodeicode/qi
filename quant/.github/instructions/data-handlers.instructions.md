---
description: "Use when reviewing or writing data handler code. Covers data providers, caching, option symbol formatting, and rate limiting."
applyTo: "data/**"
---

# Data Handler Standards

## Providers & Fallbacks

| Data Type            | Primary   | Fallback  |
| -------------------- | --------- | --------- |
| Stock bars           | Alpaca    | Databento |
| Option bars          | Massive   | Databento |
| Futures bars         | Databento | —         |
| Options on futures   | Databento | —         |
| Option chains (live) | Polygon   | —         |

## Timestamp / timezone contract

Both Alpaca and Databento are **UTC**. Keep the stack in UTC; do **not** store bars as
`America/New_York`. DST is handled only where ET wall-clock intent is translated
into an instant (via `zoneinfo.ZoneInfo("America/New_York")`).

| Artifact | Contract |
| -------- | -------- |
| Equity **intraday** from `get_historical_data` | **UTC-naive** index (UTC wall clock, `tz=None`) |
| Equity **daily** from `get_historical_data` | Prefer UTC-aware (or leave as vendor); strategies map to ET session dates via `to_et_session_midnight` |
| Futures **intraday** from `get_historical_data` | **UTC-naive** index (same contract as equity; `FuturesDataHandler._finalize_futures_index`) |
| Futures **daily** from `get_historical_data` | Prefer UTC-aware (vendor); no RTH filter |
| CBBO / option quote cache | **UTC-aware** index |
| CBBO `at_time` inputs | **UTC-naive** meaning UTC wall clock; handler promotes with `_ensure_utc` |
| Entry clock (all assets) | `calculate_entry_time` in `quant.backtest.utils`: bar at `T` with interval `I` → entry at `T+I`; daily-ish → next BDay **09:30 ET** via `et_wall_to_utc_naive`. Strategy `IC_ENTRY_*` is only a bar gate (e.g. example_equity), never remaps backtest/live clocks |

**Naive means UTC**, never ET. Passing ET-naive into CBBO lookups mis-prices by 4–5 hours.

### DST (no manual ±1h backtest offset)

Market open `09:30 ET` → UTC-naive via `America/New_York`:

| Season | 09:30 ET | UTC-naive |
| ------ | -------- | --------- |
| EDT (summer) | 2026-06-04 09:30 | `2026-06-04 13:30:00` |
| EST (winter) | 2026-01-08 09:30 | `2026-01-08 14:30:00` |

ET boundaries only: market open / RTH / AM window filters, session-date labels, strategy entry-bar gates.
Do **not** hardcode `UTC-4` / `UTC-5`. Do **not** strip tz with `replace(tzinfo=None)` without `tz_convert("UTC")` first.

## AlpacaDataHandler (`data/data_handler.py`)

- Supported intervals: `1m`, `5m`, `15m`, `30m`, `1h`, `3h`, `1d`
- Intraday data is fetched in 30-day chunks to respect API limits.
- Rate limit: 200 requests/min (sliding window via `_wait_for_rate_limit()`).
- Market hours filtering (9:30 AM – 4:00 PM ET) is applied for intraday intervals (DST-safe via `tz_convert`).
- Caching is handled by `StockDataCache` — Parquet + SQLite metadata (UTC-aware on disk).

## FuturesDataHandler (`data/futures_data_handler.py`)

- Dataset: `GLBX.MDP3` (CME Globex MDP 3.0).
- Futures symbology:
  - Outright: `ESZ4`, `NQH5` via `stype_in="raw_symbol"`.
  - Continuous: `ES.c.0` via `stype_in="continuous"`.
- Options-on-futures (OOF) use CME `raw_symbol` resolved via the `definition` schema
  with parent symbology `{ROOT}.OPT` — **not** OCC/OPRA formatting.
- Fetches at 1-minute resolution internally, then resamples to 5m/15m/30m.
- No equity market-hours filter (CME futures trade nearly 24h).
- Intraday `get_historical_data` / `get_futures_data` returns **UTC-naive** indexes
  (parity with `AlpacaDataHandler`); Parquet cache may still store UTC-aware.
- Thread-safe caching via `FuturesDataCache` (default: `quant/data/futures_cache/`).
- Implements `get_historical_data()` for `DataHandlerProtocol` compatibility (uses
  `default_stype_in`, typically `"continuous"`).

## OptionDataHandler (`data/options_data_handler.py`)

- Symbol formats differ by provider:
  - Massive: `O:SPY251219C00590000`
  - Databento: `SPY   241115P00525000` (ticker padded to 6 chars)
- Fetches at 1-minute resolution internally, then resamples to 5m/15m/30m.
- OHLCV and CBBO paths both normalize indexes to **UTC** before cache/use.
- Synthesizes bid/ask spreads at 5% around mid price (OHLCV path).
- Thread-safe caching via `OptionDataCache`.

## Caching Rules

- `StockDataCache` uses append-only Parquet partitions per ticker/interval with zstd compression.
- `FuturesDataCache` uses dual partitions: `asset=fut/symbol=` for bars and
  `asset=oof/root=` for options-on-futures; SQLite tracks coverage and resolved
  `raw_symbol` per OOF contract.
- SQLite metadata tracks `(ticker, interval)` coverage with `min_ts_utc` / `max_ts_utc`.
- Incremental fetching fills gaps — never re-download existing ranges.
- **Never bypass the cache layer** — all data access goes through the handler, not raw API calls.

## PR Review Checks

- [ ] New API calls respect rate limits (use `_wait_for_rate_limit()`).
- [ ] Timezone handling is explicit — vendors UTC; intraday equity returns UTC-naive; CBBO cache UTC-aware; naive means UTC.
- [ ] No fixed UTC−4/UTC−5 offsets for ET; use `America/New_York` / `et_wall_to_utc_naive` for wall-clock → UTC-naive.
- [ ] Option symbol formatting matches the provider being used.
- [ ] Cache invalidation or schema changes are backward-compatible with existing Parquet files.
- [ ] No API keys or secrets in code — use `.env` and `os.environ`.
