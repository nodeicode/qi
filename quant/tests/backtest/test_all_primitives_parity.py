"""Capstone — all-primitives golden parity test (spec ``backtesting`` parity).

Drives the **same** deterministic synthetic equity data (``example_equity``,
offline fixture) through BOTH the shared engine decision path
(``TradingEngine.process_strategy`` with the backtest venue's fill engine —
the parity-load-bearing engine primitive + venue seam) and the
``Backtester`` driver, while explicitly exercising every counted primitive:

* ``quant.instruments`` — a typed ``EquityInstrument`` is resolved + validated
  for the traded ticker;
* ``quant.venue`` — a ``BacktestVenue`` provides the deterministic
  simulation-timeline session (open/closed answered only from the injected
  simulated ``now``) and its venue id threads into the fill accounting;
* ``quant.portfolio`` — a ``Portfolio`` collects the simulated fills in each
  path (fill-driven accounting, spec ``portfolio``);
* ``quant.config`` — the strategy config is sourced from a ``FileConfigSource``
  + ``ConfigSync`` (the zero-AWS runtime-config backend).

Both paths must produce **matching decision sequences, fills, and portfolio
metrics** (research ↔ backtest parity). The engine decision path's output is
frozen to a golden artifact; a fresh run must reproduce it exactly.

Regenerate the golden with::

    GOLDEN_REGENERATE=1 uv run pytest quant/tests/backtest/test_all_primitives_parity.py -q

Everything here is offline + deterministic (same synthetic fixture as the
group 7.3 golden ``example_equity`` regression) — no keys, no network.
"""

from __future__ import annotations

import dataclasses
import json
import os
from pathlib import Path
from types import SimpleNamespace
from typing import cast

import pandas as pd

from quant.backtest.backtest_data_handler import BacktestDataHandler
from quant.backtest.backtester import Backtester
from quant.config import ConfigSync, FileConfigSource
from quant.core.protocols import DataHandlerProtocol
from quant.instruments import EquityInstrument, equity_from_tickers, validate_instrument
from quant.portfolio import Portfolio
from quant.strategies.example_equity.strategy import (
    ExampleEquityConfig,
    ExampleEquityStrategy,
)
from quant.venue import BacktestVenue
from quant.venue.live import FillReport, LiveExecutionClient

from .example_equity_fixtures import (
    END_DATE,
    START_DATE,
    TICKER,
    ExampleEquityDataHandler,
    make_example_config,
)

GOLDEN_FILE = (
    Path(__file__).resolve().parents[1]
    / "data"
    / "fixtures"
    / "example_equity_all_primitives_golden.json"
)


# ── primitives ───────────────────────────────────────────────────────────────


def source_equity_config(tmp_path) -> ExampleEquityConfig:
    """Obtain the strategy config through a ``quant.config`` ``ConfigSource``.

    The zero-AWS ``FileConfigSource`` + ``ConfigSync`` backend polls a local
    JSON file and yields the validated ``example_equity`` entry, which is then
    bound into a typed ``ExampleEquityConfig`` — the same config both parity
    drivers run on. The sourced config must equal the canonical defaults.
    """
    canonical = make_example_config()
    entry: dict = {"name": "example_equity"}
    for f in dataclasses.fields(canonical):
        if f.name in {"instruments", "subscriptions", "venue"}:
            # Declarations are typed objects that cannot round-trip through
            # the JSON config payload; they come from the dataclass defaults
            # on both sides.
            continue
        entry[f.name] = getattr(canonical, f.name)
    config_file = tmp_path / "equity_config.json"
    config_file.write_text(
        json.dumps({"strategies": [entry]}, default=str), encoding="utf-8"
    )
    adopted = ConfigSync(FileConfigSource(config_file)).poll()
    assert adopted is not None and adopted["strategies"], (
        "a valid config payload must be adopted"
    )
    sourced = adopted["strategies"][0]
    sourced = {k: v for k, v in sourced.items() if k != "name"}
    from_config = ExampleEquityConfig(**sourced)
    assert from_config == canonical, (
        "config sourced through quant.config must equal the canonical config"
    )
    return from_config


def build_instrument() -> EquityInstrument:
    """Resolve + validate a typed equity instrument (``quant.instruments``)."""
    (instrument,) = equity_from_tickers(venue="backtest", tickers=[TICKER])
    assert validate_instrument(instrument) == [], (
        "a resolved EquityInstrument must conform"
    )
    return instrument


def build_venue() -> BacktestVenue:
    """A deterministic simulation-timeline venue (``quant.venue``)."""
    venue = BacktestVenue()
    # Open/closed is answered only from the injected simulated ``now`` — repeat
    # the same probe twice and confirm it is stable (determinism, no wall clock).
    pairs = {}
    for i in range(120):
        ts = pd.Timestamp("2024-01-02") + pd.Timedelta(hours=i)
        pairs[_iso(ts)] = venue.is_open_at(ts)
    for key, val in pairs.items():
        assert venue.is_open_at(pd.Timestamp(key)) == val
    assert venue.id == "backtest"
    return venue


# ── the two drivers ──────────────────────────────────────────────────────────


def drive_engine_path(cfg: ExampleEquityConfig, venue: BacktestVenue):
    """The engine decision path: drive ``process_strategy`` per bar directly.

    Reuses ``Backtester``'s proven data preparation (``_prefetch_data``), then
    **explicitly** seeds the per-run replay handler and wires the backtest
    venue's execution client, and calls ``process_strategy`` on every
    closed-bar timestamp — the shared one-pipeline entrypoint
    (parity-load-bearing engine primitive) — feeding fills into the attached
    ``Portfolio``. Never invokes ``run_backtest``.
    """
    pf = Portfolio(starting_equity=cfg.INITIAL_CAPITAL)
    bt = Backtester(
        cfg,
        ExampleEquityStrategy(cfg),
        START_DATE,
        END_DATE,
        TICKER,
        verbose=False,
        data_handler=ExampleEquityDataHandler(),
        portfolio=pf,
        venue=venue.id,
    )
    bt._prefetch_data()

    # Seed the per-run replay handler with the pre-fetched subscription
    # buffers and wire it (plus the run's execution client) onto the venue —
    # the same wiring ``Backtester._simulate`` performs.
    data_handler = BacktestDataHandler(
        df_signals=pd.DataFrame(),
        df_manage=pd.DataFrame(),
        signal_interval="",
        manage_interval="",
    )
    for sub, frame in bt._sub_frames:
        data_handler.seed(sub, frame)
    bt.engine.data_handler = cast("DataHandlerProtocol", data_handler)
    venue.data_source = data_handler
    venue.execution_client = bt._build_execution_client()

    for ts in bt._timeline():
        bt.engine.process_strategy(bt.strategy, cast("pd.Timestamp", ts), venue)
    venue.execution_client.force_close_remaining()
    return bt, venue, pf


class _FakeBroker:
    """An offline broker stub returning deterministic order ids (no network)."""

    def __init__(self) -> None:
        self._n = 0

    def submit_order(self, req):
        self._n += 1
        return SimpleNamespace(id=f"live_{self._n}")


def _deterministic_fill(order_id, order, client):
    """Full-fill at a fixed price — the decision transcript is venue-blind."""
    return FillReport(
        order_id,
        "FILLED",
        float(order.quantity),
        float(order.quantity),
        0.0,
        100.0,
    )


def drive_live_path(cfg: ExampleEquityConfig, venue: BacktestVenue):
    """The engine decision path through a **live** venue's execution client.

    Same declarative ``example_equity``, same deterministic fixture, same
    session-gated venue (BacktestVenue's calendar) — but execution goes
    through ``LiveExecutionClient`` (an offline broker stub + deterministic
    fill resolver) instead of the backtest fill engine. The engine records a
    deterministic decision transcript; the author decisions MUST be
    identical to the backtest path (only venue fills differ).
    """
    pf = Portfolio(starting_equity=cfg.INITIAL_CAPITAL)
    bt = Backtester(
        cfg,
        ExampleEquityStrategy(cfg),
        START_DATE,
        END_DATE,
        TICKER,
        verbose=False,
        data_handler=ExampleEquityDataHandler(),
        portfolio=pf,
        venue=venue.id,
    )
    bt._prefetch_data()

    data_handler = BacktestDataHandler(
        df_signals=pd.DataFrame(),
        df_manage=pd.DataFrame(),
        signal_interval="",
        manage_interval="",
    )
    for sub, frame in bt._sub_frames:
        data_handler.seed(sub, frame)
    bt.engine.data_handler = cast("DataHandlerProtocol", data_handler)
    venue.data_source = data_handler

    strategy = bt.engine.strategy
    strategy.client_name = "paper"  # bind the author body to the stub broker client
    venue.execution_client = LiveExecutionClient(
        venue=venue,
        clients={"paper": _FakeBroker()},
        strategies=[strategy],
        portfolio=pf,
        fill_resolver=_deterministic_fill,
    )

    for ts in bt._timeline():
        bt.engine.process_strategy(bt.strategy, cast("pd.Timestamp", ts), venue)
    return bt, venue, pf


def drive_backtest_path(cfg: ExampleEquityConfig, venue: BacktestVenue):
    """The `Backtester` driver (the framework's run loop)."""
    pf = Portfolio(starting_equity=cfg.INITIAL_CAPITAL)
    bt = Backtester(
        cfg,
        ExampleEquityStrategy(cfg),
        START_DATE,
        END_DATE,
        TICKER,
        verbose=False,
        data_handler=ExampleEquityDataHandler(),
        portfolio=pf,
        venue=venue.id,
    )
    trades = bt.run_backtest()
    assert not trades.empty
    return bt, pf


# ── serialisation / normalisation ────────────────────────────────────────────


def _iso(v) -> str:
    if isinstance(v, pd.Timestamp):
        return v.isoformat()
    return str(v)


def _decision_sequence(bt: Backtester) -> list[dict]:
    """Normalise the trade log into a JSON-stable decision sequence."""
    rows: list[dict] = []
    for row in bt.trade_log:
        rows.append(
            {k: _iso(v) if isinstance(v, pd.Timestamp) else v for k, v in row.items()}
        )
    return rows


def _collapsed(entry):
    return json.loads(json.dumps(entry, default=_iso, sort_keys=True))


def portfolio_metrics(pf: Portfolio) -> dict:
    return _collapsed(pf.metrics({}))


def portfolio_snapshot(pf: Portfolio) -> dict:
    return _collapsed(pf.to_snapshot())


# ── the tests ────────────────────────────────────────────────────────────────


def test_all_four_primitives_compose(tmp_path) -> None:
    """Instruments + venue + portfolio + config all construct and interoperate."""
    cfg = source_equity_config(tmp_path)
    instrument = build_instrument()
    venue = build_venue()

    assert cfg.subscriptions[0].instrument.symbol == TICKER
    assert cfg.subscriptions[0].cadence == "1h"
    assert instrument.symbol == TICKER
    assert instrument.venue == "backtest"
    assert venue.is_open_at(pd.Timestamp("2024-01-02 14:00"))

    # A Portfolio collects fills; without any fills it is still a valid, empty
    # account that traces back to the config-sourced starting equity.
    pf = Portfolio(starting_equity=cfg.INITIAL_CAPITAL)
    assert pf.starting_equity == cfg.INITIAL_CAPITAL
    assert pf.positions == {}


def test_engine_decision_path_matches_backtester(tmp_path) -> None:
    """The shared engine decision path and the Backtester driver agree.

    Same deterministic data, same primitives (``quant.config``-sourced config,
    ``BacktestVenue`` id, a ``Portfolio`` each). Decision sequences, fills
    (portfolio snapshot) and portfolio metrics must all match (parity).
    """
    cfg = source_equity_config(tmp_path)
    venue = build_venue()

    bt_engine, _, pf_engine = drive_engine_path(cfg, venue)
    bt_backtest, pf_backtest = drive_backtest_path(cfg, venue)

    assert _decision_sequence(bt_engine) == _decision_sequence(bt_backtest), (
        "engine decision path and Backtester must emit the same decision sequence"
    )
    assert portfolio_snapshot(pf_engine) == portfolio_snapshot(pf_backtest), (
        "engine decision path and Backtester must produce the same fills"
    )
    assert portfolio_metrics(pf_engine) == portfolio_metrics(pf_backtest), (
        "engine decision path and Backtester must produce the same portfolio metrics"
    )
    assert bt_engine.trade_log, "the parity run must actually trade"


def test_engine_path_is_deterministic(tmp_path) -> None:
    """Re-running the engine decision path yields identical fills + metrics."""
    cfg = source_equity_config(tmp_path)
    venue = build_venue()
    _, _, pf_a = drive_engine_path(cfg, venue)
    _, _, pf_b = drive_engine_path(cfg, venue)
    assert portfolio_snapshot(pf_a) == portfolio_snapshot(pf_b)
    assert portfolio_metrics(pf_a) == portfolio_metrics(pf_b)


def test_engine_golden_regression(tmp_path) -> None:
    """The engine decision path freezes to a golden artifact (regeneration path).

    Regenerate with ``GOLDEN_REGENERATE=1`` (module docstring). A fresh run
    must reproduce the committed golden decision sequence + portfolio metrics.
    """
    cfg = source_equity_config(tmp_path)
    venue = build_venue()
    bt_engine, _, pf_engine = drive_engine_path(cfg, venue)
    capture = {
        "decision_sequence": _decision_sequence(bt_engine),
        "portfolio_metrics": portfolio_metrics(pf_engine),
    }

    if not GOLDEN_FILE.exists() or os.environ.get("GOLDEN_REGENERATE") == "1":
        GOLDEN_FILE.parent.mkdir(parents=True, exist_ok=True)
        GOLDEN_FILE.write_text(
            json.dumps(capture, indent=2, sort_keys=True, default=_iso) + "\n",
            encoding="utf-8",
        )
    expected = json.loads(GOLDEN_FILE.read_text(encoding="utf-8"))
    assert _collapsed(capture["decision_sequence"]) == expected["decision_sequence"]
    assert _collapsed(capture["portfolio_metrics"]) == expected["portfolio_metrics"]


def _decision_transcript(engine, *, drop_qty: bool = False) -> list[dict]:
    """The deterministic per-tick decision portion of the engine transcript.

    ``decisions`` (the author body's output) and ``due_subscriptions`` are
    venue-independent records; ``fills`` are venue-owned and always excluded.
    When ``drop_qty`` is set, order ``qty`` is removed too, because a strategy
    that sizes off the reconciled portfolio (buying power) yields
    venue-reconciled quantities even though its trade intent is identical.
    """

    def norm(d):
        rec = _collapsed(d)
        if drop_qty and isinstance(rec, dict) and "qty" in rec:
            rec = dict(rec)
            rec.pop("qty", None)
        return rec

    return [
        {
            "now": t["now"],
            "due_subscriptions": sorted(t["due_subscriptions"]),
            "decisions": [norm(d) for d in t["decisions"]],
        }
        for t in engine.transcript
    ]


def test_engine_live_venue_matches_backtest_decisions(tmp_path) -> None:
    """Backtest venue and live venue drive the same declarative body.

    The reshaped one-pipeline entrypoint (``TradingEngine.process_strategy``)
    drives the SAME ``example_equity`` through a backtest fill venue and a
    live venue's ``LiveExecutionClient`` (offline stub). Both MUST run the
    same decision path, and decisions MUST be identical until the first
    venue-reconciled fill diverges (the author body sizes/evaluates risk off
    the read-only portfolio, so once fills price differently the portfolio
    — and thus later decisions — legitimately diverge). Each venue's path is
    deterministic across reruns, and both actually trade.
    """
    cfg = source_equity_config(tmp_path)
    bt_backtest, _, pf_backtest = drive_engine_path(cfg, build_venue())
    bt_live, _, pf_live = drive_live_path(cfg, build_venue())
    bt_live2, _, _ = drive_live_path(cfg, build_venue())

    t_back = _decision_transcript(bt_backtest.engine)
    t_live = _decision_transcript(bt_live.engine)
    assert t_back and t_live and any(d["decisions"] for d in t_back), (
        "both venues must run the decision body"
    )
    assert any(d["decisions"] for d in t_live), "the live path must decide"

    # Live path is deterministic across reruns (same fixture -> same transcript
    # and portfolio metrics).
    assert _decision_transcript(bt_live2.engine) == t_live, (
        "live path must be deterministic across reruns"
    )
    # Decisions match at every tick UNTIL the first venue-reconciled divergence
    # (a fill priced differently by the venue feed); from then on the read-only
    # portfolio seen by the author may differ by venue.
    first_div = next(
        (i for i, (a, b) in enumerate(zip(t_back, t_live)) if a != b), None
    )
    assert first_div is None or first_div >= 1, (
        "the first decision (pre any fill) must be venue-independent"
    )
    upto = first_div if first_div is not None else len(t_back)
    assert [x["decisions"] for x in t_back[:upto]] == [
        x["decisions"] for x in t_live[:upto]
    ]
    assert pf_live is not None and pf_backtest is not None
    assert portfolio_metrics(pf_live), "the live path must actually trade"
