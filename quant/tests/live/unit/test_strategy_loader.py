"""
Tests for python/strategy_loader.py

No network calls — quant.strategies.get_strategy and AlpacaDataHandler
are fully mocked.

Covers:
  - Empty strategies list → empty result
  - Entry missing 'name' field → warns and skips
  - Invalid 'clients' value (non-list, empty list, dict, string) → warns and skips
  - Unknown strategy class (get_strategy raises) → logs error and skips
  - Client not registered in data_handler.clients → warns and skips
  - Valid paper client → env='paper', name='<strat>_paper', client_name='paper'
  - Valid prod client → env='prod', name='<strat>_prod'
  - Non-prod non-paper client → env='paper' (fallback)
  - Config overrides applied to strategy.config
  - Multiple clients in one entry → multiple independent instances
  - Multiple strategy entries → aggregated into single list
  - Strategy __init__ raises → logs error and skips
"""

from __future__ import annotations

# Alpaca is stubbed per-test by the autouse fixture in quant/tests/live/conftest.py.
# ---------------------------------------------------------------------------
# Imports under test
# ---------------------------------------------------------------------------
import logging
import sys
from unittest.mock import MagicMock, patch

import pytest

from quant.live.strategy_loader import load_strategies

_logger = logging.getLogger("test_strategy_loader")


# ---------------------------------------------------------------------------
# Fakes
# ---------------------------------------------------------------------------


class _FakeConfig:
    INTERVAL = "1h"
    SOME_PARAM = 42
    OTHER_PARAM = "default"


class _FakeStrategy:
    def __init__(self) -> None:
        self.name: str = "FakeStrategy"
        self.env: str = ""
        self.client_name: str = ""
        self.config = _FakeConfig()


def _make_data_handler(client_names: list[str] | None = None) -> MagicMock:
    dh = MagicMock()
    names = client_names or ["paper"]
    dh.clients = {n: MagicMock() for n in names}
    return dh


# ===========================================================================
# load_strategies
# ===========================================================================


class TestLoadStrategies:
    _PATCH = "quant.live.strategy_loader.get_strategy"

    def test_empty_strategies_returns_empty_list(self) -> None:
        result = load_strategies({"strategies": []}, _make_data_handler(), _logger)

        assert result == []

    def test_no_strategies_key_returns_empty_list(self) -> None:
        result = load_strategies({}, _make_data_handler(), _logger)

        assert result == []

    def test_missing_name_skips_entry(self) -> None:
        config = {"strategies": [{"clients": ["paper"]}]}

        with patch(self._PATCH) as mock_get:
            mock_get.return_value = _FakeStrategy
            result = load_strategies(config, _make_data_handler(), _logger)

        assert result == []

    @pytest.mark.parametrize("clients_val", [{}, "paper", []])
    def test_invalid_clients_skips_entry(self, clients_val: object) -> None:
        config = {"strategies": [{"name": "FakeStrategy", "clients": clients_val}]}

        with patch(self._PATCH) as mock_get:
            mock_get.return_value = _FakeStrategy
            result = load_strategies(config, _make_data_handler(), _logger)

        assert result == []

    def test_unknown_strategy_class_skips(self) -> None:
        config = {"strategies": [{"name": "NoSuchStrategy", "clients": ["paper"]}]}

        with patch(self._PATCH) as mock_get:
            mock_get.side_effect = KeyError("NoSuchStrategy")
            result = load_strategies(config, _make_data_handler(), _logger)

        assert result == []

    def test_client_not_in_data_handler_skips(self) -> None:
        config = {"strategies": [{"name": "FakeStrategy", "clients": ["prod"]}]}

        with patch(self._PATCH) as mock_get:
            mock_get.return_value = _FakeStrategy
            result = load_strategies(config, _make_data_handler(["paper"]), _logger)

        assert result == []

    def test_valid_paper_client_sets_correct_attrs(self) -> None:
        config = {"strategies": [{"name": "FakeStrategy", "clients": ["paper"]}]}

        with patch(self._PATCH) as mock_get:
            mock_get.return_value = _FakeStrategy
            result = load_strategies(config, _make_data_handler(["paper"]), _logger)

        assert len(result) == 1
        s = result[0]
        assert s.env == "paper"
        assert s.name == "FakeStrategy_paper"
        assert s.client_name == "paper"

    def test_valid_prod_client_sets_env_prod(self) -> None:
        config = {"strategies": [{"name": "FakeStrategy", "clients": ["prod"]}]}

        with patch(self._PATCH) as mock_get:
            mock_get.return_value = _FakeStrategy
            result = load_strategies(config, _make_data_handler(["prod"]), _logger)

        assert len(result) == 1
        assert result[0].env == "prod"
        assert result[0].name == "FakeStrategy_prod"

    def test_non_prod_non_paper_client_env_is_paper(self) -> None:
        config = {"strategies": [{"name": "FakeStrategy", "clients": ["paper2"]}]}

        with patch(self._PATCH) as mock_get:
            mock_get.return_value = _FakeStrategy
            result = load_strategies(config, _make_data_handler(["paper2"]), _logger)

        assert result[0].env == "paper"

    def test_config_overrides_applied_to_strategy(self) -> None:
        config = {
            "strategies": [
                {
                    "name": "FakeStrategy",
                    "clients": ["paper"],
                    "config": {"SOME_PARAM": 99, "OTHER_PARAM": "overridden"},
                }
            ]
        }

        with patch(self._PATCH) as mock_get:
            mock_get.return_value = _FakeStrategy
            result = load_strategies(config, _make_data_handler(["paper"]), _logger)

        assert result[0].config.SOME_PARAM == 99
        assert result[0].config.OTHER_PARAM == "overridden"

    def test_config_overrides_do_not_affect_other_instances(self) -> None:
        """Each client gets its own instance, so overrides are independent."""
        config = {
            "strategies": [
                {
                    "name": "FakeStrategy",
                    "clients": ["paper", "prod"],
                    "config": {"SOME_PARAM": 77},
                }
            ]
        }

        with patch(self._PATCH) as mock_get:
            mock_get.return_value = _FakeStrategy
            result = load_strategies(
                config, _make_data_handler(["paper", "prod"]), _logger
            )

        assert len(result) == 2
        assert all(s.config.SOME_PARAM == 77 for s in result)

    def test_multiple_clients_produce_multiple_instances(self) -> None:
        config = {
            "strategies": [{"name": "FakeStrategy", "clients": ["paper", "prod"]}]
        }

        with patch(self._PATCH) as mock_get:
            mock_get.return_value = _FakeStrategy
            result = load_strategies(
                config, _make_data_handler(["paper", "prod"]), _logger
            )

        assert len(result) == 2
        names = {s.name for s in result}
        assert names == {"FakeStrategy_paper", "FakeStrategy_prod"}

    def test_multiple_strategy_entries_aggregated(self) -> None:
        config = {
            "strategies": [
                {"name": "FakeStrategy", "clients": ["paper"]},
                {"name": "FakeStrategy", "clients": ["prod"]},
            ]
        }

        with patch(self._PATCH) as mock_get:
            mock_get.return_value = _FakeStrategy
            result = load_strategies(
                config, _make_data_handler(["paper", "prod"]), _logger
            )

        assert len(result) == 2

    def test_strategy_init_exception_skips_that_client(self) -> None:
        class ExplodingStrategy:
            def __init__(self) -> None:
                raise RuntimeError("init failed")

        config = {"strategies": [{"name": "FakeStrategy", "clients": ["paper"]}]}

        with patch(self._PATCH) as mock_get:
            mock_get.return_value = ExplodingStrategy
            result = load_strategies(config, _make_data_handler(["paper"]), _logger)

        assert result == []

    def test_partial_failure_keeps_succeeding_instances(self) -> None:
        """If one client fails init, the other should still be returned."""
        call_count = 0

        class SometimesExploding:
            def __init__(self) -> None:
                nonlocal call_count
                call_count += 1
                if call_count == 1:
                    raise RuntimeError("first init fails")
                self.name: str = "FakeStrategy"
                self.env: str = ""
                self.client_name: str = ""
                self.config = _FakeConfig()

        config = {
            "strategies": [{"name": "FakeStrategy", "clients": ["paper", "prod"]}]
        }

        with patch(self._PATCH) as mock_get:
            mock_get.return_value = SometimesExploding
            result = load_strategies(
                config, _make_data_handler(["paper", "prod"]), _logger
            )

        # Second instance should succeed
        assert len(result) == 1

    def test_default_clients_is_paper_when_missing(self) -> None:
        """When 'clients' key is absent, defaults to ['paper']."""
        config = {"strategies": [{"name": "FakeStrategy"}]}

        with patch(self._PATCH) as mock_get:
            mock_get.return_value = _FakeStrategy
            result = load_strategies(config, _make_data_handler(["paper"]), _logger)

        assert len(result) == 1
        assert result[0].client_name == "paper"

    def test_no_config_overrides_uses_strategy_defaults(self) -> None:
        config = {"strategies": [{"name": "FakeStrategy", "clients": ["paper"]}]}

        with patch(self._PATCH) as mock_get:
            mock_get.return_value = _FakeStrategy
            result = load_strategies(config, _make_data_handler(["paper"]), _logger)

        assert result[0].config.SOME_PARAM == 42  # unchanged default
