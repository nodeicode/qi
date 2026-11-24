"""Deterministic gate: quant submodule import layering.

The submodule enforces one-way layering (see ``openspec/changes/...`` and the
``quant-boundary`` capability):

* ``quant.utils/`` is a leaf package — it must import only its own sibling
  modules (``quant.utils.*``) and the standard library, never ``quant.core`` /
  ``quant.data`` / ``quant.backtest`` / ``quant.strategies``.
* ``quant.backtest`` is the simulator — a **terminal** consumer.  Nothing
  outside ``src/quant/backtest/**`` may import it (except a documented
  ``# layering-ok`` shadow-signal bridge in ``core/monitor_utils.py``).
* ``quant.strategies`` is private alpha — nothing outside
  ``src/quant/strategies/**`` (or the monitor's single noqa'd dynamic-load
  line) may import it.

These rules are enforced by ruff ``TID251`` ``banned-api`` in ``pyproject.toml``
AND scanned here at the AST level, so a missing ``banned-api`` entry is still
caught.  Any sanctioned exception must carry an inline ``# layering-ok`` on the
import line and be mirrored here.
"""

from __future__ import annotations

import ast
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[1] / "src" / "quant"

# Top-level packages exempt from the backtest/strategies bans (mirror the
# per-file-ignores in pyproject.toml).
_EXEMPT_TOPLVL = {"backtest", "strategies"}


def _quant_imports(tree: ast.Module):
    """Yield (module, node) for every ``from quant.X import ...`` statement."""
    for node in ast.walk(tree):
        if (
            isinstance(node, ast.ImportFrom)
            and node.module
            and node.module.startswith("quant")
        ):
            yield node
        elif isinstance(node, ast.Import):
            for alias in node.names:
                if alias.name.startswith("quant"):
                    yield _ImportShim(alias.name, node)


class _ImportShim:
    def __init__(self, module: str, node) -> None:
        self.module = module
        self.lineno = node.lineno


def test_quant_utils_is_leaf_package():
    """quant/utils/** may import only quant.utils.* and stdlib."""
    offenders: list[str] = []
    for f in sorted((_ROOT / "utils").rglob("*.py")):
        tree = ast.parse(f.read_text())
        for imp in _quant_imports(tree):
            parts = imp.module.split(".")
            if parts[1] != "utils":
                offenders.append(f"{f.relative_to(_ROOT)}:{imp.lineno}: {imp.module}")
    assert not offenders, (
        "quant/utils imported a non-utils quant module:\n" + "\n".join(offenders)
    )


def test_no_backtest_or_strategy_leaks_outside_terminal_packages():
    """core/data (and any top-level module) must not import quant.backtest /
    quant.strategies unless the import line carries an inline ``# layering-ok``."""
    lines_cache: dict[Path, list[str]] = {}

    def _lines(p: Path) -> list[str]:
        if p not in lines_cache:
            lines_cache[p] = p.read_text().splitlines()
        return lines_cache[p]

    offenders: list[str] = []
    for f in sorted(_ROOT.rglob("*.py")):
        rel = f.relative_to(_ROOT).as_posix()
        parts = rel.split("/")
        # utils handled by the leaf test (strict); backtest/strategies are terminal consumers.
        if parts[0] in _EXEMPT_TOPLVL or parts[0] == "utils":
            continue
        tree = ast.parse(f.read_text())
        for imp in _quant_imports(tree):
            modparts = imp.module.split(".")
            if modparts[1] in _EXEMPT_TOPLVL:  # banned package
                line = (
                    _lines(f)[imp.lineno - 1] if imp.lineno - 1 < len(_lines(f)) else ""
                )
                if "layering-ok" not in line:
                    offenders.append(
                        f"{rel}:{imp.lineno}: {imp.module} (no inline # layering-ok)"
                    )
    assert not offenders, (
        "quant.backtest / quant.strategies imported outside the terminal packages "
        "without a documented noqa:\n" + "\n".join(offenders)
    )
