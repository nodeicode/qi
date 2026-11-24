"""Unit tests for quant.live.monitor emit — the non-blocking enqueue primitive.

The sidecar consumer / shadow worker is a developer-built extension (design
D7); it was cut from the public tree in the group 7 monitor trim together with
``core/monitor_utils.py`` (moves to the private superset as the reference
extension).  Only the hot-path emit surface is modelled here.
"""

from __future__ import annotations

import queue
import threading
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

import quant.live.monitor.emit as emit_mod


@pytest.fixture(autouse=True)
def _reset_emit_globals():
    """Ensure emit module globals do not leak across tests."""
    emit_mod._queue = None
    emit_mod._drops = None
    yield
    emit_mod._queue = None
    emit_mod._drops = None


def test_emit_noop_without_sidecar():
    emit_mod.emit({"kind": "signal_evaluated", "ticker": "SPY"})  # must not raise


def test_emit_never_blocks_on_full_queue():
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


def test_ns_wall_ns_enabled():
    assert isinstance(emit_mod.ns(), int)
    assert isinstance(emit_mod.wall_ns(), int)
    assert emit_mod.enabled() is False
    emit_mod._queue = queue.Queue()
    assert emit_mod.enabled() is True


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
