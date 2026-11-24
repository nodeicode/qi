"""Portfolio primitives — the ``Position`` state machine (spec `portfolio`, D4).

A ``Position`` is an explicit state machine keyed per (venue, symbol): it
transitions ``opened -> adjusting -> closed`` as fills arrive, and ONLY
:meth:`Position.apply` may mutate it. ``Fill`` is the canonical event that both
environments produce — backtest (from fill simulation) and live (from venue
execution) — so position and portfolio semantics plus the metric computations
are the *same functions* in research, backtest, and live (parity by
construction).

The legacy ``PositionDict`` / ``build_position_dict`` schema is preserved as the
state representation through :meth:`Position.to_position_dict` (ticker strings
stay legal sugar).
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from enum import StrEnum
from typing import Any

from quant.instruments.base import Instrument


class Side(StrEnum):
    """Fill direction relative to the instrument (BUY adds to the long side)."""

    BUY = "BUY"
    SELL = "SELL"


class PositionState(StrEnum):
    """Lifecycle a position travels through, exclusively fill-driven."""

    OPENED = "opened"
    ADJUSTING = "adjusting"
    CLOSED = "closed"


@dataclass(frozen=True)
class Fill:
    """A single controller-and-venue-executed fill (the only position mutator).

    ``qty`` is a non-negative magnitude; ``side`` gives the direction. ``venue``
    and ``symbol`` key the position; an optional typed :class:`Instrument` keeps
    the (venue, symbol) key source of truth (``ticker`` string sugar preserved).
    """

    venue: str
    symbol: str
    side: Side
    qty: float
    price: float
    strategy: str | None = None
    timestamp: Any = None
    instrument: Instrument | None = None
    order_id: str | None = None

    @property
    def delta(self) -> float:
        """Signed quantity: ``+qty`` for BUY, ``-qty`` for SELL."""
        return self.qty if self.side is Side.BUY else -self.qty

    @property
    def key(self) -> tuple[str, str]:
        """Position identity ``(venue, symbol)`` — the portfolio keying unit."""
        if self.instrument is not None:
            return self.instrument.key()
        return (self.venue, self.symbol)

    def _snapshot(self) -> dict[str, Any]:
        return {
            "venue": self.venue,
            "symbol": self.symbol,
            "side": self.side.value,
            "qty": self.qty,
            "price": self.price,
            "strategy": self.strategy,
            "timestamp": None if self.timestamp is None else str(self.timestamp),
            "order_id": self.order_id,
        }


class Position:
    """Explicit per-(venue, symbol) state machine driven only by fills.

    Created from an opening fill in the ``opened`` state. Each subsequent fill
    moves the state via the canonical accounting rules:

    * same-direction fill  -> ``adjusting`` (quantity + weighted average price);
    * opposing fill that reduces without closing -> ``adjusting``
      (average price unchanged, partial realized PnL recorded);
    * opposing fill that brings quantity to zero -> ``closed``;
    * overshooting opposing fill -> ``closed`` on this position, returning the
      residual fill (same side) for the portfolio to open a flipped position.

    `apply` is the only mutator; nothing here is changed by side effects.
    """

    def __init__(
        self,
        opening_fill: Fill,
        *,
        signal: str = "BUY",
        confidence: float = 0.0,
    ) -> None:
        self.key = opening_fill.key
        self.venue, self.symbol = self.key
        self.instrument = opening_fill.instrument
        self.strategy = opening_fill.strategy
        self.signal = signal
        self.confidence = confidence
        self.entry_time = opening_fill.timestamp
        self.entry_price = float(opening_fill.price)
        self.order_ids: list[str] = (
            [opening_fill.order_id] if opening_fill.order_id else []
        )

        self.state: PositionState = PositionState.OPENED
        self.signed_qty = opening_fill.delta
        self.avg_price = float(opening_fill.price)
        self.cost_basis = abs(self.signed_qty) * self.avg_price
        self._realized = 0.0

    # ── PositionDict-compat aliases (legacy schema names kept) ────────────
    @property
    def qty(self) -> float:
        """Magnitude of the position (legacy ``PositionDict`` ``Qty``)."""
        return abs(self.signed_qty)

    @property
    def direction(self) -> int:
        """``1`` long, ``-1`` short (legacy ``Direction``)."""
        return 1 if self.signed_qty >= 0 else -1

    @property
    def realized_pnl(self) -> float:
        """Realized PnL across every closed portion of this position."""
        return self._realized

    # ── Mark-to-market ────────────────────────────────────────────────────
    def market_value(self, price: float) -> float:
        """Value of the open position at current *price* (signed)."""
        return self.signed_qty * float(price)

    def unrealized_pnl(self, price: float) -> float:
        """Unrealized PnL of the open quantity at current *price*."""
        return self.signed_qty * (float(price) - self.avg_price)

    # ── The only mutator ──────────────────────────────────────────────────
    def apply(self, fill: Fill) -> Fill | None:
        """Apply *fill*, transitioning state; return a residual (flip) fill.

        Raises if the position is already closed (the portfolio must create a
        fresh position for a new opening fill).
        """
        if self.state is PositionState.CLOSED or self.signed_qty == 0:
            raise ValueError(
                f"position {self.key} is closed; open a new Position from the fill"
            )
        if fill.order_id:
            self.order_ids.append(fill.order_id)

        delta = fill.delta
        if delta == 0:
            return None

        if (delta > 0 and self.signed_qty > 0) or (delta < 0 and self.signed_qty < 0):
            # Same-direction fill -> adjusting with a weighted average price.
            total = self.signed_qty + delta
            self.avg_price = (
                self.avg_price * abs(self.signed_qty) + fill.price * abs(delta)
            ) / abs(total)
            self.signed_qty = total
            self.cost_basis = abs(total) * self.avg_price
            self.state = PositionState.ADJUSTING
            return None

        # Opposing fill: reduce (or fully close) the open quantity.
        close_qty = min(abs(delta), abs(self.signed_qty))
        direction_sign = 1 if self.signed_qty > 0 else -1
        self._realized += close_qty * direction_sign * (fill.price - self.avg_price)

        if abs(delta) > close_qty:
            # Overshoot: this position closes fully; the residual reopens flipped.
            remnant = abs(delta) - close_qty
            self.signed_qty = 0
            self.state = PositionState.CLOSED
            return replace(fill, qty=remnant)

        self.signed_qty += delta
        self.cost_basis = abs(self.signed_qty) * self.avg_price
        self.state = (
            PositionState.CLOSED if self.signed_qty == 0 else PositionState.ADJUSTING
        )
        return None

    # ── Legacy schema surface (state representation) ──────────────────────
    def to_position_dict(self) -> dict[str, Any]:
        """Return the ``PositionDict``-compatible representation (legacy sugar)."""
        d: dict[str, Any] = {
            "EntryTime": self.entry_time,
            "EntryPrice": self.entry_price,
            "EntryBasePrice": self.entry_price,
            "UnderlyingEntryPrice": self.entry_price,
            "Direction": self.direction,
            "Signal": self.signal,
            "Confidence": self.confidence,
            "StopLoss": None,
            "TakeProfit": None,
            "UseTrail": False,
            "MaxHoldTime": 0,
            "AssetType": (
                self.instrument.asset_class.value
                if self.instrument is not None
                else "EQUITY"
            ),
            "Ticker": self.symbol,
            "Qty": self.qty,
            "Strategy": self.strategy,
            "Venue": self.venue,
            # Qi invariants the legacy schema does not carry:
            "_state": self.state.value,
            "_signed_qty": self.signed_qty,
            "_avg_price": self.avg_price,
            "_cost_basis": self.cost_basis,
            "_realized_pnl": self.realized_pnl,
        }
        return d

    def to_dict(self) -> dict[str, Any]:
        """Alias for :meth:`to_position_dict` (legacy naming)."""
        return self.to_position_dict()

    @staticmethod
    def from_position_dict(d: dict[str, Any]) -> Position:
        """Rebuild a position from its :meth:`to_position_dict` representation.

        Reconstructs the *internal* state exactly (signed quantity, weighted
        average price, partial realized PnL) so a snapshot round-trips without
        re-deriving it from fills.
        """
        pos = Position(
            Fill(
                venue=str(d["Venue"]),
                symbol=str(d["Ticker"]),
                side=Side.BUY if int(d["Direction"]) >= 0 else Side.SELL,
                qty=abs(float(d["Qty"])),
                price=float(d["EntryPrice"]),
                strategy=d.get("Strategy"),
                timestamp=d.get("EntryTime"),
            ),
            signal=str(d.get("Signal", "BUY")),
            confidence=float(d.get("Confidence", 0.0)),
        )
        if "_signed_qty" in d:
            pos.signed_qty = float(d["_signed_qty"])
        if "_avg_price" in d:
            pos.avg_price = float(d["_avg_price"])
        if "_realized_pnl" in d:
            pos._realized = float(d["_realized_pnl"])
        pos.cost_basis = abs(pos.signed_qty) * pos.avg_price
        pos.state = PositionState(str(d.get("_state", PositionState.OPENED.value)))
        pos.entry_price = float(d["EntryPrice"])
        if d.get("OrderId"):
            pos.order_ids = [str(d["OrderId"])]
        return pos

    # ── Snapshot (live persistence / recovery) ───────────────────────────
    def _snapshot(self) -> dict[str, Any]:
        return {
            "venue": self.venue,
            "symbol": self.symbol,
            "strategy": self.strategy,
            "signal": self.signal,
            "confidence": self.confidence,
            "entry_time": None if self.entry_time is None else str(self.entry_time),
            "entry_price": self.entry_price,
            "state": self.state.value,
            "signed_qty": self.signed_qty,
            "avg_price": self.avg_price,
            "cost_basis": self.cost_basis,
            "realized": self._realized,
            "order_ids": self.order_ids,
        }

    @staticmethod
    def _from_snapshot(snap: dict[str, Any]) -> Position:
        pos = Position(
            Fill(
                venue=str(snap["venue"]),
                symbol=str(snap["symbol"]),
                side=Side.BUY if float(snap["signed_qty"]) >= 0 else Side.SELL,
                qty=abs(float(snap["signed_qty"])),
                price=float(snap["avg_price"]),
                strategy=snap.get("strategy"),
                timestamp=snap.get("entry_time"),
            ),
            signal=str(snap.get("signal", "BUY")),
            confidence=float(snap.get("confidence", 0.0)),
        )
        pos.signed_qty = float(snap["signed_qty"])
        pos.avg_price = float(snap["avg_price"])
        pos.cost_basis = float(snap["cost_basis"])
        pos._realized = float(snap["realized"])
        pos.state = PositionState(str(snap["state"]))
        pos.entry_time = snap.get("entry_time")
        pos.entry_price = float(snap["entry_price"])
        pos.order_ids = list(snap.get("order_ids") or [])
        return pos

    def __repr__(self) -> str:  # pragma: no cover - debug aid
        return (
            f"Position({self.venue}:{self.symbol} qty={self.signed_qty:g} "
            f"avg={self.avg_price:g} state={self.state.value})"
        )
