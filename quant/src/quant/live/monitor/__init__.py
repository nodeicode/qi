"""Async trading monitor — emit-only hot path (sidecar is an extension, D7).

The queue consumer / shadow worker is a developer-built extension built on the
``quant.observability`` primitives; the public tree ships only the
non-blocking enqueue.  Import from submodules to avoid shadowing
``quant.live.monitor.emit``::

    from quant.live.monitor.emit import emit
"""

from quant.live.monitor.emit import emit as emit_event

__all__ = ["emit_event"]
