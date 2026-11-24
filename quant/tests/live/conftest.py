"""
Shared pytest configuration for the python/tests suite.
"""

from __future__ import annotations

import contextlib
import logging
import sys
from typing import Any
from unittest.mock import MagicMock, patch

import pytest


@pytest.fixture(autouse=True)
def stub_alpaca(monkeypatch: pytest.MonkeyPatch):
    """Per-test stub of the alpaca SDK for the live suite.

    Replaces the old import-time ``sys.modules["alpaca*"] = mock`` blocks in
    individual test modules, which leaked into every other test in a merged
    (workspace) pytest session. Scoped to this suite via the fixture.
    """
    mock_alpaca = MagicMock(name="alpaca")
    for mod in (
        "alpaca",
        "alpaca.trading",
        "alpaca.trading.requests",
        "alpaca.trading.client",
        "alpaca.data",
        "alpaca.data.historical",
        "alpaca.data.requests",
        "alpaca.data.enums",
        "alpaca.data.timeframe",
    ):
        monkeypatch.setitem(sys.modules, mod, mock_alpaca)


@pytest.fixture(autouse=True)
def mock_monitor(request: pytest.FixtureRequest):
    """Stub the monitor emit hook for trader/integration tests.

    ``UnifiedTrader`` wires the optional emit hook by default (``MONITOR=1``);
    the queue consumer (sidecar) is a developer-built extension, and the
    public tree no longer ships one.  ``emit`` is a ``MagicMock`` that
    records events for assertions, and the trader still wires
    ``_monitor_emit`` into ``TradingEngine`` as in prod.

    ``tests/unit/test_monitor_emit.py`` exercises the real emit module and is
    excluded from this stub.
    """
    if request.module.__name__.endswith("test_monitor_emit"):
        yield None
        return

    mock_emit = MagicMock(name="monitor_emit")
    with patch("quant.live.monitor.emit.emit", mock_emit):
        # Attach for unittest.TestCase methods that cannot take fixtures.
        request.node.monitor_emit = mock_emit
        yield mock_emit


@pytest.fixture(autouse=True)
def _mock_aws_and_env():
    """Prevent real AWS and filesystem calls in every test.

    - Patches ``quant.live.config_sync.boto3`` so ConfigSync.__init__ never
      creates a real SSM client.  The mock SSM returns an empty-strategies
      payload (Version 0) which passes _validate (empty list is a valid
      "pause all" config), so poll() returns {"strategies": []} and the
      trader starts with no active strategies.
    - Patches ``quant.live.trader.resolve_env`` to return "staging" so no
      .environment file is required on the developer's machine.
    """
    mock_ssm = MagicMock()
    mock_ssm.get_parameter.return_value = {
        "Parameter": {"Version": 0, "Value": '{"strategies":[]}'}
    }
    mock_boto3 = MagicMock()
    mock_boto3.client.return_value = mock_ssm
    with (
        patch("quant.live.config_sync.boto3", mock_boto3),
        patch("quant.live.trader.resolve_env", return_value="staging"),
    ):
        yield


def emitted_events(
    emit_mock: MagicMock, *, kind: str | None = None
) -> list[dict[str, Any]]:
    """Return dict payloads recorded by the autouse ``mock_monitor`` emit stub."""
    events: list[dict[str, Any]] = []
    for call in emit_mock.call_args_list:
        if not call.args:
            continue
        event = call.args[0]
        if isinstance(event, dict) and (kind is None or event.get("kind") == kind):
            events.append(event)
    return events


def pytest_sessionfinish(session, exitstatus) -> None:
    """
    Drain all logging handlers before the interpreter exits.

    ``trader.py`` registers an atexit hook that calls ``logger.info()``
    after pytest has closed I/O streams, producing a spurious
    ``ValueError: I/O operation on closed file`` on stderr.

    Removing all handlers here (before atexit fires) ensures the hook
    fires silently: loggers still exist but have nowhere to write.
    """
    all_loggers: list[logging.Logger] = [logging.getLogger()]
    for candidate in logging.Logger.manager.loggerDict.values():
        if isinstance(candidate, logging.Logger):
            all_loggers.append(candidate)

    for logger in all_loggers:
        for handler in logger.handlers[:]:
            with contextlib.suppress(Exception):
                handler.close()
            logger.removeHandler(handler)
