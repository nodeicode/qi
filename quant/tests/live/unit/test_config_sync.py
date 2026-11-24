"""Unit tests for ConfigSync.poll() and ConfigSync._validate()."""

import json
import logging
from unittest.mock import MagicMock, patch

import pytest
from botocore.exceptions import BotoCoreError, ClientError

from quant.live.config_sync import ConfigSync

logger = logging.getLogger("test")

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_VALID_CONFIG = {"strategies": [{"name": "ml_tech", "config": {}}]}
_VALID_VALUE = json.dumps(_VALID_CONFIG)


def _make_sync(version: int = 0, value: str = _VALID_VALUE) -> ConfigSync:
    """Return a ConfigSync whose SSM client is a fresh mock."""
    with patch("quant.live.config_sync.boto3") as mock_boto3:
        mock_ssm = MagicMock()
        mock_boto3.client.return_value = mock_ssm
        sync = ConfigSync(
            ssm_param_name="/trading/staging/strategies_config", logger=logger
        )

    sync._ssm = MagicMock()
    sync._ssm.get_parameter.return_value = {
        "Parameter": {"Version": version, "Value": value}
    }
    return sync


def _client_error(code: str) -> ClientError:
    return ClientError({"Error": {"Code": code, "Message": code}}, "GetParameter")


# ---------------------------------------------------------------------------
# _validate
# ---------------------------------------------------------------------------


class TestValidate:
    def test_valid_config_passes(self) -> None:
        assert ConfigSync._validate(_VALID_CONFIG) is True

    def test_non_dict_fails(self) -> None:
        assert ConfigSync._validate([]) is False

    def test_missing_strategies_key_fails(self) -> None:
        assert ConfigSync._validate({}) is False

    def test_empty_strategies_list_passes(self) -> None:
        # Empty list is a valid "pause all" payload — _validate does not require
        # at least one entry; the all() vacuously returns True.
        assert ConfigSync._validate({"strategies": []}) is True

    def test_entry_missing_name_fails(self) -> None:
        assert ConfigSync._validate({"strategies": [{"config": {}}]}) is False

    def test_non_list_strategies_fails(self) -> None:
        assert ConfigSync._validate({"strategies": "bad"}) is False


# ---------------------------------------------------------------------------
# poll() — hot path (unchanged version)
# ---------------------------------------------------------------------------


class TestPoll:
    def test_unchanged_version_returns_none(self) -> None:
        sync = _make_sync(version=1)
        sync._last_version = 1
        assert sync.poll() is None
        sync._ssm.get_parameter.assert_called_once()

    def test_version_change_returns_config(self) -> None:
        sync = _make_sync(version=2)
        sync._last_version = 1
        result = sync.poll()
        assert result == _VALID_CONFIG
        assert sync._last_version == 2

    def test_json_parse_error_commits_version_returns_none(self, caplog) -> None:
        sync = _make_sync(version=3, value="not json {{{")
        sync._last_version = None
        with caplog.at_level(logging.ERROR, logger="test"):
            result = sync.poll()
        assert result is None
        assert sync._last_version == 3
        assert "parse error" in caplog.text

    def test_validation_failure_commits_version_returns_none(self, caplog) -> None:
        # A strategies list whose entry is missing 'name' fails _validate.
        sync = _make_sync(version=4, value='{"strategies": [{"config": {}}]}')
        sync._last_version = None
        with caplog.at_level(logging.ERROR, logger="test"):
            result = sync.poll()
        assert result is None
        assert sync._last_version == 4
        assert "schema validation" in caplog.text

    # -----------------------------------------------------------------------
    # Error branches — all must return None without raising
    # -----------------------------------------------------------------------

    def test_parameter_not_found_returns_none(self, caplog) -> None:
        sync = _make_sync()
        sync._ssm.get_parameter.side_effect = _client_error("ParameterNotFound")
        with caplog.at_level(logging.WARNING, logger="test"):
            result = sync.poll()
        assert result is None
        assert "not found" in caplog.text

    def test_client_error_returns_none(self, caplog) -> None:
        sync = _make_sync()
        sync._ssm.get_parameter.side_effect = _client_error("AccessDeniedException")
        with caplog.at_level(logging.ERROR, logger="test"):
            result = sync.poll()
        assert result is None
        assert "SSM fetch error" in caplog.text

    def test_botocore_transport_error_returns_none(self, caplog) -> None:
        """EndpointConnectionError, ConnectTimeout, etc. must not bubble up."""
        sync = _make_sync()
        sync._ssm.get_parameter.side_effect = BotoCoreError()
        with caplog.at_level(logging.ERROR, logger="test"):
            result = sync.poll()
        assert result is None
        assert "transport error" in caplog.text

    def test_key_error_on_malformed_response_returns_none(self, caplog) -> None:
        sync = _make_sync()
        sync._ssm.get_parameter.return_value = {"unexpected": "shape"}
        with caplog.at_level(logging.ERROR, logger="test"):
            result = sync.poll()
        assert result is None
        assert "response shape error" in caplog.text

    def test_unexpected_exception_returns_none(self, caplog) -> None:
        """Any unanticipated exception must be swallowed and logged."""
        sync = _make_sync()
        sync._ssm.get_parameter.side_effect = RuntimeError("boom")
        with caplog.at_level(logging.ERROR, logger="test"):
            result = sync.poll()
        assert result is None
        assert "Unexpected error" in caplog.text


# ---------------------------------------------------------------------------
# _imds_region — success path (lines 33-39)
# ---------------------------------------------------------------------------


class TestImdsRegionSuccess:
    def test_returns_region_from_imds(self) -> None:
        """Cover the IMDS try-block success path (lines 33-39)."""
        from unittest.mock import MagicMock, patch

        from quant.live.config_sync import _imds_region

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
# last_version property (line 55)
# ---------------------------------------------------------------------------


class TestLastVersionProperty:
    def test_last_version_returns_none_initially(self) -> None:
        sync = _make_sync()
        assert sync.last_version is None

    def test_last_version_returns_updated_value_after_poll(self) -> None:
        sync = _make_sync(version=7)
        sync.poll()
        assert sync.last_version == 7
