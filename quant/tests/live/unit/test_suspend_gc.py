"""Tests for quant.live.utils.suspend_gc (hot-path GC pause)."""

import gc

import pytest

from quant.live.utils import suspend_gc


def test_suspend_gc_disables_inside_and_reenables_after():
    assert gc.isenabled()
    with suspend_gc():
        assert not gc.isenabled()
    assert gc.isenabled()


def test_suspend_gc_reenables_on_exception():
    assert gc.isenabled()
    with pytest.raises(RuntimeError), suspend_gc():
        assert not gc.isenabled()
        raise RuntimeError("boom")
    assert gc.isenabled()


def test_suspend_gc_nested_still_leaves_gc_enabled():
    with suspend_gc(), suspend_gc():
        assert not gc.isenabled()
    assert gc.isenabled()
