"""Two-process determinism / replay guard (decision-engine capability).

The engine is a deterministic state machine: for identical (strategy, config,
bars, executor behavior) inputs it must reproduce an identical decision
sequence. Running the backtest in two separate interpreter processes and
comparing canonical fingerprints proves process-independence — no hidden
dependence on random seeds, environment, iteration order, or wall clock.

In-process double-runs are already covered by
``test_golden_regression.py::test_run_is_deterministic``; this file adds the
across-process guarantee.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pandas as pd

_RUNNER = Path(__file__).resolve().parent / "_replay_runner.py"
_ROOT = Path(__file__).resolve().parents[3]


def _replay() -> str:
    res = subprocess.run(  # noqa: S603 — argv list, no shell
        [sys.executable, str(_RUNNER)],
        cwd=str(_ROOT),
        capture_output=True,
        text=True,
        check=True,
    )
    return res.stdout.strip()


def _replay_parsed() -> dict:
    import json

    return json.loads(_replay())


def test_two_processes_produce_identical_decision_sequence():
    a = _replay()
    b = _replay()
    assert a == b


def test_replay_matches_expected_shapes():
    out = _replay_parsed()
    assert out["trades"]  # the run actually traded
    assert len(out["equity_curve"]) >= 2
    assert out["final_equity"] > 0
    # trades are time-ordered and reproducible (both stamps are ISO strings
    # after the JSON round-trip, so parse both sides before comparing)
    assert all(
        pd.to_datetime(t["EntryTime"]) <= pd.to_datetime(t["ExitTime"])
        for t in out["trades"]
    )
    # the reshaped engine records a deterministic decision transcript
    assert out["transcript"]
    assert isinstance(out["transcript"], list)
