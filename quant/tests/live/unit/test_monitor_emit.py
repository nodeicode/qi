"""Unit tests for quant.live.monitor emit + sidecar process_event."""

from __future__ import annotations

import datetime
import json
import logging
import queue
import threading
import time
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

import quant.live.monitor.emit as emit_mod
import quant.live.monitor.sidecar as sidecar_mod
from quant.live.monitor.sidecar import (
    ShadowWorker,
    SidecarState,
    _fetch_orders,
    _is_actionable,
    _order_submitted_at,
    _order_underlying,
    _percentile,
    _setup_logging,
    _trading_clients,
    _underlying_from_symbol,
    compare_manage_exits,
    compare_signals,
    is_expected_delay_gap,
    is_freshness_sampleable,
    match_order_to_pending,
    pd_timestamp_iso,
    process_event,
    reconcile_orders,
    run_sidecar,
    shadow_backtest_signal,
    staleness_excess_ns,
)


@pytest.fixture(autouse=True)
def _reset_emit_globals():
    """Ensure emit module globals do not leak across tests."""
    emit_mod._queue = None
    emit_mod._drops = None
    emit_mod._process = None
    emit_mod._ctx = None
    yield
    emit_mod._queue = None
    emit_mod._drops = None
    emit_mod._process = None
    emit_mod._ctx = None


def test_emit_noop_without_sidecar():
    emit_mod.emit({"kind": "signal_evaluated", "ticker": "SPY"})  # must not raise


def test_emit_never_blocks_on_full_queue():
    import threading

    q: queue.Queue = queue.Queue(maxsize=1)
    lock = threading.Lock()
    drops = SimpleNamespace(value=0, get_lock=lambda: lock)
    emit_mod._queue = q
    emit_mod._drops = drops

    emit_mod.emit({"kind": "a", "n": 1})
    # Second put: queue full → drop oldest then put freshest
    emit_mod.emit({"kind": "b", "n": 2})
    assert q.qsize() == 1
    item = q.get_nowait()
    assert item["kind"] == "b"
    assert drops.value >= 1


def test_compare_signals_drift():
    assert compare_signals("SELL_IC", "HOLD") == "signal_drift"
    assert compare_signals("SELL_IC", "sell_ic") is None


def test_compare_manage_exits_requires_same_class_and_bar():
    bar = "2024-01-02T15:00:00+00:00"
    assert compare_manage_exits("proximity", bar, "proximity", bar) is None
    assert compare_manage_exits("hold", None, "proximity", bar) == "manage_exit_drift"
    assert (
        compare_manage_exits("proximity", bar, "proximity", "2024-01-02T15:15:00+00:00")
        == "manage_exit_drift"
    )


def test_process_event_manage_exit_drift(tmp_path):
    state = SidecarState(str(tmp_path / "m.jsonl"), str(tmp_path / "s.json"))
    shadow = {
        "exit_class": "proximity",
        "exit_bar": "2024-01-02T15:00:00+00:00",
        "exit_idx": 3,
        "manage_bar_ts": "2024-01-02T15:00:00+00:00",
        "shadow_manage_ns": 123,
    }
    with patch("quant.live.monitor.sidecar.shadow_manage_exit", return_value=shadow):
        flags = process_event(
            state,
            {
                "kind": "manage_evaluated",
                "strategy_name": "example_equity_paper",
                "ticker": "SPY",
                "position": {"EntryTime": "2024-01-02T14:00:00+00:00"},
                "end_dt": "2024-01-02T15:20:00+00:00",
                "interval": "15m",
                "manage_interval": "15m",
                "live_exit": "hold",
                "exit_bar": None,
            },
            clients={},
        )
    assert flags == ["manage_exit_drift"]
    records = [
        json.loads(line) for line in (tmp_path / "m.jsonl").read_text().splitlines()
    ]
    assert records[-1]["kind"] == "shadow_manage_result"
    assert records[-1]["shadow_exit"] == "proximity"


def _manage_signal_event(**overrides) -> dict:
    base = {
        "kind": "signal_evaluated",
        "strategy_name": "example_equity_paper1",
        "client_name": "paper1",
        "ticker": "SPY",
        "bar_ts": "2026-10-15T19:15:00+00:00",
        "end_dt": "2026-10-15T19:30:00+00:00",
        "interval": "15m",
        "live_signal": "NEUTRAL",
        "live_reason": "SKIP: not entry bar",
        "live_frame_has_bar": True,
        "active_positions": 1,
        "phase": "signal+manage",
    }
    base.update(overrides)
    return base


def test_manage_telemetry_missing_flagged_when_plane_silent(tmp_path):
    """Open position on a manage phase owes a manage_evaluated event; when
    none have ever arrived, the emitter's manage parity plane is silently
    dead (no shadow_manage_result / manage_exit_drift).  The sidecar must
    flag it instead of staying quiet."""
    state = SidecarState(str(tmp_path / "m.jsonl"), str(tmp_path / "s.json"))
    flags = process_event(state, _manage_signal_event(), clients={}, run_shadow=False)
    assert "manage_telemetry_missing" in flags


def test_manage_telemetry_missing_quiet_after_arrival(tmp_path):
    """Same-tick manage_evaluated arrival keeps the presence guard quiet."""
    state = SidecarState(str(tmp_path / "m.jsonl"), str(tmp_path / "s.json"))
    state.record_manage_eval("example_equity_paper1", "SPY")
    flags = process_event(state, _manage_signal_event(), clients={}, run_shadow=False)
    assert "manage_telemetry_missing" not in flags


def test_manage_telemetry_missing_skipped_without_manage_evidence(tmp_path):
    """No open positions or no manage phase ⇒ no manage_evaluated is owed —
    guard must stay quiet (strategies without a manage phase don't owe events)."""
    state = SidecarState(str(tmp_path / "m.jsonl"), str(tmp_path / "s.json"))
    ev = _manage_signal_event(active_positions=0)
    assert "manage_telemetry_missing" not in process_event(
        state, ev, clients={}, run_shadow=False
    )
    state2 = SidecarState(str(tmp_path / "m2.jsonl"), str(tmp_path / "s2.json"))
    ev2 = _manage_signal_event(phase="signal")
    assert "manage_telemetry_missing" not in process_event(
        state2, ev2, clients={}, run_shadow=False
    )


def test_manage_telemetry_missing_rate_limited(tmp_path):
    """The flag fires once per cooldown window, not on every bar."""
    state = SidecarState(str(tmp_path / "m.jsonl"), str(tmp_path / "s.json"))
    ev = _manage_signal_event()
    assert "manage_telemetry_missing" in process_event(
        state, ev, clients={}, run_shadow=False
    )
    # Second signal event within the cooldown → no repeat alert.
    flags = process_event(state, ev, clients={}, run_shadow=False)
    assert "manage_telemetry_missing" not in flags


def test_process_event_signal_drift(tmp_path):
    metrics = tmp_path / "m.jsonl"
    state_path = tmp_path / "s.json"
    state = SidecarState(str(metrics), str(state_path))

    shadow = {
        "shadow_signal": "HOLD",
        "shadow_confidence": 0.1,
        "shadow_reason": "",
        "bar_ts": "2024-01-02T15:00:00+00:00",
        "entry_time": "2024-01-02T15:15:00+00:00",
        "shadow_prepare_signals_ns": 123,
        "actionable": False,
        "frame_has_bar": True,
    }
    with patch(
        "quant.live.monitor.sidecar.shadow_backtest_signal", return_value=shadow
    ):
        flags = process_event(
            state,
            {
                "kind": "signal_evaluated",
                "strategy_name": "example_equity_paper",
                "client_name": "paper",
                "ticker": "SPY",
                "bar_ts": "2024-01-02T15:00:00+00:00",
                "end_dt": "2024-01-02T15:20:00+00:00",
                "interval": "15m",
                "live_signal": "SELL_IC",
                "live_confidence": 0.9,
                "live_frame_has_bar": True,
                "live_reason": "ok",
                "generate_signals_ns": 42,
                "fetch_data_ns": 10,
                "run_signals_ns": 50,
                "emit_wall_ns": time.time_ns(),
            },
            clients={},
            run_shadow=True,
        )
    assert "signal_drift" in flags
    lines = metrics.read_text().strip().splitlines()
    assert lines
    first = json.loads(lines[0])
    assert first["generate_signals_ns"] == 42
    assert isinstance(first["generate_signals_ns"], int)


def test_signal_without_trade_after_grace(tmp_path):
    metrics = tmp_path / "m.jsonl"
    state_path = tmp_path / "s.json"
    state = SidecarState(str(metrics), str(state_path))
    state.watched_tickers.add("SPY")
    state.client_names.add("paper")
    state.pending["paper|SPY|bar"] = {
        "strategy_name": "example_equity_paper",
        "client_name": "paper",
        "ticker": "SPY",
        "bar_ts": "2024-01-02T15:00:00+00:00",
        "entry_time": (
            datetime.datetime.now(datetime.UTC) - datetime.timedelta(minutes=20)
        ).isoformat(),
        "shadow_signal": "SELL_IC",
        "detect_wall_ns": time.time_ns() - int(20 * 60 * 1e9),
    }
    flags = reconcile_orders(
        state, clients={"paper": MagicMock(get_orders=MagicMock(return_value=[]))}
    )
    assert "signal_without_trade" in flags
    assert "paper|SPY|bar" not in state.pending


def test_signal_without_trade_within_grace_no_flag(tmp_path):
    metrics = tmp_path / "m.jsonl"
    state_path = tmp_path / "s.json"
    state = SidecarState(str(metrics), str(state_path))
    state.pending["paper|SPY|bar"] = {
        "strategy_name": "example_equity_paper",
        "client_name": "paper",
        "ticker": "SPY",
        "bar_ts": "2024-01-02T15:00:00+00:00",
        "entry_time": datetime.datetime.now(datetime.UTC).isoformat(),
        "shadow_signal": "SELL_IC",
        "detect_wall_ns": time.time_ns(),
    }
    flags = reconcile_orders(
        state, clients={"paper": MagicMock(get_orders=MagicMock(return_value=[]))}
    )
    assert "signal_without_trade" not in flags
    assert "paper|SPY|bar" in state.pending


def test_trade_without_signal(tmp_path):
    metrics = tmp_path / "m.jsonl"
    state_path = tmp_path / "s.json"
    state = SidecarState(str(metrics), str(state_path))
    state.watched_tickers.add("SPY")
    state.client_names.add("paper")

    order = SimpleNamespace(
        id="ord-1",
        symbol="SPY",
        status="filled",
        submitted_at=datetime.datetime.now(datetime.UTC),
        legs=None,
    )
    client = MagicMock()
    with patch("quant.live.monitor.sidecar._fetch_orders", return_value=[order]):
        flags = reconcile_orders(state, clients={"paper": client})
    assert "trade_without_signal" in flags


def test_match_order_to_pending():
    now = datetime.datetime.now(datetime.UTC)
    entry = now - datetime.timedelta(minutes=2)
    pending = {
        "k": {
            "ticker": "SPY",
            "entry_time": entry.isoformat(),
        }
    }
    order = SimpleNamespace(
        symbol="SPY",
        status="filled",
        submitted_at=now,
        legs=None,
    )
    assert match_order_to_pending(order, pending, now=now) == "k"


def test_actionable_pending_added_on_shadow(tmp_path):
    metrics = tmp_path / "m.jsonl"
    state_path = tmp_path / "s.json"
    state = SidecarState(str(metrics), str(state_path))
    shadow = {
        "shadow_signal": "SELL_IC",
        "shadow_confidence": 0.8,
        "shadow_reason": "",
        "bar_ts": "2024-01-02T15:00:00+00:00",
        "entry_time": "2024-01-02T15:15:00+00:00",
        "shadow_prepare_signals_ns": 99,
        "actionable": True,
        "frame_has_bar": True,
    }
    with patch(
        "quant.live.monitor.sidecar.shadow_backtest_signal", return_value=shadow
    ):
        flags = process_event(
            state,
            {
                "kind": "signal_evaluated",
                "strategy_name": "example_equity_paper",
                "client_name": "paper",
                "ticker": "SPY",
                "bar_ts": "2024-01-02T15:00:00+00:00",
                "end_dt": "2024-01-02T15:20:00+00:00",
                "interval": "15m",
                "live_signal": "SELL_IC",
                "live_frame_has_bar": True,
                "generate_signals_ns": 1,
            },
            clients={},
        )
    assert flags == []
    assert any(v.get("shadow_signal") == "SELL_IC" for v in state.pending.values())


def test_drops_survive_throttled_rollup(tmp_path, caplog):
    """Drops banked before a throttled maybe_rollup must still appear later."""
    import logging

    import quant.live.monitor.sidecar as sc

    metrics = tmp_path / "m.jsonl"
    state_path = tmp_path / "s.json"
    state = SidecarState(str(metrics), str(state_path))
    state._last_rollup = time.monotonic()  # just rolled up → throttle active
    state.accumulate_drops(3)
    state.accumulate_drops(2)
    with caplog.at_level(logging.ERROR, logger="monitor.sidecar"):
        state.maybe_rollup()  # throttled — must not clear bank
    assert state._drops_since_rollup == 5
    assert "queue drops" not in caplog.text

    state._last_rollup = time.monotonic() - (sc._ROLLUP_SECS + 1)
    with caplog.at_level(logging.ERROR, logger="monitor.sidecar"):
        state.maybe_rollup()
    assert state._drops_since_rollup == 0
    assert "queue drops=5" in caplog.text


# ── emit helpers / lifecycle ───────────────────────────────────────────────


def test_ns_wall_ns_enabled():
    assert isinstance(emit_mod.ns(), int)
    assert isinstance(emit_mod.wall_ns(), int)
    assert emit_mod.enabled() is False
    emit_mod._queue = queue.Queue()
    assert emit_mod.enabled() is True


def test_start_sidecar_idempotent_and_stop(tmp_path):
    metrics = str(tmp_path / "m.jsonl")
    state_path = str(tmp_path / "s.json")

    fake_proc = MagicMock()
    fake_proc.is_alive.return_value = True
    fake_proc.pid = 4242

    fake_ctx = MagicMock()
    fake_q = MagicMock()
    fake_drops = MagicMock()
    fake_ctx.Queue.return_value = fake_q
    fake_ctx.Value.return_value = fake_drops
    fake_ctx.Process.return_value = fake_proc

    with patch("multiprocessing.get_context", return_value=fake_ctx):
        emit_mod.start_sidecar(
            metrics_path=metrics, state_path=state_path, queue_size=16
        )
        assert emit_mod._process is fake_proc
        fake_proc.start.assert_called_once()
        # Idempotent while alive
        emit_mod.start_sidecar(metrics_path=metrics, state_path=state_path)
        assert fake_proc.start.call_count == 1

    emit_mod.stop_sidecar(timeout=0.01)
    fake_q.put_nowait.assert_called_with({"kind": "shutdown"})
    fake_proc.join.assert_called()
    assert emit_mod._queue is None
    assert emit_mod._process is None


def test_stop_sidecar_terminates_if_still_alive():
    proc = MagicMock()
    proc.is_alive.side_effect = [True, True]  # after join still alive
    q = MagicMock()
    emit_mod._queue = q
    emit_mod._process = proc
    emit_mod._drops = MagicMock()
    emit_mod._ctx = MagicMock()

    emit_mod.stop_sidecar(timeout=0.01)
    proc.terminate.assert_called_once()


def test_emit_records_drop_when_both_puts_fail():
    q = MagicMock()
    q.put_nowait.side_effect = [Exception("full"), Exception("still full")]
    q.get_nowait.side_effect = Exception("empty")
    lock = MagicMock()
    lock.__enter__ = MagicMock(return_value=None)
    lock.__exit__ = MagicMock(return_value=False)
    drops = SimpleNamespace(value=0, get_lock=lambda: lock)
    emit_mod._queue = q
    emit_mod._drops = drops

    emit_mod.emit({"kind": "x"})
    assert drops.value == 1


def test_emit_drop_oldest_then_put_increments_drops():
    q = MagicMock()
    q.put_nowait.side_effect = [Exception("full"), None]
    q.get_nowait.return_value = {"kind": "old"}
    lock = MagicMock()
    lock.__enter__ = MagicMock(return_value=None)
    lock.__exit__ = MagicMock(return_value=False)
    drops = SimpleNamespace(value=0, get_lock=lambda: lock)
    emit_mod._queue = q
    emit_mod._drops = drops

    emit_mod.emit({"kind": "fresh"})
    assert drops.value == 1


# ── sidecar helpers ────────────────────────────────────────────────────────


def test_percentile_empty_and_values():
    assert _percentile([], 50) is None
    assert _percentile([10, 20, 30], 50) == 20
    assert _percentile([1], 99) == 1


def test_pd_timestamp_iso():
    assert "2024" in pd_timestamp_iso("2024-01-02T15:00:00Z")


def test_underlying_from_symbol():
    assert _underlying_from_symbol("") == ""
    assert _underlying_from_symbol("spy") == "SPY"
    assert _underlying_from_symbol("SPY240119C00450000") == "SPY"


def test_order_underlying_from_legs():
    order = SimpleNamespace(
        symbol="", legs=[SimpleNamespace(symbol="SPY240119P00400000")]
    )
    assert _order_underlying(order) == "SPY"
    order_dict_legs = SimpleNamespace(
        symbol=None, legs=[{"symbol": "QQQ240119C00400000"}]
    )
    assert _order_underlying(order_dict_legs) == "QQQ"
    assert _order_underlying(SimpleNamespace(symbol="", legs=[])) == ""


def test_order_submitted_at_variants():
    assert (
        _order_submitted_at(SimpleNamespace(submitted_at=None, created_at=None)) is None
    )
    naive = datetime.datetime(2024, 1, 2, 15, 0, 0)
    got = _order_submitted_at(SimpleNamespace(submitted_at=naive, created_at=None))
    assert got is not None and got.tzinfo is not None
    aware = datetime.datetime(2024, 1, 2, 15, 0, 0, tzinfo=datetime.UTC)
    assert _order_submitted_at(SimpleNamespace(submitted_at=aware)) == aware
    from_str = _order_submitted_at(
        SimpleNamespace(submitted_at="2024-01-02T15:00:00", created_at=None)
    )
    assert from_str is not None and from_str.tzinfo is not None


def test_is_actionable():
    assert _is_actionable("SELL_IC") is True
    assert _is_actionable("HOLD") is False


def test_match_order_edge_cases():
    now = datetime.datetime.now(datetime.UTC)
    pending = {
        "k": {
            "ticker": "SPY",
            "entry_time": (now - datetime.timedelta(minutes=1)).isoformat(),
        }
    }
    assert (
        match_order_to_pending(
            SimpleNamespace(symbol="", status="filled", submitted_at=now, legs=None),
            pending,
            now=now,
        )
        is None
    )
    assert (
        match_order_to_pending(
            SimpleNamespace(
                symbol="SPY", status="canceled", submitted_at=now, legs=None
            ),
            pending,
            now=now,
        )
        is None
    )
    assert (
        match_order_to_pending(
            SimpleNamespace(symbol="QQQ", status="filled", submitted_at=now, legs=None),
            pending,
            now=now,
        )
        is None
    )
    assert (
        match_order_to_pending(
            SimpleNamespace(symbol="SPY", status="filled", submitted_at=now, legs=None),
            {"k": {"ticker": "SPY"}},
            now=now,
        )
        is None
    )
    # Naive entry_time still matches
    naive_entry = (now - datetime.timedelta(minutes=1)).replace(tzinfo=None)
    assert (
        match_order_to_pending(
            SimpleNamespace(symbol="SPY", status="filled", submitted_at=now, legs=None),
            {"k": {"ticker": "SPY", "entry_time": naive_entry.isoformat()}},
            now=now,
        )
        == "k"
    )


def test_sidecar_state_load_save_roundtrip(tmp_path):
    metrics = tmp_path / "m.jsonl"
    state_path = tmp_path / "s.json"
    state_path.write_text(
        json.dumps(
            {
                "pending": {"a": {"ticker": "SPY"}},
                "matched_order_ids": ["o1"],
                "watched_tickers": ["SPY"],
                "client_names": ["paper"],
            }
        )
    )
    state = SidecarState(str(metrics), str(state_path))
    assert state.pending["a"]["ticker"] == "SPY"
    assert "o1" in state.matched_order_ids
    assert "SPY" in state.watched_tickers

    # Corrupt file → warning, empty pending
    bad = tmp_path / "bad.json"
    bad.write_text("{not-json")
    state2 = SidecarState(str(metrics), str(bad))
    assert state2.pending == {}


def test_save_state_failure_logs(tmp_path, caplog):
    metrics = tmp_path / "m.jsonl"
    state_path = tmp_path / "s.json"
    state = SidecarState(str(metrics), str(state_path))
    with (
        patch("builtins.open", side_effect=OSError("disk full")),
        caplog.at_level(logging.WARNING, logger="monitor.sidecar"),
    ):
        state.save_state()
    assert "failed to save state" in caplog.text


def test_setup_logging_idempotent():
    handlers_before = list(sidecar_mod.logger.handlers)
    propagate_before = sidecar_mod.logger.propagate
    try:
        sidecar_mod.logger.handlers.clear()
        _setup_logging()
        n = len(sidecar_mod.logger.handlers)
        _setup_logging()
        assert len(sidecar_mod.logger.handlers) == n
    finally:
        sidecar_mod.logger.handlers.clear()
        sidecar_mod.logger.handlers.extend(handlers_before)
        sidecar_mod.logger.propagate = propagate_before


def test_trading_clients_and_fetch_orders():
    with patch(
        "quant.data.data_handler.AlpacaDataHandler",
        return_value=SimpleNamespace(clients={"paper": object()}),
    ):
        assert "paper" in _trading_clients()
    with patch(
        "quant.data.data_handler.AlpacaDataHandler",
        side_effect=RuntimeError("no keys"),
    ):
        assert _trading_clients() == {}

    client = MagicMock()
    client.get_orders.return_value = [SimpleNamespace(id="1")]
    with (
        patch.dict(
            "sys.modules",
            {
                "alpaca.trading.enums": MagicMock(
                    QueryOrderStatus=MagicMock(ALL="all")
                ),
                "alpaca.trading.requests": MagicMock(GetOrdersRequest=MagicMock),
            },
        ),
    ):
        # Import path uses real alpaca if installed — just call through mock client
        orders = _fetch_orders(client, datetime.datetime.now(datetime.UTC))
    assert len(orders) == 1

    client.get_orders.side_effect = RuntimeError("boom")
    assert _fetch_orders(client, datetime.datetime.now(datetime.UTC)) == []


def test_shadow_backtest_signal_mocked():
    fake_cfg = SimpleNamespace(PRE_LOAD_DAYS=3)
    fake_strategy = SimpleNamespace(config=fake_cfg)
    fake_cls = MagicMock(return_value=fake_strategy)
    row = {"Signal": "SELL_IC", "Confidence": 0.7, "Reason": "ok"}
    bt = MagicMock()
    bt.prepare_signals.return_value = MagicMock()

    with (
        patch("quant.strategies.get_strategy", return_value=fake_cls),
        patch("quant.backtest.backtester.Backtester", return_value=bt) as BT,
        patch(
            "quant.core.monitor_utils.closed_bar_asof",
            return_value=__import__("pandas").Timestamp("2024-01-02T15:00:00Z"),
        ),
        patch(
            "quant.core.monitor_utils.select_signal_row",
            return_value=("SELL_IC", 0.7, row),
        ),
        patch(
            "quant.utils.time.calculate_entry_time",
            return_value=__import__("pandas").Timestamp("2024-01-02T15:15:00Z"),
        ),
    ):
        out = shadow_backtest_signal(
            strategy_name="example_equity_paper",
            ticker="SPY",
            end_dt="2024-01-02T15:20:00+00:00",
            interval="15m",
            bar_ts="2024-01-02T15:00:00+00:00",
            config_snapshot={"INTERVAL": "15m"},
        )
    assert out["shadow_signal"] == "SELL_IC"
    assert out["actionable"] is True
    assert out["shadow_confidence"] == 0.7
    assert out["frame_has_bar"] is True
    # start = calendar day of end (PRE_LOAD applied once inside prepare_signals);
    # end keeps the live intraday cutoff (not date-only midnight).
    # No DATA_DELAY re-subtract: end arg is the live end_dt verbatim.
    _args, _kwargs = BT.call_args
    assert _args[2] == "2024-01-02"
    assert "15:20" in _args[3]
    assert "T" in _args[3]
    bt.prepare_signals.assert_called_once()


def test_shadow_backtest_signal_rejects_date_only_end_regression():
    """Date-only end → T00:00:00 drops today's bars → false HOLD drift."""
    fake_cfg = SimpleNamespace(PRE_LOAD_DAYS=800)
    fake_strategy = SimpleNamespace(config=fake_cfg)
    fake_cls = MagicMock(return_value=fake_strategy)
    bt = MagicMock()
    bt.prepare_signals.return_value = MagicMock()

    with (
        patch("quant.strategies.get_strategy", return_value=fake_cls),
        patch("quant.backtest.backtester.Backtester", return_value=bt) as BT,
        patch(
            "quant.core.monitor_utils.closed_bar_asof",
            return_value=__import__("pandas").Timestamp("2026-07-31T13:45:00Z"),
        ),
        patch(
            "quant.core.monitor_utils.select_signal_row",
            return_value=(
                "NEUTRAL",
                0.0,
                {"Reason": "SKIP: not entry bar"},
            ),
        ),
        patch(
            "quant.utils.time.calculate_entry_time",
            return_value=__import__("pandas").Timestamp("2026-07-31T14:00:00Z"),
        ),
    ):
        shadow_backtest_signal(
            strategy_name="example_equity_paper1",
            ticker="SPY",
            end_dt="2026-07-31T14:00:00+00:00",
            interval="15m",
            bar_ts="2026-07-31T13:45:00+00:00",
            config_snapshot={"PRE_LOAD_DAYS": 800, "INTERVAL": "15m"},
        )
    start, end_s = BT.call_args[0][2], BT.call_args[0][3]
    assert start == "2026-07-31"
    # Must not be date-only (would parse as midnight and exclude the 09:45 ET bar).
    assert end_s != "2026-07-31"
    assert "2026-07-31T14:00:00" in end_s.replace(" ", "T")


def test_shadow_does_not_reapply_data_delay():
    """Live end_dt is already now-DATA_DELAY; Backtester end must equal it."""
    fake_cfg = SimpleNamespace(PRE_LOAD_DAYS=800, DATA_DELAY=15)
    fake_strategy = SimpleNamespace(config=fake_cfg)
    fake_cls = MagicMock(return_value=fake_strategy)
    bt = MagicMock()
    bt.prepare_signals.return_value = MagicMock()
    live_end = "2026-07-30T14:00:00+00:00"  # wall ~10:15 with delay 15

    with (
        patch("quant.strategies.get_strategy", return_value=fake_cls),
        patch("quant.backtest.backtester.Backtester", return_value=bt) as BT,
        patch(
            "quant.core.monitor_utils.closed_bar_asof",
            return_value=__import__("pandas").Timestamp("2026-07-30T13:45:00Z"),
        ),
        patch(
            "quant.core.monitor_utils.select_signal_row",
            return_value=("SELL_IC", 0.65, {"Reason": "ok"}),
        ),
        patch(
            "quant.utils.time.calculate_entry_time",
            return_value=__import__("pandas").Timestamp("2026-07-30T14:00:00Z"),
        ),
    ):
        shadow_backtest_signal(
            strategy_name="example_equity_paper",
            ticker="SPY",
            end_dt=live_end,
            interval="15m",
            bar_ts="2026-07-30T13:45:00+00:00",
            config_snapshot={"DATA_DELAY": 15, "INTERVAL": "15m"},
        )
    end_s = BT.call_args[0][3]
    assert "14:00:00" in end_s.replace(" ", "T")
    # Must not have subtracted another 15m (that would be 13:45 as end).
    assert "13:45" not in end_s.split("T")[-1] if "T" in end_s else True


def test_reconcile_matches_pending_order(tmp_path):
    metrics = tmp_path / "m.jsonl"
    state_path = tmp_path / "s.json"
    state = SidecarState(str(metrics), str(state_path))
    state.client_names.add("paper")
    state.watched_tickers.add("SPY")
    now = datetime.datetime.now(datetime.UTC)
    state.pending["paper|SPY|bar"] = {
        "strategy_name": "example_equity_paper",
        "client_name": "paper",
        "ticker": "SPY",
        "bar_ts": "bar",
        "entry_time": (now - datetime.timedelta(minutes=1)).isoformat(),
        "shadow_signal": "SELL_IC",
        "detect_wall_ns": time.time_ns(),
    }
    order = SimpleNamespace(
        id="ord-match",
        symbol="SPY",
        status="filled",
        submitted_at=now,
        legs=None,
    )
    with patch("quant.live.monitor.sidecar._fetch_orders", return_value=[order]):
        flags = reconcile_orders(state, clients={"paper": MagicMock()})
    assert "signal_without_trade" not in flags
    assert "paper|SPY|bar" not in state.pending
    assert "ord-match" in state.matched_order_ids


def test_reconcile_age_via_entry_time_fallback(tmp_path):
    metrics = tmp_path / "m.jsonl"
    state_path = tmp_path / "s.json"
    state = SidecarState(str(metrics), str(state_path))
    state.client_names.add("paper")
    state.pending["paper|SPY|bar"] = {
        "strategy_name": "example_equity_paper",
        "client_name": "paper",
        "ticker": "SPY",
        "bar_ts": "bar",
        "entry_time": (
            datetime.datetime.now(datetime.UTC) - datetime.timedelta(minutes=30)
        ).isoformat(),
        "shadow_signal": "SELL_IC",
        # no detect_wall_ns → entry_time path
    }
    flags = reconcile_orders(
        state, clients={"paper": MagicMock(get_orders=MagicMock(return_value=[]))}
    )
    assert "signal_without_trade" in flags


def test_process_event_heartbeat_and_unknown_kind(tmp_path):
    metrics = tmp_path / "m.jsonl"
    state_path = tmp_path / "s.json"
    state = SidecarState(str(metrics), str(state_path))
    with patch(
        "quant.live.monitor.sidecar.reconcile_orders", return_value=["x"]
    ) as rec:
        flags = process_event(
            state, {"kind": "heartbeat"}, clients={}, run_shadow=False
        )
    rec.assert_called_once()
    assert flags == ["x"]

    flags2 = process_event(state, {"kind": "other"}, clients={}, run_shadow=False)
    assert flags2 == []


def test_process_event_shadow_exception(tmp_path, caplog):
    metrics = tmp_path / "m.jsonl"
    state_path = tmp_path / "s.json"
    state = SidecarState(str(metrics), str(state_path))
    sidecar_mod.logger.propagate = True
    with (
        patch(
            "quant.live.monitor.sidecar.shadow_backtest_signal",
            side_effect=ValueError("Strategy DummyStrategy not found."),
        ),
        caplog.at_level(logging.ERROR, logger="monitor.sidecar"),
    ):
        flags = process_event(
            state,
            {
                "kind": "signal_evaluated",
                "strategy_name": "DummyStrategy_paper",
                "ticker": "SPY",
                "end_dt": "2024-01-02T15:20:00+00:00",
                "live_signal": "HOLD",
                "live_frame_has_bar": True,
            },
            clients={},
        )
    assert "shadow_failed" in flags
    assert "shadow_backtest_signal failed" in caplog.text


def test_process_event_shadow_bar_missing(tmp_path, caplog):
    """Dual HOLD with missing shadow bar must hard-fault (Jul 30 class)."""
    metrics = tmp_path / "m.jsonl"
    state_path = tmp_path / "s.json"
    state = SidecarState(str(metrics), str(state_path))
    shadow = {
        "shadow_signal": "HOLD",
        "shadow_confidence": 0.0,
        "shadow_reason": "",
        "bar_ts": "2026-07-30T13:45:00+00:00",
        "entry_time": "2026-07-30T14:00:00+00:00",
        "shadow_prepare_signals_ns": 10,
        "actionable": False,
        "frame_has_bar": False,
    }
    sidecar_mod.logger.propagate = True
    with (
        patch("quant.live.monitor.sidecar.shadow_backtest_signal", return_value=shadow),
        caplog.at_level(logging.ERROR, logger="monitor.sidecar"),
    ):
        flags = process_event(
            state,
            {
                "kind": "signal_evaluated",
                "strategy_name": "example_equity_paper1",
                "client_name": "paper1",
                "ticker": "SPY",
                "bar_ts": "2026-07-30T13:45:00+00:00",
                "end_dt": "2026-07-30T14:00:00+00:00",
                "interval": "15m",
                "live_signal": "HOLD",
                "live_confidence": 0.0,
                "live_frame_has_bar": True,
            },
            clients={},
        )
    assert "shadow_bar_missing" in flags
    assert "signal_drift" not in flags
    assert state.pending == {}
    assert "shadow_bar_missing" in caplog.text


def test_process_event_live_bar_missing(tmp_path, caplog):
    metrics = tmp_path / "m.jsonl"
    state_path = tmp_path / "s.json"
    state = SidecarState(str(metrics), str(state_path))
    shadow = {
        "shadow_signal": "SELL_IC",
        "shadow_confidence": 0.65,
        "shadow_reason": "ok",
        "bar_ts": "2026-07-30T13:45:00+00:00",
        "entry_time": "2026-07-30T14:00:00+00:00",
        "shadow_prepare_signals_ns": 10,
        "actionable": True,
        "frame_has_bar": True,
    }
    sidecar_mod.logger.propagate = True
    with (
        patch("quant.live.monitor.sidecar.shadow_backtest_signal", return_value=shadow),
        caplog.at_level(logging.ERROR, logger="monitor.sidecar"),
    ):
        flags = process_event(
            state,
            {
                "kind": "signal_evaluated",
                "strategy_name": "example_equity_paper1",
                "client_name": "paper1",
                "ticker": "SPY",
                "bar_ts": "2026-07-30T13:45:00+00:00",
                "end_dt": "2026-07-30T14:00:00+00:00",
                "interval": "15m",
                "live_signal": "HOLD",
                "live_confidence": 0.0,
                "live_frame_has_bar": False,
            },
            clients={},
        )
    assert "live_bar_missing" in flags
    # Drift compare only when both frames have the bar.
    assert "signal_drift" not in flags
    assert "live_bar_missing" in caplog.text
    # Shadow still actionable with bar → pending expected.
    assert any(v.get("shadow_signal") == "SELL_IC" for v in state.pending.values())


def test_process_event_dual_hold_both_missing(tmp_path):
    """Both sides missing bar → both miss flags, no pending, no green drift skip."""
    metrics = tmp_path / "m.jsonl"
    state_path = tmp_path / "s.json"
    state = SidecarState(str(metrics), str(state_path))
    shadow = {
        "shadow_signal": "HOLD",
        "shadow_confidence": 0.0,
        "shadow_reason": "",
        "bar_ts": "2026-07-30T13:45:00+00:00",
        "entry_time": "2026-07-30T14:00:00+00:00",
        "shadow_prepare_signals_ns": 1,
        "actionable": False,
        "frame_has_bar": False,
    }
    with patch(
        "quant.live.monitor.sidecar.shadow_backtest_signal", return_value=shadow
    ):
        flags = process_event(
            state,
            {
                "kind": "signal_evaluated",
                "strategy_name": "example_equity_paper1",
                "client_name": "paper1",
                "ticker": "SPY",
                "bar_ts": "2026-07-30T13:45:00+00:00",
                "end_dt": "2026-07-30T14:00:00+00:00",
                "interval": "15m",
                "live_signal": "HOLD",
                "live_frame_has_bar": False,
            },
            clients={},
        )
    assert "live_bar_missing" in flags
    assert "shadow_bar_missing" in flags
    assert "signal_drift" not in flags
    assert state.pending == {}


def test_actionable_without_frame_has_bar_skips_pending(tmp_path):
    metrics = tmp_path / "m.jsonl"
    state_path = tmp_path / "s.json"
    state = SidecarState(str(metrics), str(state_path))
    shadow = {
        "shadow_signal": "SELL_IC",
        "shadow_confidence": 0.8,
        "shadow_reason": "",
        "bar_ts": "2024-01-02T15:00:00+00:00",
        "entry_time": "2024-01-02T15:15:00+00:00",
        "shadow_prepare_signals_ns": 99,
        "actionable": True,
        "frame_has_bar": False,  # inconsistent but must not enqueue pending
    }
    with patch(
        "quant.live.monitor.sidecar.shadow_backtest_signal", return_value=shadow
    ):
        process_event(
            state,
            {
                "kind": "signal_evaluated",
                "strategy_name": "example_equity_paper",
                "client_name": "paper",
                "ticker": "SPY",
                "bar_ts": "2024-01-02T15:00:00+00:00",
                "end_dt": "2024-01-02T15:20:00+00:00",
                "interval": "15m",
                "live_signal": "HOLD",
                "live_frame_has_bar": True,
            },
            clients={},
        )
    assert state.pending == {}


def test_process_event_metrics_append_failure(tmp_path):
    metrics = tmp_path / "m.jsonl"
    state_path = tmp_path / "s.json"
    state = SidecarState(str(metrics), str(state_path))
    with patch.object(state, "append_metrics", side_effect=OSError("ro fs")):
        flags = process_event(
            state,
            {"kind": "signal_evaluated", "ticker": "SPY", "live_signal": "HOLD"},
            clients={},
            run_shadow=False,
        )
    assert flags == []
    assert "SPY" in state.watched_tickers


def test_run_sidecar_drains_until_shutdown(tmp_path):
    metrics = str(tmp_path / "m.jsonl")
    state_path = str(tmp_path / "s.json")
    events = [
        {"kind": "signal_evaluated", "ticker": "SPY", "live_signal": "HOLD"},
        "not-a-dict",
        {"kind": "shutdown"},
    ]
    q = MagicMock()
    q.get.side_effect = events
    drops = MagicMock()
    lock = MagicMock()
    lock.__enter__ = MagicMock(return_value=None)
    lock.__exit__ = MagicMock(return_value=False)
    drops.get_lock.return_value = lock
    drops.value = 2

    with (
        patch("quant.live.monitor.sidecar._trading_clients", return_value={}),
        patch("quant.live.monitor.sidecar.process_event", return_value=[]) as pe,
        patch("quant.live.monitor.sidecar._setup_logging"),
    ):
        run_sidecar(q, drops, metrics_path=metrics, state_path=state_path)

    assert pe.call_count == 1
    assert (tmp_path / "m.jsonl").exists()


def test_run_sidecar_queue_get_exception_exits(tmp_path):
    q = MagicMock()
    q.get.side_effect = RuntimeError("closed")
    with (
        patch("quant.live.monitor.sidecar._trading_clients", return_value={}),
        patch("quant.live.monitor.sidecar._setup_logging"),
    ):
        run_sidecar(
            q,
            None,
            metrics_path=str(tmp_path / "m.jsonl"),
            state_path=str(tmp_path / "s.json"),
        )


def test_rollup_emits_percentile_parts(tmp_path, caplog):
    metrics = tmp_path / "m.jsonl"
    state_path = tmp_path / "s.json"
    state = SidecarState(str(metrics), str(state_path))
    state.record_spans(
        {"generate_signals_ns": 100, "fetch_data_ns": 50, "run_signals_ns": 75},
        queue_lag_ns=10,
    )
    state._last_rollup = time.monotonic() - (sidecar_mod._ROLLUP_SECS + 1)
    sidecar_mod.logger.propagate = True
    with caplog.at_level(logging.INFO, logger="monitor.sidecar"):
        state.maybe_rollup()
    assert "generate_signals_ns_p50=" in caplog.text


def test_reconcile_skips_mismatched_and_dead_orders(tmp_path):
    metrics = tmp_path / "m.jsonl"
    state_path = tmp_path / "s.json"
    state = SidecarState(str(metrics), str(state_path))
    state.client_names.update({"paper", "live"})
    state.watched_tickers.add("SPY")
    now = datetime.datetime.now(datetime.UTC)
    state.pending["paper|SPY|bar"] = {
        "strategy_name": "example_equity_paper",
        "client_name": "paper",
        "ticker": "SPY",
        "entry_time": (now - datetime.timedelta(minutes=1)).isoformat(),
        "detect_wall_ns": time.time_ns(),
    }
    state.matched_order_ids.add("already")

    paper_client = MagicMock()
    live_client = MagicMock()
    clients = {"paper": paper_client, "live": live_client}

    live_orders = [
        SimpleNamespace(
            id="o-live",
            symbol="SPY",
            status="filled",
            submitted_at=now,
            legs=None,
        ),
    ]
    paper_orders = [
        SimpleNamespace(
            id="already",
            symbol="SPY",
            status="filled",
            submitted_at=now,
            legs=None,
        ),
        SimpleNamespace(
            id="dead",
            symbol="SPY",
            status="rejected",
            submitted_at=now,
            legs=None,
        ),
        SimpleNamespace(
            id="no-sym",
            symbol="",
            status="filled",
            submitted_at=now,
            legs=None,
        ),
        SimpleNamespace(
            id="qqq",
            symbol="QQQ",
            status="filled",
            submitted_at=now,
            legs=None,
        ),
        SimpleNamespace(
            id="old",
            symbol="SPY",
            status="filled",
            submitted_at=now - datetime.timedelta(hours=5),
            legs=None,
        ),
        SimpleNamespace(
            id="no-ts",
            symbol="SPY",
            status="filled",
            submitted_at=None,
            created_at=None,
            legs=None,
        ),
    ]

    def _fetch(client, after):
        if client is live_client:
            return live_orders
        if client is paper_client:
            return paper_orders
        return []

    with patch("quant.live.monitor.sidecar._fetch_orders", side_effect=_fetch):
        flags = reconcile_orders(state, clients=clients)
    # pending stays (live order skipped due to client_name; no paper match)
    assert "paper|SPY|bar" in state.pending
    assert "trade_without_signal" not in flags


def test_reconcile_bad_entry_time_age(tmp_path):
    metrics = tmp_path / "m.jsonl"
    state_path = tmp_path / "s.json"
    state = SidecarState(str(metrics), str(state_path))
    state.client_names.add("paper")
    state.pending["paper|SPY|bar"] = {
        "strategy_name": "example_equity_paper",
        "client_name": "paper",
        "ticker": "SPY",
        "entry_time": "not-a-timestamp",
    }
    flags = reconcile_orders(
        state, clients={"paper": MagicMock(get_orders=MagicMock(return_value=[]))}
    )
    # age_ok False → stays pending
    assert "paper|SPY|bar" in state.pending
    assert "signal_without_trade" not in flags


def test_run_sidecar_process_event_and_drops_errors(tmp_path):
    metrics = str(tmp_path / "m.jsonl")
    state_path = str(tmp_path / "s.json")
    q = MagicMock()
    q.get.side_effect = [
        {"kind": "signal_evaluated", "ticker": "SPY"},
        {"kind": "heartbeat"},
        {"kind": "shutdown"},
    ]
    drops = MagicMock()
    drops.get_lock.side_effect = RuntimeError("lock fail")

    with (
        patch("quant.live.monitor.sidecar._trading_clients", return_value={}),
        patch(
            "quant.live.monitor.sidecar.process_event",
            side_effect=[RuntimeError("boom"), []],
        ),
        patch("quant.live.monitor.sidecar._setup_logging"),
    ):
        run_sidecar(q, drops, metrics_path=metrics, state_path=state_path)


# ---------------------------------------------------------------------------
# DATA_DELAY expected-gap skip + shadow cache / async worker
# ---------------------------------------------------------------------------


def test_is_expected_delay_gap_pre_rth_with_data_delay():
    # 13:00 UTC = 09:00 ET — pre-RTH; only when DATA_DELAY > 0
    assert is_expected_delay_gap(
        "2026-08-03T13:00:00+00:00", interval="15m", data_delay_min=15
    )
    assert is_expected_delay_gap(
        "2026-08-03T13:15:00+00:00", interval="15m", data_delay_min=15
    )
    # 13:30 UTC = 09:30 ET — first RTH stamp
    assert not is_expected_delay_gap(
        "2026-08-03T13:30:00+00:00", interval="15m", data_delay_min=15
    )
    # No delay → do not treat as expected gap
    assert not is_expected_delay_gap(
        "2026-08-03T13:00:00+00:00", interval="15m", data_delay_min=0
    )
    # Daily intervals ignored
    assert not is_expected_delay_gap(
        "2026-08-03T13:00:00+00:00", interval="1d", data_delay_min=15
    )


def test_process_event_expected_delay_gap_skips_missing_and_shadow(tmp_path, caplog):
    """Open phantoms under DATA_DELAY must not raise *_bar_missing or run shadow."""
    metrics = tmp_path / "m.jsonl"
    state_path = tmp_path / "s.json"
    state = SidecarState(str(metrics), str(state_path))
    sidecar_mod.logger.propagate = True
    with (
        patch("quant.live.monitor.sidecar.shadow_backtest_signal") as shadow_fn,
        caplog.at_level(logging.INFO, logger="monitor.sidecar"),
    ):
        flags = process_event(
            state,
            {
                "kind": "signal_evaluated",
                "strategy_name": "example_equity_paper1",
                "client_name": "paper1",
                "ticker": "SPY",
                "bar_ts": "2026-08-03T13:00:00+00:00",
                "end_dt": "2026-08-03T13:15:44+00:00",
                "interval": "15m",
                "live_signal": "HOLD",
                "live_frame_has_bar": False,
                "config_snapshot": {"DATA_DELAY": 15},
                "emit_wall_ns": time.time_ns(),
            },
            clients={},
        )
    assert flags == []
    shadow_fn.assert_not_called()
    assert "live_bar_missing" not in caplog.text
    assert "shadow_bar_missing" not in caplog.text
    assert "expected_delay_gap" in caplog.text
    line = json.loads(metrics.read_text().strip().splitlines()[0])
    assert line["expected_delay_gap"] is True
    assert line["DATA_DELAY"] == 15
    # No shadow_result → does not dilute bar coverage attempts
    kinds = [
        json.loads(x).get("kind") for x in metrics.read_text().strip().splitlines()
    ]
    assert "shadow_result" not in kinds


def test_process_event_expected_delay_gap_requires_data_delay(tmp_path):
    """Without DATA_DELAY, pre-RTH missing still alerts (not an expected gap)."""
    metrics = tmp_path / "m.jsonl"
    state_path = tmp_path / "s.json"
    state = SidecarState(str(metrics), str(state_path))
    shadow = {
        "shadow_signal": "HOLD",
        "shadow_confidence": 0.0,
        "shadow_reason": "",
        "bar_ts": "2026-08-03T13:00:00+00:00",
        "entry_time": "2026-08-03T13:15:00+00:00",
        "shadow_prepare_signals_ns": 1,
        "actionable": False,
        "frame_has_bar": False,
    }
    with patch(
        "quant.live.monitor.sidecar.shadow_backtest_signal", return_value=shadow
    ):
        flags = process_event(
            state,
            {
                "kind": "signal_evaluated",
                "strategy_name": "example_equity_paper1",
                "client_name": "paper1",
                "ticker": "SPY",
                "bar_ts": "2026-08-03T13:00:00+00:00",
                "end_dt": "2026-08-03T13:15:00+00:00",
                "interval": "15m",
                "live_signal": "HOLD",
                "live_frame_has_bar": False,
                # no config_snapshot / DATA_DELAY
            },
            clients={},
        )
    assert "live_bar_missing" in flags
    assert "shadow_bar_missing" in flags


def test_shadow_cache_shared_across_paper_clients(tmp_path):
    metrics = tmp_path / "m.jsonl"
    state_path = tmp_path / "s.json"
    state = SidecarState(str(metrics), str(state_path))
    shadow = {
        "shadow_signal": "HOLD",
        "shadow_confidence": 0.0,
        "shadow_reason": "",
        "bar_ts": "2026-08-03T14:00:00+00:00",
        "entry_time": "2026-08-03T14:15:00+00:00",
        "shadow_prepare_signals_ns": 50,
        "actionable": False,
        "frame_has_bar": True,
    }
    with patch(
        "quant.live.monitor.sidecar.shadow_backtest_signal", return_value=shadow
    ) as shadow_fn:
        for client, sname in (
            ("paper", "example_equity_paper"),
            ("paper1", "example_equity_paper1"),
        ):
            process_event(
                state,
                {
                    "kind": "signal_evaluated",
                    "strategy_name": sname,
                    "client_name": client,
                    "ticker": "SPY",
                    "bar_ts": "2026-08-03T14:00:00+00:00",
                    "end_dt": "2026-08-03T14:15:00+00:00",
                    "interval": "15m",
                    "live_signal": "HOLD",
                    "live_frame_has_bar": True,
                    "config_snapshot": {"DATA_DELAY": 15},
                },
                clients={},
            )
    assert shadow_fn.call_count == 1


def test_shadow_worker_async_heartbeat_low_lag(tmp_path):
    """Drain must not wait on prepare_signals — heartbeat lag stays tiny."""
    metrics = tmp_path / "m.jsonl"
    state_path = tmp_path / "s.json"
    state = SidecarState(str(metrics), str(state_path))
    worker = ShadowWorker(state)
    worker.start()
    started = threading.Event()

    def slow_shadow(**kwargs):
        started.set()
        time.sleep(0.25)
        return {
            "shadow_signal": "HOLD",
            "shadow_confidence": 0.0,
            "shadow_reason": "",
            "bar_ts": kwargs.get("bar_ts") or "2026-08-03T14:00:00+00:00",
            "entry_time": "2026-08-03T14:15:00+00:00",
            "shadow_prepare_signals_ns": 250_000_000,
            "actionable": False,
            "frame_has_bar": True,
        }

    with patch(
        "quant.live.monitor.sidecar.shadow_backtest_signal", side_effect=slow_shadow
    ):
        t_emit = time.time_ns()
        process_event(
            state,
            {
                "kind": "signal_evaluated",
                "strategy_name": "example_equity_paper",
                "client_name": "paper",
                "ticker": "SPY",
                "bar_ts": "2026-08-03T14:00:00+00:00",
                "end_dt": "2026-08-03T14:15:00+00:00",
                "interval": "15m",
                "live_signal": "HOLD",
                "live_frame_has_bar": True,
                "config_snapshot": {"DATA_DELAY": 15},
                "emit_wall_ns": t_emit,
            },
            clients={},
            shadow_worker=worker,
        )
        assert started.wait(timeout=2.0)
        # Heartbeat while shadow still running — queue_lag must not include shadow work
        hb_emit = time.time_ns()
        with patch("quant.live.monitor.sidecar.reconcile_orders", return_value=[]):
            process_event(
                state,
                {"kind": "heartbeat", "emit_wall_ns": hb_emit},
                clients={},
                shadow_worker=worker,
            )
        lines = [
            json.loads(x) for x in metrics.read_text().strip().splitlines() if x.strip()
        ]
        hb = next(e for e in lines if e.get("kind") == "heartbeat")
        lag_ms = (hb.get("queue_lag_ns") or 0) / 1e6
        assert lag_ms < 50, f"heartbeat queue_lag={lag_ms}ms — drain blocked on shadow"

    worker.stop(timeout=3.0)
    # Shadow result eventually landed
    deadline = time.time() + 3.0
    while time.time() < deadline:
        kinds = [
            json.loads(x).get("kind")
            for x in metrics.read_text().strip().splitlines()
            if x.strip()
        ]
        if "shadow_result" in kinds:
            break
        time.sleep(0.05)
    kinds = [
        json.loads(x).get("kind")
        for x in metrics.read_text().strip().splitlines()
        if x.strip()
    ]
    assert "shadow_result" in kinds


# ---------------------------------------------------------------------------
# Bar freshness — SLO eligibility, excess normalisation, breach alerting
# ---------------------------------------------------------------------------


def _eval_event(**overrides):
    base = {
        "kind": "signal_evaluated",
        "strategy_name": "example_equity_paper",
        "client_name": "paper",
        "ticker": "SPY",
        "bar_ts": "2026-08-03T14:00:00+00:00",
        "end_dt": "2026-08-03T14:15:00+00:00",
        "interval": "15m",
        "live_signal": "HOLD",
        "live_frame_has_bar": True,
        "config_snapshot": {"DATA_DELAY": 15},
    }
    base.update(overrides)
    return base


def test_staleness_excess_subtracts_delay_and_floors_at_zero():
    fifteen_min = 15 * 60 * 10**9
    assert staleness_excess_ns(fifteen_min + 4_000_000_000, 15) == 4_000_000_000
    assert staleness_excess_ns(fifteen_min, 0) == fifteen_min
    assert staleness_excess_ns(fifteen_min - 1, 15) == 0
    assert staleness_excess_ns(None, 15) is None
    assert staleness_excess_ns("nope", 15) is None


def test_freshness_eligibility_excludes_daily_and_expected_gaps():
    """Only bars whose availability sets the action time belong in the SLO."""
    assert is_freshness_sampleable(_eval_event())
    assert not is_freshness_sampleable(_eval_event(interval="1d"))
    assert not is_freshness_sampleable(_eval_event(interval="1wk"))
    assert not is_freshness_sampleable(_eval_event(expected_delay_gap=True))


def test_process_event_stamps_excess_for_legacy_payloads(tmp_path):
    """Older emits carry raw staleness only — the sidecar derives the SLO value."""
    state = SidecarState(str(tmp_path / "m.jsonl"), str(tmp_path / "s.json"))
    with patch("quant.live.monitor.sidecar.shadow_backtest_signal") as shadow_fn:
        shadow_fn.side_effect = RuntimeError("skip shadow")
        process_event(
            state,
            _eval_event(bar_staleness_ns=15 * 60 * 10**9 + 7_000_000_000),
            clients={},
        )
    line = json.loads((tmp_path / "m.jsonl").read_text().strip().splitlines()[0])
    assert line["DATA_DELAY"] == 15
    assert line["bar_staleness_excess_ns"] == 7_000_000_000
    assert line["freshness_slo_eligible"] is True


def test_process_event_prefers_engine_computed_excess(tmp_path):
    """The engine knows the delay actually applied — do not recompute over it."""
    state = SidecarState(str(tmp_path / "m.jsonl"), str(tmp_path / "s.json"))
    with patch("quant.live.monitor.sidecar.shadow_backtest_signal") as shadow_fn:
        shadow_fn.side_effect = RuntimeError("skip shadow")
        process_event(
            state,
            _eval_event(
                bar_staleness_ns=15 * 60 * 10**9 + 7_000_000_000,
                bar_staleness_excess_ns=1_234,
            ),
            clients={},
        )
    line = json.loads((tmp_path / "m.jsonl").read_text().strip().splitlines()[0])
    assert line["bar_staleness_excess_ns"] == 1_234


def test_freshness_samples_skip_ineligible_bars(tmp_path):
    """Daily bars and open phantoms must not pollute the freshness percentiles."""
    state = SidecarState(str(tmp_path / "m.jsonl"), str(tmp_path / "s.json"))
    samples = state._span_samples["bar_staleness_excess_ns"]

    state.record_spans(_eval_event(bar_staleness_excess_ns=5_000_000), None)
    assert list(samples) == [5_000_000]

    state.record_spans(
        _eval_event(interval="1d", bar_staleness_excess_ns=80_000_000_000), None
    )
    state.record_spans(
        _eval_event(expected_delay_gap=True, bar_staleness_excess_ns=90_000_000_000),
        None,
    )
    assert list(samples) == [5_000_000]


def test_rollup_flags_freshness_breach(tmp_path, caplog):
    """With the hard-coded 10 ms SLO, a 60 ms p99 excess trips a breach."""
    state = SidecarState(str(tmp_path / "m.jsonl"), str(tmp_path / "s.json"))
    state._span_samples["bar_staleness_excess_ns"].extend([60_000_000] * 5)
    state._last_rollup = time.monotonic() - 120
    sidecar_mod.logger.propagate = True
    with caplog.at_level(logging.ERROR, logger="monitor.sidecar"):
        flags = state.maybe_rollup()
    assert "bar_staleness_breach" in flags
    assert "60ms" in caplog.text


def test_rollup_silent_when_freshness_within_budget(tmp_path):
    """Sub-10 ms p99 excess stays silent under the tight budget."""
    state = SidecarState(str(tmp_path / "m.jsonl"), str(tmp_path / "s.json"))
    state._span_samples["bar_staleness_excess_ns"].extend([5_000_000] * 5)
    state._last_rollup = time.monotonic() - 120
    assert state.maybe_rollup() == []
