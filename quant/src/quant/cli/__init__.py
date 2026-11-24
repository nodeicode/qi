"""Qt command-line entry points (spec `backtesting`, `execution`; task group 8).

argparse + stdlib only — no extra runtime dependencies. Four console scripts are
declared in ``pyproject.toml`` under ``[project.scripts]``:

* ``quant-backtest`` — offline, deterministic backtest on the reference equity
  strategy (synthetic bars — no network, no keys).
* ``quant-paper`` — zero-AWS dev-mode dry run: starts, polls a local config
  file, logs what it would do, exits cleanly (no boto3 required).
* ``quant-live`` — precondition-checked launcher for the real-time trader
  (requires the ``quant[alpaca]`` extra + Alpaca credentials).
* ``quant-portfolio`` — print portfolio metrics from a snapshot (or a small
  offline demo portfolio).

Each module exposes a ``main(argv=None) -> int`` callable and a
``build_parser() -> argparse.ArgumentParser``.
"""
