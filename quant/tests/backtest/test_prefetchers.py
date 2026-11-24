"""Tests for the standalone prefetch helpers moved out of ``Backtester``.

``prefetch_option_contracts`` / ``prefetch_ic_legs`` used to be private
methods on ``Backtester`` that mutated ``self`` directly. They are now
pure(-ish) functions in ``quant.backtest.prefetchers`` that *return* their
results — callers (``Backtester._prefetch_data``) must capture and persist
those return values. These tests pin down both the request-building logic
and the full round trip of return values so a future refactor can't silently
drop a field again (as happened with ``_option_data_cache`` / ``_ic_leg_prices``
/ ``_ic_leg_cbbo``).
"""

from __future__ import annotations

import logging
from types import SimpleNamespace
from typing import cast
from unittest.mock import MagicMock

import pandas as pd

from quant.backtest.prefetchers import prefetch_ic_legs, prefetch_option_contracts
from quant.utils.strategy_config import BaseStrategyConfig

_LOGGER = logging.getLogger("test_prefetchers")


def _make_config(**overrides) -> BaseStrategyConfig:
    # A duck-typed stand-in is enough at runtime (the prefetchers only use
    # ``getattr``/attribute access); cast so type checkers see the real type.
    base = {
        "INTERVAL": "15m",
        "MANAGE_INTERVAL": "15m",
        "DATA_DELAY": 0,
        "MAX_HOLDING_MINUTES": 360,
        "OPTION_MAX_HOLD_DAYS": 10,
        "IC_RECENTER_AT_FILL": False,
        "CBBO_WINDOW_MIN": 2,
        # Strike/spread selection now runs through the pure core functions
        # (``quant.core._utils.select_strike`` / ``calc_spread_width``), not the
        # engine. Defaults yield ATM round(100 / 10) * 10 = 100.0 and a $5 width.
        "OPTION_STRIKE_METHOD": "atm",
        "OPTION_STRIKE_INCREMENT": 10,
        "MIN_SPREAD_WIDTH": 5,
    }
    base.update(overrides)
    return cast("BaseStrategyConfig", SimpleNamespace(**base))


class TestPrefetchOptionContracts:
    def test_option_single_leg_builds_batch_request_and_returns_cache(self):
        signals = pd.DataFrame(
            {"Signal": ["BUY_CALL"], "Raw_Close": [100.0]},
            index=pd.DatetimeIndex(["2024-06-03 13:45:00"]),
        )
        expiration_cache = {"2024-06-03": "2024-06-03"}
        config = _make_config()
        option_data_handler = MagicMock()
        fake_cache = {
            ("SPY", "2024-06-03", 100.0, "call"): pd.DataFrame({"bid": [1.0]})
        }
        option_data_handler.fetch_cbbo_range_batch.return_value = fake_cache

        out = prefetch_option_contracts(
            "SPY",
            "OPTION",
            signals,
            expiration_cache,
            option_data_handler,
            config,
        )

        assert out is fake_cache
        option_data_handler.fetch_cbbo_range_batch.assert_called_once()
        (reqs,), _ = option_data_handler.fetch_cbbo_range_batch.call_args
        assert len(reqs) == 1
        req = reqs[0]
        assert req["ticker"] == "SPY"
        assert req["expiration"] == "2024-06-03"
        assert req["strike"] == 100.0
        assert req["opt_type"] == "call"

    def test_spread_signal_builds_two_leg_batch_request(self):
        signals = pd.DataFrame(
            {"Signal": ["SELL_CALL"], "Raw_Close": [100.0]},
            index=pd.DatetimeIndex(["2024-06-03 13:45:00"]),
        )
        expiration_cache = {"2024-06-03": "2024-06-03"}
        config = _make_config()
        option_data_handler = MagicMock()
        option_data_handler.fetch_cbbo_range_batch.return_value = {}

        prefetch_option_contracts(
            "SPY",
            "SPREAD",
            signals,
            expiration_cache,
            option_data_handler,
            config,
        )

        (reqs,), _ = option_data_handler.fetch_cbbo_range_batch.call_args
        # One task -> two legs (long + short) for a spread.
        assert len(reqs) == 2
        strikes = {r["strike"] for r in reqs}
        # SELL_CALL is a credit spread: long = select_strike() = 100.0,
        # short = long + calc_spread_width() = 105.0.
        assert strikes == {100.0, 105.0}

    def test_missing_expiration_skips_signal_and_does_not_fetch(self):
        signals = pd.DataFrame(
            {"Signal": ["BUY_CALL"], "Raw_Close": [100.0]},
            index=pd.DatetimeIndex(["2024-06-03 13:45:00"]),
        )
        config = _make_config()
        option_data_handler = MagicMock()

        out = prefetch_option_contracts(
            "SPY",
            "OPTION",
            signals,
            {},  # no expiration cached for this day -> skip
            option_data_handler,
            config,
        )

        assert out is None
        option_data_handler.fetch_cbbo_range_batch.assert_not_called()


class TestPrefetchIcLegs:
    def _signals(self) -> pd.DataFrame:
        return pd.DataFrame(
            {
                "Signal": ["SELL_IC"],
                "K_LP": [90.0],
                "K_SP": [95.0],
                "K_SC": [105.0],
                "K_LC": [110.0],
                "Raw_Close": [100.0],
            },
            index=pd.DatetimeIndex(["2024-06-03 13:45:00"]),
        )

    def test_returns_leg_prices_and_cbbo_alongside_manage_frames(self):
        """Regression test: the free function must hand back leg prices/cbbo,
        not just the manage frames — these are required for entry fills and
        were previously dropped after the method -> function refactor.
        """
        df_signals = self._signals()
        expiration_cache = {"2024-06-03": "2024-06-03"}
        config = _make_config()

        cbbo_by_leg = {
            (0, "lp"): {"bid": 0.9, "ask": 1.1, "mid": 1.0, "ts_used": None},
            (0, "sp"): {"bid": 1.4, "ask": 1.6, "mid": 1.5, "ts_used": None},
            (0, "sc"): {"bid": 1.4, "ask": 1.6, "mid": 1.5, "ts_used": None},
            (0, "lc"): {"bid": 0.9, "ask": 1.1, "mid": 1.0, "ts_used": None},
        }
        option_data_handler = MagicMock()
        option_data_handler.prefetch_ic_cbbo.return_value = cbbo_by_leg
        option_data_handler._last_cbbo_prefetch_stats = {}
        option_data_handler._missing_symbols = set()

        data_handler = MagicMock()
        data_handler.get_historical_data.return_value = pd.DataFrame()

        (
            df_manage,
            df_manage_1m,
            ic_leg_prices,
            ic_leg_cbbo,
            ic_prefetch_stats,
            ic_prefetch_detail,
        ) = prefetch_ic_legs(
            "SPY",
            _LOGGER,
            {},
            expiration_cache,
            config,
            df_signals,
            option_data_handler,
            data_handler,
        )

        assert ic_leg_prices == {k: v["mid"] for k, v in cbbo_by_leg.items()}
        assert ic_leg_cbbo == cbbo_by_leg
        assert ic_prefetch_stats["complete_tasks"] == 1
        assert ic_prefetch_stats["legs_found"] == 4
        assert len(ic_prefetch_detail) == 1
        assert ic_prefetch_detail[0]["complete"] is True
        # Same-interval IC reuses the signal frame for manage data.
        assert isinstance(df_manage, pd.DataFrame)
        assert isinstance(df_manage_1m, pd.DataFrame)

    def test_no_ic_signals_returns_all_empty_without_fetching(self):
        df_signals = pd.DataFrame(
            {
                "Signal": ["BUY_CALL"],
                "K_LP": [90.0],
                "K_SP": [95.0],
                "K_SC": [105.0],
                "K_LC": [110.0],
            },
            index=pd.DatetimeIndex(["2024-06-03 13:45:00"]),
        )
        config = _make_config()
        option_data_handler = MagicMock()
        data_handler = MagicMock()

        out = prefetch_ic_legs(
            "SPY",
            _LOGGER,
            {},
            {},
            config,
            df_signals,
            option_data_handler,
            data_handler,
        )

        df_manage, df_manage_1m, ic_leg_prices, ic_leg_cbbo, stats, detail = out
        assert df_manage.empty
        assert df_manage_1m.empty
        assert ic_leg_prices == {}
        assert ic_leg_cbbo == {}
        assert stats == {}
        assert detail == []
        option_data_handler.prefetch_ic_cbbo.assert_not_called()

    def test_missing_expiration_drops_signal_via_ic_drop_stats(self):
        df_signals = self._signals()
        config = _make_config()
        option_data_handler = MagicMock()
        data_handler = MagicMock()
        ic_drop_stats: dict[str, int] = {}

        out = prefetch_ic_legs(
            "SPY",
            _LOGGER,
            ic_drop_stats,
            {},  # no expiration cached -> signal dropped
            config,
            df_signals,
            option_data_handler,
            data_handler,
        )

        assert ic_drop_stats.get("prefetch_no_expiration") == 1
        _, _, ic_leg_prices, ic_leg_cbbo, _, _ = out
        assert ic_leg_prices == {}
        assert ic_leg_cbbo == {}
        option_data_handler.prefetch_ic_cbbo.assert_not_called()
