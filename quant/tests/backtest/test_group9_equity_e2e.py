"""Group 9 — integration verification on ``main`` (equity e2e, spec
``portfolio`` recovery / ``observability`` emission).

Proves the public repo is green from a clean state and that the reference
``example_equity`` decision pipeline works end-to-end:

* **backtest → portfolio** — ``Backtester`` streams simulated fills into a
  ``quant.portfolio.Portfolio`` (spec ``portfolio`` collection mode: the same
  fill-primitive live uses).
* **portfolio recovery across restart** — snapshot → temp JSON file →
  ``Portfolio.from_snapshot`` reproduces the exact pre-restart state, so a
  paper/live restart re-derives position + cash state without manual
  intervention (spec ``portfolio`` "Restart recovery" scenario).
* **paper smoke with an emitter sink** — the shared engine decision path
  (``process_strategy``, byte-identical in backtest and live) runs over the
  same synthetic bars with a ``monitor_emit`` sink; span rows are emitted
  (``signal_evaluated`` with latency fields, plus ``execution_dispatch``
  spans). This is exactly the span stream the live monitor would consume.

Everything here is offline + deterministic (the same synthetic fixture as the
group 7.3 golden ``example_equity`` regression) — no keys, no network.
"""

from __future__ import annotations

import json

import pandas as pd

from quant.backtest.backtester import Backtester
from quant.core.engine import TradingEngine
from quant.observability import SpanName, span_key
from quant.portfolio.portfolio import Portfolio
from quant.strategies.example_equity.strategy import ExampleEquityStrategy

from .example_equity_fixtures import (
    END_DATE,
    START_DATE,
    TICKER,
    ExampleEquityDataHandler,
    load_example_equity_bars,
    make_example_config,
)


def _run_equity_backtest() -> tuple[Backtester, Portfolio]:
    """Run the example_equity backtest with fill-collection into a Portfolio."""
    cfg = make_example_config()
    portfolio = Portfolio(starting_equity=cfg.INITIAL_CAPITAL)
    bt = Backtester(
        cfg,
        ExampleEquityStrategy(cfg),
        START_DATE,
        END_DATE,
        TICKER,
        verbose=False,
        data_handler=ExampleEquityDataHandler(),
        portfolio=portfolio,
    )
    trades = bt.run_backtest()
    bt.compute_equity_curve()
    assert not trades.empty
    return bt, portfolio


def test_backtest_streams_fills_into_portfolio() -> None:
    """Backtest fills land in the Portfolio primitive (same as live)."""
    bt, portfolio = _run_equity_backtest()
    # Every simulated round-trip produces a settled position; the trade log and
    # the portfolio ledger must both be non-empty and agree on the count.
    assert portfolio.closed_positions, "backtest fills must settle portfolio positions"
    assert len(portfolio.closed_positions) == len(bt.trade_log)
    # Cash moved to buy the positions → it differs from the initial capital.
    assert portfolio.cash < portfolio.starting_equity
    assert not bt.equity_curve.empty
    assert len(portfolio.positions) + len(portfolio.closed_positions) > 0


def test_portfolio_recovers_across_restart(tmp_path) -> None:
    """Snapshot to a persistent file, then recover exactly on "restart"."""
    _, portfolio = _run_equity_backtest()
    snapshot = portfolio.to_snapshot()

    state_file = tmp_path / "portfolio_snapshot.json"
    state_file.write_text(json.dumps(snapshot, default=str), encoding="utf-8")

    # Simulated restart: a fresh Portfolio is rebuilt purely from persisted state.
    recovered = Portfolio.from_snapshot(
        json.loads(state_file.read_text(encoding="utf-8"))
    )

    assert recovered.to_snapshot() == snapshot, (
        "recovered portfolio must match the pre-restart snapshot exactly"
    )
    assert recovered.cash == portfolio.cash
    assert len(recovered.closed_positions) == len(portfolio.closed_positions)
    assert {k: v.qty for k, v in recovered.positions.items()} == {
        k: v.qty for k, v in portfolio.positions.items()
    }


class _PaperExecutor:
    """Minimal venue execution-client stand-in for the paper smoke.

    Parameter names match the engine's keyword calls (``ran_signal`` etc.);
    ``get_precomputed_signal`` returns ``None`` so the engine computes signals
    exactly as the live/trader path would on a bar.
    """

    def should_run(self, strategy, now) -> tuple[bool, bool]:
        return (True, True)

    def data_end_dt(self, now):
        return pd.Timestamp(now) + pd.Timedelta(hours=1)

    def get_precomputed_signal(self, ticker, now) -> pd.DataFrame | None:
        return None

    def active_positions(self, strategy_name) -> list:
        return []

    def record_run(self, strategy_name, now, ran_signal) -> None:
        pass

    def after_process_ticker(self, strategy_name, ticker, results) -> None:
        pass

    def enter(self, strategy, ticker, signal_row, engine, *, now=None):
        return None

    def exit(self, strategy_name, position, sig, *, manage_df=None) -> bool:
        return True

    def after_entry(self, strategy_name, position) -> None:
        pass

    def after_exit(self, strategy_name, position) -> None:
        pass


def test_paper_smoke_emits_span_rows() -> None:
    """The shared decision path emits span rows through an emitter sink.

    This is the "paper" smoke: it runs ``process_strategy`` (the exact
    one-pipeline entrypoint live also drives) over synthetic bars with a
    ``monitor_emit`` sink and asserts both the ``signal_evaluated`` latency
    rows and the ``execution_dispatch`` spans surface.
    """
    cfg = make_example_config()
    strategy = ExampleEquityStrategy(cfg)
    sink: list[dict] = []
    engine = TradingEngine(
        strategy, ExampleEquityDataHandler(), monitor_emit=sink.append
    )
    bars = load_example_equity_bars()
    ed_key = span_key(SpanName.EXECUTION_DISPATCH)

    signal_rows_seen = False
    ed_span_seen = False
    for ts in bars.index[30:60]:  # skip early warm-up lookback; bounded loop
        engine.process_strategy(strategy, ts.to_pydatetime(), _PaperExecutor())
        for ev in sink:
            if ev.get("kind") == "signal_evaluated":
                signal_rows_seen = signal_rows_seen or "generate_signals_ns" in ev
            if ed_key in ev:
                ed_span_seen = True
        if signal_rows_seen and ed_span_seen:
            break

    assert sink, "the emitter sink must collect span rows"
    assert signal_rows_seen, "signal_evaluated span rows must carry generate_signals_ns"
    assert ed_span_seen, "execution-dispatch span must be emitted"

    keys = {k for ev in sink for k in ev if k.endswith("_ns")}
    assert keys, "span rows must carry latency fields"
    assert "generate_signals_ns" in keys
    assert ed_key in keys
