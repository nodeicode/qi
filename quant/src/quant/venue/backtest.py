"""BacktestVenue — a simulation-timeline venue (spec `venue`).

State is answered exclusively from the *injected* simulation ``now`` + the
configured window — never the host wall clock — so the same simulated
timestamp always yields the same open/closed answer, independent of when or
where the run executes (determinism). A ``calendar`` may mark specific days
closed (e.g. holidays); with no calendar the venue is open during its hours.

``BacktestExecutionClient`` is this venue's execution client and the only
backtest fill simulator: it absorbed the legacy per-asset-class executors
(``ExecutorRouter`` / ``EquityExecutor`` / ``BacktestExecContext`` under
``quant/backtest/executors/*``, deleted) into one object that owns the run
ledger (active positions, trade log, running equity, collateral) and speaks
two surfaces:

* the typed-order seam — ``submit(order)`` simulates a fill from the closed
  bar's tradable price (limit-crossing rules), and
* the engine-facing duck surface the shared ``process_strategy`` loop drives
  (``should_run`` / ``record_run`` / ``data_end_dt`` /
  ``get_precomputed_signal`` / ``enter`` / ``exit`` / lifecycle hooks, plus
  ``force_close_remaining`` for end-of-run), so fills price on closed
  tradable bars, honor the entry/exit timing rules, and record into the same
  ledger that ``Backtester.compute_equity_curve`` /
  ``calculate_performance_metrics`` read.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from datetime import time
from typing import Any

import pandas as pd

from quant.order import Order, OrderPriceType
from quant.portfolio import Fill, Side
from quant.utils.fills import apply_slippage, calculate_fill_time, tradable_close_at
from quant.utils.strategy_config import BaseStrategyConfig
from quant.venue.base import (
    BaseVenue,
    CalendarProtocol,
    DataSourceProtocol,
)


class BacktestExecutionClient:
    """Backtest fill engine: typed ``Order`` -> simulated fill + run ledger.

    The backtest venue's execution client simulates the fill for every order
    the engine submits (typed ``submit`` seam) and for every entry/exit the
    shared per-bar loop dispatches (the duck surface absorbed from the
    deleted ``ExecutorRouter``/``EquityExecutor`` pair). Fills price on the
    closed signal bar's *tradable* close — the engine feeds the closed bar's
    price, so the author body ran on adjusted bars while the fill is in
    tradable space — honor the entry/exit timing rules (``T+I+DATA_DELAY``
    with slippage), and record into the injected run ledger (positions,
    ``trade_log``, ``running_equity``) from which the backtester computes the
    equity curve and performance metrics.

    The ledger lists are injected by reference (mirroring the deleted
    ``BacktestExecContext``), so ``Backtester`` keeps its own ``trade_log`` /
    positions attributes and reads the run's results back from this client
    after ``force_close_remaining``.

    Gating: an order outside the fill rules is not filled and never touches
    the portfolio — a MARKET order fills at the tradable price; a LIMIT order
    fills only when the limit is crossed (BUY: market <= limit; SELL: market
    >= limit); an order with no resolvable positive price (or an unfilled
    limit) reports ``None``. Entry-window/direction gating lives on the
    engine's shared entry gate; session gating arrives with subscription
    cadence scheduling.
    """

    def __init__(
        self,
        *,
        config: BaseStrategyConfig | None = None,
        ticker: str | None = None,
        logger: logging.Logger | None = None,
        df_signals: pd.DataFrame | None = None,
        df_manage: pd.DataFrame | None = None,
        df_manage_1m: pd.DataFrame | None = None,
        active_positions: list | None = None,
        trade_log: list | None = None,
        starting_equity: float = 0.0,
        running_equity: float = 0.0,
        collateral_in_use: float = 0.0,
        portfolio: Any = None,
        venue: str = "backtest",
        update_reason_stats: Callable[[dict], None] | None = None,
        price_source: Callable[[Order], float | None] | None = None,
    ) -> None:
        self.config = config
        self.ticker = ticker
        self._log = logger or logging.getLogger(__name__)

        # Bar data (signal + manage resolution) the shared loop consumes.
        self.df_signals: pd.DataFrame = (
            df_signals if df_signals is not None else pd.DataFrame()
        )
        self.df_manage: pd.DataFrame = (
            df_manage if df_manage is not None else pd.DataFrame()
        )
        self.df_manage_1m: pd.DataFrame = (
            df_manage_1m if df_manage_1m is not None else pd.DataFrame()
        )

        # Shared mutable run ledger (injected by reference from the Backtester).
        self._active_positions: list = (
            active_positions if active_positions is not None else []
        )
        self.trade_log: list = trade_log if trade_log is not None else []
        self.starting_equity: float = float(starting_equity)
        self._running_equity: float = float(running_equity)
        self._collateral_in_use: float = float(collateral_in_use)

        # Portfolio-collecting mode (spec `portfolio`): simulated fills stream
        # into this Portfolio (same primitives as live).
        self.portfolio = portfolio
        self.venue = venue
        self._update_reason_stats: Callable[[dict], None] = (
            update_reason_stats if update_reason_stats is not None else lambda _t: None
        )

        # Typed-order seam: resolves the tradable price for ``submit(order)``.
        self._price_source: Callable[[Order], float | None] = price_source or (
            lambda _order: None
        )
        # Fill-clock cadence default (overridden per submission by the engine).
        self._cadence: str = "1h"

    # ── Ledger sync (Backtester reads these after the run) ────────────────
    @property
    def running_equity(self) -> float:
        return self._running_equity

    @property
    def collateral_in_use(self) -> float:
        return self._collateral_in_use

    @property
    def _fill_config(self) -> Any:
        """Strategy config the fill path reads fill knobs from.

        The backtest fill path prices and clocks fills from strategy-owned
        knobs (``DATA_DELAY``) plus the entry/exit tick cadence the engine
        passes per submission; ``Any`` keeps the duck surface honest for
        configs that opt into the legacy ``DATA_DELAY`` spelling.
        """
        return self.config

    def _data_delay(self) -> int:
        """Strategy-config ``DATA_DELAY`` (minutes), 0 when unset."""
        cfg = self._fill_config
        try:
            return max(0, int(getattr(cfg, "DATA_DELAY", 0) or 0))
        except (TypeError, ValueError):
            return 0

    # ── entry/exit fill simulation (typed-order seam) ────────────────────
    def submit(
        self,
        order: Order,
        *,
        now=None,
        cadence: str | None = None,
        reference_price=None,
    ) -> Fill | None:
        """Simulate a fill for *order*, or ``None`` when it cannot fill.

        Two pricing paths share one seam. With a tick timestamp (*now*), the
        fill is priced on the tradable (raw) close of the first closed bar at/
        after the fill clock ``T+I+DATA_DELAY`` (``calculate_fill_time``) — the
        author body ran on adjusted bars while execution is in tradable space
        with per-side slippage. Without a clock (direct seam calls), the fill
        prices at the injected ``price_source`` with the same limit-crossing
        rule. An order with no resolvable positive price (or an unfilled
        limit) reports ``None``; an opening order spins up a Position in the
        portfolio, an opposing order closes/reduces it and settles a trade-log
        entry when the position closes. ``reference_price`` is accepted for
        shared-loop parity with the live client and unused here.
        """
        del reference_price
        now_ts = pd.Timestamp(now) if now is not None else None
        if now_ts is not None:
            cadence = cadence or self._cadence or "1h"
            fill_time = calculate_fill_time(now_ts, cadence, self._data_delay())
            df_entry = self._slice_manage(start=fill_time)
            if df_entry.empty:
                return None
            raw_market = tradable_close_at(df_entry.iloc[0])
            if raw_market is None or raw_market <= 0:
                return None
            direction = 1 if order.side is Side.BUY else -1
            market = apply_slippage(
                self._fill_config, raw_market, direction, is_entry=True
            )
            fill_price = self._resolve_fill_price(order, market)
            if fill_price is None or fill_price <= 0:
                return None
            fill = Fill(
                venue=order.venue,
                symbol=order.instrument.symbol,
                side=order.side,
                qty=float(order.quantity),
                price=float(fill_price),
                strategy=order.strategy,
                instrument=order.instrument,
                timestamp=fill_time,
            )
            return self._apply_fill(order, fill)

        # Direct seam calls without a tick clock: price from the injected
        # source (tests / ad-hoc venues) with the same limit-crossing rule.
        raw_market = self._price_source(order)
        if raw_market is None or float(raw_market) <= 0:
            return None
        fill_price = self._resolve_fill_price(order, float(raw_market))
        if fill_price is None or fill_price <= 0:
            return None
        fill = Fill(
            venue=order.venue,
            symbol=order.instrument.symbol,
            side=order.side,
            qty=float(order.quantity),
            price=fill_price,
            strategy=order.strategy,
            instrument=order.instrument,
        )
        if self.portfolio is not None:
            self.portfolio.apply_fill(fill)
        return fill

    def _resolve_fill_price(self, order: Order, market: float) -> float | None:
        """Fill at *market* only when a limit order's limit is crossed."""
        if order.price_type is OrderPriceType.MARKET or order.price is None:
            return market
        if order.side.value == "BUY" and market <= float(order.price):
            return market
        if order.side.value == "SELL" and market >= float(order.price):
            return market
        return None

    def _apply_fill(self, order: Order, fill: Fill) -> Fill | None:
        """Apply *fill* to the portfolio and settle run-ledger bookkeeping.

        An opening fill (no open position for the key) spins up the Position.
        An opposing fill reduces/closes it; a full close also settles the trade
        log + running equity, and the venue's forced-close uses the same path.
        """
        if self.portfolio is None:
            return None
        key = (order.venue, order.instrument.symbol)
        existing = self.portfolio.position(*key)
        if existing is not None:
            # Capture pre-close facts before the fill mutates the position.
            pre = (
                existing.entry_time,
                existing.entry_price,
                existing.qty,
                existing.direction,
                existing.signal,
            )
        self.portfolio.apply_fill(fill)
        after = self.portfolio.position(*key)
        if existing is not None and after is None:
            self._settle_trade(
                order, fill, pre, forced=bool(order.tag == "Force_Close")
            )
        return fill

    def _settle_trade(
        self,
        order: Order,
        fill: Fill,
        pre,
        *,
        forced: bool,
    ) -> None:
        """Append one realized trade-log entry + update running equity."""
        entry_time, entry_price, qty, direction, signal = pre
        qty = float(qty or 0.0)
        if qty <= 0:
            return
        pnl = (float(fill.price) - float(entry_price)) * qty * direction
        exit_time = pd.Timestamp(fill.timestamp)
        reason = str(order.tag or ("Force_Close" if forced else "Signal"))
        self._running_equity += pnl
        trade = {
            "EntryTime": entry_time,
            "ExitTime": exit_time,
            "Signal": signal,
            "Direction": direction,
            "EntryPrice": float(entry_price),
            "ExitPrice": float(fill.price),
            "Contracts": qty,
            "PnL": pnl,
            "DurationMinutes": (exit_time - pd.Timestamp(entry_time)).total_seconds()
            / 60.0,
            "Reason": reason,
            "Confidence": 0.0,
        }
        self.trade_log.append(trade)
        self._update_reason_stats(trade)

    def _force_close(self, position) -> None:
        """Close one open position at the last available tradable bar."""
        if self.df_manage.empty or self.portfolio is None:
            return
        instrument = getattr(position, "instrument", None)
        if instrument is None:
            instrument = self._instrument_for_symbol(getattr(position, "symbol", ""))
        if instrument is None:
            return
        direction = int(getattr(position, "direction", 1) or 1)
        side = Side.SELL if direction > 0 else Side.BUY
        order = instrument.make_order(
            side,
            float(getattr(position, "qty", 0.0) or 0.0),
            tag="Force_Close",
            strategy=getattr(position, "strategy", None),
        )
        if order.quantity <= 0:
            return
        exit_row = self.df_manage.iloc[-1]
        exit_time = pd.Timestamp(exit_row.name)
        raw_market = tradable_close_at(exit_row)
        side_dir = 1 if side is Side.BUY else -1
        market = apply_slippage(self._fill_config, raw_market, side_dir, is_entry=False)
        fill_price = self._resolve_fill_price(order, market)
        if fill_price is None or fill_price <= 0:
            return
        fill = Fill(
            venue=position.venue,
            symbol=position.symbol,
            side=side,
            qty=order.quantity,
            price=float(fill_price),
            strategy=order.strategy,
            instrument=instrument,
            timestamp=exit_time,
        )
        self._apply_fill(order, fill)

    def _instrument_for_symbol(self, symbol: str):
        """Best-effort typed instrument for a symbol (venue-scoped)."""
        if not symbol:
            return None
        try:
            from quant.instruments import EquityInstrument

            return EquityInstrument(symbol=symbol, venue=self.venue)
        except Exception:
            return None

    # ── End-of-run ─────────────────────────────────────────────────────────
    def force_close_remaining(self) -> None:
        """Close any positions still active at the end of the backtest."""
        if self.portfolio is None:
            return
        open_positions = [
            p for p in self.portfolio.open_positions() if p.venue == self.venue
        ]
        if not open_positions:
            return
        self._log.info("Force-closing %d remaining position(s).", len(open_positions))
        for pos in list(open_positions):
            self._force_close(pos)

    # ── Shared helpers ─────────────────────────────────────────────────────
    def _slice_manage(self, start=None, end=None) -> pd.DataFrame:
        """Slice manage-resolution data for a time window."""
        if self.df_manage.empty:
            return self.df_manage

        idx = self.df_manage.index

        if start is not None and end is not None:
            mask = (idx >= start) & (idx <= end)
        elif start is not None:
            mask = idx >= start
        elif end is not None:
            mask = idx <= end
        else:
            return self.df_manage
        return self.df_manage[mask]


class BacktestVenue(BaseVenue):
    """Deterministic backtest venue: ``is_open_at(now)`` uses only simulated input."""

    id = "backtest"

    def __init__(
        self,
        data_source: DataSourceProtocol | None = None,
        *,
        calendar: CalendarProtocol | None = None,
        execution_client: Any = None,
        logger: logging.Logger | None = None,
        open_time: time = time(13, 30),
        close_time: time = time(20, 0),
        force_close_before_minutes: int = 15,
    ) -> None:
        super().__init__(
            data_source,
            calendar=calendar,
            execution_client=execution_client,
            logger=logger,
            default_open=open_time,
            default_close=close_time,
            default_force_close=close_time,
            force_close_before_minutes=force_close_before_minutes,
            treat_no_calendar_as_closed=False,
        )
