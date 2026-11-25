from typing import Any, Protocol, runtime_checkable

import pandas as pd

from quant.data.subscription import Subscription
from quant.utils.positions import (
    PositionDict,  # noqa: F401  (re-exported for consumers)
)
from quant.utils.strategy_config import BaseStrategyConfig

# ─────────────────────────────────────────────────────────────────────────────
# Strategy Protocol — catches bad strategies at construction time
# ─────────────────────────────────────────────────────────────────────────────


@runtime_checkable
class StrategyProtocol(Protocol):
    """Declarative strategy surface (spec `strategy-authoring`).

    A strategy declares its typed instruments, data subscriptions, and venue,
    then implements a single author body ``decide(events, portfolio)`` that
    BOTH generates entries and evaluates the portfolio's existing open
    positions for exits. It returns typed ``Order``/exit intents — no signal
    strings or 3-method contracted ``calculate_indicators`` /
    ``generate_signals`` / ``manage_trade`` surface appears here.
    """

    name: str
    config: BaseStrategyConfig
    instruments: list[Any]
    subscriptions: list[Any]
    venue: str

    def decide(self, events, portfolio) -> list[Any]: ...


# ─────────────────────────────────────────────────────────────────────────────
# DataHandler Protocol — abstracts data fetching for backtest and live
# ─────────────────────────────────────────────────────────────────────────────
@runtime_checkable
class DataHandlerProtocol(Protocol):
    """Interface for fetching historical data.  Implemented by both
    ``AlpacaDataHandler`` (live) and ``BacktestDataHandler`` (backtest).
    """

    def get_data(
        self,
        subscription: Subscription,
        start,
        end,
        use_cache: bool = True,
        adjustment: str | None = None,
    ) -> pd.DataFrame:
        """Fulfill a typed ``(instrument, datatype, cadence)`` subscription.

        Ships the ``bars`` datatype: returns OHLCV bars for the subscription's
        instrument over ``[start, end]`` at its cadence, with the canonical
        ``BARS_COLUMNS`` shape on both the backtest and live roots. The
        ``adjustment`` argument is the equity-bar adjustment space (``"all"`` /
        ``"raw"``); handlers for instruments without adjustments accept and
        ignore it. Unsupported datatypes raise.
        """
        ...

    def get_historical_data(
        self,
        ticker: str,
        start,
        end,
        interval: str,
        use_cache: bool = True,
        adjustment: str | None = None,
    ) -> pd.DataFrame:
        """Fetch OHLCV bars for *ticker* over ``[start, end]``.

        The engine-owned fetch path (``fetch_data`` / ``fetch_manage_data``)
        still consumes this while the engine is migrated to subscription
        buffers; it resolves to the same bars as :meth:`get_data`.
        """
        ...
