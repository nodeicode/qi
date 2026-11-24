---
description: "Use when installing dependencies, running tests, managing the Python environment, or executing any shell command in the quant package. Covers uv commands, venv setup, and CI workflow."
applyTo: pyproject.toml, uv.lock, .github/workflows/**
---

# Package Manager — uv (not pip)

This project uses **uv** exclusively. Never use `pip install`, `pip freeze`, or `python -m pytest`.

## Install Dependencies

```bash
uv sync
```

This creates the venv and installs all core + dev dependencies (pytest, ruff, marimo, etc.).
The dev group is the default sync group (PEP 735 `[dependency-groups]` in pyproject.toml).

## Run Tests

```bash
uv run pytest tests -v --tb=short
```

## Add a Dependency

```bash
uv add <pkg>==<version>
uv lock
```

All dependencies must be pinned to exact versions (`==`).

## Python Version

Python 3.12 only. If not installed:

```bash
uv python install 3.12
```

## Key Rules

- **Never** use `pip` or `conda` — `uv sync` is the only install path.
- **Never** run `pytest` directly — always `uv run pytest` to use the correct venv.
- **Never** widen `requires-python` without clearing all Numba cache files first.
- Lock file (`uv.lock`) must be committed with any dependency change.
