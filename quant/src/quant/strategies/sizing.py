"""Strategy-owned sizing — the folded-in replacement for the deleted sizing registry.

Sizing is the author's job: the strategy's ``decide`` body computes the target
quantity from the instrument's contract facts (lot, multiplier, margin, price
precision — the single source on the typed instrument) and the injected
read-only portfolio (equity / buying power), then builds the order with
``instrument.make_order``. There is no engine- or registry-owned sizing on the
decision path — the legacy ``SIZING_STRATEGIES`` / ``size_for_signal`` /
``size_for_asset`` dispatch in ``quant.core.sizing`` is deleted.

A sizing result of zero means "do not trade": the author simply does not emit
an order for that quantity.
"""

from __future__ import annotations

from typing import Any

from quant.instruments.base import Instrument


def _portfolio_equity(
    portfolio: Any, instrument: Instrument, entry_price: float
) -> float:
    """Mark the portfolio's open positions so equity is current at *entry_price*.

    Falls back to ``cash`` for a minimal read-only projection that exposes no
    pricing surface (the author always faces at least cash).
    """
    prices = {(instrument.venue, instrument.symbol): float(entry_price)}
    equity = getattr(portfolio, "equity", None)
    if callable(equity):
        try:
            return float(equity(prices))
        except (TypeError, ValueError):
            pass
    return float(getattr(portfolio, "cash", 0.0))


def size_equity_lot(
    portfolio: Any,
    entry_price: float,
    *,
    instrument: Instrument,
    buying_power_fraction: float = 1.0,
) -> int:
    """Equity shares affordable with *buying_power_fraction* of buying power.

    Buying power is the portfolio's mark-to-market equity at *entry_price*.
    The result is floored to a whole number of the instrument's ``lot_size``
    (the minimum tradable unit), so a price that buys less than one lot sizes
    to zero shares — no order. ``price_precision`` is the venue's quote rule,
    not a sizing input; the author rounds the order price via
    ``instrument.round_price`` when a limit price is used.
    """
    if entry_price <= 0:
        return 0
    equity = _portfolio_equity(portfolio, instrument, entry_price)
    capital = equity * float(buying_power_fraction)
    lot = float(instrument.lot_size or 1.0)
    if lot <= 0:
        lot = 1.0
    shares = int(capital / entry_price)
    return int(shares // lot * lot)


def size_futures_margin(
    portfolio: Any,
    entry_price: float,
    *,
    instrument: Instrument,
    margin_per_contract: float | None = None,
    margin_fraction: float = 0.10,
    capital_fraction: float = 1.0,
) -> int:
    """Futures contracts affordable against per-contract margin.

    The per-contract margin resolves in priority order: an explicit
    *margin_per_contract* (an exchange margin the author knows), then the
    instrument's own ``margin`` contract fact, then a fraction of the
    contract notional (``margin_fraction * entry_price * multiplier``).
    Buying power is the portfolio's mark-to-market equity times
    *capital_fraction*. Because futures bind margin rather than notional, the
    quantity derives from margin plus the portfolio — never from a generic
    capital fraction alone. A margin the buying power cannot cover sizes to
    zero contracts — no order.
    """
    if entry_price <= 0:
        return 0
    if margin_per_contract is None:
        margin_per_contract = instrument.margin
    if margin_per_contract is None:
        multiplier = float(instrument.multiplier or 1.0)
        margin_per_contract = margin_fraction * entry_price * multiplier
    margin = float(margin_per_contract)
    if margin <= 0:
        return 0
    buying_power = _portfolio_equity(portfolio, instrument, entry_price)
    buying_power *= float(capital_fraction)
    return int(buying_power // margin)
