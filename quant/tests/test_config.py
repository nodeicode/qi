"""Primitive tests for the runtime-config package (spec `runtime-config`).

Covers (all zero-AWS):
  - validate_config / parse_config schema guard + payload coercion
  - FileConfigSource: adoption once per version, mtime-change detection,
    unchanged-version never re-reads, invalid-payload-once, missing-file silence
  - EnvConfigSource: unset silent, adopt once, unchanged short-circuit
  - ConfigSync shell + backend selection by configuration (registry)
  - zero-AWS base install: package import and the local-first backends work
    with boto3 completely unavailable; the SSM backend resolves lazily and
    degrades to a silent ``None`` poll
"""

from __future__ import annotations

import builtins
import importlib
import json
import logging
import os
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from quant.config import (
    ConfigSync,
    EnvConfigSource,
    FileConfigSource,
    config_source_by_name,
    parse_config,
    register_source,
    validate_config,
)
from quant.config.base import ConfigPayloadError

_LOG = logging.getLogger("testcfg")

_CONFIG_A = {"strategies": [{"name": "alpha", "config": {"FOO": 1}}]}
_CONFIG_B = {"strategies": [{"name": "alpha"}, {"name": "beta"}]}
_JSON_A = json.dumps(_CONFIG_A)
_JSON_B = json.dumps(_CONFIG_B)

_NEXT_MTIME = int(1e18)  # far-future, strictly increasing


def _bump_mtime(path: Path) -> None:
    """Give *path* a strictly-newer mtime than any previous write."""
    global _NEXT_MTIME
    _NEXT_MTIME += 1_000_000
    os.utime(path, ns=(_NEXT_MTIME, _NEXT_MTIME))


# ---------------------------------------------------------------------------
# validate_config / parse_config
# ---------------------------------------------------------------------------


class TestValidateConfig:
    def test_valid_passes(self) -> None:
        assert validate_config(_CONFIG_A) is True

    def test_non_dict_fails(self) -> None:
        assert validate_config([]) is False
        assert validate_config("strategies") is False

    def test_missing_strategies_fails(self) -> None:
        assert validate_config({}) is False
        assert validate_config({"config": {}}) is False

    def test_empty_strategies_is_valid_pause_all(self) -> None:
        assert validate_config({"strategies": []}) is True

    def test_entry_missing_name_fails(self) -> None:
        assert validate_config({"strategies": [{"config": {}}]}) is False

    def test_non_dict_entry_fails(self) -> None:
        assert validate_config({"strategies": ["alpha"]}) is False

    def test_non_list_strategies_fails(self) -> None:
        assert validate_config({"strategies": "bad"}) is False


class TestParseConfig:
    def test_json_string_passes(self) -> None:
        assert parse_config(_JSON_A) == _CONFIG_A

    def test_bytes_payload_passes(self) -> None:
        assert parse_config(_JSON_A.encode()) == _CONFIG_A

    def test_already_parsed_dict_passes(self) -> None:
        assert parse_config(_CONFIG_A) == _CONFIG_A

    def test_garbage_json_raises(self) -> None:
        with pytest.raises(ConfigPayloadError):
            parse_config("not json {{{")

    def test_valid_json_wrong_shape_raises(self) -> None:
        with pytest.raises(ConfigPayloadError):
            parse_config(json.dumps({"strategies": "bad"}))
        with pytest.raises(ConfigPayloadError):
            parse_config(json.dumps(["not", "a", "dict"]))


# ---------------------------------------------------------------------------
# FileConfigSource
# ---------------------------------------------------------------------------


class TestFileConfigSource:
    def test_adopts_config_once_per_version(self, tmp_path: Path) -> None:
        path = tmp_path / "strategies_config.json"
        path.write_text(_JSON_A)
        source = FileConfigSource(path=path)
        assert source.poll() == _CONFIG_A
        assert source.last_version is not None
        assert source.poll() is None  # unchanged -> None, no re-parse

    def test_mtime_change_adopts_new_config_once(self, tmp_path: Path) -> None:
        path = tmp_path / "strategies_config.json"
        path.write_text(_JSON_A)
        source = FileConfigSource(path=path)
        assert source.poll() == _CONFIG_A

        path.write_text(_JSON_B)
        _bump_mtime(path)
        assert source.poll() == _CONFIG_B
        assert source.poll() is None

    def test_unchanged_version_never_reads_file(
        self, tmp_path: Path, monkeypatch
    ) -> None:
        path = tmp_path / "strategies_config.json"
        path.write_text(_JSON_A)
        source = FileConfigSource(path=path)
        real_read_text = Path.read_text
        reads = {"count": 0}

        def _counting_read_text(self, *args, **kwargs):
            reads["count"] += 1
            return real_read_text(self, *args, **kwargs)

        monkeypatch.setattr(Path, "read_text", _counting_read_text)

        assert source.poll() == _CONFIG_A
        assert reads["count"] == 1  # read only to adopt the changed version

        assert source.poll() is None
        assert reads["count"] == 1  # unchanged version: hot path = stat only

    def test_invalid_payload_refused_once(self, tmp_path: Path, caplog) -> None:
        path = tmp_path / "bad.json"
        path.write_text("{not json")
        source = FileConfigSource(path=path, logger=_LOG)
        with caplog.at_level(logging.ERROR, logger="testcfg"):
            assert source.poll() is None
        assert source.last_version is not None
        assert "invalid payload" in caplog.text

        # invalid-payload-once: same version polls silently (error logged once)
        caplog.clear()
        with caplog.at_level(logging.ERROR, logger="testcfg"):
            assert source.poll() is None
        assert caplog.text == ""

        # operator fixes the payload and bumps the version -> adopted again
        path.write_text(_JSON_A)
        _bump_mtime(path)
        assert source.poll() == _CONFIG_A

    def test_wrong_shape_refused_once(self, tmp_path: Path, caplog) -> None:
        path = tmp_path / "bad.json"
        path.write_text(json.dumps({"strategies": "nope"}))
        source = FileConfigSource(path=path, logger=_LOG)
        with caplog.at_level(logging.ERROR, logger="testcfg"):
            assert source.poll() is None
        assert "invalid payload" in caplog.text

    def test_missing_file_is_silent(self, tmp_path: Path) -> None:
        source = FileConfigSource(path=tmp_path / "no_such_file.json")
        assert source.poll() is None
        assert source.last_version is None
        assert source.poll() is None  # still silent on repeat polls


# ---------------------------------------------------------------------------
# EnvConfigSource
# ---------------------------------------------------------------------------


class TestEnvConfigSource:
    def test_unset_env_is_silent(self) -> None:
        source = EnvConfigSource(env={})
        assert source.poll() is None
        assert source.last_version is None

    def test_adopts_env_json_once(self) -> None:
        source = EnvConfigSource(env={"QI_CONFIG_JSON": _JSON_A})
        assert source.poll() == _CONFIG_A
        assert source.poll() is None  # unchanged value short-circuits

    def test_env_change_adopts_new_config_once(self) -> None:
        env: dict[str, str] = {"QI_CONFIG_JSON": _JSON_A}
        source = EnvConfigSource(env=env)
        assert source.poll() == _CONFIG_A
        env["QI_CONFIG_JSON"] = _JSON_B
        assert source.poll() == _CONFIG_B
        assert source.poll() is None

    def test_custom_key(self) -> None:
        source = EnvConfigSource(env={"MY_CONFIG": _JSON_A}, key="MY_CONFIG")
        assert source.poll() == _CONFIG_A

    def test_invalid_json_returns_none(self, caplog) -> None:
        source = EnvConfigSource(env={"QI_CONFIG_JSON": "{oops"}, logger=_LOG)
        with caplog.at_level(logging.ERROR, logger="testcfg"):
            assert source.poll() is None
        assert "invalid payload" in caplog.text


# ---------------------------------------------------------------------------
# ConfigSync shell + backend selection by configuration
# ---------------------------------------------------------------------------


class TestConfigSyncShell:
    def test_poll_returns_adopted_config(self, tmp_path: Path) -> None:
        path = tmp_path / "strategies_config.json"
        path.write_text(_JSON_A)
        sync = ConfigSync(source=FileConfigSource(path=path))
        assert sync.poll() == _CONFIG_A
        assert sync.last_version is not None
        assert sync.poll() is None

    def test_from_backend_file(self, tmp_path: Path) -> None:
        path = tmp_path / "strategies_config.json"
        path.write_text(_JSON_A)
        sync = ConfigSync.from_backend("file", path=path)
        assert sync.poll() == _CONFIG_A

    def test_from_backend_env(self) -> None:
        sync = ConfigSync.from_backend("env", env={"QI_CONFIG_JSON": _JSON_B})
        assert sync.poll() == _CONFIG_B

    def test_validate_static(self) -> None:
        assert ConfigSync.validate(_CONFIG_A) is True
        assert ConfigSync.validate({}) is False


class TestBackendSelection:
    def test_backend_switch_by_config(self, tmp_path: Path) -> None:
        from quant.config.ssm import SSMConfigSource

        file_source = config_source_by_name("file", path=tmp_path / "c.json")
        assert isinstance(file_source, FileConfigSource)
        env_source = config_source_by_name("env")
        assert isinstance(env_source, EnvConfigSource)
        ssm_source = config_source_by_name(
            "ssm", param_name="/x", region_name="us-east-1"
        )
        assert isinstance(ssm_source, SSMConfigSource)

    def test_unknown_backend_raises(self) -> None:
        with pytest.raises(ValueError, match="Unknown config backend"):
            config_source_by_name("kafka")

    def test_register_source_override(self) -> None:
        class _Dummy(FileConfigSource):
            name = "dummy"

        register_source("file", _Dummy)  # override then restore
        try:
            assert isinstance(config_source_by_name("file", path="x.json"), _Dummy)
        finally:
            register_source("file", FileConfigSource)


# ---------------------------------------------------------------------------
# Zero-AWS base install (boto3 completely unavailable)
# ---------------------------------------------------------------------------


class TestZeroAws:
    def test_local_first_with_boto3_blocked(self, monkeypatch, tmp_path: Path) -> None:
        """The local-first defaults must work with boto3 uninstalled."""
        real_import = builtins.__import__

        def _block_boto3(name, *args, **kwargs):
            if name == "boto3" or name.startswith("boto3."):
                raise ImportError("boto3 not installed (base install)")
            return real_import(name, *args, **kwargs)

        monkeypatch.setattr(builtins, "__import__", _block_boto3)

        # package import must not need boto3
        import quant.config

        importlib.reload(quant.config)

        # file backend full cycle
        path = tmp_path / "strategies_config.json"
        path.write_text(_JSON_A)
        sync = ConfigSync(source=FileConfigSource(path=path))
        assert sync.poll() == _CONFIG_A
        assert sync.poll() is None

        # env backend full cycle
        sync = ConfigSync(source=EnvConfigSource(env={"QI_CONFIG_JSON": _JSON_B}))
        assert sync.poll() == _CONFIG_B

        # ssm backend resolves lazily and degrades to None (no boto3) — no raise
        ssm = config_source_by_name("ssm", param_name="/x", region_name="us-east-1")
        assert ssm.name == "ssm"
        assert ssm.poll() is None
