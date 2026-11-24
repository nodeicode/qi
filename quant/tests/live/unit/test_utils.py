"""
Tests for python/utils.py

No network calls — TradingClient is fully mocked.

Covers:
  - EquityManager.__init__: fetches initial equity from broker
  - EquityManager.get: cached within TTL; re-syncs when TTL elapsed
  - EquityManager.debit: reduces equity, floors at 0.0
  - EquityManager.credit: increases equity
  - EquityManager._sync: handles API failure on initial and re-sync
  - MultiClientEquityManager.__init__: creates one EquityManager per client
  - MultiClientEquityManager.set_active: switches active; unknown key warns
  - MultiClientEquityManager.get / debit / credit: proxy to active manager
  - MultiClientEquityManager.for_client: returns inner EquityManager by name
  - MultiClientEquityManager._active_mgr: raises RuntimeError when no active
  - load_config: missing file, valid JSON, malformed JSON, empty strategies
  - load_state: missing file, valid JSON, malformed JSON
  - save_state: atomic write (no .tmp remains), round-trip, overwrites
  - parse_interval: minutes, hours, days, invalid → 60-min fallback
  - get_occ_symbol: correct OCC format for calls and puts
"""

from __future__ import annotations

# ---------------------------------------------------------------------------
# Stub Alpaca before any import that transitively loads alpaca modules.
# ---------------------------------------------------------------------------
# N.B. alpaca is stubbed per-test by the autouse fixture in tests/live/conftest.py.
# ---------------------------------------------------------------------------
# Imports under test
# ---------------------------------------------------------------------------
import datetime
import json
import logging
import os
import sys
import threading
import time
from pathlib import Path
from typing import cast
from unittest.mock import MagicMock

import pytest

from quant.live.utils import (
    EquityManager,
    MultiClientEquityManager,
    get_occ_symbol,
    load_config,
    load_state,
    parse_interval,
    resolve_config_file,
    save_state,
    setup_logging,
)

_logger = logging.getLogger("test_utils")


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_client(equity: str = "100000.0") -> MagicMock:
    """Return a mock TradingClient whose get_account() yields a fake account."""
    account = MagicMock()
    account.equity = equity
    client = MagicMock()
    client.get_account.return_value = account
    return client


# ===========================================================================
# EquityManager
# ===========================================================================


class TestEquityManager:
    def test_init_fetches_equity(self) -> None:
        client = _make_client("75000.0")
        mgr = EquityManager(client, _logger)

        assert mgr._equity == 75000.0
        client.get_account.assert_called_once()

    def test_get_returns_cached_within_ttl(self) -> None:
        client = _make_client("50000.0")
        mgr = EquityManager(client, _logger)
        mgr._ts = time.monotonic()  # just refreshed
        client.reset_mock()

        result = mgr.get()

        assert result == 50000.0
        client.get_account.assert_not_called()

    def test_get_resyncs_when_ttl_elapsed(self) -> None:
        account = MagicMock()
        account.equity = "60000.0"
        client = MagicMock()
        client.get_account.return_value = account
        mgr = EquityManager(client, _logger)

        # Force TTL expiry then update the mock return value
        mgr._ts = time.monotonic() - EquityManager.SYNC_TTL - 1
        account.equity = "62000.0"
        result = mgr.get()

        assert result == 62000.0
        assert client.get_account.call_count == 2  # initial + re-sync

    def test_debit_reduces_equity(self) -> None:
        mgr = EquityManager(_make_client("10000.0"), _logger)
        mgr.debit(3000.0)

        assert mgr._equity == 7000.0

    def test_debit_floors_at_zero(self) -> None:
        mgr = EquityManager(_make_client("500.0"), _logger)
        mgr.debit(1000.0)

        assert mgr._equity == 0.0

    def test_credit_increases_equity(self) -> None:
        mgr = EquityManager(_make_client("5000.0"), _logger)
        mgr.credit(2500.0)

        assert mgr._equity == 7500.0

    def test_sync_initial_failure_defaults_to_zero(self) -> None:
        client = MagicMock()
        client.get_account.side_effect = Exception("API down")
        mgr = EquityManager(client, _logger)

        assert mgr._equity == 0.0

    def test_sync_resync_failure_keeps_last_known_value(self) -> None:
        client = _make_client("8000.0")
        mgr = EquityManager(client, _logger)
        mgr._ts = time.monotonic() - EquityManager.SYNC_TTL - 1
        client.get_account.side_effect = Exception("timeout")

        result = mgr.get()

        assert result == 8000.0  # last known value preserved

    def test_equity_none_coerced_to_zero(self) -> None:
        """account.equity = None must not raise; defaults to 0.0."""
        account = MagicMock()
        account.equity = None
        client = MagicMock()
        client.get_account.return_value = account
        mgr = EquityManager(client, _logger)

        assert mgr._equity == 0.0

    def test_debit_then_credit_roundtrip(self) -> None:
        mgr = EquityManager(_make_client("10000.0"), _logger)
        mgr.debit(4000.0)
        mgr.credit(4000.0)

        assert mgr._equity == 10000.0

    def test_sync_timer_reset_on_failure(self) -> None:
        """TTL clock is reset even when sync fails to avoid hammering the API."""
        client = MagicMock()
        client.get_account.side_effect = Exception("API down")
        mgr = EquityManager(client, _logger)

        assert mgr._ts > 0


# ===========================================================================
# MultiClientEquityManager
# ===========================================================================


class TestMultiClientEquityManager:
    def _make_multi(
        self, names: list[str] | None = None
    ) -> tuple[MultiClientEquityManager, dict[str, MagicMock]]:
        names = names or ["paper", "prod"]
        clients: dict[str, MagicMock] = {n: _make_client("10000.0") for n in names}
        from alpaca.trading.client import TradingClient

        multi = MultiClientEquityManager(
            cast("dict[str, TradingClient]", clients), _logger
        )
        return multi, clients

    def test_init_creates_manager_per_client(self) -> None:
        multi, _ = self._make_multi(["paper", "prod"])

        assert set(multi._managers.keys()) == {"paper", "prod"}

    def test_init_active_defaults_to_first_key(self) -> None:
        multi, _ = self._make_multi(["paper", "prod"])

        assert multi.active_client in {"paper", "prod"}

    def test_set_active_switches_correctly(self) -> None:
        multi, _ = self._make_multi(["paper", "prod"])
        multi.set_active("prod")
        assert multi.active_client == "prod"

        multi.set_active("paper")
        assert multi.active_client == "paper"

    def test_set_active_unknown_key_keeps_current(self) -> None:
        multi, _ = self._make_multi(["paper", "prod"])
        multi.set_active("paper")
        multi.set_active("nonexistent")  # unknown → no change

        assert multi.active_client == "paper"

    def test_get_proxies_to_active_manager(self) -> None:
        multi, _ = self._make_multi(["paper"])
        multi._managers["paper"]._equity = 42000.0

        assert multi.get() == 42000.0

    def test_debit_proxies_to_active_manager(self) -> None:
        multi, _ = self._make_multi(["paper"])
        multi._managers["paper"]._equity = 10000.0
        multi.debit(1000.0)

        assert multi._managers["paper"]._equity == 9000.0

    def test_credit_proxies_to_active_manager(self) -> None:
        multi, _ = self._make_multi(["paper"])
        multi._managers["paper"]._equity = 10000.0
        multi.credit(5000.0)

        assert multi._managers["paper"]._equity == 15000.0

    def test_for_client_returns_named_manager(self) -> None:
        multi, _ = self._make_multi(["paper", "prod"])

        assert multi.for_client("prod") is multi._managers["prod"]
        assert multi.for_client("paper") is multi._managers["paper"]

    def test_active_mgr_raises_when_no_clients(self) -> None:
        multi = MultiClientEquityManager({}, _logger)
        assert multi.active_client is None

        with pytest.raises(RuntimeError):
            _ = multi._active_mgr

    def test_set_active_then_proxy_uses_new_active(self) -> None:
        multi, _ = self._make_multi(["paper", "prod"])
        multi._managers["paper"]._equity = 1000.0
        multi._managers["prod"]._equity = 9999.0
        multi.set_active("prod")

        assert multi.get() == 9999.0

    def test_single_client_init(self) -> None:
        multi, _ = self._make_multi(["paper"])

        assert len(multi._managers) == 1
        assert multi.active_client == "paper"


# ===========================================================================
# resolve_config_file
# ===========================================================================


class TestResolveConfigFile:
    """Tests for resolve_config_file()."""

    def test_defaults_to_staging_when_marker_absent(self, tmp_path: Path) -> None:
        env_file = tmp_path / ".environment"
        config = tmp_path / "strategies_config.staging.json"
        config.write_text('{"strategies": []}')

        with (
            pytest.MonkeyPatch.context() as mp,
        ):
            mp.setattr("quant.live.utils._PROJECT_ROOT", tmp_path)
            mp.setattr("quant.live.utils._ENV_FILE", env_file)
            result = resolve_config_file("strategies_config", _logger)

        assert result == str(config)

    def test_reads_env_from_marker_file(self, tmp_path: Path) -> None:
        env_file = tmp_path / ".environment"
        env_file.write_text("prod\n")
        config = tmp_path / "strategies_config.prod.json"
        config.write_text('{"strategies": []}')

        with pytest.MonkeyPatch.context() as mp:
            mp.setattr("quant.live.utils._PROJECT_ROOT", tmp_path)
            mp.setattr("quant.live.utils._ENV_FILE", env_file)
            result = resolve_config_file("strategies_config", _logger)

        assert result == str(config)

    def test_env_is_lowercased(self, tmp_path: Path) -> None:
        env_file = tmp_path / ".environment"
        env_file.write_text("PROD")
        config = tmp_path / "strategies_config.prod.json"
        config.write_text("{}")

        with pytest.MonkeyPatch.context() as mp:
            mp.setattr("quant.live.utils._PROJECT_ROOT", tmp_path)
            mp.setattr("quant.live.utils._ENV_FILE", env_file)
            result = resolve_config_file("strategies_config", _logger)

        assert result == str(config)

    def test_raises_on_missing_config_file(self, tmp_path: Path) -> None:
        env_file = tmp_path / ".environment"
        env_file.write_text("staging")

        with pytest.MonkeyPatch.context() as mp:
            mp.setattr("quant.live.utils._PROJECT_ROOT", tmp_path)
            mp.setattr("quant.live.utils._ENV_FILE", env_file)
            with pytest.raises(FileNotFoundError):
                resolve_config_file("strategies_config", _logger)

    def test_raises_on_invalid_environment(self, tmp_path: Path) -> None:
        env_file = tmp_path / ".environment"
        env_file.write_text("../evil")

        with pytest.MonkeyPatch.context() as mp:
            mp.setattr("quant.live.utils._PROJECT_ROOT", tmp_path)
            mp.setattr("quant.live.utils._ENV_FILE", env_file)
            with pytest.raises(ValueError, match="Invalid environment"):
                resolve_config_file("strategies_config", _logger)

    def test_empty_marker_defaults_to_staging(self, tmp_path: Path) -> None:
        env_file = tmp_path / ".environment"
        env_file.write_text("   \n")
        config = tmp_path / "strategies_config.staging.json"
        config.write_text("{}")

        with pytest.MonkeyPatch.context() as mp:
            mp.setattr("quant.live.utils._PROJECT_ROOT", tmp_path)
            mp.setattr("quant.live.utils._ENV_FILE", env_file)
            result = resolve_config_file("strategies_config", _logger)

        assert result == str(config)


# ===========================================================================
# load_config
# ===========================================================================


class TestLoadConfig:
    def test_missing_file_returns_default(self) -> None:
        result = load_config("/nonexistent/path/config.json", _logger)

        assert result == {"strategies": []}

    def test_valid_json_loaded(self, tmp_path: Path) -> None:
        f = tmp_path / "config.json"
        f.write_text(json.dumps({"strategies": [{"name": "TestStrategy"}]}))
        result = load_config(str(f), _logger)

        assert result["strategies"][0]["name"] == "TestStrategy"

    def test_multiple_strategies_loaded(self, tmp_path: Path) -> None:
        f = tmp_path / "config.json"
        data = {"strategies": [{"name": "S1"}, {"name": "S2"}]}
        f.write_text(json.dumps(data))
        result = load_config(str(f), _logger)

        assert len(result["strategies"]) == 2

    def test_malformed_json_returns_default(self, tmp_path: Path) -> None:
        f = tmp_path / "config.json"
        f.write_text("{not valid json")
        result = load_config(str(f), _logger)

        assert result == {"strategies": []}

    def test_empty_strategies_list(self, tmp_path: Path) -> None:
        f = tmp_path / "config.json"
        f.write_text(json.dumps({"strategies": []}))
        result = load_config(str(f), _logger)

        assert result["strategies"] == []


# ===========================================================================
# load_state / save_state
# ===========================================================================


class TestStatePersistence:
    def test_load_state_missing_file_returns_empty_dict(self) -> None:
        result = load_state("/no/such/file.json", _logger)

        assert result == {}

    def test_save_then_load_roundtrip(self, tmp_path: Path) -> None:
        state_file = str(tmp_path / "state.json")
        lock = threading.RLock()
        state: dict = {"positions": [{"Ticker": "SPY", "Qty": 10}]}

        save_state(state_file, state, lock, _logger)
        loaded = load_state(state_file, _logger)

        assert loaded["positions"][0]["Ticker"] == "SPY"
        assert loaded["positions"][0]["Qty"] == 10

    def test_save_state_no_tmp_file_remains(self, tmp_path: Path) -> None:
        state_file = str(tmp_path / "state.json")
        lock = threading.RLock()

        save_state(state_file, {"key": "value"}, lock, _logger)

        assert os.path.exists(state_file)
        assert not os.path.exists(f"{state_file}.tmp")

    def test_load_state_malformed_json_returns_empty_dict(self, tmp_path: Path) -> None:
        f = tmp_path / "state.json"
        f.write_text("{bad json}")
        result = load_state(str(f), _logger)

        assert result == {}

    def test_save_state_overwrites_existing(self, tmp_path: Path) -> None:
        state_file = str(tmp_path / "state.json")
        lock = threading.RLock()

        save_state(state_file, {"v": 1}, lock, _logger)
        save_state(state_file, {"v": 2}, lock, _logger)
        loaded = load_state(state_file, _logger)

        assert loaded["v"] == 2

    def test_save_state_nested_structures(self, tmp_path: Path) -> None:
        state_file = str(tmp_path / "state.json")
        lock = threading.RLock()
        state: dict = {
            "positions": {"SPY": {"Qty": 5, "EntryPrice": 450.0}},
            "day_trade_count": 3,
        }

        save_state(state_file, state, lock, _logger)
        loaded = load_state(state_file, _logger)

        assert loaded["positions"]["SPY"]["EntryPrice"] == 450.0
        assert loaded["day_trade_count"] == 3


# ===========================================================================
# parse_interval
# ===========================================================================


class TestParseInterval:
    @pytest.mark.parametrize(
        "interval,expected",
        [
            ("1m", datetime.timedelta(minutes=1)),
            ("5m", datetime.timedelta(minutes=5)),
            ("30m", datetime.timedelta(minutes=30)),
            ("1h", datetime.timedelta(hours=1)),
            ("4h", datetime.timedelta(hours=4)),
            ("1d", datetime.timedelta(days=1)),
            ("7d", datetime.timedelta(days=7)),
        ],
    )
    def test_valid_intervals(self, interval: str, expected: datetime.timedelta) -> None:
        assert parse_interval(interval) == expected

    @pytest.mark.parametrize("bad", ["", "1x", "abc", "h1", "1W", "1Y"])
    def test_invalid_falls_back_to_60_minutes(self, bad: str) -> None:
        assert parse_interval(bad) == datetime.timedelta(minutes=60)

    def test_large_minute_value(self) -> None:
        assert parse_interval("240m") == datetime.timedelta(minutes=240)


# ===========================================================================
# get_occ_symbol
# ===========================================================================


class TestGetOccSymbol:
    def test_call_atm(self) -> None:
        symbol = get_occ_symbol("SPY", 450.0, "2025-03-21", "call")

        assert symbol == "SPY250321C00450000"

    def test_put_atm(self) -> None:
        symbol = get_occ_symbol("SPY", 440.0, "2025-03-21", "put")

        assert symbol == "SPY250321P00440000"

    def test_fractional_strike_250_decimal(self) -> None:
        # 150.5 → strike_int = round(150.5 * 1000) = 150500
        symbol = get_occ_symbol("AAPL", 150.5, "2025-01-17", "put")

        assert symbol == "AAPL250117P00150500"

    def test_ticker_uppercased(self) -> None:
        symbol = get_occ_symbol("spy", 400.0, "2025-06-20", "call")

        assert symbol.startswith("SPY")

    def test_strike_zero_padded_to_8_digits(self) -> None:
        # 5.0 → 5000 → "00005000"
        symbol = get_occ_symbol("VIX", 5.0, "2025-02-19", "call")

        assert "00005000" in symbol

    def test_large_strike_fits(self) -> None:
        # 4500.0 → 4500000 → "04500000"
        symbol = get_occ_symbol("SPX", 4500.0, "2025-12-31", "call")

        assert "04500000" in symbol

    def test_expiration_date_parsed_correctly(self) -> None:
        # year[2:4]="26", month[5:7]="11", day[8:10]="20"
        symbol = get_occ_symbol("TSLA", 200.0, "2026-11-20", "call")

        assert "261120" in symbol

    def test_call_produces_uppercase_c(self) -> None:
        # Use a ticker without 'C' or 'P' to isolate the type character
        symbol = get_occ_symbol("QQQ", 450.0, "2025-03-21", "call")

        assert "C" in symbol
        assert "P" not in symbol

    def test_put_produces_uppercase_p(self) -> None:
        symbol = get_occ_symbol("SPY", 450.0, "2025-03-21", "put")

        assert "P" in symbol
        assert "C" not in symbol


# ===========================================================================
# setup_logging
# ===========================================================================


class TestSetupLogging:
    """Verify that setup_logging routes logs from every relevant namespace
    to the log file and console handler.

    Covers:
      - quant.strategies.* WARNING / INFO / ERROR → log file
      - quant.core.* WARNING → log file
      - UnifiedTrader INFO → log file
      - quant.strategies propagate=False → exactly one copy per message
      - LOG_LEVEL=WARNING drops INFO from quant.strategies
      - LOG_LEVEL=WARNING keeps WARNING from quant.strategies
    """

    @pytest.fixture(autouse=True)
    def _cleanup_loggers(self) -> None:  # type: ignore[override]
        """Remove all handlers added by setup_logging after each test.

        ``sh`` / ``fh`` are shared between ``UnifiedTrader`` and
        ``quant.strategies``, so we track by ``id`` to avoid closing the
        same handler object twice.
        """
        yield
        seen: set[int] = set()
        for name in ("UnifiedTrader", "quant.core", "quant.strategies"):
            lg = logging.getLogger(name)
            for h in lg.handlers[:]:
                if id(h) not in seen:
                    seen.add(id(h))
                    h.close()
                lg.removeHandler(h)
            lg.propagate = True

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _flush(logger_name: str) -> None:
        """Flush all handlers reachable from *logger_name*."""
        lg = logging.getLogger(logger_name)
        for h in lg.handlers:
            h.flush()

    # ------------------------------------------------------------------
    # quant.strategies.*
    # ------------------------------------------------------------------

    def test_strategy_warning_reaches_log_file(self, tmp_path: Path) -> None:
        log_file = tmp_path / "trader.log"
        setup_logging(log_file=str(log_file))

        logging.getLogger("quant.strategies.quant_range.strategy").warning(
            "strat-warn-msg"
        )
        self._flush("quant.strategies")

        assert "strat-warn-msg" in log_file.read_text()

    def test_strategy_error_reaches_log_file(self, tmp_path: Path) -> None:
        log_file = tmp_path / "trader.log"
        setup_logging(log_file=str(log_file))

        logging.getLogger("quant.strategies.quant_range.strategy").error(
            "strat-err-msg"
        )
        self._flush("quant.strategies")

        assert "strat-err-msg" in log_file.read_text()

    def test_strategy_logger_name_preserved_in_output(self, tmp_path: Path) -> None:
        """The logger name (not a remapped alias) should appear in the line."""
        log_file = tmp_path / "trader.log"
        setup_logging(log_file=str(log_file))

        logging.getLogger("quant.strategies.quant_range.strategy").warning("name-check")
        self._flush("quant.strategies")

        content = log_file.read_text()
        assert "quant.strategies.quant_range.strategy" in content

    # ------------------------------------------------------------------
    # quant.core.*
    # ------------------------------------------------------------------

    def test_engine_warning_reaches_log_file(self, tmp_path: Path) -> None:
        log_file = tmp_path / "trader.log"
        setup_logging(log_file=str(log_file))

        logging.getLogger("quant.core.engine").warning("engine-warn-msg")
        self._flush("quant.core")

        assert "engine-warn-msg" in log_file.read_text()

    # ------------------------------------------------------------------
    # UnifiedTrader
    # ------------------------------------------------------------------

    def test_unified_trader_info_reaches_log_file(self, tmp_path: Path) -> None:
        log_file = tmp_path / "trader.log"
        lgr = setup_logging(log_file=str(log_file))

        lgr.info("trader-info-msg")
        self._flush("UnifiedTrader")

        assert "trader-info-msg" in log_file.read_text()

    # ------------------------------------------------------------------
    # Propagation / deduplication
    # ------------------------------------------------------------------

    def test_strategy_propagate_false_no_double_logging(self, tmp_path: Path) -> None:
        """propagate=False on quant.strategies means each message appears
        exactly once in the file, not twice (once via strat handler, once
        via root)."""
        log_file = tmp_path / "trader.log"
        setup_logging(log_file=str(log_file))

        msg = "unique-no-dup-msg"
        logging.getLogger("quant.strategies.any").warning(msg)
        self._flush("quant.strategies")

        assert log_file.read_text().count(msg) == 1

    def test_strategy_propagate_is_false(self, tmp_path: Path) -> None:
        """After setup_logging the quant.strategies logger has propagate=False."""
        setup_logging(log_file=str(tmp_path / "trader.log"))

        assert logging.getLogger("quant.strategies").propagate is False

    def test_engine_propagate_is_false(self, tmp_path: Path) -> None:
        """After setup_logging the quant.core logger has propagate=False."""
        setup_logging(log_file=str(tmp_path / "trader.log"))

        assert logging.getLogger("quant.core").propagate is False

    # ------------------------------------------------------------------
    # LOG_LEVEL env var
    # ------------------------------------------------------------------

    def test_warning_log_level_drops_info_from_strategies(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("LOG_LEVEL", "WARNING")
        log_file = tmp_path / "trader.log"
        setup_logging(log_file=str(log_file))

        logging.getLogger("quant.strategies.quant_range.strategy").info(
            "should-be-dropped"
        )
        self._flush("quant.strategies")

        assert "should-be-dropped" not in log_file.read_text()

    def test_warning_log_level_keeps_warnings_from_strategies(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("LOG_LEVEL", "WARNING")
        log_file = tmp_path / "trader.log"
        setup_logging(log_file=str(log_file))

        logging.getLogger("quant.strategies.quant_range.strategy").warning(
            "should-appear"
        )
        self._flush("quant.strategies")

        assert "should-appear" in log_file.read_text()
