"""Tests for quant.core.sizing — per-asset-class sizing strategies.

Covers the Strategy-pattern registry, dispatch via ``size_for_asset``/the
engine's ``size_for_signal``, byte-parity of the four existing engine size
paths, and the additive futures sizing.
"""

from __future__ import annotations

import math
from types import SimpleNamespace
from typing import Any, cast

import pytest

from quant.core._utils import SIGNAL_REGISTRY, AssetClass
from quant.core.sizing import (
    SIZING_STRATEGIES,
    SizingRegistry,
    size_for_asset,
    size_for_signal,
)
from quant.utils.strategy_config import BaseStrategyConfig


def _config(**overrides) -> BaseStrategyConfig:
    """Build a ``BaseStrategyConfig`` subclass carrying sizing-extension fields.

    ``MAX_CAPITAL_PER_TRADE`` / ``USE_CONFIDENCE_SIZING`` / ``CONFIDENCE_SIZING_TIERS``
    / ``FUTURES_MARGIN_*`` are strategy-subclass fields in the real codebase
    (not on ``BaseStrategyConfig``), so tests inject them via a dynamic subclass
    — the same mechanism strategies use.
    """
    base = {
        "INTERVAL": "1d",
        "MANAGE_INTERVAL": "1d",
        "PRE_LOAD_DAYS": 5,
        "INITIAL_CAPITAL": 10_000.0,
        "CAPITAL_PER_TRADE": 0.1,
        "MAX_CONCURRENT_POSITIONS": 1,
        "STOP_LOSS_PERCENT": 0.3,
        "TAKE_PROFIT_PERCENT": -1.0,
        "USE_TRAIL_LOSS": False,
        "MAX_HOLDING_MINUTES": 1440,
        "MIN_SIGNAL_CONFIDENCE": 0.5,
        "BASE_SLIPPAGE_FACTOR": 0.0,
        "TICKERS": ["TEST"],
        # CRITICAL: modern IC configs carry MAX_CAPITAL_PER_TRADE (e.g.
        # example_equity uses 100_000.0).  Without it the historical size_ic
        # clamps capital to min(capital, 0.0) = 0.0.  Base sets a sane cap so
        # the default-path sizing tests exercise the normal branch.
        "MAX_CAPITAL_PER_TRADE": 9_999_999.0,
    }
    base.update(overrides)
    base.pop("cap_override", None)
    extra = {
        k: v
        for k, v in base.items()
        if k not in BaseStrategyConfig.__dataclass_fields__
    }
    for k in extra:
        del base[k]
    cls = type("TestStrategyConfig", (BaseStrategyConfig,), extra)
    return cls(**base)


class TestRegistry:
    def test_all_engine_asset_classes_registered(self):
        registered = set(SIZING_STRATEGIES.asset_classes())
        assert {"EQUITY", "OPTION", "SPREAD", "IC", "FUTURES"} <= registered

    def test_get_unknown_asset_class_fails(self):
        with pytest.raises(KeyError):
            SIZING_STRATEGIES.get(AssetClass.OOF, strict=True)

    def test_duplicate_register_rejected(self):
        from quant.core.sizing import EquitySizing

        reg = SizingRegistry()
        reg.register(EquitySizing())
        with pytest.raises(ValueError):
            reg.register(EquitySizing())

    def test_custom_strategy_registerable(self):
        from typing import cast

        from quant.core.sizing import SizingStrategy

        class MySizing(SizingStrategy):
            asset_class = AssetClass.OOF

            def size(self, *args, **kw):
                return {"n_contracts": 3}

        reg = SizingRegistry()
        reg.register(MySizing())
        assert reg.get(AssetClass.OOF).size(config=None) == {"n_contracts": 3}


class TestEquitySizingParity:
    def test_matches_historical_size_equity(self):
        # No engine needed — sizes are pure functions of config.
        cfg = _config(CAPITAL_PER_TRADE=0.1)
        out = size_for_asset(
            AssetClass.EQUITY, config=cfg, equity=10_000, entry_price=50.0
        )
        assert out["effective_capital"] == pytest.approx(1_000.0)
        assert out["shares"] == 20
        assert out["capital_fraction"] == pytest.approx(0.1)

    def test_zero_equity_price_gives_zero_shares(self):
        cfg = _config()
        out = size_for_asset(
            AssetClass.EQUITY, config=cfg, equity=10_000, entry_price=0.0
        )
        assert out["shares"] == 0

    def test_position_size_multiplier(self):
        cfg = _config(CAPITAL_PER_TRADE=0.5)
        out = size_for_asset(
            AssetClass.EQUITY,
            config=cfg,
            equity=10_000,
            entry_price=100.0,
            pos_size=0.5,
        )
        assert out["effective_capital"] == 2_500.0
        assert out["shares"] == 25


class TestOptionSpreadIcParity:
    def test_option_sizing(self):
        cfg = _config(CAPITAL_PER_TRADE=0.1)
        out = size_for_asset(
            AssetClass.OPTION, config=cfg, equity=10_000, premium_per_contract=40.0
        )
        assert out["capital"] == pytest.approx(1_000.0)
        assert out["n_contracts"] == 25

    def test_option_zero_premium_skips(self):
        cfg = _config()
        out = size_for_asset(
            AssetClass.OPTION, config=cfg, equity=10_000, premium_per_contract=0.0
        )
        assert out["n_contracts"] == 0

    def test_spread_sizing_uses_cpt_as_dollars(self):
        # Spread sizing reads CAPITAL_PER_TRADE as a direct dollar amount —
        # historically a duck-typed config (values >1 can't pass the validated
        # BaseStrategyConfig), so tests hand it a SimpleNamespace.
        cfg = cast("BaseStrategyConfig", SimpleNamespace(CAPITAL_PER_TRADE=1_000.0))
        out = size_for_asset(AssetClass.SPREAD, config=cfg, entry_price_per_share=0.5)
        assert out == {"capital": 1_000.0, "n_contracts": 20}

    def test_spread_zero_width_skips(self):
        cfg = cast("BaseStrategyConfig", SimpleNamespace(CAPITAL_PER_TRADE=1_000.0))
        out = size_for_asset(AssetClass.SPREAD, config=cfg, entry_price_per_share=0.0)
        assert out["n_contracts"] == 0

    def test_ic_sizing_flat(self):
        cfg = _config(CAPITAL_PER_TRADE=0.1)
        out = size_for_asset(
            AssetClass.IC,
            config=cfg,
            equity=10_000,
            confidence=0.9,
            collateral_per_contract=200.0,
        )
        assert out["capital"] == pytest.approx(1_000.0)
        assert out["n_contracts"] == 5

    def test_ic_confidence_tiers(self):
        cfg = _config(
            CAPITAL_PER_TRADE=0.1,
            USE_CONFIDENCE_SIZING=True,
            CONFIDENCE_SIZING_TIERS=[(0.8, 0.25), (0.6, 0.15)],
            MAX_CAPITAL_PER_TRADE=9_999_999.0,
        )
        out = size_for_asset(
            AssetClass.IC,
            config=cfg,
            equity=10_000,
            confidence=0.9,
            collateral_per_contract=200.0,
        )
        assert out["capital"] == 2_500.0  # 0.25 tier
        assert out["n_contracts"] == 12

    def test_ic_max_capital_clamp(self):
        cfg = _config(
            CAPITAL_PER_TRADE=0.5,
            MAX_CAPITAL_PER_TRADE=900.0,
        )
        out = size_for_asset(
            AssetClass.IC,
            config=cfg,
            equity=10_000,
            confidence=0.5,
            collateral_per_contract=200.0,
        )
        assert out["capital"] == pytest.approx(900.0)
        assert out["n_contracts"] == 4

    def test_ic_invalid_collateral_bails_to_zero(self):
        cfg = _config()
        out = size_for_asset(
            AssetClass.IC,
            config=cfg,
            equity=10_000,
            confidence=0.5,
            collateral_per_contract=0.0,
        )
        assert out["n_contracts"] == 0

    def test_ic_nonfinite_capital_bails_to_zero(self):
        cfg = _config()
        out = size_for_asset(
            AssetClass.IC,
            config=cfg,
            equity=float("nan"),
            confidence=0.5,
            collateral_per_contract=200.0,
        )
        assert out["n_contracts"] == 0


class TestFuturesSizing:
    def test_margin_fraction_model(self):
        cfg = _config(CAPITAL_PER_TRADE=0.1, FUTURES_MARGIN_FRACTION=0.10)
        # MES multiplier 5.0; entry_price 4000 -> margin = 0.10*4000*5 = $2,000/contract
        out = size_for_asset(
            AssetClass.FUTURES,
            config=cfg,
            equity=25_000,
            entry_price=4_000.0,
            root="MES",
        )
        assert out["n_contracts"] == 1  # capital 2,500 // 2,000
        assert out["margin_per_contract"] == 2_000.0

    def test_explicit_margin_override(self):
        cfg = _config(CAPITAL_PER_TRADE=0.5, FUTURES_MARGIN_PER_CONTRACT=1_000.0)
        out = size_for_asset(
            AssetClass.FUTURES,
            config=cfg,
            equity=10_000,
            entry_price=4_000.0,
            root="ES",
        )
        # capital 5,000 // 1,000 = 5 contracts
        assert out["n_contracts"] == 5
        assert out["margin_per_contract"] == 1_000.0

    def test_insufficient_capital_skips(self):
        cfg = _config(CAPITAL_PER_TRADE=0.01, FUTURES_MARGIN_PER_CONTRACT=5_000.0)
        out = size_for_asset(
            AssetClass.FUTURES,
            config=cfg,
            equity=10_000,
            entry_price=4_000.0,
            root="ES",
        )
        assert out["n_contracts"] == 0

    def test_unknown_root_fails_loud(self):
        cfg = _config()
        with pytest.raises(KeyError):
            size_for_asset(
                AssetClass.FUTURES,
                config=cfg,
                equity=10_000,
                entry_price=4_000.0,
                root="OIL",
            )

    def test_missing_root_and_no_margin_fails(self):
        cfg = _config()
        with pytest.raises(ValueError):
            size_for_asset(
                AssetClass.FUTURES, config=cfg, equity=10_000, entry_price=4_000.0
            )


class TestEngineFacade:
    def _engine(self, config):
        from types import SimpleNamespace

        return SimpleNamespace(strategy=SimpleNamespace(config=config))

    def test_size_for_signal_dispatches_by_asset_class(self):
        cfg = _config()
        eq = size_for_signal("BUY", config=cfg, equity=10_000, entry_price=50.0)
        assert eq["shares"] == 20
        # Unknown signal falls back to equity sizing (live-safe forgiveness).
        unk = size_for_signal(
            "NOT_A_SIGNAL", config=cfg, equity=10_000, entry_price=50.0
        )
        assert unk == eq

    def test_size_for_signal_ic_and_option_dispatch(self):
        cfg = _config()
        ic = size_for_signal(
            "SELL_IC",
            config=cfg,
            equity=10_000,
            confidence=0.9,
            collateral_per_contract=200.0,
        )
        assert ic["n_contracts"] == 5
        opt = size_for_signal(
            "BUY_CALL", config=cfg, equity=10_000, premium_per_contract=40.0
        )
        assert opt["n_contracts"] == 25
        spread = size_for_signal("BUY_SPREAD", config=cfg, entry_price_per_share=1.0)
        assert spread["n_contracts"] >= 1
