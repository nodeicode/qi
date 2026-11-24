"""ConfigSync — the generic version-tracking/validation/apply shell.

The live runtime's main loop calls :meth:`ConfigSync.poll` once per tick.
Unchanged backend versions and any poll error short-circuit to ``None``
without re-parsing, so the hot path is a single lightweight backend call. A
changed version whose payload validates is returned **exactly once** for the
runtime to apply synchronously between ticks (no threads, no callbacks, no
locks). An invalid payload is logged once per version and never adopted — the
operator bumps the version to retry.

Backend selection is by configuration: bind an explicit ``ConfigSource``
(``ConfigSync(source=...)``) or resolve one by name
(``ConfigSync.from_backend("file" | "env" | "ssm")``). Applying an adopted
config — add/remove strategy instances, per-strategy overrides, cache
invalidation — belongs to the runtime's strategies layer
(``quant.live.trader._apply_config_update``), not to this generic shell, so
the import layering stays clean.
"""

from __future__ import annotations

import logging
from typing import Any

from quant.config._registry import config_source_by_name
from quant.config.base import (
    ConfigSource,
    parse_config,
    validate_config,
)

_LOG = logging.getLogger(__name__)

BackendOverrides = dict[str, Any]


class ConfigSync:
    """Passive poller over any :class:`quant.config.base.ConfigSource`.

    ``poll()`` is the spec `runtime-config` contract: a validated config dict
    only when the backend's content version changed, ``None`` otherwise, never
    raising.
    """

    def __init__(
        self,
        source: ConfigSource,
        *,
        logger: logging.Logger | None = None,
    ) -> None:
        self.source = source
        self.logger = logger or _LOG

    @property
    def last_version(self) -> Any | None:
        """Last backend version seen (``None`` before the first adoption)."""
        return getattr(self.source, "last_version", None)

    @classmethod
    def from_backend(
        cls,
        backend: str,
        *,
        logger: logging.Logger | None = None,
        **overrides: Any,
    ) -> ConfigSync:
        """Select the config backend by name (spec `runtime-config` scenario)."""
        return cls(source=config_source_by_name(backend, **overrides), logger=logger)

    def poll(self) -> dict[str, Any] | None:
        """Poll the backend; return a validated config dict only on adoption."""
        config = self.source.poll(self.logger)
        if config is not None:
            self.logger.info(
                "[ConfigSync] config update adopted (%s backend).", self.source.name
            )
        return config

    @staticmethod
    def validate(config: Any) -> bool:
        """Schema guard, kept from the original SSM poller surface."""
        return validate_config(config)


__all__ = [
    "BackendOverrides",
    "ConfigSync",
    "config_source_by_name",
    "parse_config",
    "validate_config",
]
