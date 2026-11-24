"""Golden offline regression for ``example_equity`` (spec backtesting, 7.3).

Freezes ``Backtester.run_backtest`` output for the public reference strategy
over **stable synthetic bars stored on disk** (``quant/tests/data/fixtures/``),
served entirely offline — no API keys, no network, no alpaca/boto3. Refactor
layers must not change the frozen trade log / equity curve; a deliberate
behavior change regenerates the fixture with ``GOLDEN_REGENERATE=1``.
"""

from __future__ import annotations

import json
import os

import pandas as pd
import pytest

from quant.backtest.backtester import Backtester
from quant.strategies.example_equity.strategy import ExampleEquityStrategy

from .example_equity_fixtures import (
    END_DATE,
    GOLDEN_JSON,
    START_DATE,
    TICKER,
    ExampleEquityDataHandler,
    make_example_config,
)


def _run_once() -> dict:
    cfg = make_example_config()
    bt = Backtester(
        cfg,
        ExampleEquityStrategy(cfg),
        START_DATE,
        END_DATE,
        TICKER,
        verbose=False,
        data_handler=ExampleEquityDataHandler(),
    )
    trades = bt.run_backtest()
    bt.compute_equity_curve()

    def _iso(v) -> str:
        return pd.Timestamp(v).isoformat()

    return {
        "trades": [
            {k: _iso(v) if isinstance(v, pd.Timestamp) else v for k, v in row.items()}
            for row in trades.to_dict("records")
        ],
        "equity_curve": [[_iso(ts), float(val)] for ts, val in bt.equity_curve.items()],
        "final_equity": float(bt.equity_curve.iloc[-1]),
    }


@pytest.fixture(scope="module")
def golden_expected() -> dict:
    if not GOLDEN_JSON.exists() or os.environ.get("GOLDEN_REGENERATE") == "1":
        data = _run_once()
        GOLDEN_JSON.parent.mkdir(parents=True, exist_ok=True)
        GOLDEN_JSON.write_text(json.dumps(data, indent=2, sort_keys=True) + "\n")
    return json.loads(GOLDEN_JSON.read_text())


def test_runs_offline_with_no_api_keys():
    """The example_equity backtest path needs no keys/network — brokerless.

    A synthetic on-disk data handler feeds the run, so no alpaca/boto3/credentials
    are ever touched (see `example_equity_fixtures`).
    """
    data = _run_once()
    assert data["trades"], "offline run must produce trades"


def test_trade_log_matches_golden(golden_expected):
    assert _run_once()["trades"] == golden_expected["trades"]


def test_equity_curve_matches_golden(golden_expected):
    assert _run_once()["equity_curve"] == golden_expected["equity_curve"]


def test_final_equity_matches_golden(golden_expected):
    actual = _run_once()
    assert actual["final_equity"] == pytest.approx(
        golden_expected["final_equity"], abs=1e-9
    )


def test_run_is_deterministic():
    assert _run_once() == _run_once()
