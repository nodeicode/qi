from quant.strategies.example_equity.strategy import ExampleEquityStrategy

# The public SDK ships the reference strategy `example_equity`. Private alpha
# strategies (e.g. example_equity) live in the private superset repo and register
# here or via the `quant.strategies` entry-point group; they never enter the
# public tree.
STRATEGY_REGISTRY = {
    "example_equity": ExampleEquityStrategy,
}


def get_strategy(name: str):
    if name not in STRATEGY_REGISTRY:
        raise ValueError(f"Strategy {name} not found.")
    return STRATEGY_REGISTRY[name]
