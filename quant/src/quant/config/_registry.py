"""Config-backend registration and config-driven selection (spec `runtime-config`)."""

from __future__ import annotations

from typing import Any

from quant.config.base import ConfigSource
from quant.config.env import EnvConfigSource
from quant.config.file import FileConfigSource

_REGISTRY: dict[str, type[ConfigSource]] = {
    "env": EnvConfigSource,
    "file": FileConfigSource,
    # "ssm" is registered lazily on first request (see config_source_by_name)
    # so `import quant.config` never touches boto3 — SSM is the quant[ssm]
    # extra backend.
}


def register_source(name: str, factory: type[ConfigSource]) -> None:
    """Register (or override) a named config backend for config-driven selection."""
    if not isinstance(name, str) or not name:
        raise ValueError("config backend name must be a non-empty string")
    _REGISTRY[name] = factory


def config_source_by_name(name: str, **overrides: Any) -> ConfigSource:
    """Resolve a config backend by name (built-ins ``file``/``env``; ``ssm`` lazily)."""
    if name == "ssm" and "ssm" not in _REGISTRY:
        from quant.config.ssm import SSMConfigSource  # lazy: quant[ssm] extra

        _REGISTRY["ssm"] = SSMConfigSource
    factory = _REGISTRY.get(name)
    if factory is None:
        raise ValueError(
            f"Unknown config backend {name!r}; registered backends: {sorted(_REGISTRY)}"
        )
    return factory(**overrides)
