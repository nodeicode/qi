"""Validation on the declarative ``BaseStrategyConfig`` (spec `strategy-sdk`).

Covers the config-contract reshaped in 3.4: the engine-driven knobs
(``INTERVAL``/``MANAGE_INTERVAL``/``CAPITAL_PER_TRADE``/``TICKERS``/option
strikes) are dropped, the declarative ``instruments``/``subscriptions``/``venue``
fields are added, and an otherwise-invalid config is rejected at construction
naming the offending field.
"""

from __future__ import annotations

import pytest

from quant.data import Datatype, Subscription
from quant.instruments import EquityInstrument
from quant.utils.strategy_config import BaseStrategyConfig

_SPY = EquityInstrument(symbol="SPY", venue="backtest")
_SUB = Subscription(instrument=_SPY, datatype=Datatype.BARS, cadence="1h")


def _kwargs(**overrides) -> dict:
    base = {
        "PRE_LOAD_DAYS": 100,
        "INITIAL_CAPITAL": 25_000.0,
        "MAX_CONCURRENT_POSITIONS": 1,
        "STOP_LOSS_PERCENT": 0.3,
        "TAKE_PROFIT_PERCENT": 2.0,
        "USE_TRAIL_LOSS": False,
        "MAX_HOLDING_MINUTES": 360,
        "MIN_SIGNAL_CONFIDENCE": 0.5,
        "BASE_SLIPPAGE_FACTOR": 0.002,
        "instruments": [_SPY],
        "subscriptions": [_SUB],
        "venue": "backtest",
    }
    base.update(overrides)
    return base


def test_valid_declarative_config_constructs() -> None:
    cfg = BaseStrategyConfig(**_kwargs())
    assert cfg.instruments == [_SPY]
    assert cfg.subscriptions == [_SUB]
    assert cfg.venue == "backtest"


def test_dropped_engine_knobs_are_no_longer_config_fields() -> None:
    """The engine-driven knobs the author no longer controls must not be part
    of the config contract — cadence/sizing/tickers moved onto declarations."""
    cfg = BaseStrategyConfig(**_kwargs())
    for knob in (
        "INTERVAL",
        "MANAGE_INTERVAL",
        "CAPITAL_PER_TRADE",
        "TICKERS",
        "MIN_OPTION_DTE",
        "MIN_SPREAD_WIDTH",
        "OPTION_STRIKE_METHOD",
    ):
        assert not hasattr(cfg, knob), f"{knob} should be dropped from the config"

    with pytest.raises(TypeError):
        # Passing a dropped knob as an init kwarg is a contract mistake.
        BaseStrategyConfig(**_kwargs(INTERVAL="1h"))


def test_sentinel_disable_values_are_allowed() -> None:
    # Real strategies use -1 / 999 as "feature disabled" sentinels — these
    # must NOT be rejected by validation.
    cfg = BaseStrategyConfig(
        **_kwargs(
            STOP_LOSS_PERCENT=999,
            TAKE_PROFIT_PERCENT=-1.0,
        )
    )
    assert cfg.TAKE_PROFIT_PERCENT == -1.0


def test_canonical_reference_config_resolves() -> None:
    # spec `strategy-sdk` "Declarative reference config resolves" — the
    # canonical `example_equity` config, shaped against the 3.4 declaration set
    # (the framework risk fields keep their current names until the reference
    # strategy rewrite lands; the declarative fields are already lowercase).
    cfg = BaseStrategyConfig(
        instruments=[_SPY],
        subscriptions=[_SUB],
        venue="backtest",
        INITIAL_CAPITAL=100_000.0,
        MAX_CONCURRENT_POSITIONS=1,
        PRE_LOAD_DAYS=60,
        STOP_LOSS_PERCENT=0.05,
        TAKE_PROFIT_PERCENT=-1.0,
        USE_TRAIL_LOSS=False,
        MAX_HOLDING_MINUTES=60 * 24 * 30,
        MIN_SIGNAL_CONFIDENCE=0.0,
        BASE_SLIPPAGE_FACTOR=0.001,
    )
    assert cfg.venue == "backtest"
    assert cfg.subscriptions == [_SUB]
    assert cfg.INITIAL_CAPITAL == 100_000.0


@pytest.mark.parametrize(
    "overrides",
    [
        {"PRE_LOAD_DAYS": -1},
        {"INITIAL_CAPITAL": 0},
        {"INITIAL_CAPITAL": -100},
        {"MAX_CONCURRENT_POSITIONS": 0},
        {"MAX_HOLDING_MINUTES": 0},
        {"MIN_SIGNAL_CONFIDENCE": -0.1},
        {"MIN_SIGNAL_CONFIDENCE": 1.1},
        {"BASE_SLIPPAGE_FACTOR": -0.01},
        {"DATA_DELAY": -1},
        {"BARS_LOOKBACK_DAYS": 0},
        {"instruments": "not-a-list"},
        {"subscriptions": "not-a-list"},
        {"venue": 123},
    ],
)
def test_invalid_config_raises_value_error(overrides):
    with pytest.raises(ValueError):
        BaseStrategyConfig(**_kwargs(**overrides))


def test_error_message_lists_all_violations() -> None:
    with pytest.raises(ValueError) as exc_info:
        BaseStrategyConfig(
            **_kwargs(
                MAX_CONCURRENT_POSITIONS=0,
                instruments="bad",
                venue=123,  # not a str — structural type violation
            )
        )
    message = str(exc_info.value)
    assert "MAX_CONCURRENT_POSITIONS" in message
    assert "instruments" in message
    assert "venue" in message


def test_bad_field_names_accepted_values() -> None:
    """A bad field value names the field and the accepted values."""
    with pytest.raises(ValueError) as exc_info:
        BaseStrategyConfig(**_kwargs(MIN_SIGNAL_CONFIDENCE=1.5))
    message = str(exc_info.value)
    assert "MIN_SIGNAL_CONFIDENCE" in message
    assert "[0, 1]" in message
