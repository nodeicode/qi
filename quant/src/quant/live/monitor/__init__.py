"""Async trading monitor — emit-only hot path + separate-process sidecar.

Import from submodules to avoid shadowing ``quant.live.monitor.emit``::

    from quant.live.monitor.emit import emit, start_sidecar, stop_sidecar
"""

from quant.live.monitor.emit import emit as emit_event
from quant.live.monitor.emit import start_sidecar, stop_sidecar

__all__ = ["emit_event", "start_sidecar", "stop_sidecar"]
