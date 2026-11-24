"""Runtime-config primitives — the shared source contract and payload validation.

Configuration is a passive, version-checked poll with pluggable backends
(spec `runtime-config`). ``ConfigSource`` is the backend protocol: a single
cheap backend call (file stat / SSM get / env check) that reports the current
``(version, raw payload)`` pair. ``BaseConfigSource`` supplies the generic poll
machinery every backend shares:

* **version short-circuit** — an unchanged version returns ``None`` without
  re-parsing the payload (the hot path stays one lightweight backend call);
* **adopt-once validation** — an invalid payload is refused while its version
  is committed, so the error is logged once per version and prior config stays
  active (the operator bumps the version to retry);
* **silent poll errors** — a backend error returns ``None`` and prior config
  stays active, never raising.

Backend-specific behavior (SSM IMDS region + ``Version`` semantics, boto3)
lives in its own module (``quant.config.ssm``) — not here in the core.
"""

from __future__ import annotations

import json
import logging
from typing import Any, Protocol, runtime_checkable

_LOG = logging.getLogger(__name__)

# Backend version token: int (SSM Version), (mtime_ns, size) (file), str (env).
ConfigVersion = Any


class ConfigPayloadError(ValueError):
    """A raw backend payload could not be parsed or failed schema validation."""


@runtime_checkable
class ConfigSource(Protocol):
    """Pluggable runtime-config backend (spec `runtime-config`).

    ``scan()`` is the single lightweight backend call. It returns
    ``(version, raw_payload)`` on success and ``None`` on any poll error — it
    must never raise, so poll errors stay silent and the previously-adopted
    config stays active. ``poll()`` is the spec contract: a validated config
    dict only when the backend's content version changed, ``None`` otherwise.
    """

    name: str

    def scan(self) -> tuple[Any, Any] | None:
        """Return ``(current_version, raw_payload)`` or ``None`` on poll error."""
        ...

    def poll(self, logger: logging.Logger | None = None) -> dict[str, Any] | None:
        """Validated config only on version change; ``None`` unchanged/on error."""
        ...


def validate_config(config: Any) -> bool:
    """Schema guard: a dict with a ``strategies`` list of ``{name: str, ...}``.

    An empty ``strategies`` list is valid — a "pause all" payload. Reference
    resolution (venue/strategy names) happens at apply time in the runtime's
    strategies layer, not in the generic shell.
    """
    if not isinstance(config, dict):
        return False
    strategies = config.get("strategies")
    if not isinstance(strategies, list):
        return False
    return all(
        isinstance(entry, dict) and isinstance(entry.get("name"), str)
        for entry in strategies
    )


def parse_config(raw: Any) -> dict[str, Any]:
    """Coerce a backend payload to a validated config dict.

    Accepts a JSON ``str``/``bytes`` payload or an already-parsed dict; raises
    :class:`ConfigPayloadError` on any parse or schema failure.
    """
    if isinstance(raw, (bytes, bytearray)):
        raw = bytes(raw).decode("utf-8")
    if isinstance(raw, str):
        try:
            raw = json.loads(raw)
        except json.JSONDecodeError as e:
            raise ConfigPayloadError(f"JSON parse error: {e}") from e
    if not validate_config(raw):
        raise ConfigPayloadError("payload failed schema validation")
    return raw


class BaseConfigSource:
    """Generic poll machinery shared by every backend.

    Subclasses set ``name`` and implement :meth:`scan` (the single cheap backend
    call). Do not override :meth:`poll` — it implements the spec contract.
    """

    name: str = "config"

    def __init__(self, *, logger: logging.Logger | None = None) -> None:
        self._config_logger = logger
        self._last_version: Any = None

    @property
    def last_version(self) -> Any | None:
        """The backend version last seen (``None`` before the first poll)."""
        return self._last_version

    def scan(self) -> tuple[Any, Any] | None:  # pragma: no cover - abstract
        raise NotImplementedError

    def poll(self, logger: logging.Logger | None = None) -> dict[str, Any] | None:
        """Return a validated config dict only when the backend version changed.

        * unchanged version -> ``None`` (payload never touched or re-parsed);
        * backend error (``scan()`` -> ``None``) -> ``None``, silent, prior
          config stays active;
        * new version that fails parse/validation -> version committed, error
          logged once, ``None`` (invalid-payload-once).

        Never raises.
        """
        lg = logger or self._config_logger or _LOG
        scanned = self.scan()
        if scanned is None:
            return None
        version, raw = scanned
        if version == self._last_version:
            return None
        try:
            config = parse_config(raw)
        except ConfigPayloadError as e:
            self._last_version = version  # commit: log once, operator bumps to retry
            lg.error(
                "[%s] v%s invalid payload — keeping prior config: %s",
                self.name,
                version,
                e,
            )
            return None
        self._last_version = version
        lg.info("[%s] config updated (version %s).", self.name, version)
        return config
