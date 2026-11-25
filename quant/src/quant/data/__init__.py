"""Data-source registry and discovery (spec `data-access`, design D13).

``get_datasource(name)`` resolves a data source by name — the built-in
``alpaca`` (live) and ``backtest`` handlers, or any extra distribution
registering under the ``quant.datasources`` entry-point group. Importing this
package never imports alpaca or the terminal ``quant.backtest`` package; both
built-ins are resolved lazily on request (spec: data-source registration and
discovery).
"""

from quant.data._registry import (
    datasource_offers,
    find_sources,
    get_datasource,
    register_datasource,
)
from quant.data.subscription import (
    BARS_COLUMNS,
    Datatype,
    Subscription,
    normalize_bars_frame,
)
from quant.data.synthetic_data_handler import SyntheticDataHandler

__all__ = [
    "BARS_COLUMNS",
    "Datatype",
    "Subscription",
    "SyntheticDataHandler",
    "datasource_offers",
    "find_sources",
    "get_datasource",
    "normalize_bars_frame",
    "register_datasource",
]
