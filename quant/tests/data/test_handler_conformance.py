"""Deterministic enforcement that every data handler routes through _times.

Two layers:

1. **Structural wiring gate** — we inspect each handler module's source and
   require it to reference ``quant.data._times`` (``parse_naive_utc`` /
   ``assert_canonical_index`` / ``aligned_at``). A frame-emitting handler that
   bypasses the boundary is a CI failure *before* behavior tests can catch it.
   Handlers not yet wired are marked ``xfail(strict=True)`` so the gate is
   green today and flips to a *failure* the moment one of them is wired —
   forcing the marker to be removed and the conformance to be confirmed.

2. **Behavioral conformance** — the wired handlers' public getters must return
   frames that pass ``assert_canonical_index``; the boundary scalars must be
   UTC-naive ``CanonicalTs``. (Boundary helpers themselves are unit-tested in
   ``test_times.py``.)
"""

from __future__ import annotations

import re
from pathlib import Path

import pandas as pd
import pytest

from quant.data._times import (
    aligned_at,
    assert_canonical_index,
    parse_naive_utc,
)

HANDLERS_ROOT = Path(__file__).resolve().parents[2] / "src" / "quant" / "data"

# Every handler that emits frames must route through the boundary module.
# ``dhan_data_handler`` is deliberately excluded: it serves NSE (India) market
# data whose stamps are local-naive IST, not UTC; forcing the UTC boundary
# would mislabel them. It has no engine consumer.
HANDLER_MODULES = {
    "data_handler.py": True,  # wired: AlpacaDataHandler
    "futures_data_handler.py": True,  # wired: intraday → to_naive_index
    "options_data_handler.py": True,  # wired: engine-facing frames → naive
    "vix_data_handler.py": True,  # wired: daily → to_naive_utc + assert
}

EXCLUDED_HANDLERS = {"dhan_data_handler.py"}

_BOUNDARY_RE = re.compile(
    r"(parse_naive_utc|assert_canonical_index|aligned_at|from\s+\.?_times\s+import|"
    r"import\s+quant\.data\._times)"
)


@pytest.mark.parametrize("mod,wired", sorted(HANDLER_MODULES.items()))
def test_handler_module_routes_through_boundary(mod: str, wired: bool):
    src = (HANDLERS_ROOT / mod).read_text()
    uses_boundary = bool(_BOUNDARY_RE.search(src))
    if wired:
        assert uses_boundary, (
            f"{mod} is marked wired but does not reference data._times: "
            "it can return non-canonical frames"
        )
    else:
        pytest.xfail(reason=f"{mod} not yet routed through data._times (Phase 0.A)")


def test_wired_handler_frame_is_canonical():
    """AlpacaDataHandler intraday output must pass assert_canonical_index."""
    import numpy as np

    from quant.data.data_handler import AlpacaDataHandler

    handler = AlpacaDataHandler.__new__(AlpacaDataHandler)
    handler._INTRADAY_INTERVALS = frozenset(
        {"1m", "5m", "15m", "30m", "60m", "1h", "2h", "4h"}
    )

    df = pd.DataFrame(
        {"Close": [1.0, 2.0]},
        index=pd.DatetimeIndex(["2024-01-02 09:30", "2024-01-02 09:45"], tz="UTC")
        .tz_localize(None)
        .as_unit("us"),
    )
    # _finalize_equity_index (intraday) is where assert_canonical_index fires.
    handler._filter_market_hours = lambda d, i: d  # skip RTH ET math in this test
    out = handler._finalize_equity_index(df, "1m")
    assert out.index.tz is None
    assert_canonical_index(out, allow_aware_daily=False)


def test_boundary_scalars_are_utc_naive():
    """parse_naive_utc returns UTC-naive CanonicalTs; aligned_at too."""
    aware = pd.Timestamp("2024-06-01 14:00:00-04:00")
    s, _ = parse_naive_utc(aware, pd.Timestamp("2024-06-01 16:00:00-04:00"))
    assert s.tzinfo is None
    idx = pd.DatetimeIndex(
        ["2024-06-01 09:00", "2024-06-01 09:15"], tz="UTC"
    ).tz_localize(None)
    at = aligned_at(pd.DataFrame({"x": [1, 2]}, index=idx), "2024-06-01 09:10")
    assert at is not None and at.tzinfo is None
