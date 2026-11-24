"""Console entry point for ``uv run test``.

``quant.tests`` is a repo-local package (never shipped in the wheel), so the
``test`` console script imports the runner by file path instead of by package
import. The runner itself stays cwd-aware (see ``quant/tests/_runner.py``), so
``uv run test`` works from the repo root, from ``quant/``, and from ``python/``.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path


def main() -> None:
    runner = Path(__file__).resolve().parents[3] / "quant" / "tests" / "_runner.py"
    spec = importlib.util.spec_from_file_location("quant_tests_runner", runner)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"could not locate test runner at {runner}")
    mod = importlib.util.module_from_spec(spec)
    sys.modules["quant_tests_runner"] = mod
    spec.loader.exec_module(mod)
    sys.exit(mod.main())


if __name__ == "__main__":
    main()
