"""Local-file config backend — the zero-AWS default (spec `runtime-config`)."""

from __future__ import annotations

import logging
import os
from pathlib import Path
from typing import Any

from quant.config.base import BaseConfigSource


class FileConfigSource(BaseConfigSource):
    """Watches a local JSON config file; version = ``(mtime_ns, size)`` stat.

    The hot path is a single ``stat``; the file is read and parsed only when
    the version changed. An absent/unreadable file yields a silent ``None`` so
    a runtime with no local file starts immediately with no config (zero-AWS).
    """

    name = "file"

    def __init__(
        self,
        path: str | os.PathLike[str] | Path,
        *,
        logger: logging.Logger | None = None,
    ) -> None:
        super().__init__(logger=logger)
        self._path = Path(path)

    def scan(self) -> tuple[Any, Any] | None:
        try:
            st = self._path.stat()
        except OSError:
            return None  # missing/unreadable -> silent no-config (zero-AWS grace)
        version = (st.st_mtime_ns, st.st_size)
        if version == self._last_version:
            return version, None  # unchanged -> shell short-circuits, nothing re-read
        try:
            raw = self._path.read_text(encoding="utf-8")
        except OSError:
            return None  # vanished between stat/read -> treat as a poll error
        return version, raw
