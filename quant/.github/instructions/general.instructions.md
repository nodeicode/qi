---
description: "Use when reviewing any Python code in the quant package. Covers project-wide conventions, dependency management, typing, and Numba rules."
applyTo: "**/*.py"
---

# Quant Package — General Conventions

## Python & Dependencies

- **Python 3.12** only (`>=3.12,<3.13`) — pinned because Numba `.nbc` cache files are CPU/version-specific.
- All dependencies are pinned with `==` in `pyproject.toml` for reproducibility.
- Package manager: **uv** (not pip). Install with `uv sync` (core deps + dev group).
- New dependencies must be pinned to exact versions and justified in the PR description.

## Naming Conventions

| Element           | Convention        | Examples                                    |
| ----------------- | ----------------- | ------------------------------------------- |
| Config fields     | UPPERCASE         | `CAPITAL_PER_TRADE`, `MAX_HOLDING_MINUTES`  |
| Classes           | PascalCase        | `TradingEngine`, `AlpacaDataHandler`        |
| Methods/functions | snake_case        | `calculate_indicators`, `fetch_option_data` |
| Private members   | underscore prefix | `_signal_cache`, `_manage_error_count`      |
| Numba functions   | `*_numba` suffix  | `manage_position_numba`, `_rsi_numba`       |
| Signal types      | UPPERCASE enum    | `BUY`, `SELL_IC`, `BUY_SPREAD`              |

## Typing

This codebase uses modern Python typing extensively:

- `Protocol` for duck typing (e.g., `StrategyProtocol`)
- `TypedDict` for structured dicts (e.g., `PositionDict`)
- `StrEnum` for string constants (e.g., `SignalType`)
- Union syntax: `str | None` (not `Optional[str]`)
- Use `Sequence`, `cast`, generics where appropriate

## Numba Rules

- The `@njit` decorator is required for hot-path trade management and indicator computation.
- **No Python objects** inside `@njit` — no strings, dicts, lists of mixed types, or class instances.
- Exit parameters are passed as a 12-element `float64` array — see `utils/constants.py` for named indices.
- Numba cache files (`.nbc`) are gitignored. Cache invalidation happens automatically on signature changes.

## Security

- API keys (`ALPACA_API_KEY`, `MASSIVE_API_KEY`, `DATABENTO_API_KEY`, etc.) live in `.env` — **never** hardcode.
- `.env` is gitignored. Required keys are documented in `README.md`.
- No credentials in logs — data handlers must sanitize any debug output.

## Project Structure

```
quant/
├── core/        # TradingEngine — unified decision layer
├── data/        # Data handlers, caching, option analytics
├── strategies/  # Strategy implementations (registry pattern)
├── backtest/    # Backtesting framework
├── utils/       # Shared utilities (indicators, config, ML)
└── tests/       # pytest suite
```

Each strategy lives in its own subfolder under `strategies/` with a config, utils, and README.
