"""Per-leg breach CBBO fallback — no all-or-nothing clear."""

from __future__ import annotations

from quant.backtest.utils import apply_breach_exit_cbbo


def _leg_meta():
    return {
        "lp": ("put", 495.0),
        "sp": ("put", 500.0),
        "sc": ("call", 510.0),
        "lc": ("call", 515.0),
    }


def _cbbo(mid: float, bid: float | None = None, ask: float | None = None):
    b = mid - 0.05 if bid is None else bid
    a = mid + 0.05 if ask is None else ask
    return {"mid": mid, "bid": b, "ask": a}


def test_partial_cbbo_miss_keeps_other_legs():
    """One missing leg falls back; other legs keep CBBO mids/bid/ask."""
    breach = {
        "lp": _cbbo(0.10),
        "sp": None,  # miss
        "sc": _cbbo(0.20),
        "lc": _cbbo(0.05),
    }
    leg_mids: dict = {}
    methods: dict = {}
    exit_bar_ts: dict = {}
    # Spot above SP → put short intrinsic = 0
    S_exit = 505.0
    bids, asks, missed = apply_breach_exit_cbbo(
        breach,
        _leg_meta(),
        S_exit,
        "2024-06-03 15:30",
        leg_mids,
        methods,
        exit_bar_ts,
    )

    assert missed == ["sp"]
    assert methods["lp"] == "breach_cbbo"
    assert methods["sc"] == "breach_cbbo"
    assert methods["lc"] == "breach_cbbo"
    assert methods["sp"] == "breach_intrinsic_fallback"
    assert leg_mids["lp"] == 0.10
    assert leg_mids["sc"] == 0.20
    assert leg_mids["lc"] == 0.05
    assert leg_mids["sp"] == 0.0  # OTM put intrinsic
    assert set(bids) == {"lp", "sc", "lc"}
    assert set(asks) == {"lp", "sc", "lc"}
    assert "sp" not in bids


def test_all_four_cbbo_enables_full_book():
    breach = {ln: _cbbo(0.1 * i) for i, ln in enumerate(("lp", "sp", "sc", "lc"), 1)}
    leg_mids: dict = {}
    methods: dict = {}
    exit_bar_ts: dict = {}
    bids, _, missed = apply_breach_exit_cbbo(
        breach,
        _leg_meta(),
        505.0,
        "t",
        leg_mids,
        methods,
        exit_bar_ts,
    )
    assert missed == []
    assert set(bids) == {"lp", "sp", "sc", "lc"}
    assert all(methods[ln] == "breach_cbbo" for ln in bids)


def test_itm_short_leg_intrinsic_on_miss():
    breach = dict.fromkeys(("lp", "sp", "sc", "lc"))
    leg_mids: dict = {}
    methods: dict = {}
    exit_bar_ts: dict = {}
    # Spot through short put
    S_exit = 498.0
    _, _, missed = apply_breach_exit_cbbo(
        breach,
        _leg_meta(),
        S_exit,
        "t",
        leg_mids,
        methods,
        exit_bar_ts,
    )
    assert set(missed) == {"lp", "sp", "sc", "lc"}
    assert leg_mids["sp"] == 2.0  # 500 - 498
    assert leg_mids["lp"] == 0.0  # 495 OTM
    assert all(m == "breach_intrinsic_fallback" for m in methods.values())
