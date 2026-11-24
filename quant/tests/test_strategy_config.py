"""Basic validation on ``BaseStrategyConfig.__post_init__``."""

from __future__ import annotations

import pytest

from quant.utils.strategy_config import BaseStrategyConfig


def _kwargs(**overrides) -> dict:
    base = {
        "INTERVAL": "15m",
        "PRE_LOAD_DAYS": 100,
        "MANAGE_INTERVAL": "15m",
        "INITIAL_CAPITAL": 25_000.0,
        "CAPITAL_PER_TRADE": 0.2,
        "MAX_CONCURRENT_POSITIONS": 1,
        "STOP_LOSS_PERCENT": 0.3,
        "TAKE_PROFIT_PERCENT": 2.0,
        "USE_TRAIL_LOSS": False,
        "MAX_HOLDING_MINUTES": 360,
        "MIN_SIGNAL_CONFIDENCE": 0.5,
        "BASE_SLIPPAGE_FACTOR": 0.002,
        "TICKERS": ["SPY"],
    }
    base.update(overrides)
    return base


def test_valid_config_constructs_without_error():
    cfg = BaseStrategyConfig(**_kwargs())
    assert cfg.TICKERS == ["SPY"]


def test_sentinel_disable_values_are_allowed():
    # Real strategies use -1 / 999 as "feature disabled" sentinels — these
    # must NOT be rejected by validation.
    cfg = BaseStrategyConfig(
        **_kwargs(
            STOP_LOSS_PERCENT=999,
            TAKE_PROFIT_PERCENT=-1.0,
        )
    )
    assert cfg.TAKE_PROFIT_PERCENT == -1.0


@pytest.mark.parametrize(
    "overrides",
    [
        {"INTERVAL": ""},
        {"MANAGE_INTERVAL": ""},
        {"PRE_LOAD_DAYS": -1},
        {"INITIAL_CAPITAL": 0},
        {"INITIAL_CAPITAL": -100},
        {"CAPITAL_PER_TRADE": 0},
        {"CAPITAL_PER_TRADE": 1.5},
        {"MAX_CONCURRENT_POSITIONS": 0},
        {"MAX_HOLDING_MINUTES": 0},
        {"MIN_SIGNAL_CONFIDENCE": -0.1},
        {"MIN_SIGNAL_CONFIDENCE": 1.1},
        {"BASE_SLIPPAGE_FACTOR": -0.01},
        {"TICKERS": []},
        {"MIN_OPTION_DTE": -1},
        {"MIN_SPREAD_WIDTH": 0},
        {"MIN_SPREAD_WIDTH": 5, "MIN_OPTION_DTE": None},
        {"OPTION_STRIKE_METHOD": "bogus"},
        {"OPTION_STRIKE_INCREMENT": 0},
        {"OPTION_MAX_HOLD_DAYS": 0},
        {"DATA_DELAY": -1},
    ],
)
def test_invalid_config_raises_value_error(overrides):
    with pytest.raises(ValueError):
        BaseStrategyConfig(**_kwargs(**overrides))


def test_error_message_lists_all_violations():
    with pytest.raises(ValueError) as exc_info:
        BaseStrategyConfig(**_kwargs(CAPITAL_PER_TRADE=0, TICKERS=[]))
    message = str(exc_info.value)
    assert "CAPITAL_PER_TRADE" in message
    assert "TICKERS" in message
