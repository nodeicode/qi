"""Strategy-owned sizing + risk exits (spec `strategy-authoring`).

Verifies the folded-in replacements for the deleted ``quant.core.sizing``
registry: the author-side helpers ``size_equity_lot`` / ``size_futures_margin``
size from instrument contract facts + the read-only portfolio, the
``exit_risk_hit`` predicate reproduces the legacy stop/take/max-hold
conditions, and a declarative author body folds both into ``decide`` — sizing
entries from margin/multiplier and evaluating its portfolio positions for
exits. A zero sizing result means no order.
"""

from __future__ import annotations

import datetime
from typing import ClassVar

import pytest

from quant.data import Datatype, Subscription
from quant.instruments import EquityInstrument, FutureInstrument
from quant.portfolio import Fill, Portfolio, Side
from quant.strategies.base import ExitIntent, Strategy
from quant.strategies.risk import exit_risk_hit
from quant.strategies.sizing import size_equity_lot, size_futures_margin


class _Config:
    """Declarative config shape: per-risk fields an author reads (lowercase)."""

    def __init__(self, **kw) -> None:
        self.__dict__.update(kw)


class _Events:
    """Minimal events stand-in: one bars frame per subscription symbol."""

    def __init__(self, frame) -> None:
        self._frame = frame

    def bars(self, symbol: str):
        return self._frame


def _ohlcv(close=100.0, n=1, start="2024-01-02 10:00", freq="1h"):
    import pandas as pd

    idx = pd.date_range(start, periods=n, freq=freq)
    return pd.DataFrame(
        {
            "Open": close,
            "High": close,
            "Low": close,
            "Close": close,
            "Volume": [1_000_000] * n,
        },
        index=idx,
    )


def _open_position(
    portfolio: Portfolio, instrument, *, price=100.0, at="2024-01-02 10:00"
) -> None:
    """Open one long position for *instrument* through the fill machinery."""
    portfolio.apply_fill(
        Fill(
            venue=instrument.venue,
            symbol=instrument.symbol,
            side=Side.BUY,
            qty=1,
            price=price,
            strategy="futures_ma",
            timestamp=at,
            instrument=instrument,
        )
    )


# ========================================================================
# size_equity_lot — equity contract facts + portfolio buying power
# ========================================================================


class TestSizeEquityLot:
    def test_sizes_from_buying_power_fraction(self) -> None:
        inst = EquityInstrument(symbol="SPY", venue="backtest")
        pf = Portfolio(starting_equity=100_000.0)
        # Buying power = equity (100k) * fraction (0.95) = 95k -> 95_000 / 100.
        qty = size_equity_lot(pf, 100.0, instrument=inst, buying_power_fraction=0.95)
        assert qty == 950

    def test_respects_lot_size(self) -> None:
        inst = EquityInstrument(symbol="SPY", venue="backtest", lot_size=100)
        pf = Portfolio(starting_equity=10_000.0)
        # 10_000 / 133 → 75 shares, floored to 100-share lots → 0 (no order).
        assert size_equity_lot(pf, 133.0, instrument=inst) == 0
        # 10_000 / 100 → 100 shares → exactly one lot.
        assert size_equity_lot(pf, 100.0, instrument=inst) == 100

    def test_zero_entry_price_means_no_trade(self) -> None:
        inst = EquityInstrument(symbol="SPY", venue="backtest")
        pf = Portfolio(starting_equity=10_000.0)
        assert size_equity_lot(pf, 0.0, instrument=inst) == 0

    def test_unaffordable_price_sizes_to_zero(self) -> None:
        inst = EquityInstrument(symbol="SPY", venue="backtest")
        pf = Portfolio(starting_equity=10_000.0)
        assert size_equity_lot(pf, 1_000_000.0, instrument=inst) == 0


# ========================================================================
# size_futures_margin — margin/multiplier + margin, never a capital fraction
# ========================================================================


class TestSizeFuturesMargin:
    def test_sizes_from_instrument_margin_fact(self) -> None:
        inst = FutureInstrument(
            symbol="ES", venue="backtest", multiplier=50.0, margin=12_000.0
        )
        pf = Portfolio(starting_equity=100_000.0)
        # 100_000 / 12_000 → 8 whole contracts.
        assert size_futures_margin(pf, 4_500.0, instrument=inst) == 8

    def test_margin_derived_from_fraction_times_notional(self) -> None:
        inst = FutureInstrument(symbol="ES", venue="backtest", multiplier=50.0)
        pf = Portfolio(starting_equity=100_000.0)
        # No instrument margin: 0.10 * 4_500 * 50 = 22_500/contract -> 4 contracts.
        assert (
            size_futures_margin(pf, 4_500.0, instrument=inst, margin_fraction=0.10) == 4
        )

    def test_explicit_margin_wins_over_instrument_fact(self) -> None:
        inst = FutureInstrument(
            symbol="ES", venue="backtest", multiplier=50.0, margin=12_000.0
        )
        pf = Portfolio(starting_equity=100_000.0)
        assert (
            size_futures_margin(
                pf, 4_500.0, instrument=inst, margin_per_contract=20_000.0
            )
            == 5
        )

    def test_margin_bigger_than_buying_power_sizes_to_zero(self) -> None:
        inst = FutureInstrument(
            symbol="ES", venue="backtest", multiplier=50.0, margin=12_000.0
        )
        pf = Portfolio(starting_equity=10_000.0)
        assert size_futures_margin(pf, 4_500.0, instrument=inst) == 0

    def test_zero_price_or_margin_means_no_trade(self) -> None:
        inst = FutureInstrument(symbol="ES", venue="backtest", multiplier=50.0)
        pf = Portfolio(starting_equity=100_000.0)
        assert size_futures_margin(pf, 0.0, instrument=inst) == 0
        assert (
            size_futures_margin(pf, 4_500.0, instrument=inst, margin_per_contract=0.0)
            == 0
        )


# ========================================================================
# exit_risk_hit — the legacy stop/take/max-hold conditions, folded
# ========================================================================


class TestExitRiskHit:
    def test_stop_loss_long(self) -> None:
        cfg = _Config(stop_loss_percent=0.05, take_profit_percent=-1.0)
        pos = _Config(entry_price=100.0, direction=1)
        assert exit_risk_hit(pos, cfg, spot=94.0) is True  # -6% <= -5%
        assert exit_risk_hit(pos, cfg, spot=96.0) is False  # -4% > -5%

    def test_stop_loss_short(self) -> None:
        cfg = _Config(stop_loss_percent=0.05, take_profit_percent=-1.0)
        pos = _Config(entry_price=100.0, direction=-1)
        assert exit_risk_hit(pos, cfg, spot=106.0) is True
        assert exit_risk_hit(pos, cfg, spot=104.0) is False

    def test_take_profit_long(self) -> None:
        cfg = _Config(stop_loss_percent=0.10, take_profit_percent=2.0)
        pos = _Config(entry_price=100.0, direction=1)
        assert exit_risk_hit(pos, cfg, spot=310.0) is True  # +210% ≥ +200%
        assert exit_risk_hit(pos, cfg, spot=290.0) is False

    def test_take_profit_legacy_minus_one_disables(self) -> None:
        # The legacy disabled sentinel: TAKE_PROFIT_PERCENT <= 0 never fires.
        cfg = _Config(stop_loss_percent=0.05, take_profit_percent=-1.0)
        pos = _Config(entry_price=100.0, direction=1)
        assert exit_risk_hit(pos, cfg, spot=1000.0) is False

    def test_stop_loss_legacy_uppercase_spelling(self) -> None:
        # Legacy uppercase config spellings are read as-is.
        cfg = _Config(STOP_LOSS_PERCENT=0.30, TAKE_PROFIT_PERCENT=-1.0)
        pos = _Config(entry_price=100.0, direction=1)
        assert exit_risk_hit(pos, cfg, spot=69.0) is True
        assert exit_risk_hit(pos, cfg, spot=71.0) is False

    def test_max_hold_from_legacy_config(self) -> None:
        cfg = _Config(MAX_HOLDING_MINUTES=60)
        entry = datetime.datetime(2024, 1, 2, 10, 0)
        pos = _Config(entry_time=entry, direction=1)
        at = datetime.datetime(2024, 1, 2, 11, 1)  # 61 minutes later
        assert exit_risk_hit(pos, cfg, at=at) is True
        at_ok = datetime.datetime(2024, 1, 2, 10, 59)  # 59 minutes later
        assert exit_risk_hit(pos, cfg, at=at_ok) is False

    def test_accepts_legacy_position_dict(self) -> None:
        cfg = _Config(STOP_LOSS_PERCENT=0.05, TAKE_PROFIT_PERCENT=-1.0)
        pos = {"EntryPrice": 100.0, "Direction": 1}
        assert exit_risk_hit(pos, cfg, spot=94.0) is True

    def test_no_spot_skips_stop_take_but_still_checks_max_hold(self) -> None:
        cfg = _Config(stop_loss_percent=0.05, take_profit_percent=-1.0)
        pos = _Config(entry_price=100.0, direction=1)
        assert exit_risk_hit(pos, cfg) is False  # no legs to evaluate
        held = _Config(
            entry_price=100.0,
            direction=1,
            entry_time=datetime.datetime(2024, 1, 1, 0, 0),
        )
        assert (
            exit_risk_hit(
                held,
                _Config(max_holding_minutes=10),
                at=datetime.datetime(2024, 1, 1, 0, 11),
            )
            is True
        )


# ========================================================================
# Declarative author body — sizing + risk exits folded into decide
# ========================================================================

_ES = FutureInstrument(symbol="ES", venue="backtest", multiplier=50.0, margin=12_000.0)


class FuturesMaCross(Strategy):
    """Declarative futures cross: sizes entries from margin/multiplier and
    evaluates its portfolio positions for risk exits — one body, both roles."""

    name = "futures_ma"
    venue = "backtest"
    config = _Config(
        stop_loss_percent=0.05,
        take_profit_percent=-1.0,
        max_holding_minutes=120,
    )
    instruments: ClassVar[list] = [_ES]
    subscriptions: ClassVar[list] = [
        Subscription(instrument=_ES, datatype=Datatype.BARS, cadence="1h")
    ]

    def decide(self, events, portfolio) -> list:
        bars = events.bars(_ES.symbol)
        spot = float(bars["Close"].iloc[-1])
        at = bars.index[-1]
        out: list = []

        # Manage pass — the folded risk-exit evaluation over the portfolio.
        # The injected portfolio exposes its open positions read-only; the
        # author filters for its own strategy's positions to evaluate exits.
        for pos in portfolio.positions.values():
            if pos.strategy != self.name:
                continue
            if exit_risk_hit(pos, self.config, spot=spot, at=at):
                out.append(ExitIntent(self.name, position=pos, reason="risk_exit"))

        # Signal pass — sizing from margin/multiplier + portfolio, no order
        # when the sizing is zero.
        qty = size_futures_margin(portfolio, spot, instrument=_ES)
        if qty > 0:
            out.append(_ES.make_order("BUY", qty, strategy=self.name))
        return out


class TestDecideFoldsSizingAndRiskExits:
    def test_sizes_entry_from_margin_and_signal_exit_from_risk(self) -> None:
        pf = Portfolio(starting_equity=100_000.0)
        _open_position(pf, _ES, price=4_400.0, at="2024-01-02 10:00")
        # Entry at 4_400; the market then gaps -6% -> the stop-loss leg fires.
        bars = _ohlcv(close=4_136.0, n=1, start="2024-01-02 10:00")
        out = FuturesMaCross().decide(_Events(bars), pf)

        assert any(isinstance(item, ExitIntent) for item in out)
        risk_exit = next(item for item in out if isinstance(item, ExitIntent))
        assert risk_exit.reason == "risk_exit"

        orders = [item for item in out if not isinstance(item, ExitIntent)]
        assert orders  # entry order for the cross
        order = orders[0]
        assert order.instrument is _ES
        assert order.quantity == 8  # 100_000 // 12_000 margin
        assert order.strategy == "futures_ma"

    def test_zero_sizing_produces_no_order(self) -> None:
        pf = Portfolio(starting_equity=10_000.0)
        bars = _ohlcv(close=4_500.0, n=1)
        out = FuturesMaCross().decide(_Events(bars), pf)
        # 10_000 < 12_000 margin → no contracts → no order emitted.
        assert all(isinstance(item, ExitIntent) for item in out) or out == []

    def test_risk_exits_fire_on_the_legacy_max_hold_condition(self) -> None:
        pf = Portfolio(starting_equity=100_000.0)
        _open_position(pf, _ES, price=4_400.0, at="2024-01-02 10:00")
        # 3 hours later (> max_holding_minutes=120) — the legacy max-hold
        # condition the old manage evaluation classified as a max_hold exit.
        bars = _ohlcv(close=4_400.0, n=1, start="2024-01-02 13:00")
        out = FuturesMaCross().decide(_Events(bars), pf)
        assert any(isinstance(item, ExitIntent) for item in out)


def test_exit_intent_carries_strategy_position_reason() -> None:
    intent = ExitIntent("futures_ma", position=object(), reason="stop")
    assert intent.strategy == "futures_ma"
    assert intent.reason == "stop"
