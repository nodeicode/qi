---
description: "Use when reviewing or writing tests. Covers the test framework, fixture patterns, and what to test for strategies and engine code."
applyTo: "tests/**"
---

# Testing Standards

## Framework

- **pytest** with `uv run --python 3.12 pytest tests -v --tb=short`
- CI runs on every PR to `main` (see `.github/workflows/ci.yml`).

## Fixture Patterns

Use lightweight fakes, not mocks:

```python
def _make_config(**overrides) -> SimpleNamespace   # config stub
def _make_strategy(manage_return=-1) -> object     # StrategyProtocol-compliant stub
```

- `SimpleNamespace` for config objects — fast, no dataclass boilerplate needed in tests.
- Strategy stubs implement `calculate_indicators`, `generate_signals`, and optionally `manage_trade`.
- Pre-built DataFrames for indicator pipeline testing.

## What to Test

### Engine (`test_engine.py`)

- Signal mapping: every `SignalType` → correct `(asset_class, direction)`.
- Strike selection: ATM, ITM, OTM branching for calls/puts.
- Spread width: adaptive ($5/$10/$20) and config-based.
- Entry gate: confidence threshold, concurrency limit, signal actionability.
- Indicator injection: `merge_asof` alignment, daily shift, no-data fallback.
- Full `process_ticker` pipeline end-to-end.

### Strategies

- Indicator calculation produces expected columns without NaN in the output range.
- Signal generation returns `Signal` and `Confidence` for every bar.
- `manage_trade` returns valid exit index or `-1`.
- Walk-forward windows don't overlap with test sets (embargo check).

### Data Handlers

- Symbol formatting matches provider spec.
- Cache hit/miss behavior.
- Resampling accuracy (1m → 5m/15m/30m).

## Anti-Patterns

- **No network calls in tests** — all data handlers should be stubbed.
- **No sleeping or polling** — tests must be deterministic and fast.
- **No full backtest runs in unit tests** — use integration test markers if needed.
