"""Runtime-config primitives (spec `runtime-config`, design D6).

Local-first by default: ``FileConfigSource`` and ``EnvConfigSource`` ship as
the backends with zero AWS dependencies. The SSM backend
(``quant.config.ssm.SSMConfigSource``, ``quant[ssm]`` extra) is resolved
lazily via ``config_source_by_name("ssm")`` so importing this package never
imports boto3. ``ConfigSync`` is the generic version-tracking/validation/apply
shell the live runtime polls once per tick; backend selection is by config.
"""

from quant.config._registry import config_source_by_name, register_source
from quant.config.base import (
    BaseConfigSource,
    ConfigPayloadError,
    ConfigSource,
    parse_config,
    validate_config,
)
from quant.config.env import EnvConfigSource
from quant.config.file import FileConfigSource
from quant.config.sync import ConfigSync

# SSMConfigSource is intentionally NOT imported here: `quant.config.ssm` only
# imports boto3 lazily on first use, so `import quant.config` stays boto3-free.
# Resolve the SSM backend with `config_source_by_name("ssm", ...)` or via
# `from quant.config.ssm import SSMConfigSource`.

__all__ = [
    "BaseConfigSource",
    "ConfigPayloadError",
    "ConfigSource",
    "ConfigSync",
    "EnvConfigSource",
    "FileConfigSource",
    "config_source_by_name",
    "parse_config",
    "register_source",
    "validate_config",
]
