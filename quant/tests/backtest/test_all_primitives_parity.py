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
