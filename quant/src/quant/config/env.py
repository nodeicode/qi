"""Environment config backend — the other zero-AWS default (spec `runtime-config`)."""

from __future__ import annotations

import logging
import os
from collections.abc import Mapping
from typing import Any

from quant.config.base import BaseConfigSource

_ENV_KEY = "QI_CONFIG_JSON"


class EnvConfigSource(BaseConfigSource):
    """Reads live config from the ``QI_CONFIG_JSON`` environment variable.

    The env value doubles as the version token: an unchanged value short-
    circuits exactly like any other backend version, and an unset variable is
    a silent, no-config (zero-AWS) state. The hot path is a single env lookup.
    """

    name = "env"

    def __init__(
        self,
        *,
        env: Mapping[str, str] | None = None,
        key: str = _ENV_KEY,
        logger: logging.Logger | None = None,
    ) -> None:
        super().__init__(logger=logger)
        self._env = os.environ if env is None else env
        self._key = key

    def scan(self) -> tuple[Any, Any] | None:
        value = self._env.get(self._key)
        if value is None:
            return None  # unset -> silent no-config (zero-AWS)
        return value, value  # the value is both the version token and the payload
