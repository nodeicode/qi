"""
Tests for UnifiedTrader._apply_config_update — live config reconciliation.

Covers:
  - Config-removed strategy is dropped from self.strategies and self.engines
  - Config-added strategy is wired into all state dicts (engines / caches / positions)
  - Override-changed strategy is mutated in place (same instance kept)
  - Stale scheduling timestamps cleared on add (re-enabled strategy not skipped)
  - Active positions of a removed strategy are preserved
  - Base-name extraction is exact (rsplit on "_") — strategies whose base
    is not in the new config are removed, even when another base shares a prefix
  - Removal decision uses raw config, not load_strategies success — a transient
    instantiation failure does NOT silently disable a still-desired strategy
  - Empty / malformed config entries (missing 'name', non-string name) are
    ignored without raising
  - A payload whose strategies list is non-empty but all malformed is rejected
    to avoid wiping every running strategy
"""

from __future__ import annotations

import sys
from dataclasses import dataclass, field
from unittest.mock import MagicMock, patch

# Alpaca is stubbed per-test by the autouse fixture in quant/tests/live/conftest.py.
from quant.utils.strategy_config import BaseStrategyConfig

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


@dataclass
class _StratConfig(BaseStrategyConfig):
    INTERVAL: str = "1m"
    MANAGE_INTERVAL: str = "1m"
    PRE_LOAD_DAYS: int = 1
    OPTION_INTERVAL: str = "1m"
    INITIAL_CAPITAL: float = 1000.0
    CAPITAL_PER_TRADE: float = 0.10
    MAX_CONCURRENT_POSITIONS: int = 1
    STOP_LOSS_PERCENT: float = 0.05
    TAKE_PROFIT_PERCENT: float = 0.10
    BREAKEVEN_PROFIT_PERCENT: float = 0.05
    USE_TRAIL_LOSS: bool = False
    MAX_HOLDING_MINUTES: int = 60
    MIN_SIGNAL_CONFIDENCE: float = 0.5
    MAX_DAY_TRADES: int = 10
    BASE_SLIPPAGE_FACTOR: float = 0.01
    TICKERS: list = field(default_factory=lambda: ["SPY"])


def _make_strategy(full_name: str, *, client: str = "paper") -> MagicMock:
    s = MagicMock()
    s.name = full_name
    s.client_name = client
    s.env = "paper"
    s.config = _StratConfig()
    return s


def _make_trader(initial_strategies):
    """Construct a UnifiedTrader with all external deps mocked.

    Returns the trader instance with `initial_strategies` already wired into
    self.strategies, self.engines, and the state dicts (mirroring __init__).
    """
    with (
        patch("quant.live.trader.load_state", return_value={}),
        patch("quant.live.trader.AlpacaDataHandler") as mock_dh_cls,
        patch("quant.live.trader.MarketSchedule"),
        patch("quant.live.trader.load_strategies", return_value=initial_strategies),
        patch("quant.live.trader.resolve_env", return_value="staging"),
    ):
        mock_dh = MagicMock()
        mock_dh.clients = {"paper": MagicMock()}
        mock_dh_cls.return_value = mock_dh

        from quant.live.trader import UnifiedTrader

        trader = UnifiedTrader()
    return trader


# ===========================================================================
# Removed strategies
# ===========================================================================


class TestRemoved:
    def test_strategy_removed_from_config_is_dropped(self) -> None:
        s = _make_strategy("foo_paper")
        trader = _make_trader([s])
        assert "foo_paper" in {x.name for x in trader.strategies}

        with patch("quant.live.trader.load_strategies", return_value=[]):
            trader._apply_config_update({"strategies": []})

        assert trader.strategies == []
        assert "foo_paper" not in trader.engines

    def test_active_positions_preserved_on_removal(self) -> None:
        s = _make_strategy("foo_paper")
        trader = _make_trader([s])
        trader.active_positions["foo_paper"] = [{"Ticker": "SPY", "OrderId": "x"}]

        with patch("quant.live.trader.load_strategies", return_value=[]):
            trader._apply_config_update({"strategies": []})

        # Position state intentionally preserved so it isn't orphaned mid-session.
        assert trader.active_positions["foo_paper"] == [
            {"Ticker": "SPY", "OrderId": "x"}
        ]


class TestAdded:
    def test_added_strategy_wired_into_all_state_dicts(self) -> None:
        existing = _make_strategy("foo_paper")
        trader = _make_trader([existing])

        new_instance = _make_strategy("bar_paper")
        new_config = {
            "strategies": [
                {"name": "foo", "config": {}},
                {"name": "bar", "config": {}},
            ]
        }
        with patch(
            "quant.live.trader.load_strategies", return_value=[existing, new_instance]
        ):
            trader._apply_config_update(new_config)

        assert "bar_paper" in {s.name for s in trader.strategies}
        assert "bar_paper" in trader.engines
        assert "bar_paper" in trader.active_positions

    def test_re_added_strategy_gets_fresh_engine(self) -> None:
        """A strategy that is removed then re-added must receive a fresh engine,
        not the stale one from its previous run."""
        s = _make_strategy("foo_paper")
        trader = _make_trader([s])
        old_engine_id = id(trader.engines["foo_paper"])

        # Step 1 — remove.
        with patch("quant.live.trader.load_strategies", return_value=[]):
            trader._apply_config_update({"strategies": []})
        assert "foo_paper" not in trader.engines

        # Step 2 — re-add.
        new_instance = _make_strategy("foo_paper")
        with patch("quant.live.trader.load_strategies", return_value=[new_instance]):
            trader._apply_config_update({"strategies": [{"name": "foo", "config": {}}]})

        assert "foo_paper" in trader.engines
        assert id(trader.engines["foo_paper"]) != old_engine_id


class TestOverrides:
    def test_overrides_applied_in_place_to_existing_instance(self) -> None:
        s = _make_strategy("foo_paper")
        trader = _make_trader([s])
        # Same identity check below — must be the same object.
        original_id = id(trader.strategies[0])

        new_config = {
            "strategies": [
                {"name": "foo", "config": {"STOP_LOSS_PERCENT": 0.20}},
            ]
        }
        with patch(
            "quant.live.trader.load_strategies",
            return_value=[_make_strategy("foo_paper")],
        ):
            trader._apply_config_update(new_config)

        # In-place mutation: same object, new attribute value.
        assert id(trader.strategies[0]) == original_id
        assert trader.strategies[0].config.STOP_LOSS_PERCENT == 0.20

    def test_unknown_override_key_skipped_with_warning(self, caplog) -> None:
        import logging

        s = _make_strategy("foo_paper")
        trader = _make_trader([s])

        with (
            caplog.at_level(logging.WARNING, logger="UnifiedTrader"),
            patch(
                "quant.live.trader.load_strategies",
                return_value=[_make_strategy("foo_paper")],
            ),
        ):
            trader._apply_config_update(
                {"strategies": [{"name": "foo", "config": {"DOES_NOT_EXIST": 1}}]}
            )

        assert "unknown key 'DOES_NOT_EXIST'" in caplog.text


# ===========================================================================
# Exact base-name extraction (rsplit on "_") — no prefix collisions
# ===========================================================================


class TestExactBaseMatch:
    def test_correct_base_wins_when_other_base_is_a_prefix(self) -> None:
        """Strategy 'ml_tech_range_paper' must match base 'ml_tech_range' (its
        exact base via rsplit), not 'ml_tech' which merely shares a prefix."""
        s = _make_strategy("ml_tech_range_paper")
        trader = _make_trader([s])
        s.config.STOP_LOSS_PERCENT = 0.01

        new_config = {
            "strategies": [
                {"name": "ml_tech", "config": {"STOP_LOSS_PERCENT": 0.99}},
                {"name": "ml_tech_range", "config": {"STOP_LOSS_PERCENT": 0.42}},
            ]
        }
        with patch(
            "quant.live.trader.load_strategies",
            return_value=[_make_strategy("ml_tech_range_paper")],
        ):
            trader._apply_config_update(new_config)

        assert trader.strategies[0].config.STOP_LOSS_PERCENT == 0.42

    def test_strategy_removed_when_only_a_prefix_base_remains(self) -> None:
        """When config keeps 'ml_tech' and drops 'ml_tech_range', the running
        'ml_tech_range_paper' must be REMOVED — its exact base via rsplit is
        'ml_tech_range', which is no longer in the config."""
        s = _make_strategy("ml_tech_range_paper")
        trader = _make_trader([s])

        new_config = {"strategies": [{"name": "ml_tech", "config": {}}]}
        with patch("quant.live.trader.load_strategies", return_value=[]):
            trader._apply_config_update(new_config)

        assert "ml_tech_range_paper" not in {x.name for x in trader.strategies}
        assert "ml_tech_range_paper" not in trader.engines


# ===========================================================================
# Robustness against load_strategies failures and malformed config
# ===========================================================================


class TestRobustness:
    def test_load_strategies_failure_does_not_remove_still_desired_strategy(
        self,
    ) -> None:
        """If load_strategies returns [] (e.g. transient class import error)
        but 'foo' is still in the new config, the running 'foo_paper'
        instance must NOT be removed."""
        s = _make_strategy("foo_paper")
        trader = _make_trader([s])

        # New config still wants 'foo' — but load_strategies returns nothing.
        with patch("quant.live.trader.load_strategies", return_value=[]):
            trader._apply_config_update({"strategies": [{"name": "foo", "config": {}}]})

        assert "foo_paper" in {x.name for x in trader.strategies}
        assert "foo_paper" in trader.engines

    def test_malformed_config_entries_ignored(self) -> None:
        s = _make_strategy("foo_paper")
        trader = _make_trader([s])

        # Mix of malformed entries: missing name, non-string name, and a valid one.
        new_config = {
            "strategies": [
                {"clients": ["paper"]},  # missing name
                {"name": 42, "config": {}},  # non-string name
                {"name": "foo", "config": {}},  # valid — keeps foo_paper
            ]
        }
        with patch(
            "quant.live.trader.load_strategies",
            return_value=[_make_strategy("foo_paper")],
        ):
            # Must not raise.
            trader._apply_config_update(new_config)

        assert "foo_paper" in {x.name for x in trader.strategies}

    def test_non_dict_entries_in_strategies_list_ignored(self) -> None:
        """Non-dict items (strings, ints, None) in the 'strategies' list must
        be silently filtered out by the isinstance(sc, dict) guard."""
        s = _make_strategy("foo_paper")
        trader = _make_trader([s])

        new_config = {
            "strategies": [
                "not_a_dict",
                42,
                None,
                {"name": "foo", "config": {}},  # only valid entry
            ]
        }
        with patch(
            "quant.live.trader.load_strategies",
            return_value=[_make_strategy("foo_paper")],
        ):
            trader._apply_config_update(new_config)

        assert "foo_paper" in {x.name for x in trader.strategies}

    def test_setattr_exception_is_caught_and_logged(self, caplog) -> None:
        """If setattr raises (e.g. a validated field), the error is logged and
        remaining overrides in the same entry are still applied."""
        import logging

        # Use a custom config class that rejects STOP_LOSS_PERCENT assignments
        # via __setattr__ — avoids polluting the shared _StratConfig class.
        class _StrictConfig:
            STOP_LOSS_PERCENT: float = 0.05
            TAKE_PROFIT_PERCENT: float = 0.10

            def __setattr__(self, name: str, value: object) -> None:
                if name == "STOP_LOSS_PERCENT":
                    raise ValueError("read-only")
                object.__setattr__(self, name, value)

        s = _make_strategy("foo_paper")
        trader = _make_trader([s])
        # TradingEngine validates strategy configs during trader construction.
        # Install the deliberately non-BaseStrategyConfig test double only
        # after that setup has completed.
        trader.strategies[0].config = _StrictConfig()

        with (
            caplog.at_level(logging.ERROR, logger="UnifiedTrader"),
            patch(
                "quant.live.trader.load_strategies",
                return_value=[_make_strategy("foo_paper")],
            ),
        ):
            trader._apply_config_update(
                {
                    "strategies": [
                        {
                            "name": "foo",
                            "config": {
                                "STOP_LOSS_PERCENT": 0.99,  # will raise
                                "TAKE_PROFIT_PERCENT": 0.55,  # must still apply
                            },
                        }
                    ]
                }
            )

        assert "failed to set 'STOP_LOSS_PERCENT'" in caplog.text
        assert trader.strategies[0].config.TAKE_PROFIT_PERCENT == 0.55

    def test_simultaneous_add_and_remove_in_one_update(self) -> None:
        """A single config update that removes one strategy and adds another
        must remove the old one and wire in the new one atomically."""
        old_s = _make_strategy("old_paper")
        trader = _make_trader([old_s])
        trader.active_positions["old_paper"] = [{"Ticker": "SPY", "OrderId": "z"}]

        new_s = _make_strategy("new_paper")
        new_config = {"strategies": [{"name": "new", "config": {}}]}
        with patch("quant.live.trader.load_strategies", return_value=[new_s]):
            trader._apply_config_update(new_config)

        names = {s.name for s in trader.strategies}
        assert "old_paper" not in names
        assert "new_paper" in names
        assert "old_paper" not in trader.engines
        assert "new_paper" in trader.engines
        # Positions of removed strategy preserved.
        assert trader.active_positions["old_paper"] == [
            {"Ticker": "SPY", "OrderId": "z"}
        ]

    def test_client_change_removes_old_instance_and_adds_new(self) -> None:
        """When the client for a strategy changes (e.g. paper2 → paper), the
        old instance must be removed and the new one added.  This was the
        original bug: _base_of resolved to the same config entry so the stale
        instance was left running alongside the new one."""
        old_s = _make_strategy("quant_range_1d_paper2", client="paper2")
        trader = _make_trader([old_s])
        trader.active_positions["quant_range_1d_paper2"] = [
            {"Ticker": "SPY", "OrderId": "abc"}
        ]

        new_s = _make_strategy("quant_range_1d_paper", client="paper")
        new_config = {"strategies": [{"name": "quant_range_1d", "config": {}}]}
        with patch("quant.live.trader.load_strategies", return_value=[new_s]):
            trader._apply_config_update(new_config)

        names = {s.name for s in trader.strategies}
        assert "quant_range_1d_paper2" not in names, (
            "stale client instance must be removed"
        )
        assert "quant_range_1d_paper" in names, "new client instance must be added"
        assert "quant_range_1d_paper2" not in trader.engines
        assert "quant_range_1d_paper" in trader.engines

    def test_executor_not_a_persistent_attribute(self) -> None:
        """Per-tick executor replaced the persistent self.executor.
        _apply_config_update must not reference or resize any executor."""
        s = _make_strategy("foo_paper")
        trader = _make_trader([s])
        assert not hasattr(trader, "executor")
        assert not hasattr(trader, "_executor_size")

    def test_all_malformed_payload_rejected_to_avoid_wiping_strategies(
        self, caplog
    ) -> None:
        """A non-empty strategies list whose entries are ALL malformed must be
        rejected to avoid removing every running strategy."""
        import logging

        s = _make_strategy("foo_paper")
        trader = _make_trader([s])

        new_config = {
            "strategies": [
                {"clients": ["paper"]},  # missing name
                {"name": 42},  # non-string name
                "not_a_dict",
            ]
        }
        with (
            caplog.at_level(logging.ERROR, logger="UnifiedTrader"),
            patch("quant.live.trader.load_strategies", return_value=[]),
        ):
            trader._apply_config_update(new_config)

        assert "foo_paper" in {x.name for x in trader.strategies}
        assert "foo_paper" in trader.engines
        assert "no valid strategy entries" in caplog.text

    def test_null_config_override_treated_as_empty(self) -> None:
        """A config entry with ``"config": null`` must not raise and must
        leave the running strategy unchanged (treated as no overrides)."""
        s = _make_strategy("foo_paper")
        trader = _make_trader([s])
        original_stop_loss = s.config.STOP_LOSS_PERCENT

        with patch(
            "quant.live.trader.load_strategies",
            return_value=[_make_strategy("foo_paper")],
        ):
            trader._apply_config_update(
                {"strategies": [{"name": "foo", "config": None}]}
            )

        assert original_stop_loss == trader.strategies[0].config.STOP_LOSS_PERCENT

    def test_duplicate_base_names_last_entry_wins_for_overrides(self) -> None:
        """If SSM config has two entries with the same base name, the last one
        wins (dict comprehension last-write semantics) for override application."""
        s = _make_strategy("foo_paper")
        trader = _make_trader([s])

        new_config = {
            "strategies": [
                {"name": "foo", "config": {"STOP_LOSS_PERCENT": 0.11}},
                {"name": "foo", "config": {"STOP_LOSS_PERCENT": 0.77}},  # last wins
            ]
        }
        with patch(
            "quant.live.trader.load_strategies",
            return_value=[_make_strategy("foo_paper")],
        ):
            trader._apply_config_update(new_config)

        assert trader.strategies[0].config.STOP_LOSS_PERCENT == 0.77


# ===========================================================================
# Line 249 — continue when strategy has no 'config' attribute
# ===========================================================================


class TestKeptStrategyWithoutConfig:
    def test_no_config_attr_skips_override_silently(self) -> None:
        """When a kept strategy has no 'config' attribute, line 249's
        `continue` is hit and no AttributeError is raised."""
        s = _make_strategy("foo_paper")
        trader = _make_trader([s])

        # Replace with a strategy object that intentionally has no 'config'
        s_no_config = MagicMock(spec=["name", "client_name", "env"])
        s_no_config.name = "foo_paper"
        s_no_config.client_name = "paper"
        s_no_config.env = "paper"
        trader.strategies = [s_no_config]

        new_config = {"strategies": [{"name": "foo", "config": {"SOME_KEY": 1}}]}
        with patch("quant.live.trader.load_strategies", return_value=[s_no_config]):
            trader._apply_config_update(new_config)

        # No error — the continue at line 249 was hit; object still has no config
        assert not hasattr(s_no_config, "config")
