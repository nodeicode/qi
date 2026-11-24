"""Primitive tests for the portfolio package (spec `portfolio`, design D4).

Covers the pure primitives (no AWS / no engine):
  - Position state machine: opened -> adjusting -> closed, fills-only mutation
  - Fill accounting: weighted average price, cost basis, realized PnL, flips
  - Per-(venue, symbol) keying: distinct positions across venues
  - Legacy PositionDict schema round-trip (to/from_position_dict)
  - Portfolio aggregation: cash/equity, realized + unrealized PnL, settled close
  - Per-strategy + combined metrics (spec combined scenario)
  - Snapshot persistence / restart recovery (spec persistence scenario)
"""

from __future__ import annotations

import pytest

from quant.instruments.base import AssetClass, Instrument, InstrumentClass
from quant.portfolio import Fill, Portfolio, Position, PositionState, Side


def _fill(
    venue: str = "paper",
    symbol: str = "SPY",
    side: Side = Side.BUY,
    qty: float = 10.0,
    price: float = 100.0,
    strategy: str | None = None,
    instrument: Instrument | None = None,
) -> Fill:
    return Fill(
        venue=venue,
        symbol=symbol,
        side=side,
        qty=qty,
        price=price,
        strategy=strategy,
        instrument=instrument,
    )


# ===========================================================================
# Position state machine
# ===========================================================================


class TestPositionStateMachine:
    def test_opened_from_opening_fill(self) -> None:
        pos = Position(_fill(price=100.0, qty=10))
        assert pos.state is PositionState.OPENED
        assert pos.signed_qty == 10
        assert pos.avg_price == 100.0
        assert pos.cost_basis == 1000.0
        assert pos.direction == 1

    def test_same_direction_fill_adjusts_weighted_average(self) -> None:
        pos = Position(_fill(price=100.0, qty=10))
        pos.apply(_fill(price=120.0, qty=10))  # buy 10 more at 120
        assert pos.state is PositionState.ADJUSTING
        assert pos.signed_qty == 20
        assert pos.avg_price == 110.0  # (10*100 + 10*120) / 20
        assert pos.cost_basis == 2200.0

    def test_partial_close_records_realized_and_stays_open(self) -> None:
        pos = Position(_fill(price=100.0, qty=10))
        pos.apply(_fill(price=120.0, qty=10))  # avg -> 110, qty 20
        pos.apply(_fill(side=Side.SELL, price=130.0, qty=5))  # sell 5 @ 130
        assert pos.state is PositionState.ADJUSTING
        assert pos.signed_qty == 15
        assert pos.realized_pnl == pytest.approx(5 * (130 - 110))  # 100

    def test_exact_close_transitions_to_closed(self) -> None:
        pos = Position(_fill(price=100.0, qty=10))
        pos.apply(_fill(side=Side.SELL, price=120.0, qty=10))
        assert pos.state is PositionState.CLOSED
        assert pos.signed_qty == 0
        assert pos.realized_pnl == pytest.approx(10 * (120 - 100))  # 200

    def test_short_round_trip_realizes_profit(self) -> None:
        pos = Position(_fill(side=Side.SELL, price=100.0, qty=10))
        pos.apply(_fill(side=Side.BUY, price=80.0, qty=10))
        assert pos.state is PositionState.CLOSED
        assert pos.realized_pnl == pytest.approx(10 * (100 - 80))  # 200

    def test_overshoot_flips_and_returns_residual(self) -> None:
        pos = Position(_fill(price=100.0, qty=10))
        residual = pos.apply(_fill(side=Side.SELL, price=110.0, qty=15))
        assert pos.state is PositionState.CLOSED
        assert pos.realized_pnl == pytest.approx(10 * (110 - 100))  # 100
        assert residual is not None
        assert residual.side is Side.SELL
        assert residual.qty == 5  # the flipped remainder (now short 5)

    def test_apply_raises_on_closed_position(self) -> None:
        pos = Position(_fill(price=100.0, qty=10))
        pos.apply(_fill(side=Side.SELL, price=110.0, qty=10))
        with pytest.raises(ValueError):
            pos.apply(_fill(price=105.0, qty=1))

    def test_unrealized_pnl(self) -> None:
        pos = Position(_fill(price=100.0, qty=10))
        assert pos.unrealized_pnl(120.0) == pytest.approx(10 * 20)  # 200
        short = Position(_fill(side=Side.SELL, price=100.0, qty=10))
        assert short.unrealized_pnl(90.0) == pytest.approx(10 * 10)

    def test_distinct_position_objects_per_venue(self) -> None:
        a = Position(_fill(venue="paper", price=100.0, qty=10))
        b = Position(_fill(venue="prod", price=101.0, qty=5))
        assert a is not b
        assert a.key != b.key
        assert a.symbol == b.symbol == "SPY"


class TestPositionDictCompatibility:
    def test_to_position_dict_carries_legacy_fields(self) -> None:
        pos = Position(_fill(price=100.0, qty=10, strategy="mom"), signal="BUY")
        d = pos.to_position_dict()
        assert d["Ticker"] == "SPY"
        assert d["Direction"] == 1
        assert d["Qty"] == 10
        assert d["EntryPrice"] == 100.0
        assert d["Venue"] == "paper"
        assert d["_signed_qty"] == 10
        assert d["_state"] == "opened"

    def test_to_and_from_position_dict_roundtrip(self) -> None:
        pos = Position(_fill(price=100.0, qty=10))
        pos.apply(_fill(price=120.0, qty=10))  # qty 20 avg 110
        pos.apply(_fill(side=Side.SELL, price=130.0, qty=5))  # realized 100
        restored = Position.from_position_dict(pos.to_position_dict())
        assert restored.signed_qty == pos.signed_qty
        assert restored.avg_price == pytest.approx(pos.avg_price)
        assert restored.realized_pnl == pytest.approx(pos.realized_pnl)
        assert restored.state is pos.state

    def test_keyed_by_typed_instrument(self) -> None:
        inst = Instrument(
            symbol="SPY",
            venue="paper",
            asset_class=AssetClass.EQUITY,
            instrument_class=InstrumentClass.EQUITY,
        )
        pos = Position(_fill(instrument=inst, price=100.0, qty=10))
        assert pos.key == ("paper", "SPY")


# ===========================================================================
# Portfolio aggregation
# ===========================================================================


class TestPortfolio:
    def test_apply_fill_opens_position_and_spends_cash(self) -> None:
        pf = Portfolio(starting_equity=10_000.0)
        pf.apply_fill(_fill(price=100.0, qty=10))  # buy 10 @ 100
        assert pf.cash == pytest.approx(10_000.0 - 10 * 100)  # 9000
        assert pf.position("paper", "SPY") is not None
        assert pf.positions[("paper", "SPY")].signed_qty == 10

    def test_pnl_totals_track_positions(self) -> None:
        pf = Portfolio(starting_equity=10_000.0)
        pf.apply_fill(_fill(price=100.0, qty=10))
        prices = {("paper", "SPY"): 120.0}
        assert pf.unrealized_pnl(prices) == pytest.approx(200.0)
        assert pf.equity(prices) == pytest.approx(9000.0 + 10 * 120)  # 10200
        assert pf.total_pnl(prices) == pytest.approx(200.0)

    def test_closed_positions_settle_into_realized(self) -> None:
        pf = Portfolio(starting_equity=10_000.0)
        pf.apply_fill(_fill(price=100.0, qty=10))
        pf.apply_fill(_fill(side=Side.SELL, price=120.0, qty=10))
        assert pf.realized_pnl() == pytest.approx(200.0)
        assert pf.closed_positions[0].realized_pnl == pytest.approx(200.0)
        assert len(pf.positions) == 0

    def test_partial_close_folds_realized_into_cash(self) -> None:
        pf = Portfolio(starting_equity=10_000.0)
        pf.apply_fill(_fill(price=100.0, qty=10))
        pf.apply_fill(_fill(side=Side.SELL, price=110.0, qty=4))  # real 40
        assert pf.realized_pnl() == pytest.approx(40.0)
        pos = pf.position("paper", "SPY")
        assert pos is not None
        assert pos.signed_qty == 6
        # cash: -1000 (buy) + 440 (sell 4 @ 110)
        assert pf.cash == pytest.approx(10_000.0 - 1000.0 + 440.0)

    def test_double_side_flip_routes_to_new_position(self) -> None:
        pf = Portfolio(starting_equity=10_000.0)
        pf.apply_fill(_fill(price=100.0, qty=10))
        pf.apply_fill(_fill(side=Side.SELL, price=110.0, qty=15))
        # original closed + a new short(5) opened
        assert len(pf.positions) == 1
        short = pf.position("paper", "SPY")
        assert short is not None
        assert short.signed_qty == -5
        assert pf.realized_pnl() == pytest.approx(10 * 10)

    def test_distinct_positions_across_venues(self) -> None:
        pf = Portfolio(starting_equity=10_000.0)
        pf.apply_fill(_fill(venue="paper", price=100.0, qty=10))
        pf.apply_fill(_fill(venue="prod", price=101.0, qty=5))
        assert len(pf.positions) == 2
        assert pf.position("paper", "SPY") is not pf.position("prod", "SPY")

    def test_symbol_string_pricing_sugar(self) -> None:
        pf = Portfolio(starting_equity=10_000.0)
        pf.apply_fill(_fill(price=100.0, qty=10))
        assert pf.unrealized_pnl({"SPY": 120.0}) == pytest.approx(200.0)


# ===========================================================================
# Combined + per-strategy metrics (spec combined scenario)
# ===========================================================================


class TestCombinedMetrics:
    def test_combined_and_per_strategy(self) -> None:
        pf = Portfolio(starting_equity=10_000.0)
        pf.apply_fill(_fill(price=100.0, qty=10, strategy="mom"))
        pf.apply_fill(_fill(symbol="AAPL", price=90.0, qty=10, strategy="value"))
        pf.apply_fill(_fill(side=Side.SELL, price=110.0, qty=10, strategy="mom"))
        pf.apply_fill(
            _fill(symbol="AAPL", side=Side.SELL, price=100.0, qty=10, strategy="value")
        )

        prices = {("paper", "SPY"): 120.0}
        m = pf.metrics(prices)
        assert m["realized_pnl"] == pytest.approx(200.0)  # 100 + 100
        assert m["open_positions"] == 0
        assert m["closed_positions"] == 2
        per = m["per_strategy"]
        assert set(per) == {"mom", "value"}
        assert per["mom"]["realized_pnl"] == pytest.approx(100.0)
        assert per["value"]["realized_pnl"] == pytest.approx(100.0)
        # combined total equals the sum of per-strategy totals
        assert m["total_pnl"] == pytest.approx(
            per["mom"]["total_pnl"] + per["value"]["total_pnl"]
        )

    def test_strategy_names_derived_from_fills(self) -> None:
        pf = Portfolio(starting_equity=10_000.0)
        pf.apply_fill(_fill(strategy="alpha"))
        assert pf.strategy_names() == ["alpha"]


# ===========================================================================
# Persistence / restart recovery (spec persistence scenario)
# ===========================================================================


class TestSnapshotRecovery:
    def test_snapshot_roundtrip_restores_state(self) -> None:
        pf = Portfolio(starting_equity=10_000.0)
        pf.apply_fill(_fill(price=100.0, qty=10, strategy="mom"))
        pf.apply_fill(_fill(price=120.0, qty=5, strategy="mom"))
        pf.apply_fill(_fill(side=Side.SELL, price=130.0, qty=5, strategy="mom"))
        # one open + one closed + cash + realized
        snapshot = pf.to_snapshot()

        recovered = Portfolio.from_snapshot(snapshot)
        assert recovered.cash == pytest.approx(pf.cash)
        assert recovered.name == pf.name
        assert len(recovered.positions) == len(pf.positions)
        assert len(recovered.closed_positions) == len(pf.closed_positions)
        for key in pf.positions:
            a = recovered.positions[key]
            b = pf.positions[key]
            assert a.signed_qty == b.signed_qty
            assert a.avg_price == pytest.approx(b.avg_price)
            assert a.state is b.state
        assert recovered.realized_pnl() == pytest.approx(pf.realized_pnl())

    def test_snapshot_restores_and_continues_from_fills(self) -> None:
        pf = Portfolio(starting_equity=10_000.0)
        pf.apply_fill(_fill(price=100.0, qty=10, strategy="mom"))
        snapshot = pf.to_snapshot()

        recovered = Portfolio.from_snapshot(snapshot)
        recovered.apply_fill(_fill(price=120.0, qty=10, strategy="mom"))
        # continuing after recovery keeps the pre-restart position and adjusts it
        pos = recovered.position("paper", "SPY")
        assert pos is not None
        assert pos.signed_qty == 20
        assert pos.avg_price == pytest.approx(110.0)
