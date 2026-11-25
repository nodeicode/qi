"""Deterministic gate: no tz handling in the engine decision path.

The timestamp boundary lives in ``quant/data/_times.py``.
The decision-path code in the engine must be **read-only** over canonical
tz-naive-UTC stamps — no ``tz_localize`` / ``tz_convert`` /
``replace(tzinfo=...)`` / ``.tzinfo`` branching.

Allowed homes (which legitimately touch tz):
* ``core/_utils.py``            — pure calendar readers (daily completeness,
                                 session clocks, ``closed_bar_asof`` / ``bar_close_utc``).
* ``backtest/utils.py``         — the one sanctioned simulation mutator,
                                 ``calculate_fill_time``.
* ``backtest/base_backtest.py`` — metrics/reporting display normalization
                                 (daily frames may be aware per equity-session
                                 convention; stripped only for plotting).
* ``backtest/backtest_data_handler.py`` — is itself part of the handler
                                 boundary (routes through ``_times`` + asserts).
* ``data/**``                   — the boundary itself (handlers + cache internals).

Any tz pattern OUTSIDE these homes is a regression and fails here deterministically
(whether any single behavior test happens to exercise it).
"""

from __future__ import annotations

from pathlib import Path

import pytest

_ROOT = Path(__file__).resolve().parents[1] / "src" / "quant"

_DISALLOWED_RELS = [
    "core/engine.py",
    "core/protocols.py",
    "backtest/backtester.py",
    "backtest/base_backtest.py",
    "backtest/backtest_data_handler.py",
    "backtest/executors",
]

# base_backtest.py and backtest_data_handler.py are in DISALLOWED_RELS but
# allowed below for reporting-normalization / handler-boundary reasons.
_ALLOWED = {
    "backtest/base_backtest.py",
    "backtest/backtest_data_handler.py",
}

_TZ = (
    ".tzinfo",
    "tz_localize(",
    "tz_convert(",
    "replace(tzinfo",
    ".tz is",
)


def _scan(root: Path, rel: str) -> list[str]:
    if rel in _ALLOWED:
        # report/metrics normalization (base_backtest) and the handler boundary
        # (backtest_data_handler) are allowed wholesale.
        return []
    path = root / rel
    files = sorted(p for p in path.rglob("*.py")) if path.is_dir() else [path]
    hits: list[str] = []
    for f in files:
        for i, line in enumerate(f.read_text().splitlines(), 1):
            if any(pat in line for pat in _TZ):
                hits.append(f"{f.relative_to(_ROOT)}:{i}: {line.strip()}")
    return hits


def test_no_tz_handling_in_engine_decision_path():
    hits: list[str] = []
    for rel in _DISALLOWED_RELS:
        hits += _scan(_ROOT, rel)
    assert not hits, "tz handling leaked into the engine decision path:\n" + "\n".join(
        hits
    )
