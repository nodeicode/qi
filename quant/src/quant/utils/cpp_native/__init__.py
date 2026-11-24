"""Native nanobind kernels for ``quant/utils`` hot paths.

The compiled ``vq_kernels.cpython-<ver>-<platform>.so`` is a per-machine build
artifact (OS / arch / CPython-version specific) and is deliberately **not**
committed to git — the repository ``.gitignore`` excludes
``utils/cpp_native/*.so`` and ``utils/cpp_native/build/``. So that
``import quant.utils.indicators`` "just works" on a fresh checkout without a
manual build step, this package lazily compiles the extension in-place the
first time it is imported whenever the binary is missing.

The extension is a **required** dependency for ``quant.utils.indicators`` —
there is no pure-Python fallback. If the binary is missing and cannot be
built, importing this package raises ``ImportError`` (it never silently
resolves to ``None``).

Behaviour / guarantees:

* If ``vq_kernels`` is already importable, nothing is built (fast import path).
* Otherwise we attempt a one-time in-place ``setup.py build_ext --inplace`` via
  a subprocess using the current interpreter (requires ``nanobind`` and a C++
  compiler). On any failure (no toolchain, read-only tree, missing
  ``QUANT_NO_AUTOBUILD`` binary, …) the import raises ``ImportError`` with a
  clear message.
* The build is idempotent and thread-safe: guarded by a module lock and it
  runs at most once per process.

Set ``QUANT_NO_AUTOBUILD=1`` in environments where the source tree is
read-only or a compiler must not be invoked (e.g. production / container
images where the extension is pre-built into the image). In those
environments a missing extension is a deployment error and raises.
"""

from __future__ import annotations

import os
import subprocess
import sys
import threading
from pathlib import Path

__all__ = ["vq_kernels"]

_PKG_DIR = Path(__file__).resolve().parent
# Top-level package root is <repo>/quant (cpp_native -> utils -> quant). The
# extension is declared as ``quant.utils.cpp_native.vq_kernels``, so setuptools
# resolves the in-place ``.so`` destination *relative to the directory that
# contains ``quant/``* (the repo root), not to this package dir. Running the
# build from any other cwd makes the final copy fail with
# "could not create 'quant/utils/cpp_native/...': No such file or directory".
_BUILD_CWD = _PKG_DIR.parent.parent.parent
_BUILD_LOCK = threading.Lock()
# Mutable module-level guard (one build attempt per process) — a list avoids
# the discouraged `global` statement (PLW0603) in _ensure_built.
_BUILD_ATTEMPTED: list[bool] = [False]


class _NativeUnavailableError(ImportError):
    """Raised when the compiled native extension is required but unavailable."""


def _binary_present() -> bool:
    return any(_PKG_DIR.glob("vq_kernels*.so"))


def _fail(message: str) -> None:
    raise _NativeUnavailableError(message)


def _ensure_built() -> None:
    """Build the extension in-place on demand (raise if it cannot load)."""
    if _binary_present() or _BUILD_ATTEMPTED[0]:
        return
    if os.environ.get("QUANT_NO_AUTOBUILD"):
        _BUILD_ATTEMPTED[0] = True
        _fail(
            "QUANT_NO_AUTOBUILD is set but no compiled 'vq_kernels' extension "
            f"is present in {_PKG_DIR}. The native kernels are required (no "
            "pure-Python fallback); build it in the image or unset the flag."
        )
    with _BUILD_LOCK:
        if _binary_present() or _BUILD_ATTEMPTED[0]:
            return
        _BUILD_ATTEMPTED[0] = True
        try:
            import nanobind  # noqa: F401  (build-time dep; cheap early out)
        except Exception as exc:
            _fail(
                "nanobind is not installed; cannot build the required "
                f"'vq_kernels' extension in {_PKG_DIR}: {exc}"
            )
        prop = subprocess.run(  # noqa: S603  (trusted, fixed command list)
            [sys.executable, str(_PKG_DIR / "setup.py"), "build_ext", "--inplace"],
            cwd=str(_BUILD_CWD),
            check=False,
            capture_output=True,
        )
        if not _binary_present() or prop.returncode != 0:
            _fail(
                "Failed to build the required 'vq_kernels' native extension in "
                f"{_PKG_DIR} (setup.py build_ext exit={prop.returncode}). "
                "Build output:\n" + prop.stderr.decode("utf-8", "replace")[-2000:]
            )


_ensure_built()

from quant.utils.cpp_native import vq_kernels  # noqa: E402  (import after build)
