"""Data-source registry and discovery (spec `data-access`, design D13).

``get_datasource(name)`` resolves a data source by name — the built-in
``alpaca`` (live) and ``backtest`` handlers, or any extra distribution
registering under the ``quant.datasources`` entry-point group. Importing this
package never imports alpaca or the terminal ``quant.backtest`` package; both
built-ins are resolved lazily on request (spec: data-source registration and
discovery).
"""

from quant.data._registry import (
    get_datasource,
    register_datasource,
)
from quant.data.synthetic_data_handler import SyntheticDataHandler

__all__ = ["SyntheticDataHandler", "get_datasource", "register_datasource"]
