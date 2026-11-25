"""Portfolio aggregation — account state, PnL, per-strategy + combined metrics.

A ``Portfolio`` collects the positions under its care (across strategies and
venues), tracks account state (cash and equity), and computes portfolio-level
realized/unrealized PnL from position state plus a pricing source. The same
class runs in backtest (fills from the backtest venue's execution client) and
live (fills from venue execution), so portfolio metrics derive from the same
ledger fields either way — research numbers equal live numbers by construction.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from quant.portfolio.base import Fill, Position, PositionState

PositionKey = tuple[str, str]

# Pricing source: ``(venue, symbol)`` tuple keys or bare ``symbol`` string sugar
# (see ``_resolve_prices``). ``Any`` keys keep the sugar ergonomic; the values
# are floats regardless of key shape.
Pricing = Mapping[Any, float]


def _key_of(venue: str, symbol: str) -> PositionKey:
    return (venue, symbol)


class Portfolio:
    """Aggregates positions across strategies/venues with account state.

    Fills are the only source of truth: :meth:`apply_fill` spins up positions
    (opened), adjusts them, and settles closed ones into a realized ledger
    while folding the fill proceeds into ``cash``. ``equity`` / PnL metrics are
    pure functions of position state plus a pricing source.
    """

    def __init__(
        self, *, starting_equity: float = 100_000.0, name: str = "default"
    ) -> None:
        self.name = name
        self.starting_equity = float(starting_equity)
        self.cash = self.starting_equity
        self._positions: dict[PositionKey, Position] = {}
        self._closed: list[Position] = []

    # ── Read-only accessors ───────────────────────────────────────────────
    @property
    def positions(self) -> Mapping[PositionKey, Position]:
        """Currently open positions, keyed by ``(venue, symbol)`` (read-only)."""
        return dict(self._positions)

    @property
    def closed_positions(self) -> list[Position]:
        """Positions that fully closed during the run (settled ledger)."""
        return list(self._closed)

    def position(self, venue: str, symbol: str) -> Position | None:
        """Return the open position for a (venue, symbol), or ``None``."""
        return self._positions.get(_key_of(venue, symbol))

    # ── The only way state changes: apply a fill ──────────────────────────
    def apply_fill(self, fill: Fill) -> Position | None:
        """Route *fill* into the matching position (or open a new one).

        Returns the resulting open ``Position``, or ``None`` when the fill
        closed a position with no residual. Never raises on well-formed fills.
        """
        key = fill.key
        self.cash -= fill.delta * fill.price  # buys spend cash; sells add it

        position = self._positions.get(key)
        if position is None:
            position = Position(opening_fill=fill, signal=fill.strategy or "BUY")
            self._positions[key] = position
            return position

        residual = position.apply(fill)
        if position.state is PositionState.CLOSED:
            self._positions.pop(key, None)
            self._closed.append(position)
        if residual is not None:
            return self.apply_fill(residual)
        return position if position.state is not PositionState.CLOSED else None

    # ── Pricing sugar ─────────────────────────────────────────────────────
    def _resolve_prices(self, prices: Pricing) -> Mapping[PositionKey, float]:
        """Normalize a pricing source: `(venue, symbol)` tuple or bare `symbol`."""
        resolved: dict[PositionKey, float] = {}
        for key in tuple(self._positions):
            if key in prices:
                resolved[key] = float(prices[key])
            elif key[1] in prices:  # symbol-string sugar for a single venue
                resolved[key] = float(prices[key[1]])
        return resolved

    # ── Account state & PnL (pricing source -> pure metrics) ──────────────
    def unrealized_pnl(self, prices: Pricing) -> float:
        """Sum of open positions' unrealized PnL at the given prices."""
        resolved = self._resolve_prices(prices)
        return sum(
            pos.unrealized_pnl(resolved[key])
            for key, pos in self._positions.items()
            if key in resolved
        )

    def realized_pnl(self) -> float:
        """Sum of realized PnL across every close + partial close so far."""
        return sum(pos.realized_pnl for pos in self._positions.values()) + sum(
            pos.realized_pnl for pos in self._closed
        )

    def market_value(self, prices: Pricing) -> float:
        """Sum of open positions' market value at the given prices."""
        resolved = self._resolve_prices(prices)
        return sum(
            pos.market_value(resolved[key])
            for key, pos in self._positions.items()
            if key in resolved
        )

    def equity(self, prices: Pricing) -> float:
        """Account equity: cash + market value of all open positions."""
        return self.cash + self.market_value(prices)

    def total_pnl(self, prices: Pricing) -> float:
        """Portfolio PnL vs starting equity: ``equity - starting_equity``."""
        return self.equity(prices) - self.starting_equity

    # ── Research across strategies: combined + per-strategy metrics ───────
    def strategy_names(self) -> list[str]:
        """Distinct strategy names across open + settled positions."""
        seen: set[str] = set()
        for pos in list(self._positions.values()) + self._closed:
            if pos.strategy is not None:
                seen.add(pos.strategy)
        return sorted(seen)

    def metrics(self, prices: Pricing) -> dict[str, Any]:
        """Combined portfolio metrics plus a per-strategy breakdown.

        Returns portfolio-level ``equity / realized / unrealized / total_pnl``
        and a ``per_strategy`` map of the realized/unrealized/total for each
        strategy that contributed fills (spec `portfolio` scenario). Cash is a
        single shared ledger, so per-strategy metrics are PnL/count-derived
        rather than an equity allocation (not derivable without extra config).
        """
        resolved = self._resolve_prices(prices)
        combined: dict[str, Any] = {
            "equity": float(self.equity(prices)),
            "realized_pnl": float(self.realized_pnl()),
            "unrealized_pnl": float(self.unrealized_pnl(prices)),
            "total_pnl": float(self.total_pnl(prices)),
            "open_positions": len(self._positions),
            "closed_positions": len(self._closed),
        }

        per_strategy: dict[str, dict[str, Any]] = {}
        for name in self.strategy_names():
            owns_open = [
                pos for pos in self._positions.values() if pos.strategy == name
            ]
            owns_settled = [pos for pos in self._closed if pos.strategy == name]
            real = sum(pos.realized_pnl for pos in owns_open + owns_settled)
            unr = sum(
                pos.unrealized_pnl(resolved[key])
                for key, pos in self._positions.items()
                if key in resolved and pos.strategy == name
            )
            per_strategy[name] = {
                "realized_pnl": float(real),
                "unrealized_pnl": float(unr),
                "total_pnl": float(real + unr),
                "open_positions": len(owns_open),
                "closed_positions": len(owns_settled),
            }
        combined["per_strategy"] = per_strategy
        return combined

    # ── Persistence / recovery (spec `portfolio` live scenario) ───────────
    def to_snapshot(self) -> dict[str, Any]:
        """Serialize open + settled positions and account state for restart."""
        return {
            "name": self.name,
            "starting_equity": self.starting_equity,
            "cash": self.cash,
            "positions": [pos._snapshot() for pos in self._positions.values()],
            "closed": [pos._snapshot() for pos in self._closed],
        }

    @staticmethod
    def from_snapshot(snap: Mapping[str, Any]) -> Portfolio:
        """Recover a portfolio exactly matching the pre-restart state."""
        pf = Portfolio(
            starting_equity=float(snap.get("starting_equity", 100_000.0)),
            name=str(snap.get("name", "default")),
        )
        pf.cash = float(snap.get("cash", pf.starting_equity))
        for rec in snap.get("positions", []):
            key = (str(rec["venue"]), str(rec["symbol"]))
            pf._positions[key] = Position._from_snapshot(rec)
        for rec in snap.get("closed", []):
            pf._closed.append(Position._from_snapshot(rec))
        return pf
