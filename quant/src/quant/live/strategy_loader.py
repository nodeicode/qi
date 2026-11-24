import logging
from typing import Any, cast

from quant.data.data_handler import AlpacaDataHandler
from quant.live.protocols import LiveStrategyProtocol
from quant.strategies import get_strategy  # layering-ok  (single dynamic-load line)

# Client names that imply a live/production environment.
# All other client names are treated as paper/sandbox.
_PROD_CLIENTS = {"prod"}


def load_strategies(
    config: dict[str, Any], data_handler: AlpacaDataHandler, logger: logging.Logger
) -> list[LiveStrategyProtocol]:
    """
    Loads one strategy instance per (strategy, client) pair defined in config.

    Config schema per strategy entry:
        {
            "name": "<strategy_name>",
            "clients": ["paper", "paper1", "prod"],  # one or more registered client keys
            "config": {}                               # optional strategy config overrides
        }

    Each (strategy, client) pair becomes an independent instance with a unique
    name of the form "<strategy_name>_<client_name>", its own state bucket,
    data cache, and trading client.

    To add a new client, register it in AlpacaDataHandler (via env vars) and
    reference its key in the 'clients' list — no loader changes required.
    """
    strategies = []

    for strat_conf in config.get("strategies", []):
        base_name = strat_conf.get("name")
        if not base_name:
            logger.warning("Strategy entry missing 'name', skipping.")
            continue

        client_names = strat_conf.get("clients", ["paper"])
        if not isinstance(client_names, list) or not client_names:
            logger.warning(
                f"Strategy '{base_name}' has invalid or empty 'clients', skipping."
            )
            continue

        try:
            StrategyClass = get_strategy(base_name)
        except Exception as e:
            logger.error(f"Could not load strategy class '{base_name}': {e}")
            continue

        for client_name in client_names:
            if client_name not in data_handler.clients:
                logger.warning(
                    f"Skipping '{base_name}' for client '{client_name}': client not initialized. "
                    f"Available clients: {list(data_handler.clients.keys())}"
                )
                continue

            try:
                strategy = StrategyClass()

                # Apply Config Overrides
                overrides = strat_conf.get("config", {})
                if hasattr(strategy, "config") and overrides:
                    for k, v in overrides.items():
                        setattr(strategy.config, k, v)
                    logger.info(
                        f"Config overrides for '{base_name}' (client '{client_name}'): {overrides}"
                    )

                # Unique name per (strategy, client) — drives state, logs, and data cache keys
                strategy.name = f"{base_name}_{client_name}"
                strategy.env = "prod" if client_name in _PROD_CLIENTS else "paper"
                strategy.client_name = client_name

                strategies.append(cast("LiveStrategyProtocol", strategy))
                logger.info(
                    f"Initialized strategy: {strategy.name} | "
                    f"client: {client_name} | env: {strategy.env}"
                )
            except Exception as e:
                logger.error(
                    f"Failed to initialize '{base_name}' for client '{client_name}': {e}"
                )

    return strategies
