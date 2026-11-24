"""Validation of the outright-futures foundation - contract math + full E2E.

Layer 2 acceptance (plain futures first, OOF deferred):

* **Contract math**: seeded ES/MES/SR3 specs obey ``tick_value == multiplier x\n  tick_size``; ``round_price`` fills are conservative; ``ContractSpec.pnl``\n  matches the $/point convention.\n* **Full pipeline E2E**: Databento-shaped MES bars (real handler processing,\n  offline) -> signals -> ``FuturesExecutor`` enter/exit -> trade log whose\n  prices are tick-rounded, contracts follow the margin model, and P&L equals\n  ``(exit - entry) x multiplier x n x dir`` exactly.\n* **Fail-loud**: an unregistered product root aborts the backtest before the\n  bar loop instead of silently mis-executing.\n\nRun: ``pytest tests/backtest/test_futures_validation.py -v``\n"""

from __future__ import annotations

import logging
from typing import cast

import numpy as np
import pandas as pd
import pytest

from quant.backtest.backtester import Backtester
from quant.backtest.executors.futures import resolve_futures_root
from quant.core._utils import PRODUCT_SPECS

from .futures_fixtures import (
    MES_SYMBOL,
    FakeFuturesHandler,
    FuturesConfig,
    GoldenFuturesStrategy,
    make_databento_raw_frame,
    make_futures_bars,
)

START = "2024-01-02"
END = "2024-01-09"
_WINDOW_MINUTES = 7 * 24 * 60  # one full 15m-resample window

# ─────────────────────────────────────────────────────────────────────────────
# Contract math
# ─────────────────────────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "root,multiplier,tick_size,tick_value",
    [
        ("ES", 50.0, 0.25, 12.50),
        ("MES", 5.0, 0.25, 1.25),
        ("SR3", 2500.0, 0.005, 12.50),
    ],
)
def test_spec_tick_consistency(root, multiplier, tick_size, tick_value):
    spec = PRODUCT_SPECS.get(root)
    assert spec.multiplier == pytest.approx(multiplier)
    assert spec.tick_size == pytest.approx(tick_size)
    assert spec.tick_value == pytest.approx(tick_value)
    # Internal consistency: a tick is exactly multiplier * tick_size $.
    assert spec.tick_value == pytest.approx(spec.multiplier * spec.tick_size)


def test_round_price_directions():
    spec = PRODUCT_SPECS.get("MES")  # tick 0.25
    price = 5600.31
    assert spec.round_price(price) == 5600.25  # nearest
    assert spec.round_price(price, side="ask") == 5600.50  # pay up
    assert spec.round_price(price, side="bid") == 5600.25  # receive down
    assert spec.round_price(5600.25, side="ask") == 5600.25  # on-tick unchanged


def test_pnl_formula():
    spec = PRODUCT_SPECS.get("MES")  # $5/point
    assert spec.pnl(5600.0, 5601.0, 3, 1) == pytest.approx(15.0)  # +1pt * $5 * 3
    assert spec.pnl(5600.0, 5599.0, 3, 1) == pytest.approx(-15.0)
    assert spec.pnl(5600.0, 5599.0, 3, -1) == pytest.approx(
        15.0
    )  # short wins down-move


def test_round_trip_fill_sides():
    spec = PRODUCT_SPECS.get("MES")
    # Buy pays ask-rounded-up; selling back pays (receives) bid-rounded-down,
    # so a round trip on an untraded bar costs at least one tick per leg.
    raw_ask, raw_bid = 5600.375, 5600.125
    entry = spec.round_price(raw_ask, side="ask")
    exit_ = spec.round_price(raw_bid, side="bid")
    assert entry > raw_ask and exit_ < raw_bid


# ─────────────────────────────────────────────────────────────────────────────
# Full pipeline E2E
# ─────────────────────────────────────────────────────────────────────────────


def _run(ticker: str = MES_SYMBOL, **cfg_overrides) -> tuple[Backtester, pd.DataFrame]:
    cfg = FuturesConfig(**cfg_overrides)
    strat = GoldenFuturesStrategy(cfg)
    bt = Backtester(
        cfg,
        strat,
        START,
        END,
        ticker,
        verbose=False,
        data_handler=FakeFuturesHandler(
            make_databento_raw_frame(
                start=pd.Timestamp("2024-01-01", tz="UTC"), n_minutes=_WINDOW_MINUTES
            )
        ),
    )
    return bt, bt.run_backtest()


def test_e2e_produces_trades_with_contract_math():
    _bt, trades = _run()
    assert not trades.empty, "expected at least one MES trade"

    for _, t in trades.iterrows():
        assert t["Root"] == MES_SYMBOL
        assert t["Multiplier"] == pytest.approx(5.0)
        assert t["TickValue"] == pytest.approx(1.25)
        # Tick-aligned fills (MES tick = 0.25).
        assert t["EntryPrice"] % 0.25 == pytest.approx(0.0)
        assert t["ExitPrice"] % 0.25 == pytest.approx(0.0)
        # P&L exactly (exit-entry)*mult*contracts*direction.
        expected = (
            (t["ExitPrice"] - t["EntryPrice"]) * 5.0 * t["Contracts"] * t["Direction"]
        )
        assert t["PnL"] == pytest.approx(expected, abs=1e-6)
        assert t["Contracts"] >= 1


def test_margin_model_controls_contracts():
    bt, trades = _run()
    cfg = bt.config  # FuturesConfig: CAPITAL_PER_TRADE=0.1, MARGIN_FRACTION=0.10
    for _, t in trades.iterrows():
        # n = floor(capital // (frac * entry * multiplier)) — recompute.
        capital = cfg.INITIAL_CAPITAL * cfg.CAPITAL_PER_TRADE
        margin = (
            cast("float", getattr(cfg, "FUTURES_MARGIN_FRACTION"))
            * t["EntryPrice"]
            * 5.0
        )
        assert t["Contracts"] == int(capital // margin)


def test_exit_coverage_includes_strategy_manage_and_force_close():
    _, trades = _run()
    reasons = set(trades["Reason"])
    # manage_trade (GoldenFuturesStrategy) drives exits.  Like equity, a
    # forced end-of-window close keeps the position's row Reason, so assert
    # the strategy reason plus a sane exit-time range.
    assert "golden_futures" in reasons
    assert pd.to_datetime(trades["ExitTime"]).notna().all()
    assert len(trades) >= 1


def test_run_is_deterministic():
    _bt1, t1 = _run()
    _bt2, t2 = _run()
    assert t1.equals(t2)


def test_forward_progression_duration_positive():
    _bt, trades = _run()
    assert (trades["DurationMinutes"] > 0).all()
    assert (
        pd.to_datetime(trades["ExitTime"]) >= pd.to_datetime(trades["EntryTime"])
    ).all()


# ─────────────────────────────────────────────────────────────────────────────
# Root resolution + fail-loud
# ─────────────────────────────────────────────────────────────────────────────


def test_root_resolved_from_symbol_and_config():
    from types import SimpleNamespace

    cfg = SimpleNamespace(FUTURES_ROOT="ES")
    assert resolve_futures_root("ES", cfg) == "ES"
    assert resolve_futures_root("MES.MAR25", cfg) == "ES"  # explicit wins
    assert resolve_futures_root("MES.MAR25", SimpleNamespace()) == "MES"  # stem
    assert resolve_futures_root("SR3", SimpleNamespace()) == "SR3"


def test_unregistered_root_aborts_before_loop(caplog):
    with pytest.raises(KeyError, match="OIL"):
        _run(ticker="OIL", FUTURES_ROOT="OIL")


def test_unregistered_root_with_symbol_aborts(caplog):
    # No explicit FUTURES_ROOT -> stem of the dated symbol is used and must
    # resolve; OIL is not in the product table -> abort before the loop.
    with pytest.raises(KeyError, match="OIL"):
        _run(ticker="OIL.MAR25", FUTURES_ROOT=None)


# ─────────────────────────────────────────────────────────────────────────────
# Bar shape (real handler pipeline)
# ─────────────────────────────────────────────────────────────────────────────


def test_futures_bars_have_bid_ask_and_utc_naive_index():
    bars = make_futures_bars(n_minutes=480)
    assert {
        "Open",
        "High",
        "Low",
        "Close",
        "Volume",
        "bid",
        "ask",
        "mid",
        "symbol",
    }.issubset(bars.columns)
    assert cast("pd.DatetimeIndex", bars.index).tz is None  # UTC-naive intraday
    assert (bars["ask"] >= bars["bid"]).all()
