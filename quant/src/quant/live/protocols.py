"""
Runtime-checkable Protocols for the python/ live-trading module.

These protocols replace ``Any`` in function signatures so the type checker
can verify call sites while keeping the code decoupled from concrete
implementation classes.

Usage
-----
    from quant.live.protocols import EquityManagerProtocol, DataHandlerProtocol, LiveStrategyProtocol
"""

from __future__ import annotations

from typing import Any, Protocol, TypeGuard, runtime_checkable

import pandas as pd

from quant.core.protocols import PositionDict, StrategyProtocol
from quant.utils.strategy_config import BaseStrategyConfig

# ---------------------------------------------------------------------------
# Equity accounting — EquityManager and MultiClientEquityManager both satisfy
# ---------------------------------------------------------------------------


@runtime_checkable
class EquityManagerProtocol(Protocol):
    """Shared interface for EquityManager and MultiClientEquityManager."""

    def get(self) -> float: ...
    def debit(self, amount: float) -> None: ...
    def credit(self, amount: float) -> None: ...


# ---------------------------------------------------------------------------
# Market-data source — AlpacaDataHandler satisfies; mocks satisfy in tests
# ---------------------------------------------------------------------------


@runtime_checkable
class DataHandlerProtocol(Protocol):
    """Minimal interface for a market-data provider consumed by fetch_or_update_cache."""

    def get_historical_data(
        self,
        ticker: str,
        start: str,
        end: str,
        interval: str,
    ) -> pd.DataFrame: ...


# ---------------------------------------------------------------------------
# Live strategy — satisfied by every strategy after load_strategies() runs.
# Attributes are injected by strategy_loader.load_strategies():
#   name        : "<strategy_name>_<client_name>"  (unique instance key)
#   env         : "paper" | "prod"
#   client_name : registered broker client key
#   config      : strategy-specific config (a ``BaseStrategyConfig`` subclass) —
#                 pass it directly to core helpers (no ``object`` erasure).
# ---------------------------------------------------------------------------


@runtime_checkable
class LiveStrategyProtocol(StrategyProtocol, Protocol):
    """Interface for live strategy instances used across the python/ module."""

    name: str
    env: str
    client_name: str
    config: BaseStrategyConfig


# ---------------------------------------------------------------------------
# Live position schema — extends PositionDict with live-execution fields.
# Use as the return type for executor enter_trade methods.
# ---------------------------------------------------------------------------


class LivePositionDict(PositionDict, total=False):
    """
    Full position record as built by live executors.

    The single sanctioned ``PositionDict`` subclass in the codebase: it extends
    the backtest-compatible shared contract with live-only execution/broker
    fields (Strategy, Ticker, Env, Qty, PositionSize, Vol_Ratio, OrderIds) that
    are passed as ``**extra`` kwargs to ``engine.build_position``.

    A record is only trustworthy as a live position once it passes
    ``is_live_position`` — do not construct or ``cast`` one without running
    that boundary guard. Option/iron-condor envelope fields were removed with
    the equity-only trim; unknown strategy fields ride the record as flat
    extras; do not add further subclasses.
    """

    # ── Common live fields ──────────────────────────────────────────────────
    Strategy: str
    Ticker: str
    Qty: int
    Env: str
    PositionSize: float
    Vol_Ratio: float
    OrderIds: dict[str, Any]


def is_live_position(position: PositionDict) -> TypeGuard[LivePositionDict]:
    """Boundary lift: true when *pos* carries the live-equity execution fields.

    Validates (never ``cast``) the keys the live executor and trader need to
    close and account for a position, so a partial record from crash recovery
    or a malformed producer cannot be silently treated as a tradable live
    position (fail closed on that record).  The option/iron-condor envelope
    fields were removed with the equity-only trim.
    """
    required = ("Strategy", "Ticker", "Env", "Qty", "OrderId")
    return all(key in position for key in required)
