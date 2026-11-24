"""
Single test runner for the merged quant-engine suite.

Tests live under ``quant/tests/`` (engine/data/backtest/utils units + the
folded ``live`` layer). ``uv run test`` at the repo root or inside ``quant/``
runs the one suite; ``-m`` scopes it.

    uv run test                  # whole suite
    uv run test -m live          # the folded live layer (trader/executors/monitor)
    uv run test -m unit          # live/unit only
    uv run test -m data          # engine data tests
    uv run test -k <filter>      # keyword filter (passed through to pytest)
"""

from __future__ import annotations

import argparse
import subprocess
import sys
from pathlib import Path

MODULES = (
    "data",  # quant/tests/data
    "backtest",  # quant/tests/backtest
    "strategies",  # quant/tests/strategies
    "live",  # quant/tests/live (the folded python/ layer)
    "unit",  # quant/tests/live/unit
)
_PATHS = {
    "data": "tests/data",
    "backtest": "tests/backtest",
    "strategies": "tests/strategies",
    "live": "tests/live",
    "unit": "tests/live/unit",
}


def _run(cwd: Path, paths: list[str], extra: list[str]) -> int:
    cmd = [sys.executable, "-m", "pytest", *paths, *extra]
    print(f"\n── pytest in {cwd} ──", flush=True)
    return subprocess.call(cmd, cwd=str(cwd))  # noqa: S603 — argv list, no shell


def main() -> None:
    cwd = Path.cwd()
    root = cwd / "quant" if (cwd / "quant").is_dir() else cwd
    parser = argparse.ArgumentParser(
        prog="test", description="Run the quant-engine test suite."
    )
    parser.add_argument(
        "-m",
        "--module",
        nargs="+",
        choices=MODULES,
        help="Run specific module(s) only.",
        metavar="MOD",
    )
    known, extra = parser.parse_known_args()

    if known.module:
        paths = [_PATHS[m] for m in known.module]
    else:
        paths = ["tests"]

    sys.exit(_run(root, paths, extra))


if __name__ == "__main__":
    main()
