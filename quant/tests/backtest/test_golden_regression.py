"""Golden end-to-end regression for the unified ``Backtester``.

Freezes ``Backtester.run_backtest`` output for a deterministic, fully-offline
equity strategy driven through the reshaped declarative path (engine → author
body → backtest venue). Refactor layers must not change the frozen trade log /
equity curve; a deliberate behavior change regenerates the fixture with
``GOLDEN_REGENERATE=1`` and reviews the diff.

No network access: ``Backtester.data_handler`` is swapped for a synthetic
``FakeDataHandler`` (the Alpaca instance the constructor creates is inert — it
never fetches).
"""

from __future__ import annotations

import json
import os
from pathlib import Path

import pandas as pd
import pytest

from quant.backtest.backtester import Backtester

from .golden_fixtures import (
    END_DATE,
    START_DATE,
    TICKER,
    FakeDataHandler,
    GoldenEquityStrategy,
    make_config,
)

FIXTURE = Path(__file__).parent / "fixtures" / "golden_equity_daily.json"


def _run_once() -> dict:
    cfg = make_config()
    bt = Backtester(
        cfg,
        GoldenEquityStrategy(cfg),
        START_DATE,
        END_DATE,
        TICKER,
        verbose=False,
        data_handler=FakeDataHandler(days=60),
    )
    trades = bt.run_backtest()
    bt.compute_equity_curve()

    def _iso(v) -> str:
        return pd.Timestamp(v).isoformat()

    return {
        "trades": [
            {
                k: _iso(v) if isinstance(v, (pd.Timestamp,)) else v
                for k, v in row.items()
            }
            for row in trades.to_dict("records")
        ],
        "equity_curve": [[_iso(ts), float(val)] for ts, val in bt.equity_curve.items()],
        "final_equity": float(bt.equity_curve.iloc[-1]),
        "transcript": bt.engine.transcript,
    }


@pytest.fixture(scope="module")
def golden_expected() -> dict:
    out = _run_once()
    if not FIXTURE.exists() or os.environ.get("GOLDEN_REGENERATE") == "1":
        FIXTURE.parent.mkdir(parents=True, exist_ok=True)
        FIXTURE.write_text(
            json.dumps(out, indent=2, sort_keys=True, default=str) + "\n"
        )
    return json.loads(FIXTURE.read_text())


def test_trade_log_matches_golden(golden_expected):
    actual = _run_once()
    assert actual["trades"] == golden_expected["trades"]


def test_equity_curve_matches_golden(golden_expected):
    actual = _run_once()
    assert actual["equity_curve"] == golden_expected["equity_curve"]


def test_run_is_deterministic():
    assert _run_once() == _run_once()


def test_final_equity_matches_golden(golden_expected):
    actual = _run_once()
    assert actual["final_equity"] == pytest.approx(
        golden_expected["final_equity"], abs=1e-9
    )


def test_transcript_is_recorded_and_deterministic():
    """The reshaped engine records a deterministic (event+body+venue) transcript."""
    first = _run_once()
    assert first["transcript"], "the run must record decision-transcript entries"
    assert _run_once()["transcript"] == first["transcript"]
