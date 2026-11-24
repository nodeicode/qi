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
    fields (Strategy, Ticker, Env, OccSymbol, IC legs, OrderIds) that are passed
    as **extra kwargs to ``engine.build_position`` / ``engine.build_ic_position``.

    A record is only trustworthy as a live IC position once it passes
    ``is_live_ic_position`` — do not construct or ``cast`` one without running
    that boundary guard. Unknown strategy/options fields use the shared
    ``quant.utils.options.ICMeta`` envelope, not additional subclasses.
    """

    # ── Common live fields ──────────────────────────────────────────────────
    Strategy: str
    Ticker: str
    OccSymbol: str
    Qty: int
    Env: str
    PositionSize: float
    Vol_Ratio: float

    # ── Option-specific ─────────────────────────────────────────────────────
    OptionType: str
    Strike: float
    Expiration: str

    # ── Iron Condor specific ─────────────────────────────────────────────────
    IC_Direction: int
    K_LP: float
    K_SP: float
    K_SC: float
    K_LC: float
    OccSymbol_LP: str
    OccSymbol_SP: str
    OccSymbol_SC: str
    OccSymbol_LC: str
    NContracts: int
    FillCredit: float
    FillCredit_SP: float
    FillCredit_SC: float
    FillDebit_LP: float
    FillDebit_LC: float
    NetCreditDollars: float
    Collateral: float
    OrderIds: dict[str, Any]
    EntryMid: float
    EntrySlippage: float
    EntryRefCredit: float
    EntrySlippageFromRef: float
    # Exit fill-loop config — persisted at entry so close uses same parameters.
    IC_FILL_STEP_PCT: float
    IC_EXIT_MAX_ATTEMPTS: int
    IC_EXIT_MAX_WAIT_SECS: int
    IC_EXIT_MIN_POLL_SECS: int


def is_live_ic_position(position: PositionDict) -> TypeGuard[LivePositionDict]:
    required = (
        "Strategy",
        "Ticker",
        "Env",
        "OccSymbol",
        "Expiration",
        "NContracts",
        "K_LP",
        "K_SP",
        "K_SC",
        "K_LC",
        "OrderIds",
    )
    return all(key in position for key in required)
