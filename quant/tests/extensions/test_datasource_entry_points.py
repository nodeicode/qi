"""Stub-plugin test: a data source registered via the ``quant.datasources``
entry-point group resolves through ``get_datasource`` with NO SDK change.

Mirror of the strategy entry-point test (task 6.3): the stub plugin is built as
a minimal distribution (a ``.dist-info`` directory with ``METADATA`` +
``entry_points.txt``) dropped on ``sys.path``, so the test is hermetic and
offline. The plugin's data source class lives in its own module and is neither
present in nor imported by the SDK.
"""

from __future__ import annotations

import textwrap

import pytest

from quant.data import get_datasource

STUB_MODULE = "my_stub_datasource"
STUB_DIST = "qi_stub_datasource-1.0.dist-info"
GROUP = "quant.datasources"
STUB_NAME = "stub_datasource"


@pytest.fixture
def stub_plugin(tmp_path, monkeypatch) -> None:
    """Install a minimal stub data-source distribution into ``sys.path``."""
    dist_info = tmp_path / STUB_DIST
    dist_info.mkdir()
    (dist_info / "METADATA").write_text(
        "Metadata-Version: 2.1\nName: qi-stub-datasource\nVersion: 1.0\n",
        encoding="utf-8",
    )
    (dist_info / "entry_points.txt").write_text(
        f"[{GROUP}]\n{STUB_NAME} = {STUB_MODULE}:StubDatasource\n",
        encoding="utf-8",
    )
    (tmp_path / f"{STUB_MODULE}.py").write_text(
        textwrap.dedent(
            """\
            class StubDatasource:
                \"\"\"A data source shipped by a third-party plugin distribution.\"\"\"

                name = "stub"
            """
        ),
        encoding="utf-8",
    )
    monkeypatch.syspath_prepend(str(tmp_path))


def test_builtin_backtest_resolves_by_name() -> None:
    import pandas as pd

    from quant.backtest.backtest_data_handler import BacktestDataHandler

    handler = get_datasource(
        "backtest",
        df_signals=pd.DataFrame(),
        df_manage=pd.DataFrame(),
        signal_interval="1d",
        manage_interval="1d",
    )
    assert isinstance(handler, BacktestDataHandler)


def test_stub_plugin_datasource_resolves_with_no_sdk_change(stub_plugin: None) -> None:
    from my_stub_datasource import StubDatasource  # type: ignore[import-not-found]

    resolved = get_datasource(STUB_NAME)

    assert isinstance(resolved, StubDatasource)
    assert resolved.name == "stub"


def test_unknown_datasource_names_available(stub_plugin: None) -> None:
    with pytest.raises(ValueError) as exc:
        get_datasource("nope")

    message = str(exc.value)
    assert "nope" in message
    assert "backtest" in message
    assert STUB_NAME in message
