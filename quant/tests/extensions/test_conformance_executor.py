"""Executor-port conformance suite (spec `execution`, design D5; D13; task 11.4).

Runs the executor contract against both roots: a conforming executor must
implement the full `ExecutorProtocol` surface (the single environment seam the
engine routes through). Qi's shipped backtest/live executors pass by
construction; a replacement missing part of the surface (e.g. no `enter`) fails
loudly.
"""

from __future__ import annotations

from quant.core.protocols import ExecutorProtocol

from ._conformance import ROOTS, run_against_roots


def conform(executor, root) -> None:
    assert isinstance(executor, ExecutorProtocol), (
        f"{root.label}: executor must implement the full ExecutorProtocol"
    )
    assert callable(executor.enter) and callable(executor.exit), (
        f"{root.label}: executor must expose enter/exit"
    )


# ── adapters ───────────────────────────────────────────────────────────────
class ConformingExecutor:
    """A minimal but complete executor surface (conforms to ExecutorProtocol)."""

    def should_run(self, strategy, now):
        return True, True

    def data_end_dt(self, now):
        import pandas as pd

        return pd.Timestamp.now()

    def get_precomputed_signal(self, ticker, now):
        return None

    def active_positions(self, strategy_name):
        return []

    def enter(self, *args, **kwargs):
        return None

    def exit(self, *args, **kwargs):
        return None

    def after_entry(self, strategy_name, position):
        return None

    def after_exit(self, strategy_name, position):
        return None

    def after_process_ticker(self, *args, **kwargs):
        return None

    def record_run(self, strategy_name, now, ran_signal):
        return None


class ViolatingExecutor:
    """Missing the `enter` method — must be rejected as non-conforming."""

    def should_run(self, strategy, now):
        return True, True


# ── both roots ─────────────────────────────────────────────────────────────
def test_conforming_executor_passes_on_both_roots() -> None:
    results = run_against_roots(lambda root: conform(ConformingExecutor(), root))
    assert results == {"backtest": None, "live": None}


def test_violating_executor_fails_loudly() -> None:
    for root in ROOTS:
        try:
            conform(ViolatingExecutor(), root)
        except AssertionError:
            continue
        raise AssertionError(f"{root.label}: violating executor must fail loudly")
