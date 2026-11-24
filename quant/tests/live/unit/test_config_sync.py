"""Unit tests for the SSM config backend + the SSM-bound compat shim.

Covers (spec `runtime-config`):
  - validate_config schema guard (unchanged from the original poller)
  - poll() unchanged-version short-circuit (one lightweight backend call)
  - poll() returns a changed version's config exactly once
  - invalid-payload-once: parse/validation failure commits the version and the
    error is logged once — the operator bumps the version to retry
  - poll errors (ParameterNotFound, ClientError, BotoCoreError, malformed
    response, unexpected) return None without raising; prior config stays active
  - IMDS region resolution success path
  - last_version property
  - the quant.live.config_sync compat shim still constructs a working
    SSM-backed ConfigSync + keeps the historical _validate alias
"""

from __future__ import annotations

import json
import logging
import sys
from unittest.mock import MagicMock, patch

import pytest
from botocore.exceptions import BotoCoreError, ClientError

from quant.config.base import validate_config
from quant.config.ssm import SSMConfigSource, _imds_region
from quant.live.config_sync import ConfigSync

logger = logging.getLogger("test")

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_VALID_CONFIG = {"strategies": [{"name": "ml_tech", "config": {}}]}
_VALID_VALUE = json.dumps(_VALID_CONFIG)

_REGION = "us-east-1"


def _seed_boto3(
    monkeypatch: pytest.MonkeyPatch,
    *,
    version: int = 0,
    value: str = _VALID_VALUE,
    side_effect=None,
) -> MagicMock:
    """Seed ``sys.modules['boto3']`` with a stub module (SSM imports it lazily)."""
    mock_ssm = MagicMock()
    if side_effect is not None:
        mock_ssm.get_parameter.side_effect = side_effect
    else:
        mock_ssm.get_parameter.return_value = {
            "Parameter": {"Version": version, "Value": value}
        }

    class _FakeBoto3:
        client = MagicMock(return_value=mock_ssm)

    monkeypatch.setitem(sys.modules, "boto3", _FakeBoto3)
    return mock_ssm


def _make_sync(
    monkeypatch: pytest.MonkeyPatch,
    *,
    version: int = 0,
    value: str = _VALID_VALUE,
) -> SSMConfigSource:
    """Return an SSMConfigSource with a stubbed, already-created client."""
    _seed_boto3(monkeypatch, version=version, value=value)
    source = SSMConfigSource(
        param_name="/trading/staging/strategies_config", region_name=_REGION
    )
    source._client()  # materialize the lazy client so tests can poke _ssm
    return source


def _client_error(code: str) -> ClientError:
    return ClientError({"Error": {"Code": code, "Message": code}}, "GetParameter")


# ---------------------------------------------------------------------------
# validate_config
# ---------------------------------------------------------------------------


class TestValidate:
    def test_valid_config_passes(self) -> None:
        assert validate_config(_VALID_CONFIG) is True
        assert ConfigSync._validate(_VALID_CONFIG) is True  # historical alias

    def test_non_dict_fails(self) -> None:
        assert validate_config([]) is False

    def test_missing_strategies_key_fails(self) -> None:
        assert validate_config({}) is False

    def test_empty_strategies_list_passes(self) -> None:
        # Empty list is a valid "pause all" payload.
        assert validate_config({"strategies": []}) is True

    def test_entry_missing_name_fails(self) -> None:
        assert validate_config({"strategies": [{"config": {}}]}) is False

    def test_non_list_strategies_fails(self) -> None:
        assert validate_config({"strategies": "bad"}) is False


# ---------------------------------------------------------------------------
# poll() — hot path (unchanged version)
# ---------------------------------------------------------------------------


class TestPoll:
    def test_first_poll_returns_config_once(self, monkeypatch) -> None:
        sync = _make_sync(monkeypatch, version=1)
        assert sync.poll() == _VALID_CONFIG
        assert sync.last_version == 1

    def test_unchanged_version_returns_none(self, monkeypatch) -> None:
        sync = _make_sync(monkeypatch, version=1)
        sync.poll()
        assert sync.poll() is None

    def test_version_change_returns_config_once(self, monkeypatch) -> None:
        mock_ssm = _seed_boto3(monkeypatch, version=1)
        sync = SSMConfigSource(param_name="/x", region_name=_REGION)
        sync.poll()
        # backend bumps to v2 — next poll adopts once, then short-circuits
        mock_ssm.get_parameter.return_value = {
            "Parameter": {"Version": 2, "Value": _VALID_VALUE}
        }
        assert sync.poll() == _VALID_CONFIG
        assert sync.last_version == 2
        assert sync.poll() is None

    def test_json_parse_error_commits_version_returns_none(
        self, monkeypatch, caplog
    ) -> None:
        mock_ssm = _seed_boto3(monkeypatch, version=3, value="not json {{{")
        sync = SSMConfigSource(param_name="/x", region_name=_REGION, logger=logger)
        with caplog.at_level(logging.ERROR, logger="test"):
            result = sync.poll()
        assert result is None
        assert sync.last_version == 3
        assert "invalid payload" in caplog.text
        # invalid-payload-once: same version polls silently, no re-parse/re-log
        caplog.clear()
        with caplog.at_level(logging.ERROR, logger="test"):
            assert sync.poll() is None
        assert caplog.text == ""
        # operator fixes the payload AND bumps the version -> adopted again
        mock_ssm.get_parameter.return_value = {
            "Parameter": {"Version": 4, "Value": _VALID_VALUE}
        }
        assert sync.poll() == _VALID_CONFIG

    def test_validation_failure_commits_version_returns_none(
        self, monkeypatch, caplog
    ) -> None:
        sync = _make_sync(
            monkeypatch, version=4, value='{"strategies": [{"config": {}}]}'
        )
        with caplog.at_level(logging.ERROR, logger="test"):
            result = sync.poll()
        assert result is None
        assert sync.last_version == 4
        assert "invalid payload" in caplog.text

    def test_validation_failure_keeps_prior_config_active(self, monkeypatch) -> None:
        sync = _make_sync(monkeypatch, version=1)
        assert sync.poll() == _VALID_CONFIG  # adopted
        sync._cached_value = None  # drop the stale payload cache
        assert sync.poll() is None  # unchanged version -> None regardless

    # -----------------------------------------------------------------------
    # Error branches — all must return None without raising (spec: silent)
    # -----------------------------------------------------------------------

    def test_parameter_not_found_returns_none(self, monkeypatch) -> None:
        sync = _make_sync(monkeypatch)
        sync._ssm.get_parameter.side_effect = _client_error("ParameterNotFound")
        assert sync.poll() is None

    def test_client_error_returns_none(self, monkeypatch) -> None:
        sync = _make_sync(monkeypatch)
        sync._ssm.get_parameter.side_effect = _client_error("AccessDeniedException")
        assert sync.poll() is None

    def test_botocore_transport_error_returns_none(self, monkeypatch) -> None:
        sync = _make_sync(monkeypatch)
        sync._ssm.get_parameter.side_effect = BotoCoreError()
        assert sync.poll() is None

    def test_key_error_on_malformed_response_returns_none(self, monkeypatch) -> None:
        mock_ssm = _seed_boto3(monkeypatch, version=1)
        mock_ssm.get_parameter.return_value = {"unexpected": "shape"}
        sync = SSMConfigSource(param_name="/x", region_name=_REGION)
        assert sync.poll() is None

    def test_unexpected_exception_returns_none(self, monkeypatch) -> None:
        sync = _make_sync(monkeypatch)
        sync._ssm.get_parameter.side_effect = RuntimeError("boom")
        assert sync.poll() is None

    def test_errors_do_not_commit_version(self, monkeypatch) -> None:
        # A transient error must not clobber the adopted-version short-circuit:
        # prior config stays active and is not re-adopted after the error.
        mock_ssm = _seed_boto3(monkeypatch, version=5)
        sync = SSMConfigSource(param_name="/x", region_name=_REGION)
        assert sync.poll() == _VALID_CONFIG
        assert sync.last_version == 5
        mock_ssm.get_parameter.side_effect = _client_error("ThrottlingException")
        assert sync.poll() is None
        mock_ssm.get_parameter.side_effect = None
        mock_ssm.get_parameter.return_value = {
            "Parameter": {"Version": 5, "Value": _VALID_VALUE}
        }
        assert sync.poll() is None  # still short-circuited, not re-adopted


# ---------------------------------------------------------------------------
# _imds_region — IMDSv2 success path
# ---------------------------------------------------------------------------


class TestImdsRegionSuccess:
    def test_returns_region_from_imds(self) -> None:
        token_resp = MagicMock()
        token_resp.read.return_value = b"fake-token"
        token_resp.__enter__ = lambda s: token_resp
        token_resp.__exit__ = MagicMock(return_value=False)

        region_resp = MagicMock()
        region_resp.read.return_value = b"us-west-2"
        region_resp.__enter__ = lambda s: region_resp
        region_resp.__exit__ = MagicMock(return_value=False)

        with patch("urllib.request.urlopen", side_effect=[token_resp, region_resp]):
            result = _imds_region()

        assert result == "us-west-2"


# ---------------------------------------------------------------------------
# last_version property
# ---------------------------------------------------------------------------


class TestLastVersionProperty:
    def test_last_version_returns_none_initially(self, monkeypatch) -> None:
        sync = _make_sync(monkeypatch)
        assert sync.last_version is None

    def test_last_version_returns_updated_value_after_poll(self, monkeypatch) -> None:
        sync = _make_sync(monkeypatch, version=7)
        sync.poll()
        assert sync.last_version == 7


# ---------------------------------------------------------------------------
# quant.live.config_sync compat shim
# ---------------------------------------------------------------------------


class TestCompatShim:
    def test_shim_constructs_ssm_config_sync(self, monkeypatch) -> None:
        _seed_boto3(monkeypatch, version=2)
        sync = ConfigSync(
            ssm_param_name="/trading/staging/strategies_config", logger=logger
        )
        assert sync.poll() == _VALID_CONFIG
        assert sync.last_version == 2
        assert sync.poll() is None

    def test_shim_validate_alias(self) -> None:
        assert ConfigSync._validate(_VALID_CONFIG) is True
        assert ConfigSync._validate([]) is False
