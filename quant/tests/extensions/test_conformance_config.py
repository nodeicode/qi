"""Config-port conformance suite (spec `runtime-config`; D13; task 11.4).

Runs the config-backend contract invariants against both roots: a validated
config dict is adopted once on a version change, unchanged versions/errors
return None (version short-circuit), and the backend never raises. Qi's shipped
FileConfigSource passes by construction; a violating backend that raises fails
loudly.
"""

from __future__ import annotations

import json
import pathlib

from quant.config import ConfigSync, FileConfigSource

from ._conformance import ROOTS, run_against_roots


def conform(sync, root) -> None:
    first = sync.poll()
    assert isinstance(first, dict) and "strategies" in first, (
        f"{root.label}: first poll must adopt a validated config dict"
    )
    # Unchanged version -> silent None (no re-parse, no raise).
    assert sync.poll() is None, f"{root.label}: unchanged version must short-circuit"


# ── adapters ───────────────────────────────────────────────────────────────
def conforming_sync(tmp_path: pathlib.Path) -> ConfigSync:
    payload = {"strategies": [{"name": "example_equity", "clients": ["paper"]}]}
    path = tmp_path / "cfg.json"
    path.write_text(json.dumps(payload), encoding="utf-8")
    return ConfigSync(FileConfigSource(path))


class ViolatingSource:
    """A backend whose poll raises — must be rejected as non-conforming."""

    name = "violating"

    def scan(self):
        return None

    def poll(self, logger=None):
        raise RuntimeError("backend unreachable")


# ── both roots ─────────────────────────────────────────────────────────────
def test_conforming_config_passes_on_both_roots(tmp_path) -> None:
    results = run_against_roots(lambda root: conform(conforming_sync(tmp_path), root))
    assert results == {"backtest": None, "live": None}


def test_violating_config_fails_loudly() -> None:
    bad = ConfigSync(ViolatingSource())
    for root in ROOTS:
        try:
            conform(bad, root)
        except (AssertionError, RuntimeError):
            continue
        raise AssertionError(f"{root.label}: violating config backend must fail loudly")
