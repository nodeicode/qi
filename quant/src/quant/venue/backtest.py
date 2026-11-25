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
from quant.utils.time import calculate_entry_time
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

    # ── Ledger sync (Backtester reads these after the run) ────────────────
    @property
    def running_equity(self) -> float:
        return self._running_equity

    @property
    def collateral_in_use(self) -> float:
        return self._collateral_in_use

    # ── Typed-order seam (spec `execution` Backtest fills) ────────────────
    def submit(self, order: Order) -> Fill | None:
        """Simulate a fill for *order* at the tradable price, or ``None``."""
        market = self._price_source(order)
        if market is None or float(market) <= 0:
            return None
        fill_price = self._resolve_fill_price(order, float(market))
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

    @property
    def _fill_config(self) -> Any:
        """Strategy config the shared-loop duck surface reads fill knobs from.

        The legacy per-bar loop prices and clocks fills from strategy-owned
        knobs (``INTERVAL`` / ``MANAGE_INTERVAL`` / ``DATA_DELAY`` /
        ``CAPITAL_PER_TRADE``) that legacy strategy configs declare beyond
        ``BaseStrategyConfig``; ``Any`` keeps this duck surface honest until
        the reshaped engine deletes it.
        """
        return self.config

    # ── Shared-loop duck surface (absorbed from ExecutorRouter/EquityExecutor)
    def should_run(self, strategy, now) -> tuple[bool, bool]:
        """Every bar runs both phases — the bar loop drives the schedule."""
        return (True, True)

    def record_run(self, strategy_name: str, now, ran_signal: bool) -> None:
        return None

    def data_end_dt(self, now):
        """Manage-window cutoff = signal bar close (T+I), not fill clock.

        Fill clock is ``T+I+DATA_DELAY`` via enter / manage-triggered exits.
        Extending the manage cutoff to the delayed fill wall would let the
        manage pass see bars live cannot see when the signal first becomes
        known — silent parity drift.
        """
        return pd.Timestamp(calculate_entry_time(now, self._fill_config.INTERVAL))

    def get_precomputed_signal(self, ticker: str, now) -> pd.DataFrame | None:
        """Return the precomputed signal frame; engine selects by ``bar_ts``."""
        if self.df_signals is None or self.df_signals.empty:
            return None
        return self.df_signals

    def active_positions(self, strategy_name: str) -> list:
        """The run ledger's open positions (shared-list reference)."""
        return self._active_positions

    def after_process_ticker(
        self, strategy_name: str, ticker: str, results: list[dict]
    ) -> None:
        return None

    def after_entry(self, strategy_name: str, position: dict) -> None:
        return None

    def after_exit(self, strategy_name: str, position: dict) -> None:
        return None

    # ── Fill simulation (equity semantics from the deleted EquityExecutor) ─
    def enter(
        self,
        strategy,
        ticker: str,
        signal_row: dict,
        engine,
        *,
        now=None,
    ) -> dict | None:
        """Open a position for an actionable entry. Returns the position dict."""
        signal_time = now
        entry_time = calculate_fill_time(
            signal_time, self._fill_config.INTERVAL, self._fill_config.DATA_DELAY
        )
        try:
            row = self.df_signals.loc[now]
        except KeyError:
            row = pd.Series(signal_row)

        signal = str(signal_row.get("Signal", "BUY"))
        confidence = float(signal_row.get("Confidence", 0.0))
        direction = engine.direction(signal)

        df_entry = self._slice_manage(start=entry_time)
        if df_entry.empty:
            return None

        raw_entry = tradable_close_at(df_entry.iloc[0])
        entry_price = apply_slippage(
            self._fill_config, raw_entry, direction, is_entry=True
        )

        # Fill quantity for the simulated entry: a fixed fraction of the run's
        # starting equity per trade, floored to whole shares. This is the
        # legacy fill-simulation sizing leg (author-side sizing on instrument
        # contract facts + the portfolio replaces it on the declarative path).
        pos_size = float(row.get("Position_Size", 1.0))
        capital_frac = float(self._fill_config.CAPITAL_PER_TRADE)
        effective_capital = self.starting_equity * capital_frac * pos_size
        shares = int(effective_capital / entry_price) if entry_price > 0 else 0
        sizing = {
            "effective_capital": effective_capital,
            "shares": shares,
            "capital_fraction": capital_frac,
        }

        position = dict(
            engine.build_position(
                entry_time=entry_time,
                entry_price=entry_price,
                direction=direction,
                signal=signal,
                confidence=confidence,
                PositionSize=pos_size,
            )
        )
        position["Ticker"] = ticker
        position["_asset_class"] = "EQUITY"
        position["_sizing"] = sizing
        position["_row"] = row
        position["_strategy_name"] = getattr(strategy, "name", None)
        self._active_positions.append(position)

        # Portfolio-collecting mode: stream the simulated entry fill through the
        # same Position machinery the live path uses (parity by construction).
        if self.portfolio is not None:
            direction = int(position.get("Direction", 1))
            shares = int(sizing.get("shares", 0) or 0)
            if shares > 0:
                self.portfolio.apply_fill(
                    Fill(
                        venue=self.venue,
                        symbol=ticker,
                        side=Side.BUY if direction >= 0 else Side.SELL,
                        qty=shares,
                        price=float(entry_price),
                        strategy=getattr(strategy, "name", None),
                        timestamp=entry_time,
                    )
                )

        return self._active_positions[-1]

    def exit(
        self,
        strategy_name: str,
        position: dict,
        sig: dict,
        *,
        forced: bool = False,
        manage_df: pd.DataFrame | None = None,
    ) -> bool:
        """Close *position*. Returns True on success (position removed)."""
        pos = sig.get("_position", position)

        if manage_df is None:
            manage_df = self.df_manage

        entry_time = pos.get("EntryTime")
        if entry_time is not None:
            _et = pd.Timestamp(entry_time)
            if pd.notna(_et):
                _trimmed = manage_df[manage_df.index >= _et]
                if not _trimmed.empty:
                    manage_df = _trimmed

        if manage_df.empty:
            self._log.warning(
                "BacktestExecutionClient.exit: manage_df empty after EntryTime trim "
                "for %s — skipping",
                pos.get("Ticker"),
            )
            if pos in self._active_positions:
                self._active_positions.remove(pos)
            return True

        exit_idx = sig.get("exit_idx", len(manage_df) - 1)
        if exit_idx >= len(manage_df):
            exit_idx = len(manage_df) - 1
        exit_row = manage_df.iloc[exit_idx]
        exit_bar = pd.Timestamp(exit_row.name)

        exit_time = calculate_fill_time(
            exit_bar,
            self._fill_config.MANAGE_INTERVAL or self._fill_config.INTERVAL,
            self._fill_config.DATA_DELAY,
        )

        direction = pos.get("Direction", 1)
        entry_price = pos["EntryPrice"]
        entry_time = pos["EntryTime"]

        exit_price = apply_slippage(
            self._fill_config,
            tradable_close_at(exit_row),
            direction,
            is_entry=False,
        )

        sizing = pos.get("_sizing", {})
        eff_cap = sizing.get(
            "effective_capital",
            self.starting_equity * float(self._fill_config.CAPITAL_PER_TRADE),
        )
        pnl = (
            (
                (exit_price - entry_price)
                if direction == 1
                else (entry_price - exit_price)
            )
            * eff_cap
            / entry_price
            if entry_price
            else 0.0
        )

        self._running_equity += pnl

        # Portfolio-collecting mode: stream the simulated exit fill through the
        # same Position machinery the live path uses (parity by construction).
        if self.portfolio is not None:
            shares = int((sizing or {}).get("shares", 0) or 0)
            if shares > 0:
                self.portfolio.apply_fill(
                    Fill(
                        venue=self.venue,
                        symbol=str(pos.get("Ticker", self.ticker)),
                        side=Side.SELL if direction >= 0 else Side.BUY,
                        qty=shares,
                        price=float(exit_price),
                        strategy=strategy_name or pos.get("_strategy_name"),
                        timestamp=exit_time,
                    )
                )

        signal_row = pos.get("_row")
        reason_default = "Force_Close" if forced else "Signal"
        reason = (
            signal_row.get("Reason", reason_default)
            if signal_row is not None
            else reason_default
        )

        self.trade_log.append(
            {
                "EntryTime": entry_time,
                "ExitTime": exit_time,
                "Signal": pos.get("Signal", ""),
                "Direction": direction,
                "EntryPrice": entry_price,
                "ExitPrice": exit_price,
                "Contracts": sizing.get("shares", 0),
                "PnL": pnl,
                "DurationMinutes": (exit_time - entry_time).total_seconds() / 60.0,
                "Reason": reason,
                "Confidence": pos.get("Confidence", 0),
            }
        )
        self._update_reason_stats(self.trade_log[-1])

        if pos in self._active_positions:
            self._active_positions.remove(pos)
        return True

    # ── End-of-run ─────────────────────────────────────────────────────────
    def force_close_remaining(self) -> None:
        """Close any positions still active at the end of the backtest."""
        if not self._active_positions:
            return
        self._log.info(
            "Force-closing %d remaining position(s).",
            len(self._active_positions),
        )
        for pos in list(self._active_positions):
            self.force_close(pos)
        self._active_positions.clear()

    def force_close(self, position: dict) -> None:
        """Force-close equity position at last available manage bar."""
        if self.df_manage.empty:
            if position in self._active_positions:
                self._active_positions.remove(position)
            return
        sig = {
            "_position": position,
            "exit_idx": len(self.df_manage) - 1,
        }
        self.exit("", position, sig, forced=True, manage_df=self.df_manage)

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
