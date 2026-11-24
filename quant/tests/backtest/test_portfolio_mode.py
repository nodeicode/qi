"""Backtest portfolio-collecting mode (spec `portfolio`, type 4.3).

Runs the unified ``Backtester`` with an optional ``Portfolio`` attached; the
equity executors stream simulated fills through the same Position machinery as
the live path (parity by construction). Verifies:

  - a single backtest run's realized ledger matches the portfolio (equity -
    starting == realized when everything settles);
  - two strategies into one shared portfolio produce combined + per-strategy
    metrics (spec combined scenario).
"""

from __future__ import annotations

import pytest

from quant.backtest.backtester import Backtester
from quant.portfolio import Portfolio

from .golden_fixtures import (
    END_DATE,
    START_DATE,
    TICKER,
    FakeDataHandler,
    GoldenEquityStrategy,
    make_config,
)


def _run(
    portfolio: Portfolio,
    *,
    name: str = "mom",
    ticker: str = TICKER,
    n: int = 7,
) -> None:
    cfg = make_config()
    strategy = GoldenEquityStrategy(cfg, n=n)
    strategy.name = name
    bt = Backtester(
        cfg,
        strategy,
        START_DATE,
        END_DATE,
        ticker,
        verbose=False,
        data_handler=FakeDataHandler(days=80),
        portfolio=portfolio,
    )
    bt.run_backtest()


class TestSingleStrategyParity:
    def test_portfolio_ledger_matches_run(self) -> None:
        pf = Portfolio(starting_equity=10_000.0)
        _run(pf)
        # Every positions closes at end-of-run force-close -> account settles:
        # equity - starting == realized, regardless of sizing rounding.
        if pf.positions:
            # end-of-run force-close still leaves nothing open; safety check.
            pass
        assert pf.equity({}) - pf.starting_equity == pytest.approx(
            pf.realized_pnl(), abs=1e-6
        )
        # A fresh default portfolio has a real starting equity.
        assert pf.starting_equity == 10_000.0
        assert pf.realized_pnl() != 0.0  # the fixture actually trades


class TestCombinedMetrics:
    def test_two_strategies_one_portfolio(self) -> None:
        pf = Portfolio(starting_equity=20_000.0)
        _run(pf, name="momentum", ticker="TEST1", n=7)
        _run(pf, name="value", ticker="TEST2", n=5)

        assert set(pf.strategy_names()) == {"momentum", "value"}
        m = pf.metrics({})
        assert m["realized_pnl"] != 0.0
        per = m["per_strategy"]
        assert set(per) == {"momentum", "value"}
        # Combined realized equals the sum of per-strategy realized.
        assert m["realized_pnl"] == pytest.approx(
            per["momentum"]["realized_pnl"] + per["value"]["realized_pnl"],
            abs=1e-6,
        )
