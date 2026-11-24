"""Stub-plugin test: a strategy registered via the ``quant.strategies`` entry-point
group resolves through ``get_strategy`` with NO SDK code changes.

The stub plugin is built as a minimal *distribution* (a ``.dist-info`` directory
with ``METADATA`` + ``entry_points.txt``) dropped on ``sys.path`` — exactly the
mechanism ``importlib.metadata`` uses to discover installed distributions — so
the test is hermetic and offline ('pip install'-free). The plugin's strategy
class lives in its own module and is neither present in nor imported by the SDK.
"""

from __future__ import annotations

import textwrap

import pytest

from quant.strategies import get_strategy

STUB_MODULE = "my_stub_strategy"
STUB_DIST = "qi_stub_strategy-1.0.dist-info"
GROUP = "quant.strategies"
STUB_NAME = "stub_strategy"


@pytest.fixture
def stub_plugin(tmp_path, monkeypatch):
    """Install a minimal stub strategy distribution into sys.path."""
    dist_info = tmp_path / STUB_DIST
    dist_info.mkdir()
    (dist_info / "METADATA").write_text(
        "Metadata-Version: 2.1\nName: qi-stub-strategy\nVersion: 1.0\n",
        encoding="utf-8",
    )
    (dist_info / "entry_points.txt").write_text(
        f"[{GROUP}]\n{STUB_NAME} = {STUB_MODULE}:StubStrategy\n",
        encoding="utf-8",
    )
    (tmp_path / f"{STUB_MODULE}.py").write_text(
        textwrap.dedent(
            """\
            class StubStrategy:
                \"\"\"A strategy shipped by a third-party plugin distribution.\"\"\"

                def __init__(self) -> None:
                    self.name = "stub_strategy"
            """
        ),
        encoding="utf-8",
    )
    monkeypatch.syspath_prepend(str(tmp_path))


def test_builtin_example_equity_resolves_by_name() -> None:
    from quant.strategies.example_equity.strategy import ExampleEquityStrategy

    resolved = get_strategy("example_equity")

    assert resolved is ExampleEquityStrategy


def test_stub_plugin_strategy_resolves_with_no_sdk_change(stub_plugin: None) -> None:
    from my_stub_strategy import StubStrategy  # type: ignore[import-not-found]

    resolved = get_strategy(STUB_NAME)

    # The plugin class comes from the plugin module, not from the SDK.
    assert resolved is StubStrategy
    assert resolved().name == "stub_strategy"


def test_stub_strategy_not_known_without_plugin() -> None:
    with pytest.raises(ValueError):
        get_strategy(STUB_NAME)


def test_unknown_strategy_lists_available_names(stub_plugin: None) -> None:
    with pytest.raises(ValueError) as exc:
        get_strategy("nope")

    message = str(exc.value)
    assert "nope" in message
    assert "example_equity" in message
    assert STUB_NAME in message
