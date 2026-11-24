import logging

from alpaca.trading.client import TradingClient

from quant.data.data_handler import AlpacaDataHandler
from quant.live.protocols import LiveStrategyProtocol

from .unified_executor import UnifiedExecutor


def get_executor(
    strategy: LiveStrategyProtocol,
    client: TradingClient,
    data_handler: AlpacaDataHandler,
    logger: logging.Logger,
) -> UnifiedExecutor:
    """
    Factory to return the Unified Executor (equity execution atom).
    """
    return UnifiedExecutor({"default": client}, data_handler, logger)
