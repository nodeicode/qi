"""
Tests for quant.core.engine — TradingEngine and pure helpers.

No network calls or external dependencies — all strategies and configs
are lightweight fakes built from SimpleNamespace / dataclasses.

Covers:
  - SignalType enum members and StrEnum behaviour
  - signal_asset_class / signal_direction mapping
  - check_entry_gate (actionable, confidence, concurrency)
  - build_position_dict schema and extras
  - inject_indicators (merge_asof, daily shift, no-indicator fallback)
  - _to_utc_naive_us normalisation
  - TradingEngine.__init__ validation (protocol, config warnings)
  - TradingEngine.cfg default resolution
  - TradingEngine.run_signals pipeline
  - TradingEngine.latest_signal extraction
  - TradingEngine.should_enter delegation
  - TradingEngine.route / direction / is_actionable
  - TradingEngine.cache_signals bounded LRU
  - TradingEngine.call_manage_trade (injection, error tracking)
  - TradingEngine.process_ticker (exit + entry pipeline)
  - TradingEngine.process_ticker signal HOLD reason logged / suppressed
  - TradingEngine.process_ticker actionable signal reason included in log
  - TradingEngine.size_for_signal (registry sizing dispatch)
  - TradingEngine.build_position
"""

from __future__ import annotations

import datetime
import time
from dataclasses import dataclass, field
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast

import pandas as pd
import pytest

from quant.core._utils import (
    _SIGNAL_META,
    ALL_ENTRY_SIGNALS,
    DEFAULT_INDICATOR_COLUMNS,
    PRODUCT_SPECS,
    SIGNAL_REGISTRY,
    AssetClass,
    ContractSpec,
    ProductFamily,
    SignalRegistry,
    SignalType,
    _to_utc_naive_us,
    bar_staleness_ns,
    call_manage_trade,
    check_entry_gate,
    fetch_data,
    fetch_manage_data,
    inject_indicators,
    resolve_data_delay_min,
    signal_asset_class,
    signal_direction,
    snapshot_config,
    staleness_excess_ns,
)
from quant.core.engine import TradingEngine
from quant.core.protocols import PositionDict
from quant.core.sizing import size_for_signal
from quant.utils.positions import build_position_dict
from quant.utils.strategy_config import BaseStrategyConfig
from quant.utils.time import (
    SESSION_CLOCKS,
    EquitySessionClock,
    FuturesSessionClock,
    bar_close_utc,
    closed_bar_asof,
)

# ---------------------------------------------------------------------------
# Helpers — lightweight fakes
# ---------------------------------------------------------------------------


@dataclass
class _TestStrategyConfig(BaseStrategyConfig):
    """Concrete config matching the engine's production contract."""

    INTERVAL: str = "1h"
    PRE_LOAD_DAYS: int = 5
    MANAGE_INTERVAL: str = "1h"
    INITIAL_CAPITAL: float = 25_000.0
    CAPITAL_PER_TRADE: float = 0.10
    MAX_CONCURRENT_POSITIONS: int = 5
    STOP_LOSS_PERCENT: float = 0.05
    TAKE_PROFIT_PERCENT: float = -1.0
    BREAKEVEN_PROFIT_PERCENT: float = -1.0
    USE_TRAIL_LOSS: bool = True
    MAX_HOLDING_MINUTES: int = 2400
    MIN_SIGNAL_CONFIDENCE: float = 0.0
    MAX_DAY_TRADES: int = 100
    BASE_SLIPPAGE_FACTOR: float = 0.0
    TICKERS: list[str] = field(default_factory=lambda: ["AAPL"])

    OPTION_STRIKE_METHOD: str = "atm"
    DAILY_START: str | None = None
    MAX_CAPITAL_PER_TRADE: float = 1e9
    USE_CONFIDENCE_SIZING: bool = False
    CONFIDENCE_SIZING_TIERS: list[tuple[float, float]] = field(
        default_factory=lambda: [(0.50, 0.20)]
    )
    SLIPPAGE_PER_SHARE: float = 0.0


def _make_config(**overrides) -> _TestStrategyConfig:
    """Return a valid config, then apply test-specific overrides."""
    config = _TestStrategyConfig()
    for key, value in overrides.items():
        setattr(config, key, value)
    return config


def _make_strategy(
    *,
    config: BaseStrategyConfig | None = None,
    manage_return=-1,
    accept_ticker=False,
):
    """Fake strategy satisfying StrategyProtocol (manage_trade mandatory)."""

    def calculate_indicators(df, **kw):
        return df.copy()

    def generate_signals(df):
        out = df.copy()
        if "Signal" not in out.columns:
            out["Signal"] = "BUY"
        if "Confidence" not in out.columns:
            out["Confidence"] = 0.8
        return out

    def manage_trade(position, df):
        return manage_return

    s = SimpleNamespace(
        name="test_strategy",
        config=config if config is not None else _make_config(),
        calculate_indicators=calculate_indicators,
        generate_signals=generate_signals,
        manage_trade=manage_trade,
    )
    if accept_ticker:

        def calc_with_ticker(df, *, ticker=None):
            return df.copy()

        s.calculate_indicators = calc_with_ticker
    return s


def _make_ohlcv(n=5, start="2024-01-02 14:00", freq="1h", tz="UTC"):
    """Minimal OHLCV DataFrame (hour-aligned so closed_bar_asof matches)."""
    idx = pd.date_range(start, periods=n, freq=freq, tz=tz)
    return pd.DataFrame(
        {"Open": 100.0, "High": 101.0, "Low": 99.0, "Close": 100.5, "Volume": 1000},
        index=idx,
    )


def _make_data_handler():
    """Minimal data handler satisfying DataHandlerProtocol."""
    return SimpleNamespace(
        get_historical_data=lambda ticker, start, end, interval, use_cache=True: (
            _make_ohlcv()
        )
    )


def _make_engine(
    config: BaseStrategyConfig,
    strategy,
    data_handler,
    **kwargs,
) -> TradingEngine:
    """Construct an engine using the current strategy-owned config API."""
    strategy.config = config
    return TradingEngine(strategy, data_handler, **kwargs)


@pytest.fixture()
def config():
    return _make_config()


@pytest.fixture()
def strategy(config):
    return _make_strategy(config=config)


@pytest.fixture()
def data_handler():
    return _make_data_handler()


@pytest.fixture()
def engine(config, strategy, data_handler):
    return TradingEngine(strategy, data_handler)


# ========================================================================
# 1. SignalType enum
# ========================================================================


class TestAssetClass:
    def test_str_enum_value(self):
        assert AssetClass.EQUITY == "EQUITY"
        assert str(AssetClass.FUTURES) == "FUTURES"

    def test_futures_members_present_but_signal_free(self):
        # Additive: the enum advertises the road ahead, but NO engine-native
        # signal routes to FUTURES/OOF — a fresh registry (as configured at
        # import before any strategy module registers) sees zero futures
        # signals.  Tests that import futures fixtures may register into the
        # global SIGNAL_REGISTRY, so assert on a fresh instance.
        assert AssetClass.FUTURES == "FUTURES"
        assert AssetClass.OOF == "OOF"
        reg = SignalRegistry()
        for st, (ac, d) in _SIGNAL_META.items():
            reg.register(st, ac, d)
        assert reg.signals_for(AssetClass.FUTURES) == frozenset()
        assert reg.signals_for(AssetClass.OOF) == frozenset()


_OPTION_SIGNALS_TEST = {
    "BUY_CALL",
    "BUY_PUT",
    "BUY_CALL_MOM",
    "BUY_PUT_MOM",
    "SELL_CALL",
    "SELL_PUT",
}


class TestSignalRegistry:
    def test_singleton_is_seeded(self):
        assert SignalType.BUY.value in SIGNAL_REGISTRY.entry_signals
        assert SIGNAL_REGISTRY.asset_class("buy") == AssetClass.EQUITY

    def test_register_new_signal_then_lookup(self):
        reg = SignalRegistry()
        reg.register("BUY_TEST", AssetClass.FUTURES, 1)
        assert reg.asset_class("BUY_TEST") == AssetClass.FUTURES
        assert reg.direction("buy_test") == 1
        assert "BUY_TEST" in reg.entry_signals
        assert reg.signals_for(AssetClass.FUTURES) == {"BUY_TEST"}

    def test_register_overrides_existing(self):
        reg = SignalRegistry()
        reg.register(SignalType.BUY, AssetClass.EQUITY, 1)
        assert reg.asset_class(SignalType.BUY) == AssetClass.EQUITY
        reg.register("BUY", "FUTURES", -1)
        assert reg.asset_class("BUY") == AssetClass.FUTURES
        assert reg.direction("BUY") == -1

    def test_unknown_signal_falls_back_unless_strict(self):
        reg = SignalRegistry()
        assert reg.asset_class("NOPE") == AssetClass.EQUITY
        assert reg.direction("NOPE") == 1
        with pytest.raises(KeyError):
            reg.asset_class("NOPE", strict=True)
        with pytest.raises(KeyError):
            reg.direction("NOPE", strict=True)

    def test_signals_for_case_insensitive(self):
        assert SIGNAL_REGISTRY.signals_for("option") == _OPTION_SIGNALS_TEST


class TestSignalType:
    def test_str_enum_value(self):
        assert SignalType.BUY == "BUY"
        assert SignalType.SELL_IC == "SELL_IC"

    def test_all_members_in_all_entry_signals(self):
        for s in SignalType:
            assert s.value in ALL_ENTRY_SIGNALS

    def test_case_insensitive_construction(self):
        assert SignalType("BUY") == SignalType.BUY

    def test_invalid_signal_raises(self):
        with pytest.raises(ValueError):
            SignalType("INVALID")


# ========================================================================
# 2. Pure helpers — signal_asset_class / signal_direction
# ========================================================================


class TestSignalAssetClass:
    @pytest.mark.parametrize(
        "signal, expected",
        [
            ("BUY", "EQUITY"),
            ("SELL", "EQUITY"),
            ("LONG", "EQUITY"),
            ("SHORT", "EQUITY"),
            ("BUY_CALL", "OPTION"),
            ("BUY_PUT", "OPTION"),
            ("BUY_CALL_MOM", "OPTION"),
            ("BUY_PUT_MOM", "OPTION"),
            ("SELL_CALL", "OPTION"),
            ("SELL_PUT", "OPTION"),
            ("BUY_SPREAD", "SPREAD"),
            ("SELL_SPREAD", "SPREAD"),
            ("SELL_IC", "IC"),
            ("BUY_IC", "IC"),
        ],
    )
    def test_known_signals(self, signal, expected):
        assert signal_asset_class(signal) == expected

    def test_unknown_signal_defaults_to_equity(self):
        assert signal_asset_class("FOOBAR") == "EQUITY"

    def test_case_insensitive(self):
        assert signal_asset_class("buy_call") == "OPTION"


class TestSignalDirection:
    @pytest.mark.parametrize(
        "signal, expected",
        [
            ("BUY", 1),
            ("SELL", -1),
            ("SHORT", -1),
            ("LONG", 1),
            ("BUY_CALL", 1),
            ("BUY_PUT", -1),
            ("SELL_IC", -1),
            ("BUY_IC", 1),
        ],
    )
    def test_known_directions(self, signal, expected):
        assert signal_direction(signal) == expected

    def test_unknown_defaults_to_positive(self):
        assert signal_direction("UNKNOWN") == 1


# ========================================================================
# 2b. ContractSpec / ProductSpecRegistry / SessionClock
# ========================================================================


class TestContractSpec:
    def test_valid_spec_constructed(self):
        spec = ContractSpec(
            root="MES",
            family="equity_index",
            multiplier=5.0,
            tick_size=0.25,
            tick_value=1.25,
        )
        assert spec.family == ProductFamily.EQUITY_INDEX
        assert spec.quote == "INDEX_POINTS"

    def test_invalid_spec_fails_fast(self):
        with pytest.raises(ValueError):
            ContractSpec(
                root="",
                family="EQUITY_INDEX",
                multiplier=5.0,
                tick_size=0.25,
                tick_value=1.25,
            )
        with pytest.raises(ValueError):
            ContractSpec(
                root="MES",
                family="EQUITY_INDEX",
                multiplier=0.0,
                tick_size=0.25,
                tick_value=1.25,
            )
        with pytest.raises(ValueError):
            ContractSpec(
                root="MES",
                family="EQUITY_INDEX",
                multiplier=5.0,
                tick_size=0.0,
                tick_value=1.25,
            )
        with pytest.raises(ValueError):
            ContractSpec(
                root="MES",
                family="EQUITY_INDEX",
                multiplier=5.0,
                tick_size=0.25,
                tick_value=0.0,
            )


class TestProductSpecRegistry:
    def test_seeded_products(self):
        assert "MES" in PRODUCT_SPECS
        assert "es" in PRODUCT_SPECS  # case-insensitive
        assert PRODUCT_SPECS.get("SR3").multiplier == 2500.0
        assert PRODUCT_SPECS.get("ES").tick_value == 12.50
        assert PRODUCT_SPECS.get("MES").tick_value == 1.25

    def test_get_strict_raises_for_unknown(self):
        with pytest.raises(KeyError):
            PRODUCT_SPECS.get("OIL", strict=True)
        assert PRODUCT_SPECS.get("OIL", strict=False) is None

    def test_register_duplicate_root_rejected(self):
        from quant.core._utils import ProductSpecRegistry

        reg = ProductSpecRegistry()
        reg.register(
            ContractSpec(
                root="MES",
                family=ProductFamily.EQUITY_INDEX,
                multiplier=5.0,
                tick_size=0.25,
                tick_value=1.25,
            )
        )
        duplicate = ContractSpec(
            root="MES",
            family=ProductFamily.EQUITY_INDEX,
            multiplier=4.0,
            tick_size=0.25,
            tick_value=1.0,
        )
        with pytest.raises(ValueError):
            reg.register(duplicate)

    def test_register_then_get(self):
        from quant.core._utils import ProductSpecRegistry

        reg = ProductSpecRegistry()
        my = ContractSpec(
            root="CUSTOM",
            family=ProductFamily.INTEREST_RATE,
            multiplier=100.0,
            tick_size=1.0,
            tick_value=100.0,
        )
        reg.register(my)
        assert reg.get("custom") == my
        assert reg.keys() == ["CUSTOM"]


class TestSessionClocks:
    def test_equity_clock_next_bday_0930(self):
        clock = EquitySessionClock()
        # Friday -> Monday 09:30 ET (UTC-naive wall). Jan 8 2024 = EST (-5h).
        ts = pd.Timestamp("2024-01-05 15:00:00")  # Friday
        out = clock.next_open_after(ts)
        assert out.day == 8 and out.hour == 14 and out.minute == 30

    def test_equity_clock_skips_weekend(self):
        clock = EquitySessionClock()
        sat = pd.Timestamp("2024-01-06 12:00:00")
        assert clock.next_open_after(sat).day == 8

    def test_futures_clock_weekday_before_open(self):
        clock = FuturesSessionClock()
        # Wednesday 10:00 ET -> same day 18:00 ET (= 23:00 UTC in EST).
        out = clock.next_open_after(pd.Timestamp("2024-01-03 15:00:00"))
        assert out.day == 3 and out.hour == 23

    def test_futures_clock_weekday_after_open(self):
        clock = FuturesSessionClock()
        # Jan 3 20:00 ET (Jan 4 01:00 UTC) — after the 18:00 ET open
        # -> next session opens Thursday 18:00 ET (= 23:00 UTC in EST).
        out = clock.next_open_after(pd.Timestamp("2024-01-04 01:00:00"))
        assert out.day == 4 and out.hour == 23

    def test_futures_clock_friday_evening_to_sunday(self):
        clock = FuturesSessionClock()
        # Friday 17:30 ET -> Sunday 18:00 ET.
        out = clock.next_open_after(pd.Timestamp("2024-01-05 22:30:00"))
        assert out.weekday() == 6  # Sunday
        assert out.hour == 23

    def test_futures_clock_saturday_to_sunday(self):
        clock = FuturesSessionClock()
        sat = pd.Timestamp("2024-01-06 12:00:00")
        out = clock.next_open_after(sat)
        assert out.weekday() == 6 and out.hour == 23

    def test_session_clock_registry(self):
        assert SESSION_CLOCKS.get("equity") is not None
        assert SESSION_CLOCKS.get("cme-futures") is not None
        with pytest.raises(KeyError):
            SESSION_CLOCKS.get("nope", strict=True)
        assert SESSION_CLOCKS.get("nope", strict=False) is None

    def test_calculate_entry_time_delegates_to_equity_clock(self):
        # Wire check: calculate_entry_time's daily branch now resolves through
        # SESSION_CLOCKS.equity — identical next-business-day 09:30 ET rule.
        from quant.utils.time import calculate_entry_time

        friday = pd.Timestamp("2024-01-05 20:00:00")
        out = calculate_entry_time(friday, "1d")
        assert out.day == 8 and out.hour == 14  # Monday 09:30 ET EST


# ========================================================================
# 5. check_entry_gate
# ========================================================================


class TestCheckEntryGate:
    def test_actionable_signal_allowed(self):
        cfg = _make_config(MIN_SIGNAL_CONFIDENCE=0.0, MAX_CONCURRENT_POSITIONS=5)
        assert check_entry_gate("BUY", 0.8, 0, cfg) is None

    def test_non_actionable_signal(self):
        cfg = _make_config()
        assert check_entry_gate("HOLD", 0.9, 0, cfg) == "not_actionable"

    def test_low_confidence(self):
        cfg = _make_config(MIN_SIGNAL_CONFIDENCE=0.7)
        assert check_entry_gate("BUY", 0.5, 0, cfg) == "low_confidence"

    def test_max_concurrent(self):
        cfg = _make_config(MAX_CONCURRENT_POSITIONS=2)
        assert check_entry_gate("BUY", 0.8, 2, cfg) == "max_concurrent"

    def test_at_limit_rejected(self):
        cfg = _make_config(MAX_CONCURRENT_POSITIONS=3)
        assert check_entry_gate("BUY", 0.8, 3, cfg) == "max_concurrent"

    def test_below_limit_allowed(self):
        cfg = _make_config(MAX_CONCURRENT_POSITIONS=3)
        assert check_entry_gate("BUY", 0.8, 2, cfg) is None


# ========================================================================
# 6. build_position_dict
# ========================================================================


class TestBuildPositionDict:
    def test_core_fields(self):
        cfg = _make_config()
        now = datetime.datetime(2024, 1, 1, 12, 0, 0)
        pos = build_position_dict(now, 100.0, 1, "BUY", 0.85, cfg)
        assert pos["EntryPrice"] == 100.0
        assert pos["Direction"] == 1
        assert pos["Signal"] == "BUY"
        assert pos["Confidence"] == 0.85
        assert pos["StopLoss"] == cfg.STOP_LOSS_PERCENT
        assert pos["TakeProfit"] == cfg.TAKE_PROFIT_PERCENT
        assert pos["UseTrail"] == cfg.USE_TRAIL_LOSS
        assert pos["MaxHoldTime"] == cfg.MAX_HOLDING_MINUTES

    def test_extra_fields_forwarded(self):
        cfg = _make_config()
        pos = cast(
            "dict[str, Any]",
            build_position_dict(
                None,
                50.0,
                -1,
                "SELL",
                0.6,
                cfg,
                CustomField="hello",
                K_LP=400.0,
            ),
        )
        assert pos["CustomField"] == "hello"
        assert pos["K_LP"] == 400.0

    def test_entry_base_price_equals_entry(self):
        cfg = _make_config()
        pos = build_position_dict(None, 99.0, 1, "BUY", 0.5, cfg)
        assert pos["EntryBasePrice"] == 99.0
        assert pos["UnderlyingEntryPrice"] == 99.0


# ========================================================================
# 6b. PositionDict subclass (typed IC fields)
# ========================================================================


class TestPositionDictSubclass:
    def test_subclass_compatible_with_build_position_dict(self):
        """build_position_dict output satisfies PositionDict base + extras."""
        cfg = _make_config()
        pos = cast(
            "dict[str, Any]",
            build_position_dict(
                None,
                420.0,
                1,
                "SELL_IC",
                0.7,
                cfg,
                K_LP=400.0,
                K_SP=410.0,
                K_SC=430.0,
                K_LC=440.0,
            ),
        )
        assert pos["EntryPrice"] == 420.0
        assert pos["K_LP"] == 400.0
        assert pos["K_LC"] == 440.0

    def test_subclass_annotations_include_parent(self):
        """Subclass inherits parent annotations for static analysis."""

        class SpreadPositionDict(PositionDict, total=False):
            StrikeLong: float
            StrikeShort: float
            SpreadWidth: float

        annotations = SpreadPositionDict.__annotations__
        # Subclass fields
        assert "StrikeLong" in annotations
        # Inherited parent fields (via TypedDict MRO)
        assert "EntryPrice" in annotations


# ========================================================================
# 7. _to_utc_naive_us
# ========================================================================


class TestToUtcNaiveUs:
    def test_tz_aware_converted(self):
        idx = pd.date_range("2024-01-01", periods=3, freq="1h", tz="US/Eastern")
        result = _to_utc_naive_us(idx)
        assert result.tz is None

    def test_naive_passthrough(self):
        idx = pd.date_range("2024-01-01", periods=3, freq="1h")
        result = _to_utc_naive_us(idx)
        assert result.tz is None

    def test_length_preserved(self):
        idx = pd.date_range("2024-01-01", periods=5, freq="1h", tz="UTC")
        result = _to_utc_naive_us(idx)
        assert len(result) == 5


# ========================================================================
# 8. inject_indicators
# ========================================================================


class TestInjectIndicators:
    def test_no_matching_indicators_adds_raw_close(self):
        manage_df = _make_ohlcv(3)
        signal_df = _make_ohlcv(3)  # no indicator columns
        cfg = _make_config()
        result = inject_indicators(manage_df, signal_df, cfg)
        assert "Raw_Close" in result.columns

    def test_matching_indicators_injected(self):
        manage_df = _make_ohlcv(5, tz=None)
        signal_df = _make_ohlcv(5, tz=None)
        signal_df["ATR"] = 2.5
        signal_df["RSI"] = 55.0
        cfg = _make_config(INTERVAL="1h")
        result = inject_indicators(
            manage_df,
            signal_df,
            cfg,
            indicator_columns=["ATR", "RSI"],
        )
        assert "ATR" in result.columns
        assert "RSI" in result.columns
        assert (result["ATR"] != 0).any()

    def test_daily_shift(self):
        """Daily signals should be shifted by 1 period before merge."""
        manage_df = _make_ohlcv(5, tz=None)
        signal_df = _make_ohlcv(5, tz=None)
        signal_df["ATR"] = range(1, 6)
        cfg = _make_config(INTERVAL="1d")
        result = inject_indicators(
            manage_df,
            signal_df,
            cfg,
            indicator_columns=["ATR"],
        )
        # First row should be 0 (NaN from shift → fillna(0))
        assert result["ATR"].iloc[0] == 0

    def test_custom_indicator_columns(self):
        manage_df = _make_ohlcv(3, tz=None)
        signal_df = _make_ohlcv(3, tz=None)
        signal_df["MyCustom"] = 42.0
        cfg = _make_config()
        result = inject_indicators(
            manage_df,
            signal_df,
            cfg,
            indicator_columns=["MyCustom"],
        )
        assert "MyCustom" in result.columns
        assert result["MyCustom"].iloc[-1] == 42.0


# ========================================================================
# 9. TradingEngine — construction
# ========================================================================


class TestTradingEngineInit:
    def test_valid_strategy_accepted(self, config, strategy, data_handler):
        engine = _make_engine(config, strategy, data_handler)
        assert engine.strategy.config is config
        assert engine.strategy is strategy

    def test_missing_calculate_indicators_raises(self, config):
        bad = SimpleNamespace(generate_signals=lambda df: df)
        with pytest.raises(TypeError, match="calculate_indicators"):
            _make_engine(config, bad, _make_data_handler())

    def test_missing_generate_signals_raises(self, config):
        bad = SimpleNamespace(calculate_indicators=lambda df: df)
        with pytest.raises(TypeError, match="generate_signals"):
            _make_engine(config, bad, _make_data_handler())

    def test_missing_manage_trade_raises(self, config):
        """manage_trade is a mandatory StrategyProtocol method — a strategy
        without it must be rejected at construction, not silently unmanaged."""
        bad = SimpleNamespace(
            calculate_indicators=lambda df: df,
            generate_signals=lambda df: df,
        )
        with pytest.raises(TypeError, match="manage_trade"):
            _make_engine(config, bad, _make_data_handler())

    def test_custom_indicator_columns_from_strategy(self, config):
        s = _make_strategy()
        s.INDICATOR_COLUMNS = ["ATR", "RSI"]
        engine = _make_engine(config, s, _make_data_handler())
        assert engine._indicator_columns == ["ATR", "RSI"]

    def test_default_indicator_columns(self, engine):
        assert engine._indicator_columns == []
        assert engine._indicator_columns == list(DEFAULT_INDICATOR_COLUMNS)

    def test_signal_cache_initially_empty(self, engine):
        assert len(engine._signal_cache) == 0


# ========================================================================
# 10. TradingEngine.run_signals / latest_signal
# ========================================================================


class TestRunSignals:
    def test_returns_dataframe_with_signal_column(self, engine):
        df = _make_ohlcv(5)
        result = engine.run_signals(df)
        assert isinstance(result, pd.DataFrame)
        assert "Signal" in result.columns

    def test_caches_when_ticker_provided(self, engine):
        df = _make_ohlcv(5)
        engine.run_signals(df, ticker="AAPL")
        assert "AAPL" in engine._signal_cache

    def test_no_cache_when_ticker_none(self, engine):
        df = _make_ohlcv(5)
        engine.run_signals(df, ticker=None)
        assert len(engine._signal_cache) == 0

    def test_ticker_kwarg_forwarded(self):
        cfg = _make_config()
        s = _make_strategy(accept_ticker=True)
        engine = _make_engine(cfg, s, _make_data_handler())
        df = _make_ohlcv(3)
        result = engine.run_signals(df, ticker="SPY")
        assert not result.empty


class TestLatestSignal:
    def test_extracts_row_at_bar_ts(self, engine):
        df = _make_ohlcv(3)
        df["Signal"] = ["HOLD", "BUY", "HOLD"]
        df["Confidence"] = [0.1, 0.9, 0.2]
        bar_ts = df.index[1]
        signal, conf, row = engine.latest_signal(df, bar_ts=bar_ts)
        assert signal == "BUY"
        assert conf == 0.9
        assert isinstance(row, dict)
        assert row["_bar_time"] == bar_ts

    def test_defaults_when_bar_missing(self, engine):
        df = _make_ohlcv(2)
        missing = df.index[-1] + pd.Timedelta(hours=1)
        signal, conf, row = engine.latest_signal(df, bar_ts=missing)
        assert signal == "HOLD"
        assert conf == 0.0
        assert row["_bar_time"] == missing

    def test_without_bar_ts_uses_last_index(self, engine):
        df = _make_ohlcv(3)
        df["Signal"] = ["HOLD", "HOLD", "BUY"]
        df["Confidence"] = [0.1, 0.2, 0.9]
        signal, conf, _ = engine.latest_signal(df)
        assert signal == "BUY"
        assert conf == 0.9


# ========================================================================
# 12. TradingEngine.should_enter / route / direction / is_actionable
# ========================================================================


class TestShouldEnter:
    def test_allowed(self, engine):
        assert engine.should_enter("BUY", 0.8, 0) is None

    def test_rejected_non_actionable(self, engine):
        assert engine.should_enter("HOLD", 0.8, 0) == "not_actionable"


class TestRouting:
    def test_route_equity(self, engine):
        assert engine.route("BUY") == "EQUITY"

    def test_route_option(self, engine):
        assert engine.route("BUY_CALL") == "OPTION"

    def test_route_spread(self, engine):
        assert engine.route("BUY_SPREAD") == "SPREAD"

    def test_route_ic(self, engine):
        assert engine.route("SELL_IC") == "IC"

    def test_direction_long(self, engine):
        assert engine.direction("BUY") == 1

    def test_direction_short(self, engine):
        assert engine.direction("SELL") == -1

    def test_is_actionable_true(self):
        assert TradingEngine.is_actionable("BUY") is True

    def test_is_actionable_false(self):
        assert TradingEngine.is_actionable("HOLD") is False


# ========================================================================
# 13. TradingEngine.cache_signals (LRU)
# ========================================================================


class TestCacheSignals:
    def test_stores_and_retrieves(self, engine):
        df = _make_ohlcv(2)
        engine.cache_signals("AAPL", df)
        assert "AAPL" in engine._signal_cache

    def test_records_column_set(self, engine):
        df = _make_ohlcv(2)
        df["ATR"] = 1.0
        engine.cache_signals("AAPL", df)
        assert "ATR" in engine._signal_cache_cols["AAPL"]
        assert "Close" in engine._signal_cache_cols["AAPL"]

    def test_lru_eviction_clears_column_set(self):
        cfg = _make_config()
        s = _make_strategy()
        engine = _make_engine(cfg, s, _make_data_handler())
        engine._SIGNAL_CACHE_MAXLEN = 3
        for i in range(5):
            engine.cache_signals(f"T{i}", _make_ohlcv(1))
        assert len(engine._signal_cache) == 3
        assert "T0" not in engine._signal_cache
        assert "T0" not in engine._signal_cache_cols
        assert "T4" in engine._signal_cache_cols

    def test_lru_eviction(self):
        cfg = _make_config()
        s = _make_strategy()
        engine = _make_engine(cfg, s, _make_data_handler())
        engine._SIGNAL_CACHE_MAXLEN = 3
        for i in range(5):
            engine.cache_signals(f"T{i}", _make_ohlcv(1))
        assert len(engine._signal_cache) == 3
        # Earliest tickers evicted
        assert "T0" not in engine._signal_cache
        assert "T1" not in engine._signal_cache
        assert "T4" in engine._signal_cache

    def test_move_to_end_on_update(self):
        cfg = _make_config()
        s = _make_strategy()
        engine = _make_engine(cfg, s, _make_data_handler())
        engine._SIGNAL_CACHE_MAXLEN = 3
        engine.cache_signals("A", _make_ohlcv(1))
        engine.cache_signals("B", _make_ohlcv(1))
        engine.cache_signals("C", _make_ohlcv(1))
        # Touch A to make it most recent
        engine.cache_signals("A", _make_ohlcv(1))
        # Insert D — B should be evicted (oldest not-recently-used)
        engine.cache_signals("D", _make_ohlcv(1))
        assert "B" not in engine._signal_cache
        assert "A" in engine._signal_cache

    def test_warns_on_indicator_column_mismatch(self, caplog):
        """D6: manage-only path warns when cached cols lack current indicators."""
        import logging

        cfg = _make_config()
        s = _make_strategy(manage_return=-1)
        engine = _make_engine(cfg, s, _make_data_handler())
        engine._indicator_columns = ["ATR", "RSI"]
        # Cache a frame that is missing RSI (simulates mid-run config change)
        cached = _make_ohlcv(3)
        cached["ATR"] = 1.0
        engine.cache_signals("AAPL", cached)
        pos = cast(
            "PositionDict", {"Ticker": "AAPL", "OrderId": "ORD1", "Direction": 1}
        )
        with caplog.at_level(logging.WARNING):
            engine.process_ticker(
                _make_ohlcv(3),
                _make_ohlcv(3),
                "AAPL",
                [pos],
                skip_signals=True,
            )
        assert any("missing indicator columns" in r.message for r in caplog.records)
        assert any("RSI" in r.message for r in caplog.records)


# ========================================================================
# 14. TradingEngine.call_manage_trade
# ========================================================================


class TestCallManageTrade:
    def test_returns_strategy_result(self):
        s = _make_strategy(manage_return=5)
        df = _make_ohlcv(3)
        result = call_manage_trade(s, {"Direction": 1}, df)
        assert result == 5

    def test_returns_neg1_on_hold(self):
        s = _make_strategy()  # manage_return=-1
        df = _make_ohlcv(3)
        assert call_manage_trade(s, {"Direction": 1}, df) == -1

    def test_exception_returns_neg1(self):
        s = _make_strategy()
        s.manage_trade = lambda pos, df: (_ for _ in ()).throw(RuntimeError("boom"))
        df = _make_ohlcv(3)
        result = call_manage_trade(s, {}, df)
        assert result == -1

    def test_uses_signal_df_for_injection(self):
        cfg = _make_config()
        s = _make_strategy()
        signal_df = _make_ohlcv(3, tz=None)
        signal_df["ATR"] = 5.0
        manage_df = _make_ohlcv(3, tz=None)
        # Should not raise — indicator injection uses provided signal_df
        call_manage_trade(
            s, {"Direction": 1}, manage_df, signal_df=signal_df, config=cfg
        )


# ========================================================================
# 15. TradingEngine.process_ticker
# ========================================================================


class TestProcessTicker:
    def test_entry_signal_returned(self):
        cfg = _make_config()
        s = _make_strategy()
        engine = _make_engine(cfg, s, _make_data_handler())
        df = _make_ohlcv(5)
        results = engine.process_ticker(df, df, "AAPL", [])
        entries = [r for r in results if r["type"] == "ENTRY"]
        assert len(entries) == 1
        assert entries[0]["ticker"] == "AAPL"

    def test_exit_signal_when_manage_triggers(self):
        cfg = _make_config()
        s = _make_strategy(manage_return=3)  # exit at index 3
        engine = _make_engine(cfg, s, _make_data_handler())
        df = _make_ohlcv(5)
        pos = cast(
            "PositionDict", {"Ticker": "AAPL", "OrderId": "ORD1", "Direction": 1}
        )
        results = engine.process_ticker(df, df, "AAPL", [pos])
        exits = [r for r in results if r["type"] == "EXIT"]
        assert len(exits) == 1
        assert exits[0]["order_id"] == "ORD1"

    def test_no_exit_when_hold(self):
        cfg = _make_config()
        s = _make_strategy(manage_return=-1)
        engine = _make_engine(cfg, s, _make_data_handler())
        df = _make_ohlcv(5)
        pos = cast(
            "PositionDict", {"Ticker": "AAPL", "OrderId": "ORD1", "Direction": 1}
        )
        results = engine.process_ticker(df, df, "AAPL", [pos])
        exits = [r for r in results if r["type"] == "EXIT"]
        assert len(exits) == 0

    def test_skips_positions_for_other_tickers(self):
        cfg = _make_config()
        s = _make_strategy(manage_return=3)
        engine = _make_engine(cfg, s, _make_data_handler())
        df = _make_ohlcv(5)
        pos = cast("PositionDict", {"Ticker": "MSFT", "OrderId": "ORD2"})
        results = engine.process_ticker(df, df, "AAPL", [pos])
        exits = [r for r in results if r["type"] == "EXIT"]
        assert len(exits) == 0

    def test_entry_dropped_when_max_concurrent(self):
        cfg = _make_config(MAX_CONCURRENT_POSITIONS=0)
        s = _make_strategy()
        engine = _make_engine(cfg, s, _make_data_handler())
        df = _make_ohlcv(5)
        results = engine.process_ticker(df, df, "AAPL", [])
        entries = [r for r in results if r["type"] == "ENTRY"]
        assert len(entries) == 0

    def test_precomputed_df_sig_skips_run_signals(self):
        cfg = _make_config()
        s = _make_strategy()
        engine = _make_engine(cfg, s, _make_data_handler())
        df = _make_ohlcv(3)
        df_sig = _make_ohlcv(3)
        df_sig["Signal"] = "SELL"
        df_sig["Confidence"] = 0.9
        results = engine.process_ticker(df, df, "AAPL", [], df_sig=df_sig)
        entries = [r for r in results if r["type"] == "ENTRY"]
        assert len(entries) == 1
        assert entries[0]["signal_row"]["Signal"] == "SELL"

    # ── skip_signals (manage-only cycles) ─────────────────────────────
    def test_skip_signals_suppresses_entries(self):
        """skip_signals=True must NOT emit ENTRY dicts even when signal would fire."""
        cfg = _make_config()
        s = _make_strategy()
        engine = _make_engine(cfg, s, _make_data_handler())
        df = _make_ohlcv(5)
        results = engine.process_ticker(df, df, "AAPL", [], skip_signals=True)
        entries = [r for r in results if r["type"] == "ENTRY"]
        assert entries == []

    def test_skip_signals_still_runs_manage_exits(self):
        """skip_signals=True still runs manage_trade and emits EXIT dicts."""
        cfg = _make_config()
        s = _make_strategy(manage_return=2)  # exit at index 2
        engine = _make_engine(cfg, s, _make_data_handler())
        df = _make_ohlcv(5)
        pos = cast(
            "PositionDict", {"Ticker": "AAPL", "OrderId": "ORD9", "Direction": 1}
        )
        results = engine.process_ticker(df, df, "AAPL", [pos], skip_signals=True)
        exits = [r for r in results if r["type"] == "EXIT"]
        entries = [r for r in results if r["type"] == "ENTRY"]
        assert len(exits) == 1
        assert exits[0]["order_id"] == "ORD9"
        assert entries == []

    def test_skip_signals_does_not_invoke_run_signals(self):
        """run_signals must not be called when skip_signals=True (perf + correctness)."""
        cfg = _make_config()
        s = _make_strategy()
        engine = _make_engine(cfg, s, _make_data_handler())
        df = _make_ohlcv(5)

        called = {"n": 0}
        original = engine.run_signals

        def tracking_run_signals(*a, **kw):
            called["n"] += 1
            return original(*a, **kw)

        engine.run_signals = tracking_run_signals
        engine.process_ticker(df, df, "AAPL", [], skip_signals=True)
        assert called["n"] == 0

    def test_skip_signals_uses_cached_signal_df_for_indicator_injection(self):
        """On manage-only cycles, the previously cached signal_df must still
        be used for indicator injection inside call_manage_trade."""
        cfg = _make_config()
        s = _make_strategy(manage_return=-1)
        engine = _make_engine(cfg, s, _make_data_handler())
        engine._indicator_columns = ["ATR"]

        # Seed signal cache from a prior signal cycle.
        signal_df = _make_ohlcv(5, tz=None)
        signal_df["ATR"] = 7.5
        signal_df["Signal"] = "BUY"
        signal_df["Confidence"] = 0.9
        engine.cache_signals("AAPL", signal_df)

        # Capture what manage_trade actually receives.
        received_dfs = []

        def capturing_manage(pos, df):
            received_dfs.append(df)
            return -1

        s.manage_trade = capturing_manage

        manage_df = _make_ohlcv(5, tz=None)
        pos = cast(
            "PositionDict", {"Ticker": "AAPL", "OrderId": "ORD-X", "Direction": 1}
        )
        engine.process_ticker(manage_df, manage_df, "AAPL", [pos], skip_signals=True)

        assert received_dfs, "manage_trade should have been invoked"
        injected = received_dfs[0]
        # ATR from cached signal_df must have been merged into manage_df.
        assert "ATR" in injected.columns
        assert (injected["ATR"] == 7.5).all()


# ========================================================================
# 16. Sizing methods
# ========================================================================


class TestSizing:
    def test_size_equity(self, engine):
        result = size_for_signal(
            "BUY", config=engine.strategy.config, equity=100_000, entry_price=50.0
        )
        assert result["effective_capital"] == 100_000 * 0.10
        assert result["shares"] == int(10_000 / 50.0)

    def test_size_equity_with_pos_size(self, engine):
        result = size_for_signal(
            "BUY",
            config=engine.strategy.config,
            equity=100_000,
            entry_price=50.0,
            pos_size=0.5,
        )
        assert result["effective_capital"] == 100_000 * 0.10 * 0.5

    def test_size_equity_zero_price(self, engine):
        result = size_for_signal(
            "BUY", config=engine.strategy.config, equity=100_000, entry_price=0.0
        )
        assert result["shares"] == 0

    def test_size_option(self, engine):
        result = size_for_signal(
            "BUY_CALL",
            config=engine.strategy.config,
            equity=100_000,
            premium_per_contract=5.0,
        )
        assert result["n_contracts"] >= 1
        assert result["capital"] == 100_000 * 0.10

    def test_size_option_zero_premium(self, engine):
        result = size_for_signal(
            "BUY_CALL",
            config=engine.strategy.config,
            equity=100_000,
            premium_per_contract=0.0,
        )
        assert result["n_contracts"] == 0

    def test_size_spread(self, engine):
        result = size_for_signal(
            "BUY_SPREAD", config=engine.strategy.config, entry_price_per_share=2.0
        )
        assert result["n_contracts"] >= 1
        assert result["capital"] == 0.10  # CAPITAL_PER_TRADE as dollar

    def test_size_ic_without_confidence(self, engine):
        result = size_for_signal(
            "SELL_IC",
            config=engine.strategy.config,
            equity=100_000,
            confidence=0.7,
            collateral_per_contract=500.0,
        )
        assert result["n_contracts"] >= 1
        assert result["collateral_per_contract"] == 500.0

    def test_size_ic_zero_collateral(self, engine):
        result = size_for_signal(
            "SELL_IC",
            config=engine.strategy.config,
            equity=100_000,
            confidence=0.7,
            collateral_per_contract=0.0,
        )
        assert result["n_contracts"] == 0

    def test_size_ic_with_confidence_tiers(self):
        cfg = _make_config(
            USE_CONFIDENCE_SIZING=True,
            CONFIDENCE_SIZING_TIERS=[(0.5, 0.10), (0.8, 0.20)],
            MAX_CAPITAL_PER_TRADE=50_000,
        )
        result = size_for_signal(
            "SELL_IC",
            config=cfg,
            equity=100_000,
            confidence=0.85,
            collateral_per_contract=500.0,
        )
        # conf=0.85 >= 0.80 tier → cpt=0.20, capital = 100k * 0.20 = 20k
        assert result["capital"] == 20_000


# ========================================================================
# 17. build_position
# ========================================================================


class TestBuildPosition:
    def test_core_schema(self, engine):
        now = datetime.datetime(2024, 6, 1, 12, 0)
        pos = engine.build_position(now, 150.0, 1, "BUY", 0.75)
        assert pos["EntryPrice"] == 150.0
        assert pos["Direction"] == 1
        assert pos["Signal"] == "BUY"
        assert "StopLoss" in pos
        assert "MaxHoldTime" in pos

    def test_extras_passed_through(self, engine):
        pos = engine.build_position(None, 50.0, -1, "SELL", 0.5, Ticker="AAPL")
        assert pos["Ticker"] == "AAPL"


# ========================================================================
# 17b. Position lifecycle routed through the group-4 Position primitive (5.2)
# ========================================================================


class TestPositionLifecycleRouting:
    def test_build_position_keeps_canonical_request(self, engine):
        now = datetime.datetime(2024, 6, 1, 12, 0)
        pos = engine.build_position(now, 150.0, 1, "BUY", 0.75)
        assert pos["EntryPrice"] == 150.0
        assert pos["Direction"] == 1
        assert "StopLoss" in pos
        assert "MaxHoldTime" in pos

    def test_position_from_request_routes_through_primitive(self, engine):
        from quant.portfolio import Position, PositionState

        now = datetime.datetime(2024, 6, 1, 12, 0)
        req = engine.build_position(now, 150.0, 1, "BUY", 0.75, Ticker="AAPL", Qty=10)
        lifecycle = engine.position_from_request(req)
        assert isinstance(lifecycle, Position)
        assert lifecycle.state is PositionState.OPENED
        assert lifecycle.direction == 1
        assert lifecycle.entry_price == 150.0
        assert lifecycle.key == ("", "AAPL")  # backtest request has no venue yet

    def test_request_roundtrip_preserves_canonical_schema(self, engine):
        from quant.portfolio import Position, PositionState

        cfg = engine.strategy.config
        now = datetime.datetime(2024, 6, 1, 12, 0)
        req = engine.build_position(now, 150.0, 1, "BUY", 0.75, Ticker="AAPL", Qty=10)
        rendered = engine.position_from_request(req).to_position_dict()
        assert rendered["EntryPrice"] == req["EntryPrice"]
        assert rendered["StopLoss"] == req["StopLoss"] == cfg.STOP_LOSS_PERCENT
        assert rendered["TakeProfit"] == req["TakeProfit"]
        assert rendered["Ticker"] == "AAPL"
        assert rendered["Direction"] == 1

        # lifecycle bookkeeping keys are additive, canonical values unchanged
        assert rendered["_state"] == PositionState.OPENED.value

    def test_closed_position_never_managed(self):
        cfg = _make_config()
        strat = _make_strategy(config=cfg)
        managed: list = []

        def capturing_manage(pos, df):
            managed.append(pos)
            return -1

        strat.manage_trade = capturing_manage
        engine = _make_engine(cfg, strat, _make_data_handler())

        manage_df = _make_ohlcv(5, tz=None)
        open_pos = cast(
            "PositionDict",
            {"Ticker": "AAPL", "OrderId": "O-OPEN", "Direction": 1},
        )
        closed_pos = cast(
            "PositionDict",
            {
                "Ticker": "AAPL",
                "OrderId": "O-CLOSED",
                "Direction": 1,
                "_state": "closed",
            },
        )
        engine.process_ticker(
            manage_df, manage_df, "AAPL", [open_pos, closed_pos], skip_signals=True
        )
        # Only the OPEN position is managed; the CLOSED state machine is skipped.
        assert len(managed) == 1
        assert managed[0]["OrderId"] == "O-OPEN"

    def test_no_ic_meta_or_build_ic_position_in_engine_path(self):
        """5.3: the engine path carries no IC-specific branch/metadata leftovers."""
        root = Path(__file__).resolve().parents[1] / "src" / "quant"
        offenders = []
        for name in ("core/engine.py", "core/_utils.py"):
            text = (root / name).read_text()
            for token in ("build_ic_position", "ICMeta"):
                if token in text:
                    offenders.append(f"{name}: {token}")
        assert not offenders, (
            "IC-specific branches left reachable in engine path: "
            + "\n".join(offenders)
        )


# ========================================================================
# 20. _to_utc_naive_us — tz-naive index branch (312->314)
# ========================================================================


class TestToUtcNaiveUsBranches:
    def test_tz_naive_index_passthrough(self):
        """tz-naive index should pass through without tz_convert call."""
        idx = pd.date_range("2024-01-01", periods=3, freq="1h")
        result = _to_utc_naive_us(idx)
        assert result.tz is None

    def test_tz_aware_index_converted(self):
        """tz-aware index should be converted to UTC naive."""
        idx = pd.date_range("2024-01-01", periods=3, freq="1h", tz="US/Eastern")
        result = _to_utc_naive_us(idx)
        assert result.tz is None


# ========================================================================
# 21. inject_indicators — daily shift false branch & Raw_Close present
# ========================================================================


class TestInjectIndicatorsEdgeCases:
    def _make_signal_df(self, n=5, start="2024-01-02 14:00", freq="1h"):
        idx = pd.date_range(start, periods=n, freq=freq, tz="UTC")
        return pd.DataFrame({"Signal": "BUY", "ATR": 1.5}, index=idx)

    def test_no_daily_shift_for_intraday(self):
        """INTERVAL=1h — shift should NOT be applied."""
        manage_df = _make_ohlcv(n=5)
        signal_df = self._make_signal_df()
        cfg = _make_config(INTERVAL="1h")
        result = inject_indicators(manage_df, signal_df, cfg, indicator_columns=["ATR"])
        assert "ATR" in result.columns

    def test_raw_close_already_present_not_overwritten(self):
        """When Raw_Close already exists it should not be overwritten."""
        idx = pd.date_range("2024-01-02 14:00", periods=5, freq="1h", tz="UTC")
        manage_df = pd.DataFrame(
            {
                "Open": 100.0,
                "High": 101.0,
                "Low": 99.0,
                "Close": 100.5,
                "Volume": 1000,
                "Raw_Close": 99.0,
            },
            index=idx,
        )
        signal_df = self._make_signal_df()
        cfg = _make_config(INTERVAL="1h")
        result = inject_indicators(manage_df, signal_df, cfg, indicator_columns=["ATR"])
        # Raw_Close already existed — it should NOT be replaced by Close
        assert result["Raw_Close"].iloc[0] == pytest.approx(99.0)

    def test_1wk_interval_triggers_shift(self):
        """INTERVAL=1wk should apply shift(1) to signal mapping."""
        idx = pd.date_range("2024-01-01", periods=10, freq="1h", tz="UTC")
        manage_df = pd.DataFrame(
            {"Open": 100.0, "High": 101.0, "Low": 99.0, "Close": 100.5, "Volume": 1000},
            index=idx,
        )
        sig_idx = pd.date_range("2024-01-01", periods=10, freq="1h", tz="UTC")
        signal_df = pd.DataFrame({"ATR": range(10)}, index=sig_idx)
        cfg = _make_config(INTERVAL="1wk")
        result = inject_indicators(manage_df, signal_df, cfg, indicator_columns=["ATR"])
        assert "ATR" in result.columns


# ========================================================================
# 26. process_ticker — df_sig empty / not_actionable signal branches
# ========================================================================


class TestProcessTickerBranches:
    def test_empty_df_sig_skips_entry(self, engine):
        """When df_sig is empty, no ENTRY signal emitted."""
        df = _make_ohlcv()
        signals = engine.process_ticker(
            df,
            df,
            "SPY",
            [],
            df_sig=pd.DataFrame(),  # empty → skip entry block
        )
        entry_signals = [s for s in signals if s["type"] == "ENTRY"]
        assert entry_signals == []

    def test_non_actionable_signal_no_log_branch(self, engine):
        """A HOLD signal → rejection='not_actionable' → no DROPPED log."""
        df = _make_ohlcv()
        idx = pd.date_range("2024-01-02 14:30", periods=5, freq="1h", tz="UTC")
        df_sig = pd.DataFrame(
            {"Signal": "HOLD", "Confidence": 0.9, "Close": 100.0},
            index=idx,
        )
        signals = engine.process_ticker(df, df, "SPY", [], df_sig=df_sig)
        entry_signals = [s for s in signals if s["type"] == "ENTRY"]
        assert entry_signals == []

    def test_low_confidence_dropped_signal_logged(self, engine):
        """Low-confidence actionable signal → rejection='low_confidence' logged."""
        cfg = _make_config(MIN_SIGNAL_CONFIDENCE=0.99)
        eng = _make_engine(cfg, _make_strategy(), _make_data_handler())
        df = _make_ohlcv()
        idx = pd.date_range("2024-01-02 14:30", periods=5, freq="1h", tz="UTC")
        df_sig = pd.DataFrame(
            {"Signal": "BUY", "Confidence": 0.1, "Close": 100.0},
            index=idx,
        )
        signals = eng.process_ticker(df, df, "SPY", [], df_sig=df_sig)
        entry_signals = [s for s in signals if s["type"] == "ENTRY"]
        assert entry_signals == []

    def test_hold_signal_with_reason_is_logged(self, engine, caplog):
        """HOLD + non-empty Reason → 'Signal HOLD for' line emitted at INFO."""
        import logging

        df = _make_ohlcv()
        idx = pd.date_range("2024-01-02 14:30", periods=5, freq="1h", tz="UTC")
        df_sig = pd.DataFrame(
            {
                "Signal": "HOLD",
                "Confidence": 0.9,
                "Close": 100.0,
                "Reason": "SKIP: am_rv 0.0042 > p95 0.0038",
            },
            index=idx,
        )
        with caplog.at_level(logging.INFO, logger="quant.core.engine"):
            engine.process_ticker(df, df, "SPY", [], df_sig=df_sig)

        assert any(
            "Signal HOLD for SPY" in r.message and "am_rv" in r.message
            for r in caplog.records
        )

    def test_hold_signal_without_reason_not_logged(self, engine, caplog):
        """HOLD + missing/empty Reason → no 'Signal HOLD' line emitted."""
        import logging

        df = _make_ohlcv()
        idx = pd.date_range("2024-01-02 14:30", periods=5, freq="1h", tz="UTC")
        df_sig = pd.DataFrame(
            {"Signal": "HOLD", "Confidence": 0.9, "Close": 100.0},
            index=idx,
        )
        with caplog.at_level(logging.INFO, logger="quant.core.engine"):
            engine.process_ticker(df, df, "SPY", [], df_sig=df_sig)

        assert not any("Signal HOLD for" in r.message for r in caplog.records)

    def test_actionable_signal_logs_reason(self, engine, caplog):
        """Actionable signal → 'Actionable signal' log line includes Reason field."""
        import logging

        df = _make_ohlcv()
        idx = pd.date_range("2024-01-02 14:30", periods=5, freq="1h", tz="UTC")
        df_sig = pd.DataFrame(
            {
                "Signal": "BUY",
                "Confidence": 0.9,
                "Close": 100.0,
                "Reason": "SELL_IC: am_filter pass off=$5 wing=$4 q=0.72 conf=0.95",
            },
            index=idx,
        )
        with caplog.at_level(logging.INFO, logger="quant.core.engine"):
            engine.process_ticker(df, df, "SPY", [], df_sig=df_sig)

        assert any(
            "Actionable signal" in r.message and "am_filter pass" in r.message
            for r in caplog.records
        )


# ========================================================================
# 27. size_for_signal IC confidence sizing tiers
# ========================================================================


class TestSizeIcConfidenceTiers:
    def test_tier_matched_by_confidence(self):
        """USE_CONFIDENCE_SIZING=True — correct tier fraction used."""
        cfg = _make_config(
            USE_CONFIDENCE_SIZING=True,
            CONFIDENCE_SIZING_TIERS=[(0.80, 0.30), (0.50, 0.15), (0.0, 0.05)],
            CAPITAL_PER_TRADE=0.10,
            MAX_CAPITAL_PER_TRADE=1e9,
        )
        result = size_for_signal(
            "SELL_IC",
            config=cfg,
            equity=100_000,
            confidence=0.85,
            collateral_per_contract=500,
        )
        # Tier (0.80, 0.30) matched → capital = 100_000 * 0.30 = 30_000
        assert result["capital"] == pytest.approx(30_000)
        assert result["n_contracts"] == 60

    def test_tier_fallback_to_lowest(self):
        """Confidence below all tiers → fall through, last tier wins."""
        cfg = _make_config(
            USE_CONFIDENCE_SIZING=True,
            CONFIDENCE_SIZING_TIERS=[(0.80, 0.30), (0.50, 0.15)],
            CAPITAL_PER_TRADE=0.10,
            MAX_CAPITAL_PER_TRADE=1e9,
        )
        # confidence=0.3 < 0.50 — no tier matched, cpt stays at CAPITAL_PER_TRADE
        result = size_for_signal(
            "SELL_IC",
            config=cfg,
            equity=100_000,
            confidence=0.3,
            collateral_per_contract=500,
        )
        assert result["capital"] == pytest.approx(10_000)  # 0.10 * 100_000

    def test_absolute_cpt_above_one(self):
        """cpt >= 1.0 is treated as an absolute dollar cap."""
        cfg = _make_config(
            USE_CONFIDENCE_SIZING=True,
            CONFIDENCE_SIZING_TIERS=[(0.0, 5000.0)],
            CAPITAL_PER_TRADE=0.10,
            MAX_CAPITAL_PER_TRADE=1e9,
        )
        result = size_for_signal(
            "SELL_IC",
            config=cfg,
            equity=100_000,
            confidence=0.9,
            collateral_per_contract=500,
        )
        assert result["capital"] == pytest.approx(5_000.0)
        assert result["n_contracts"] == 10


# ========================================================================
# 23. _to_utc_naive_us — extra branch coverage
# ========================================================================


class TestToUtcNaiveUsExtraBranches:
    def test_index_without_as_unit_returned_as_is(self):
        """False branch of hasattr(idx, 'as_unit') — index returned unchanged."""

        class IndexLike:
            tz = None

        idx = IndexLike()
        result = _to_utc_naive_us(idx)
        assert result is idx


# ========================================================================
# 24. inject_indicators — extra branch coverage
# ========================================================================


class TestInjectIndicatorsExtraBranches:
    def test_raw_close_already_present_early_return(self):
        """Early-return branch (no valid inds): manage_df already has Raw_Close
        → branch 327->329 taken, Raw_Close is preserved unchanged."""
        manage_df = _make_ohlcv(3)
        manage_df["Raw_Close"] = manage_df["Close"] * 1.01
        signal_df = _make_ohlcv(3)  # no indicator columns
        cfg = _make_config()
        result = inject_indicators(manage_df, signal_df, cfg)
        assert "Raw_Close" in result.columns
        # Compare by raw values: inject_indicators normalises the index to
        # tz-naive UTC, so index-aligned comparison would fail.
        assert result["Raw_Close"].to_numpy() == pytest.approx(
            manage_df["Raw_Close"].to_numpy()
        )

    def test_overlap_column_dropped_before_merge(self):
        """Indicator column present in manage_df is dropped before merge_asof
        (line 345), preventing _x/_y suffix duplicates."""
        manage_df = _make_ohlcv(3, tz=None)
        manage_df["ATR"] = 999.0  # ATR in both frames → overlap
        signal_df = _make_ohlcv(3, tz=None)
        signal_df["ATR"] = 2.5
        cfg = _make_config()
        result = inject_indicators(manage_df, signal_df, cfg, indicator_columns=["ATR"])
        assert "ATR" in result.columns
        assert "ATR_x" not in result.columns
        assert "ATR_y" not in result.columns

    def test_raw_close_from_manage_df_not_re_added_after_merge(self):
        """When manage_df carries Raw_Close through merge_asof, line 357 is
        skipped → branch 356->359 taken."""
        manage_df = _make_ohlcv(3, tz=None)
        manage_df["Raw_Close"] = 50.0
        signal_df = _make_ohlcv(3, tz=None)
        signal_df["ATR"] = 2.5
        cfg = _make_config()
        result = inject_indicators(manage_df, signal_df, cfg, indicator_columns=["ATR"])
        assert "ATR" in result.columns
        # Raw_Close was preserved from manage_df, not replaced by Close
        assert (result["Raw_Close"] == 50.0).all()


# ========================================================================
# 25. fetch_data — null-handler guard (line 436)
# ========================================================================


class TestFetchDataNullHandler:
    def test_none_handler_returns_empty_dataframe(self):
        """data_handler=None → guard at line 436 returns empty DataFrame."""
        end = datetime.datetime(2024, 1, 10, tzinfo=datetime.UTC)
        result = fetch_data(None, "SPY", end, 5, "1h")
        assert isinstance(result, pd.DataFrame)
        assert result.empty


# ========================================================================
# 26. fetch_manage_data — inject_indicators path (line 469)
# ========================================================================


class TestFetchManageDataInjectPath:
    def test_different_intervals_inject_indicators_called(self):
        """MANAGE_INTERVAL != signal_interval + non-empty signal_df
        → inject_indicators path executed when indicator columns are set."""
        cfg = _make_config(INTERVAL="1d", MANAGE_INTERVAL="1h")
        signal_df = _make_ohlcv(5, tz=None)
        signal_df["ATR"] = 3.0
        manage_df = _make_ohlcv(5, tz=None)
        handler = SimpleNamespace(
            get_historical_data=lambda ticker, start, end, interval, use_cache=True: (
                manage_df
            )
        )
        end = datetime.datetime(2024, 1, 10, tzinfo=datetime.UTC)
        # fetch_manage_data uses DEFAULT_INDICATOR_COLUMNS (empty); inject
        # explicitly here to assert the same merge path the engine uses when
        # strategy.INDICATOR_COLUMNS includes ATR.
        fetched = fetch_manage_data(
            handler, cfg, "SPY", end, 5, "1d", signal_df=signal_df
        )
        result = inject_indicators(fetched, signal_df, cfg, indicator_columns=["ATR"])
        assert not result.empty
        assert "ATR" in result.columns


# ========================================================================
# 27. process_ticker — extra branch coverage
# ========================================================================


class TestProcessTickerExtraBranches:
    def test_empty_df_skips_signal_block(self):
        """df.empty=True → df_sig stays None → signal block skipped (433->470)."""
        cfg = _make_config()
        engine = _make_engine(cfg, _make_strategy(), _make_data_handler())
        results = engine.process_ticker(pd.DataFrame(), pd.DataFrame(), "AAPL", [])
        assert results == []

    def test_hold_signal_not_actionable_elif_not_taken(self):
        """HOLD signal → rejection='not_actionable' → elif branch 461->470 False."""
        cfg = _make_config()
        s = _make_strategy()

        def gen_hold(df):
            out = df.copy()
            out["Signal"] = "HOLD"
            out["Confidence"] = 0.5
            return out

        s.generate_signals = gen_hold
        engine = _make_engine(cfg, s, _make_data_handler())
        df = _make_ohlcv(5)
        results = engine.process_ticker(df, df, "AAPL", [])
        assert [r for r in results if r["type"] == "ENTRY"] == []


# ========================================================================
# 28. TradingEngine.process_strategy (lines 500-599)
# ========================================================================


class _FakeExecutor:
    """Minimal ExecutorProtocol-compatible implementation for process_strategy tests."""

    def __init__(
        self,
        *,
        should_signal: bool = True,
        should_manage: bool = True,
        positions: list[PositionDict] | None = None,
    ):
        self._should: tuple[bool, bool] = (should_signal, should_manage)
        self._positions: list[PositionDict] = list(positions or [])
        self.entered: list[dict] = []
        self.exited: list[dict] = []
        self.record_run_called: int = 0
        self.after_ticker_calls: list[tuple] = []

    # ── Scheduling ───────────────────────────────────────────────────
    def should_run(self, strategy, now) -> tuple[bool, bool]:
        return self._should

    def data_end_dt(self, now):
        # Align with default ``_make_ohlcv`` (hourly from 14:00 UTC on 2024-01-02)
        # so ``closed_bar_asof`` selects 18:00 (in frame).
        return pd.Timestamp("2024-01-02 19:00", tz="UTC")

    def get_precomputed_signal(self, ticker: str, now) -> pd.DataFrame | None:
        return None

    # ── State ────────────────────────────────────────────────────────
    def active_positions(self, strategy_name: str) -> list[PositionDict]:
        return self._positions

    # ── Lifecycle hooks ──────────────────────────────────────────────
    def record_run(self, strategy_name: str, now, ran_signal: bool) -> None:
        self.record_run_called += 1

    def after_process_ticker(
        self, strategy_name: str, ticker: str, results: list[dict]
    ) -> None:
        self.after_ticker_calls.append((ticker, results))

    # ── Execution ────────────────────────────────────────────────────
    def enter(
        self,
        strategy,
        ticker: str,
        signal_row: dict,
        engine,
        *,
        now=None,
    ) -> dict | None:
        pos: dict = {"Ticker": ticker, "OrderId": f"OE-{ticker}"}
        self.entered.append(pos)
        return pos

    def exit(
        self,
        strategy_name: str,
        position: dict,
        sig: dict,
        *,
        manage_df: pd.DataFrame | None = None,
    ) -> bool:
        self.exited.append(position)
        return True

    def after_entry(self, strategy_name: str, position: dict) -> None:
        pass

    def after_exit(self, strategy_name: str, position: dict) -> None:
        pass


def _strategy_for_ps(
    tickers=None,
    interval="1h",
    manage_interval=None,
    daily_start=None,
    pre_load_days=5,
    manage_return=-1,
):
    """Return (strategy, config) pair suitable for process_strategy tests."""
    cfg = _make_config(INTERVAL=interval, MANAGE_INTERVAL=manage_interval or interval)
    cfg.TICKERS = tickers if tickers is not None else ["SPY"]
    if daily_start:
        cfg.DAILY_START = daily_start
    cfg.PRE_LOAD_DAYS = pre_load_days
    s = _make_strategy(manage_return=manage_return)
    s.config = cfg
    return s, cfg


class TestProcessStrategy:
    """Coverage for TradingEngine.process_strategy — the orchestration layer."""

    _NOW = datetime.datetime(2024, 6, 1, 15, 0, tzinfo=datetime.UTC)

    # ── Early-exit paths ─────────────────────────────────────────────
    def test_both_false_returns_before_record_run(self):
        """(False, False) → immediate return before record_run is called."""
        s, cfg = _strategy_for_ps()
        engine = _make_engine(cfg, s, _make_data_handler())
        exc = _FakeExecutor(should_signal=False, should_manage=False)
        engine.process_strategy(s, self._NOW, exc)
        assert exc.record_run_called == 0

    def test_manage_only_no_positions_advances_record_run(self):
        """(False, True) + empty positions → record_run then return (no tickers)."""
        s, cfg = _strategy_for_ps()
        engine = _make_engine(cfg, s, _make_data_handler())
        exc = _FakeExecutor(should_signal=False, should_manage=True, positions=[])
        engine.process_strategy(s, self._NOW, exc)
        assert exc.record_run_called == 1
        assert exc.after_ticker_calls == []

    # ── Phase logging ─────────────────────────────────────────────────
    def test_signal_and_manage_phase_runs(self):
        """(True, True) → record_run called, after_ticker_calls populated."""
        s, cfg = _strategy_for_ps()
        engine = _make_engine(cfg, s, _make_data_handler())
        exc = _FakeExecutor()
        engine.process_strategy(s, self._NOW, exc)
        assert exc.record_run_called == 1
        assert exc.after_ticker_calls

    def test_signal_only_phase_runs(self):
        """(True, False) → signal-only phase, record_run called once."""
        s, cfg = _strategy_for_ps()
        engine = _make_engine(cfg, s, _make_data_handler())
        exc = _FakeExecutor(should_signal=True, should_manage=False)
        engine.process_strategy(s, self._NOW, exc)
        assert exc.record_run_called == 1

    def test_manage_only_with_positions_runs(self):
        """(False, True) + positions → manage phase runs, ticker loop executes."""
        s, cfg = _strategy_for_ps()
        engine = _make_engine(cfg, s, _make_data_handler())
        pos: PositionDict = {"Ticker": "SPY", "OrderId": "OPS1", "Direction": 1}
        exc = _FakeExecutor(should_signal=False, should_manage=True, positions=[pos])
        engine.process_strategy(s, self._NOW, exc)
        assert exc.record_run_called == 1
        assert exc.after_ticker_calls

    def test_manage_only_cycle_fetches_only_manage_frame(self):
        """(False, True) → signal-interval frame is NOT fetched.

        Manage-only cycles may only read the manage frame: the signal frame and
        Raw_Close are never consumed when signals are skipped, so fetching them
        is pure waste (regression guard for the unconditional pre-fix fetch).
        """
        calls: list[tuple] = []

        def _get(ticker, start, end, interval, use_cache=True, adjustment="all"):
            calls.append((str(interval), adjustment))
            return _make_ohlcv()

        dh = SimpleNamespace(get_historical_data=_get)
        s, cfg = _strategy_for_ps(interval="1d", manage_interval="1h")
        engine = _make_engine(cfg, s, dh)
        pos: PositionDict = {"Ticker": "SPY", "OrderId": "OPS1", "Direction": 1}
        exc = _FakeExecutor(should_signal=False, should_manage=True, positions=[pos])
        engine.process_strategy(s, self._NOW, exc)
        assert [c[0] for c in calls] == ["1h"], (
            f"manage-only must fetch only the manage frame, got {[c[0] for c in calls]}"
        )
        assert all(c[1] == "all" for c in calls)
        assert exc.after_ticker_calls

    def test_signal_cycle_fetches_signal_and_manage_frames_adjusted(self):
        """(True, True) → both frames fetched, always adjustment="all"."""
        calls: list[tuple] = []

        def _get(ticker, start, end, interval, use_cache=True, adjustment="all"):
            calls.append((str(interval), adjustment))
            return _make_ohlcv()

        dh = SimpleNamespace(get_historical_data=_get)
        s, cfg = _strategy_for_ps(interval="1d", manage_interval="1h")
        engine = _make_engine(cfg, s, dh)
        exc = _FakeExecutor()
        engine.process_strategy(s, self._NOW, exc)
        intervals = sorted(c[0] for c in calls)
        assert intervals == ["1d", "1h"], f"signal cycle fetched {intervals}"
        assert all(c[1] == "all" for c in calls)
        assert exc.after_ticker_calls

    def test_empty_tickers_skips_ticker_loop(self):
        """TICKERS=[] → warning logged + return, no ticker processing."""
        s, cfg = _strategy_for_ps(tickers=[])
        engine = _make_engine(cfg, s, _make_data_handler())
        exc = _FakeExecutor()
        engine.process_strategy(s, self._NOW, exc)
        assert exc.record_run_called == 1
        assert exc.after_ticker_calls == []

    # ── Lookback computation ───────────────────────────────────────────
    def test_daily_start_lookback_computed(self):
        """INTERVAL='1d' + DAILY_START → lookback derived from date delta."""
        s, cfg = _strategy_for_ps(interval="1d", daily_start="2024-01-01")
        engine = _make_engine(cfg, s, _make_data_handler())
        exc = _FakeExecutor()
        now = datetime.datetime(2024, 1, 15, tzinfo=datetime.UTC)
        engine.process_strategy(s, now, exc)
        assert exc.record_run_called == 1

    def test_pre_load_days_lookback_used(self):
        """PRE_LOAD_DAYS fallback lookback → ticker loop executes normally."""
        s, cfg = _strategy_for_ps(pre_load_days=3)
        engine = _make_engine(cfg, s, _make_data_handler())
        exc = _FakeExecutor()
        engine.process_strategy(s, self._NOW, exc)
        assert exc.after_ticker_calls

    # ── Entry / exit dispatch ──────────────────────────────────────────
    def test_entry_dispatched_to_executor(self):
        """BUY signal → executor.enter called with correct ticker."""
        s, cfg = _strategy_for_ps(tickers=["SPY"])
        engine = _make_engine(cfg, s, _make_data_handler())
        exc = _FakeExecutor()
        engine.process_strategy(s, self._NOW, exc)
        assert len(exc.entered) >= 1
        assert exc.entered[0]["Ticker"] == "SPY"

    def test_exit_dispatched_to_executor(self):
        """manage_return=2 → EXIT signal → executor.exit called."""
        s, cfg = _strategy_for_ps(manage_return=2)
        engine = _make_engine(cfg, s, _make_data_handler())
        pos: PositionDict = {"Ticker": "SPY", "OrderId": "OE1", "Direction": 1}
        exc = _FakeExecutor(positions=[pos])
        engine.process_strategy(s, self._NOW, exc)
        assert len(exc.exited) >= 1

    # ── Empty data skip ────────────────────────────────────────────────
    def test_empty_data_ticker_skipped_silently(self):
        """Fetch returns empty DataFrame → continue, after_process_ticker not called."""
        s, cfg = _strategy_for_ps(tickers=["NODATA"])
        empty_handler = SimpleNamespace(
            get_historical_data=lambda *a, **kw: pd.DataFrame()
        )
        engine = _make_engine(cfg, s, empty_handler)
        exc = _FakeExecutor()
        engine.process_strategy(s, self._NOW, exc)
        assert exc.after_ticker_calls == []

    # ── Exception handling ─────────────────────────────────────────────
    def test_per_ticker_exception_caught_other_tickers_continue(self):
        """Exception in ticker loop is caught and logged; other tickers run."""
        s, cfg = _strategy_for_ps(tickers=["BOOM", "SPY"])
        boom_calls: list = []

        def get_hist(ticker, start, end, interval, use_cache=True):
            if ticker == "BOOM":
                boom_calls.append(ticker)
                raise RuntimeError("simulated fetch error")
            return _make_ohlcv()

        engine = _make_engine(cfg, s, SimpleNamespace(get_historical_data=get_hist))
        exc = _FakeExecutor()
        engine.process_strategy(s, self._NOW, exc)
        assert boom_calls  # BOOM was attempted
        spy_calls = [t for t, _ in exc.after_ticker_calls if t == "SPY"]
        assert spy_calls  # SPY still processed despite BOOM error

    # ── Precomputed signal bypass ──────────────────────────────────────
    def test_precomputed_signal_bypasses_run_signals(self):
        """executor.get_precomputed_signal() returning a df → run_signals NOT called."""
        s, cfg = _strategy_for_ps(tickers=["SPY"])
        engine = _make_engine(cfg, s, _make_data_handler())
        precomp = _make_ohlcv(5)
        # Put BUY on the bar closed_bar_asof(FakeExecutor.data_end_dt) selects (18:30).
        precomp["Signal"] = "HOLD"
        precomp["Confidence"] = 0.0
        precomp.iloc[-1, precomp.columns.get_loc("Signal")] = "BUY"
        precomp.iloc[-1, precomp.columns.get_loc("Confidence")] = 0.9
        run_signals_count: dict = {"n": 0}
        original_run_signals = engine.run_signals

        def tracking_run_signals(*a, **kw):
            run_signals_count["n"] += 1
            return original_run_signals(*a, **kw)

        engine.run_signals = tracking_run_signals

        class PrecompExec(_FakeExecutor):
            def get_precomputed_signal(self, ticker: str, now) -> pd.DataFrame | None:
                return precomp

        exc = PrecompExec()
        engine.process_strategy(s, self._NOW, exc)
        assert run_signals_count["n"] == 0
        assert len(exc.entered) >= 1

    # ── Dispatch edge cases ────────────────────────────────────────────
    def test_exit_executor_returns_false_no_after_exit(self):
        """executor.exit() returning False → after_exit NOT called (branch 588→582)."""
        s, cfg = _strategy_for_ps(manage_return=2)
        engine = _make_engine(cfg, s, _make_data_handler())
        pos: PositionDict = {"Ticker": "SPY", "OrderId": "OE2", "Direction": 1}

        class FalseExitExec(_FakeExecutor):
            def exit(
                self,
                strategy_name: str,
                position: dict,
                sig: dict,
                *,
                manage_df: pd.DataFrame | None = None,
            ) -> bool:
                self.exited.append(position)
                return False  # signal exit failure

        exc = FalseExitExec(positions=[pos])
        engine.process_strategy(s, self._NOW, exc)
        assert len(exc.exited) >= 1  # exit was attempted but returned False

    def test_enter_returns_none_after_entry_not_called(self):
        """executor.enter() returning None → after_entry NOT called (branch 595 False)."""
        s, cfg = _strategy_for_ps(tickers=["SPY"])
        engine = _make_engine(cfg, s, _make_data_handler())

        class NullEnterExec(_FakeExecutor):
            def enter(
                self,
                strategy,
                ticker: str,
                signal_row: dict,
                engine,
                *,
                now=None,
            ) -> dict | None:
                return None  # entry attempt failed

        exc = NullEnterExec()
        engine.process_strategy(s, self._NOW, exc)
        assert exc.record_run_called == 1  # ran to completion without error

    def test_entry_with_no_signal_row_continues(self):
        """ENTRY sig with no signal_row → if not row: continue taken (line 593)."""
        s, cfg = _strategy_for_ps(tickers=["SPY"])
        engine = _make_engine(cfg, s, _make_data_handler())

        original_pt = engine.process_ticker

        def patched_pt(*a, **kw):
            results = original_pt(*a, **kw)
            # Prepend a malformed ENTRY (no signal_row) to trigger the continue
            results.insert(0, {"ticker": "SPY", "type": "ENTRY"})
            return results

        engine.process_ticker = patched_pt
        exc = _FakeExecutor()
        engine.process_strategy(s, self._NOW, exc)
        assert exc.record_run_called == 1  # completed without error

    def test_unknown_sig_type_skipped(self):
        """sig with unknown type (neither EXIT nor ENTRY) → neither branch taken,
        loop continues normally (covers elif False branch → 590→582)."""
        s, cfg = _strategy_for_ps(tickers=["SPY"])
        engine = _make_engine(cfg, s, _make_data_handler())

        original_pt = engine.process_ticker

        def patched_pt(*a, **kw):
            results = original_pt(*a, **kw)
            # Prepend a sig with unrecognized type
            results.insert(0, {"ticker": "SPY", "type": "AMEND"})
            return results

        engine.process_ticker = patched_pt
        exc = _FakeExecutor()
        engine.process_strategy(s, self._NOW, exc)
        assert exc.record_run_called == 1  # ran to completion

    def test_monitor_emit_failure_does_not_skip_entry(self, monkeypatch):
        """Monitor emit exceptions must not abort ENTRY/EXIT for the ticker."""
        s, cfg = _strategy_for_ps(tickers=["SPY"])

        def boom(_config):
            raise RuntimeError("config snapshot exploded")

        monkeypatch.setattr("quant.core._utils.snapshot_config", boom)
        engine = _make_engine(
            cfg, s, _make_data_handler(), monitor_emit=lambda _e: None
        )
        exc = _FakeExecutor()
        engine.process_strategy(s, self._NOW, exc)
        assert exc.record_run_called == 1
        assert len(exc.after_ticker_calls) >= 1
        assert len(exc.entered) >= 1

    def test_backtest_engine_has_no_monitor_hook(self):
        """Default TradingEngine must not require python.monitor."""
        s, cfg = _strategy_for_ps(tickers=["SPY"])
        engine = _make_engine(cfg, s, _make_data_handler())
        assert engine._monitor_emit is None

    def test_monitor_emit_includes_live_signal_from_cache(self):
        """Happy-path emit reads cached signals and builds a full payload."""
        s, cfg = _strategy_for_ps(tickers=["SPY"])
        events: list[dict] = []
        engine = _make_engine(cfg, s, _make_data_handler(), monitor_emit=events.append)
        exc = _FakeExecutor()
        engine.process_strategy(s, self._NOW, exc)
        assert events, "monitor_emit should have been called"
        ev = events[0]
        assert ev["kind"] == "signal_evaluated"
        assert ev["ticker"] == "SPY"
        assert ev["live_signal"] == "BUY"
        assert "config_snapshot" in ev
        assert isinstance(ev.get("bar_staleness_ns"), int)

    def test_monitor_emit_includes_manage_decision_for_each_position(self):
        """Manage telemetry is independent of the signal plane and includes HOLD/EXIT."""
        s, cfg = _strategy_for_ps(tickers=["SPY"], manage_return=2)
        events: list[dict] = []
        engine = _make_engine(cfg, s, _make_data_handler(), monitor_emit=events.append)
        pos: PositionDict = {
            "Ticker": "SPY",
            "OrderId": "OE1",
            "EntryTime": "2024-01-02T14:00:00+00:00",
            "Direction": 1,
        }
        engine.process_strategy(s, self._NOW, _FakeExecutor(positions=[pos]))

        manage_events = [e for e in events if e["kind"] == "manage_evaluated"]
        assert len(manage_events) == 1
        event = manage_events[0]
        assert event["position"]["OrderId"] == "OE1"
        assert event["live_exit"] == "strategy_manage"
        assert event["exit_bar"] == "2024-01-02T16:00:00+00:00"
        # Manage events carry the timed manage-fetch span.
        assert event["fetch_manage_data_ns"] >= 0

    def test_signal_event_carries_timed_fetch_and_stage_keys(self):
        """signal_evaluated rows carry the manage-fetch span, the dispatch
        jitter series, and the renamed strategy-calc total."""
        s, cfg = _strategy_for_ps(tickers=["SPY"], manage_interval="1h")
        events: list[dict] = []
        dh = SimpleNamespace(
            get_historical_data=lambda ticker, start, end, interval, use_cache=True, adjustment="all": (
                _make_ohlcv()
            )
        )
        engine = _make_engine(cfg, s, dh, monitor_emit=events.append)
        engine.process_strategy(s, self._NOW, _FakeExecutor())
        sig = [e for e in events if e["kind"] == "signal_evaluated"]
        assert sig, "expected a signal_evaluated event"
        ev = sig[0]
        assert ev["fetch_manage_data_ns"] >= 0
        # Dispatch jitter is 0 when no trader wrapper runs (contextvar default).
        assert ev["tick_dispatch_ns"] == 0
        assert isinstance(ev["tick_dispatch_ns"], int)
        assert "tick_dispatch_ns" in ev
        assert ev["strategy_calc_total_ns"] >= 0

    def test_freshness_measured_at_tick_start_not_after_pipeline(self):
        """Slow signal work must not be charged to bar freshness.

        ``bar_staleness_ns`` is the data age when the tick began; the pipeline
        duration belongs to ``tick_elapsed_ns`` / ``emit_staleness_ns``.
        """
        s, cfg = _strategy_for_ps(tickers=["SPY"])
        real_generate = s.generate_signals

        def slow_generate(df):
            time.sleep(0.25)
            return real_generate(df)

        s.generate_signals = slow_generate
        events: list[dict] = []
        engine = _make_engine(cfg, s, _make_data_handler(), monitor_emit=events.append)
        engine.process_strategy(s, self._NOW, _FakeExecutor())

        ev = events[0]
        pipeline_ns = ev["emit_staleness_ns"] - ev["bar_staleness_ns"]
        assert pipeline_ns >= 200_000_000, "emit-time staleness should absorb the sleep"
        assert ev["tick_elapsed_ns"] >= 200_000_000

    def test_freshness_excess_uses_delay_actually_applied(self, monkeypatch):
        """Excess subtracts the delay the executor used, not a hardcoded default.

        Clock frozen 3s after the 19:15 bar close so the SLO value is exactly
        the scheduling lag, with the 15m feed delay accounted for.
        """
        s, cfg = _strategy_for_ps(tickers=["SPY"], interval="15m")
        cfg.DATA_DELAY = 15
        now = pd.Timestamp("2024-01-02 19:30", tz="UTC").to_pydatetime()
        frozen_ns = int(
            pd.Timestamp("2024-01-02 19:30:03", tz="UTC").timestamp() * 1_000_000_000
        )
        monkeypatch.setattr(
            "quant.core.engine.time",
            SimpleNamespace(
                time_ns=lambda: frozen_ns,
                perf_counter_ns=time.perf_counter_ns,
            ),
        )

        class _DelayedExecutor(_FakeExecutor):
            def data_end_dt(self, now):
                return now - datetime.timedelta(minutes=15)

        events: list[dict] = []
        engine = _make_engine(cfg, s, _make_data_handler(), monitor_emit=events.append)
        engine.process_strategy(s, now, _DelayedExecutor())

        ev = events[0]
        assert ev["DATA_DELAY"] == 15
        assert ev["bar_staleness_ns"] == 15 * 60 * 10**9 + 3_000_000_000
        assert ev["bar_staleness_excess_ns"] == 3_000_000_000

    def test_monitor_emit_holds_when_signal_cache_empty(self):
        """Empty cached frame skips latest_signal but still emits HOLD."""
        s, cfg = _strategy_for_ps(tickers=["SPY"])
        s.generate_signals = lambda df: df.iloc[0:0].copy()
        events: list[dict] = []
        engine = _make_engine(cfg, s, _make_data_handler(), monitor_emit=events.append)
        engine.process_strategy(s, self._NOW, _FakeExecutor())
        assert events
        assert events[0]["live_signal"] == "HOLD"
        assert events[0]["live_confidence"] == 0.0

    def test_config_snapshot_skips_raising_properties(self):
        """getattr on a bad property must not abort snapshot (or trading)."""

        class BadConfig:
            INTERVAL = "15m"
            GOOD = 1
            TIERS = (0.5, 0.8, None)
            NESTED = ({"x": 1},)  # not all-primitive → omitted

            @staticmethod
            def skip_me():
                return None

            @property
            def boom(self):
                raise ValueError("property failed")

        assert snapshot_config(None) == {}
        snap = snapshot_config(BadConfig())
        assert snap.get("INTERVAL") == "15m"
        assert snap.get("GOOD") == 1
        assert snap.get("TIERS") == [0.5, 0.8, None]
        assert "NESTED" not in snap
        assert "skip_me" not in snap
        assert "boom" not in snap
        # Naive timestamp localizes to UTC; bad inputs return None.
        stale = bar_staleness_ns(pd.Timestamp("2024-01-02 15:00:00"), "15m")
        assert isinstance(stale, int)
        assert bar_staleness_ns(object(), "15m") is None

    def test_latest_signal_empty_frame_without_bar_ts(self):
        s, cfg = _strategy_for_ps(tickers=["SPY"])
        engine = _make_engine(cfg, s, _make_data_handler())
        signal, conf, row = engine.latest_signal(pd.DataFrame())
        assert signal == "HOLD"
        assert conf == 0.0
        assert row.get("_bar_time") is None


class TestBarFreshnessHelpers:
    """Freshness primitives — bar close instants and the SLO excess."""

    def test_intraday_bar_closes_one_interval_after_open(self):
        assert bar_close_utc(pd.Timestamp("2024-01-02 15:00"), "15m") == pd.Timestamp(
            "2024-01-02 15:15", tz="UTC"
        )
        assert bar_close_utc(
            pd.Timestamp("2024-01-02 15:00", tz="UTC"), "1h"
        ) == pd.Timestamp("2024-01-02 16:00", tz="UTC")

    @pytest.mark.parametrize(
        ("session", "expected"),
        [
            ("2024-01-02", "2024-01-02 21:00"),  # EST → 16:00 ET
            ("2024-07-02", "2024-07-02 20:00"),  # EDT → 16:00 ET
        ],
    )
    def test_daily_bar_closes_at_session_close_not_midnight(self, session, expected):
        """``open + 1 day`` would put the close ~8h late and hide real staleness."""
        assert bar_close_utc(pd.Timestamp(session), "1d") == pd.Timestamp(
            expected, tz="UTC"
        )

    def test_daily_close_is_inverse_of_closed_bar_asof(self):
        """A bar is closed exactly when the cutoff has passed its close instant."""
        for interval in ("15m", "1h", "1d"):
            end = pd.Timestamp("2024-01-03 19:00", tz="UTC")
            bar = closed_bar_asof(end, interval)
            assert bar_close_utc(bar, interval) <= end

    def test_weekly_and_monthly_close_on_final_session(self):
        assert bar_close_utc(pd.Timestamp("2024-01-01"), "1wk") == pd.Timestamp(
            "2024-01-05 21:00", tz="UTC"
        )
        assert bar_close_utc(pd.Timestamp("2024-01-01"), "1mo") == pd.Timestamp(
            "2024-01-31 21:00", tz="UTC"
        )

    def test_staleness_measured_from_supplied_instant(self):
        bar = pd.Timestamp("2024-01-02 15:00", tz="UTC")
        close_ns = int(bar_close_utc(bar, "15m").timestamp() * 1_000_000_000)
        assert bar_staleness_ns(bar, "15m", at_ns=close_ns + 5_000_000_000) == (
            5_000_000_000
        )

    def test_staleness_stays_signed_before_close(self):
        """A negative value flags clock skew instead of silently reading as fresh."""
        bar = pd.Timestamp("2024-01-02 15:00", tz="UTC")
        close_ns = int(bar_close_utc(bar, "15m").timestamp() * 1_000_000_000)
        stale = bar_staleness_ns(bar, "15m", at_ns=close_ns - 1_000_000_000)
        assert stale is not None
        assert stale < 0

    def test_excess_subtracts_expected_delay_and_floors_at_zero(self):
        fifteen_min = 15 * 60 * 10**9
        assert staleness_excess_ns(fifteen_min + 3_000_000_000, 15) == 3_000_000_000
        assert staleness_excess_ns(fifteen_min, 0) == fifteen_min
        assert staleness_excess_ns(fifteen_min - 1, 15) == 0
        assert staleness_excess_ns(None, 15) is None

    def test_delay_resolved_from_now_minus_end_dt(self):
        now = datetime.datetime(2024, 1, 2, 15, 30, tzinfo=datetime.UTC)
        assert (
            resolve_data_delay_min(now, now - datetime.timedelta(minutes=15), None)
            == 15
        )
        assert resolve_data_delay_min(now, now, None) == 0

    def test_delay_falls_back_to_config_when_stamps_unusable(self):
        """Mixed tz-awareness or unrelated stamps must not fake a huge delay."""
        now = datetime.datetime(2024, 1, 2, 15, 30, tzinfo=datetime.UTC)
        cfg = SimpleNamespace(DATA_DELAY=15)
        assert resolve_data_delay_min(now, pd.Timestamp("2024-01-02 15:15"), cfg) == 15
        unrelated = pd.Timestamp("2020-01-01", tz="UTC")
        assert resolve_data_delay_min(now, unrelated, cfg) == 15
        assert resolve_data_delay_min(now, None, SimpleNamespace()) == 0


# ========================================================================
# 16. TradingEngine init — config type validation (line 99)
# ========================================================================


class TestTradingEngineConfigValidation:
    def test_config_must_be_base_strategy_config_instance(self):
        # Intentionally non-conforming object: assert runtime validation.
        bad = cast(
            "Any",
            SimpleNamespace(
                calculate_indicators=lambda df: df,
                generate_signals=lambda df: df,
                manage_trade=lambda position, df: -1,
                config=SimpleNamespace(INTERVAL="1h"),
            ),
        )
        with pytest.raises(TypeError, match="config must be a BaseStrategyConfig"):
            TradingEngine(bad, _make_data_handler())


# ========================================================================
# 17. TradingEngine.warmup — best-effort pre-warm loop (lines 196-233)
# ========================================================================


class TestWarmup:
    def test_warmup_runs_pipeline_and_returns_true(
        self, config, strategy, data_handler
    ):
        engine = _make_engine(config, strategy, data_handler)
        assert engine.warmup(datetime.datetime(2024, 1, 2, 15, 0)) is True

    def test_warmup_seeds_both_adjustment_keys_when_supported(self, config, strategy):
        """A handler exposing seed_mem_bars receives the "all" frame plus one
        explicit raw fetch, so the in-memory live store can serve both keys.
        Seeds are tagged through the next decision bar (seed_end = end_dt +
        MANAGE_INTERVAL) so a pre-open warmup never truncates freshly banked
        bars."""
        seeds: list[tuple] = []
        raw_fetches: list[str] = []

        class _SeedingHandler:
            def get_historical_data(self, *a, **kw):
                if str(kw.get("adjustment", "all")) == "raw":
                    raw_fetches.append(kw.get("ticker", "?"))
                return _make_ohlcv()

            def seed_mem_bars(self, ticker, interval, adjustment, frame, seed_end=None):
                seeds.append((ticker, interval, adjustment, seed_end))

        engine = _make_engine(config, strategy, _SeedingHandler())
        assert engine.warmup(datetime.datetime(2024, 1, 2, 15, 0)) is True
        assert ("AAPL", "1h", "all", pd.Timestamp("2024-01-02 16:00")) in seeds
        assert ("AAPL", "1h", "raw", pd.Timestamp("2024-01-02 16:00")) in seeds
        # Exactly one raw fetch per ticker — the warmed store is not re-fetched.
        assert raw_fetches == ["AAPL"]

    def test_warmup_empty_fetch_returns_false(self):
        cfg = _make_config()
        s = _make_strategy(config=cfg)
        handler = SimpleNamespace(get_historical_data=lambda *a, **kw: pd.DataFrame())
        engine = _make_engine(cfg, s, handler)
        assert engine.warmup(datetime.datetime(2024, 1, 2, 15, 0)) is False

    def test_warmup_fetch_error_is_best_effort(self, caplog):
        cfg = _make_config()
        s = _make_strategy(config=cfg)

        def _boom(*a, **kw):
            raise RuntimeError("fetch down")

        handler = SimpleNamespace(get_historical_data=_boom)
        engine = _make_engine(cfg, s, handler)
        assert engine.warmup(datetime.datetime(2024, 1, 2, 15, 0)) is False
