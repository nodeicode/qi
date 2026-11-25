"""Auxiliary executable for the two-process determinism test.

Runs the offline equity backtest once and prints a canonical JSON fingerprint
(trades + equity curve + the engine's decision transcript). The decision
transcript is the deterministic (event stream + author body + venue) record the
reshaped ``TradingEngine`` writes; ``test_determinism_two_process.py`` launches
this file in two *separate interpreter processes* and asserts identical output —
proving the decision sequence depends only on its inputs, not on process state
(random seeds, env, iteration order, wall clock).

Run directly: ``python quant/tests/backtest/_replay_runner.py``
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pandas as pd

# make ``quant`` importable (editable install normally covers this; be explicit
# when executed as a bare file) and reach the test fixtures.
_ROOT = Path(__file__).resolve().parents[3]  # repo root
sys.path.insert(0, str(_ROOT / "quant" / "src"))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from golden_fixtures import (
    END_DATE,
    START_DATE,
    TICKER,
    FakeDataHandler,
    GoldenEquityStrategy,
    make_config,
)

from quant.backtest.backtester import Backtester


def run_once() -> dict:
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
    transcript = bt.engine.transcript

    def _iso(v) -> str:
        return pd.Timestamp(v).isoformat()

    return {
        "trades": [
            {k: _iso(v) if isinstance(v, pd.Timestamp) else v for k, v in row.items()}
            for row in trades.to_dict("records")
        ],
        "equity_curve": [[_iso(ts), float(val)] for ts, val in bt.equity_curve.items()],
        "final_equity": float(bt.equity_curve.iloc[-1]),
        "transcript": transcript,
    }


if __name__ == "__main__":
    print(json.dumps(run_once(), sort_keys=True, default=str))
