"""Dependency surface tests (group 7.4 / 7.2).

Guards the lean base install:
  - the base import graph (core/backtest/config/portfolio/venue/utils/
    observability) never pulls ``alpaca``/``boto3`` at import time (brokerless);
  - no shipped module imports the dropped ``ta-lib`` / ``numba`` indicator
    stack (7.2) — the indicator library + cpp_native were already cut.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

_SRC = Path(__file__).resolve().parents[1] / "src" / "quant"

_BASE_MODULES = (
    "quant",  # package __init__ (empty) + the import chain below
    "quant.core",
    "quant.backtest",
    "quant.config",
    "quant.portfolio",
    "quant.venue",
    "quant.utils",
    "quant.observability",
)


def test_base_import_graph_is_brokerless():
    """A fresh interpreter importing the base package pulls no broker/AWS/JIT."""
    imports = ";".join(f"import {m}" for m in _BASE_MODULES)
    code = (
        "import sys\n" + imports + "\n" + "print('PULLED=' + ','.join(x for x in "
        "('alpaca','boto3','numba','talib','lightgbm') if x in sys.modules))\n"
    )
    out = subprocess.check_output(  # noqa: S603 - argv list, no shell
        [sys.executable, "-c", code], text=True
    )
    pulled = out.split("PULLED=", 1)[-1].strip()
    assert pulled == "", f"base import graph pulled optional deps: {pulled!r}"


def test_base_imports_with_optional_runtime_blocked():
    """Blocking the optional extras still imports the base package (brokerless).

    Runs in a fresh interpreter with ``__import__`` patched to refuse
    alpaca/boto3/numba/talib — if the base graph needed any of them at import
    time, this subprocess would fail with ImportError.
    """
    guard = (
        "import builtins\n"
        "_real = builtins.__import__\n"
        "_blocked = {'alpaca','boto3','numba','talib','lightgbm'}\n"
        "def _guard(name, *a, **k):\n"
        "    r = name.split('.')[0]\n"
        "    if r in _blocked: raise ImportError(f'{name} blocked')\n"
        "    return _real(name, *a, **k)\n"
        "builtins.__import__ = _guard\n"
    )
    imports = ";".join(f"import {m}" for m in _BASE_MODULES)
    code = guard + imports + "\nprint('OK')\n"
    out = subprocess.check_output(  # noqa: S603 - argv list, no shell
        [sys.executable, "-c", code], text=True
    )
    assert "OK" in out


def test_no_shipped_module_imports_ta_lib_or_numba():
    """7.2: the indicator library is gone — no shipped module imports it."""
    offenders: list[str] = []
    for f in sorted(_SRC.rglob("*.py")):
        text = f.read_text(encoding="utf-8")
        for line in text.splitlines():
            stripped = line.strip()
            if (stripped.startswith("import ") or stripped.startswith("from ")) and (
                "talib" in stripped or " numba" in stripped
            ):
                offenders.append(f"{f.relative_to(_SRC)}: {stripped}")
    assert not offenders, "shipped modules still import ta-lib/numba:\n" + "\n".join(
        offenders
    )
