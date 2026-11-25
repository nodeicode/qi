"""Live venue execution client — the live venue's broker-bound client.

The live layer's ``LiveExecutor`` / ``UnifiedExecutor`` pair
(``quant/live/executors/*``, deleted) reshapes into this single object, the
execution client the live venue (``AlpacaVenue``) holds alongside its session
calendar and data source. It owns:

* **gated submission** — a typed order is checked against the venue's session
  (market open / entry window) before any broker call; a gated order returns
  ``None`` (rejected in the decision transcript) and never reaches the broker
  or the Portfolio,
* **fill reconciliation** — after placement the venue reports the order's
  outcome (full / partial fill, filled average price, remaining quantity),
  tracked per order id and reported via :meth:`LiveExecutionClient.order_report`,
* **portfolio feed** — every reconciled fill (partial or full) streams through
  the same ``Portfolio`` machinery the backtest venue uses, so the strategy
  reads live fills in the identical position schema (parity by construction),
  and
* the **shared-loop duck surface** (``should_run`` / ``data_end_dt`` /
  ``get_precomputed_signal`` / ``active_positions`` / ``enter`` / ``exit`` /
  lifecycle hooks / ``record_run``) that ``engine.process_strategy`` still
  drives while the engine loop is migrated to typed submission.

Failures surface as errors: a broker or reconciliation failure raises rather
than being silently swallowed; only a *gated/rejected* order returns ``None``.
"""

from __future__ import annotations

import datetime
import logging
import os
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

import pandas as pd

from quant.instruments import EquityInstrument
from quant.order import Order, OrderPriceType
from quant.portfolio import Fill, Side
from quant.utils.time import calculate_entry_time, closed_bar_asof

# Fallback delay when a strategy config omits DATA_DELAY (Alpaca free-tier
# delivers data with a 15-minute delay). Strategy ``config.DATA_DELAY`` wins
# per strategy; this value is only the env fallback (mirrors the legacy live
# executor).
_LIVE_DATA_DELAY_MINUTES: int = int(os.environ.get("LIVE_DATA_DELAY_MINUTES", "15"))


@dataclass(frozen=True)
class FillReport:
    """Reconciled outcome of one submitted live order (venue-reported).

    ``requested_qty`` is what the broker accepted, ``filled_qty`` what actually
    printed (equal for a full fill, less for a partial fill), ``remaining_qty``
    what is still open on the order, ``status`` the venue's order status, and
    ``avg_price`` the venue-reported average fill price (``None`` when the
    venue did not surface one).
    """

    order_id: str
    status: str
    requested_qty: float
    filled_qty: float
    remaining_qty: float
    avg_price: float | None


class _LocalEquityLedger:
    """Minimal per-account equity tracker for the live client's duck sizing.

    Reads broker equity lazily from ``client.get_account()`` (on first use,
    then after a TTL) and keeps ``debit`` / ``credit`` deltas applied between
    reads. The trader injects ``MultiClientEquityManager`` — which exposes the
    same ``get`` / ``debit`` / ``credit`` / ``set_active`` surface — so
    concurrent paper/prod strategies keep isolated balances; direct client
    tests use this default. This is the shared-loop's pre-declarative sizing
    bookkeeping only: declarative author bodies size from the injected
    Portfolio instead.
    """

    SYNC_TTL: float = 300.0

    def __init__(
        self,
        clients: dict[str, Any],
        logger: logging.Logger | None = None,
    ) -> None:
        self._clients: dict[str, Any] = dict(clients or {})
        self._logger = logger or logging.getLogger(__name__)
        self._balances: dict[str, float] = {}
        self._synced: dict[str, float] = {}
        self._active: str | None = next(iter(self._clients), None)
        self._lock = threading.RLock()

    @property
    def active_client(self) -> str | None:
        """Name of the account currently selected for the calling thread."""
        return self._active

    def set_active(self, client_name: str) -> bool:
        """Select *client_name*'s account. Returns False when unknown."""
        if client_name not in self._clients:
            return False
        self._active = client_name
        return True

    def get(self) -> float:
        """Current balance of the active account (lazy broker sync)."""
        with self._lock:
            name = self._active
            if name is None:
                return 0.0
            if name not in self._balances or (
                time.monotonic() - self._synced.get(name, 0.0) > self.SYNC_TTL
            ):
                self._sync(name)
            return self._balances.get(name, 0.0)

    def debit(self, amount: float) -> None:
        """Subtract deployed capital after an entry."""
        with self._lock:
            name = self._active
            if name is None:
                return
            self._balances[name] = max(
                0.0, self._balances.get(name, 0.0) - float(amount)
            )

    def credit(self, amount: float) -> None:
        """Add freed capital back after an exit."""
        with self._lock:
            name = self._active
            if name is None:
                return
            self._balances[name] = self._balances.get(name, 0.0) + float(amount)

    def _sync(self, name: str) -> None:
        """Fetch the account balance from the broker (fail soft to last known)."""
        client = self._clients.get(name)
        if client is None:
            self._balances[name] = 0.0
            self._synced[name] = time.monotonic()
            return
        try:
            account = client.get_account()
            raw = (
                account.get("equity")
                if isinstance(account, dict)
                else getattr(account, "equity", "0")
            )
            self._balances[name] = float(raw or 0)
        except Exception as exc:
            self._logger.warning("[equity] failed to sync %s: %s", name, exc)
            self._balances[name] = self._balances.get(name, 0.0)
        self._synced[name] = time.monotonic()


class LiveExecutionClient:
    """Live execution client: gated submission + partial-fill reconciliation.

    All external bindings are injected explicitly (venue, broker clients,
    shared state dicts, portfolio, ledger, logger, lock, persistence hook) so
    the client is self-contained and testable without broker credentials.
    """

    def __init__(
        self,
        *,
        venue=None,
        clients: dict[str, Any] | None = None,
        strategies: list[Any] | None = None,
        active_positions: dict[str, list[Any]] | None = None,
        portfolio: Any | None = None,
        logger: logging.Logger | None = None,
        lock: threading.RLock | None = None,
        save_state: Callable[[], None] | None = None,
        last_update: dict | None = None,
        last_manage_update: dict | None = None,
        equity_mgr: Any | None = None,
        delay_minutes: int = _LIVE_DATA_DELAY_MINUTES,
        fill_resolver: Callable[[str, Order, Any], FillReport] | None = None,
        price_source: Callable[[Order], float | None] | None = None,
    ) -> None:
        self.venue = venue
        # Shared collections are kept by reference (when passed) so the trader's
        # live mutation (config-sync strategy churn, position bookkeeping) flows
        # through the same objects instead of silently forking.
        self._clients = clients if clients is not None else {}
        self._strategies = strategies if strategies is not None else []
        self._active_positions = (
            active_positions if active_positions is not None else {}
        )
        self._portfolio = portfolio
        self._log = logger or logging.getLogger(__name__)
        self._lock = lock or threading.RLock()
        self._save_state = save_state or (lambda: None)
        self.last_update = last_update if last_update is not None else {}
        self.last_manage_update = (
            last_manage_update if last_manage_update is not None else {}
        )
        self.delay_minutes: int = int(delay_minutes)
        self._fill_resolver = fill_resolver
        self._price_source = price_source
        self.equity_mgr = equity_mgr or _LocalEquityLedger(self._clients, self._log)
        # order_id -> FillReport: reconciled (incl. partial) fill outcomes.
        self._reports: dict[str, FillReport] = {}
        # Tick timestamp the shared loop stamps; used as the submission clock.
        self._now: datetime.datetime | None = None
        # Per-thread delay/strategy: the shared-loop duck methods see only the
        # strategy this thread is currently processing (pool concurrency).
        self._tls = threading.local()

    # ── Scheduling (bar-driven, absorbed from the deleted LiveExecutor) ───
    def should_run(self, strategy, now) -> tuple[bool, bool]:
        """Return ``(should_signal, should_manage)`` for this live tick.

        Bar-driven: fires when ``closed_bar_asof(data_end_dt)`` advances past
        the bar implied by the last recorded run. When
        ``MANAGE_INTERVAL == INTERVAL`` both flags share one check.
        """
        now = self._coerce_now(now)
        self._now = now
        s_name = strategy.name
        cfg = getattr(strategy, "config", None)
        interval = getattr(cfg, "INTERVAL", "1h")
        manage_interval = getattr(cfg, "MANAGE_INTERVAL", interval)
        delay = self._delay_for(strategy)
        self._tls.strategy_name = s_name
        self._tls.delay = delay

        end_dt = now - datetime.timedelta(minutes=delay)
        bar_ts = closed_bar_asof(end_dt, interval)

        should_signal = True
        last_run = self.last_update.get(s_name)
        if last_run is not None:
            last_run = self._coerce_now(last_run)
            last_bar = closed_bar_asof(
                last_run - datetime.timedelta(minutes=delay), interval
            )
            if pd.Timestamp(bar_ts) <= pd.Timestamp(last_bar):
                should_signal = False

        if manage_interval == interval:
            should_manage = should_signal
        else:
            manage_bar = closed_bar_asof(end_dt, manage_interval)
            should_manage = True
            last_manage = self.last_manage_update.get(s_name)
            if last_manage is not None:
                last_manage = self._coerce_now(last_manage)
                last_m_bar = closed_bar_asof(
                    last_manage - datetime.timedelta(minutes=delay), manage_interval
                )
                if pd.Timestamp(manage_bar) <= pd.Timestamp(last_m_bar):
                    should_manage = False
        return should_signal, should_manage

    def data_end_dt(self, now):
        """Live: ``now - delay`` (delay from the calling thread's last should_run)."""
        now = self._coerce_now(now)
        self._now = now
        delay = getattr(self._tls, "delay", self.delay_minutes)
        return now - datetime.timedelta(minutes=int(delay))

    def get_precomputed_signal(self, ticker: str, now) -> pd.DataFrame | None:
        """Live always runs the full signal pipeline — never precomputed."""
        return None

    # ── Data / state (absorbed from LiveExecutor) ─────────────────────────
    def active_positions(self, strategy_name: str) -> list[Any]:
        """Snapshot copy of the strategy's open position dicts.

        The backing dict is shared across strategy threads (config sync,
        recovery); returning a shallow copy keeps the engine's iteration stable.
        """
        return list(self._active_positions.get(strategy_name, []))

    def after_process_ticker(
        self, strategy_name: str, ticker: str, results: list[dict]
    ) -> None:
        """No live-side per-ticker instrumentation needed."""
        return None

    def after_entry(self, strategy_name: str, position: dict) -> None:
        """Append *position* to the live book and persist state."""
        with self._lock:
            self._active_positions.setdefault(strategy_name, []).append(position)
            self._save_state()

    def after_exit(self, strategy_name: str, position: dict) -> None:
        """Remove *position* from the live book and persist state."""
        with self._lock:
            positions = self._active_positions.get(strategy_name, [])
            if position in positions:
                positions.remove(position)
            self._save_state()

    def record_run(self, strategy_name: str, now, ran_signal: bool) -> None:
        """Stamp ``last_update`` (signal phase) and ``last_manage_update``."""
        now = self._coerce_now(now)
        self._now = now
        with self._lock:
            if ran_signal:
                self.last_update[strategy_name] = now
            self.last_manage_update[strategy_name] = now

    # ── Typed-order seam (spec `execution` Live orders … venue-bound clients)
    def submit(
        self,
        order: Order,
        *,
        now=None,
        reference_price: float | None = None,
    ) -> Fill | None:
        """Gate, place, and reconcile *order*.

        Verifies the venue session (market open / entry window) before any
        broker call — a gated order returns ``None`` (rejected) and never
        reaches the broker or the Portfolio. The venue's reported fill (full or
        partial) is applied to the Portfolio and returned as a ``Fill`` bearing
        the filled quantity. Broker/reconciliation failures raise so they
        surface as errors rather than silent no-ops.
        """
        now = self._coerce_now(now) if now is not None else (self._now or None)
        allowed, reason = self._gate_allows(order, now)
        if not allowed:
            self._log.info(
                "[%s] order %s rejected: %s",
                order.strategy,
                order.instrument.symbol,
                reason,
            )
            return None
        if float(order.quantity) < 1:
            self._log.warning(
                "[%s] order %s has qty %s < 1 — skipped",
                order.strategy,
                order.instrument.symbol,
                order.quantity,
            )
            return None
        try:
            return self._place_and_reconcile(order, now, reference_price)
        except Exception:
            self._log.exception(
                "[%s] live submission failed for %s",
                order.strategy,
                order.instrument.symbol,
            )
            raise

    def order_report(self, order_id: str) -> FillReport | None:
        """Reconciled outcome of the (possibly partial) submitted *order_id*."""
        return self._reports.get(order_id)

    # ── Shared-loop duck surface (absorbed from the executors) ────────────
    def enter(self, strategy, ticker: str, signal_row: dict, engine, *, now=None):
        """Legacy shared-loop entry: size, build a typed order, submit gated.

        The duck surface ``engine.process_strategy`` still drives while the
        engine loop is migrated to typed submission; sizing here is the
        pre-declarative legacy leg (account equity x config capital fraction),
        which the declarative author body replaces by sizing from instrument
        contract facts + the injected Portfolio. Returns the new position dict,
        or ``None`` when gated / skipped / rejected.
        """
        now = self._coerce_now(now) if now is not None else (self._now or None)
        s_name = getattr(strategy, "name", "?")
        client = self._client_for(strategy)
        if client is None:
            self._log.error(
                "[%s] broker client '%s' not found in trading clients — skipping %s",
                s_name,
                getattr(strategy, "client_name", "?"),
                ticker,
            )
            return None
        entry_price = float(signal_row.get("Close") or 0)
        if entry_price <= 0:
            self._log.warning(
                "[%s] no positive close for %s — skipping entry", s_name, ticker
            )
            return None
        signal = str(signal_row.get("Signal", "BUY")).upper()
        direction = engine.direction(signal)
        side = "BUY" if direction >= 0 else "SELL"
        if not self.equity_mgr.set_active(getattr(strategy, "client_name", "")):
            self._log.error(
                "[%s] client '%s' not registered in the equity manager — skipping %s",
                s_name,
                getattr(strategy, "client_name", "?"),
                ticker,
            )
            return None
        equity = self.equity_mgr.get()
        pos_size = float(
            signal_row.get("PositionSize", signal_row.get("Position_Size", 1.0))
        )
        capital_frac = float(getattr(strategy.config, "CAPITAL_PER_TRADE", 0.25))
        effective_capital = equity * capital_frac * pos_size
        qty = int(effective_capital / entry_price) if entry_price > 0 else 0
        if qty < 1:
            self._log.warning("[%s] qty < 1 for %s — skipping entry", s_name, ticker)
            return None

        instrument = EquityInstrument(symbol=ticker, venue=self._venue_id())
        order = instrument.make_order(side, qty, strategy=s_name)
        fill = self.submit(order, now=now, reference_price=entry_price)
        if fill is None:
            return None

        confidence = float(signal_row.get("Confidence", 0.0))
        position = dict(
            engine.build_position(
                self._entry_time_iso(signal_row, strategy),
                float(fill.price),
                direction,
                signal,
                confidence,
                Strategy=s_name,
                Ticker=ticker,
                UnderlyingEntryPrice=entry_price,
                Qty=float(fill.qty),
                PositionSize=pos_size,
                Vol_Ratio=float(signal_row.get("Vol_Ratio", 1.0)),
                OrderId=fill.order_id or "",
                Env=getattr(strategy, "env", ""),
                AssetType="EQUITY",
                StopLoss=signal_row.get("StopLoss"),
                TakeProfit=signal_row.get("TakeProfit"),
            )
        )
        position["_strategy_name"] = s_name
        # Partial-fill bookkeeping reported in the position schema (spec
        # `execution` Partial fill tracked): filled qty + remaining + status.
        report = self.order_report(fill.order_id or "")
        if report is not None:
            position["OrderStatus"] = report.status
            position["FilledQty"] = report.filled_qty
            position["RemainingQty"] = report.remaining_qty
        self.equity_mgr.debit(float(fill.qty) * float(fill.price))
        return position

    def exit(
        self,
        strategy_name: str,
        position: dict,
        sig: dict,
        *,
        manage_df=None,
    ) -> bool:
        """Legacy shared-loop exit: submit a market order for the open qty.

        The close fill is reconciled through the same gated path and streams
        into the Portfolio (an opposing side reduces/closes the position).
        Returns True when an exit order was placed and filled; a gated/rejected
        exit returns False (nothing placed).
        """
        strategy = next((s for s in self._strategies if s.name == strategy_name), None)
        if strategy is None:
            self._log.error("[%s] no live strategy found for exit", strategy_name)
            return False
        if self._client_for(strategy) is None:
            return False
        symbol = position.get("Ticker") or position.get("symbol")
        qty = float(position.get("Qty", 0) or 0)
        if not symbol or qty <= 0:
            self._log.error(
                "[%s] exit has no symbol/qty for %s",
                strategy_name,
                position.get("Ticker", "?"),
            )
            return False
        direction = int(position.get("Direction", 1))
        side = "SELL" if direction >= 0 else "BUY"
        instrument = EquityInstrument(symbol=symbol, venue=self._venue_id())
        order = instrument.make_order(side, qty, strategy=strategy_name)
        try:
            fill = self.submit(order)
        except Exception as exc:
            self._log.exception(
                "[%s] exit submit failed for %s: %s", strategy_name, symbol, exc
            )
            return False
        if fill is None:
            return False
        self._log.info(
            "[%s] EXIT %s %s qty=%.1f id=%s reason=%s",
            strategy_name,
            side,
            symbol,
            qty,
            fill.order_id or "",
            sig.get("reason", "Strategy Signal"),
        )
        entry_price = float(position.get("EntryPrice", 0.0) or 0.0)
        if entry_price > 0:
            self.equity_mgr.credit(float(fill.qty) * entry_price)
        return True

    # ── Internals ─────────────────────────────────────────────────────────
    def _gate_allows(self, order: Order, now) -> tuple[bool, str]:
        """True when the venue session permits an entry at *now*.

        Fails closed: when the session lookup itself errors (calendar down),
        the order is rejected rather than placed into an unknown market. ``now``
        is ``None`` only for deferred calls made outside a tick — those skip the
        clock gate (the broker still validates).
        """
        venue = self._venue_or_none()
        if venue is None or now is None:
            return True, ""
        try:
            if not venue.allows_entry_at(now):
                return False, (
                    f"venue '{getattr(venue, 'id', 'live')}' is not open for "
                    f"entries at {now}"
                )
        except Exception as exc:  # calendar failure is not a trading signal
            self._log.warning(
                "session gate failed at %s (%s); rejecting order %s",
                now,
                exc,
                order.instrument.symbol,
            )
            return False, f"session lookup failed: {exc}"
        return True, ""

    def _place_and_reconcile(
        self, order: Order, now, reference_price: float | None
    ) -> Fill:
        """Place the order on the broker and apply its reconciled fill."""
        client = self._client_for_order(order)
        if client is None:
            raise RuntimeError(
                f"no broker client to place order for {order.instrument.symbol}"
            )
        order_id = self._submit_to_broker(order, client)
        # Reconciliation is an injected resolver (the broker's async order
        # surface, or an offline stub). With no resolver, an accepted order is
        # treated as a full fill at the resolved price — the client never
        # invents a partial-fill split without a venue to report it.
        report = (
            self._fill_resolver(order_id, order, client)
            if self._fill_resolver is not None
            else FillReport(
                order_id, "FILLED", order.quantity, order.quantity, 0.0, None
            )
        )
        self._reports[order_id] = report
        if report.filled_qty <= 0:
            raise RuntimeError(
                f"order {order_id} for {order.instrument.symbol} reported no "
                f"fill (status={report.status})"
            )
        price = report.avg_price
        if price is None:
            price = reference_price
        if price is None and order.price_type is OrderPriceType.LIMIT:
            price = order.price
        if price is None and self._price_source is not None:
            price = self._price_source(order)
        if price is None or float(price) <= 0:
            raise RuntimeError(
                f"order {order_id} filled without a reportable positive price"
            )
        fill = Fill(
            venue=order.venue,
            symbol=order.instrument.symbol,
            side=order.side,
            qty=float(report.filled_qty),
            price=float(price),
            strategy=order.strategy,
            instrument=order.instrument,
            timestamp=now,
            order_id=order_id,
        )
        if self._portfolio is not None:
            self._portfolio.apply_fill(fill)
        return fill

    def _submit_to_broker(self, order: Order, client: Any) -> str:
        """Place the (market or limit) order; return the broker order id."""
        from alpaca.trading.requests import (  # heavy import — keep lazy
            LimitOrderRequest,
            MarketOrderRequest,
            OrderSide,
            TimeInForce,
        )

        side_enum = OrderSide.BUY if order.side is Side.BUY else OrderSide.SELL
        common = {
            "symbol": order.instrument.symbol,
            "qty": order.quantity,
            "side": side_enum,
            "time_in_force": TimeInForce.DAY,
        }
        if order.price_type is OrderPriceType.LIMIT and order.price is not None:
            req = LimitOrderRequest(**common, limit_price=float(order.price))
        else:
            req = MarketOrderRequest(**common)
        submitted = client.submit_order(req)
        if isinstance(submitted, dict):
            order_id = str(submitted.get("id", "") or "")
        else:
            order_id = str(getattr(submitted, "id", "") or "")
        if not order_id:
            raise RuntimeError(
                f"broker returned no order id for {order.instrument.symbol}"
            )
        return order_id

    def _client_for(self, strategy) -> Any | None:
        """Broker client bound to *strategy*'s declared account, or None."""
        name = getattr(strategy, "client_name", None)
        if name is None:
            return None
        return self._clients.get(name)

    def _client_for_order(self, order: Order) -> Any | None:
        """Broker client for an order's declaring strategy (first-client fallback)."""
        if order.strategy is not None:
            for s in self._strategies:
                if s.name == order.strategy:
                    return self._client_for(s)
        # Unknown strategy name (tests / ad-hoc declarative orders) routes to
        # the first registered client; an empty ledger fails loudly upstream.
        return next(iter(self._clients.values()), None)

    def _venue_id(self) -> str:
        """Stable venue id stamped on synthetic duck-path instruments."""
        venue = self._venue_or_none()
        return getattr(venue, "id", "alpaca") if venue is not None else "alpaca"

    def _venue_or_none(self):
        """The live venue currently bound (swap-able for a session stub)."""
        return self.venue

    def _delay_for(self, strategy) -> int:
        """Config ``DATA_DELAY`` wins; the constructor/env value is the fallback."""
        cfg = getattr(strategy, "config", None)
        if cfg is not None and hasattr(cfg, "DATA_DELAY"):
            try:
                return int(cfg.DATA_DELAY)
            except (TypeError, ValueError):
                return int(self.delay_minutes)
        return int(self.delay_minutes)

    @staticmethod
    def _coerce_now(now: datetime.datetime) -> datetime.datetime:
        """Canonicalise a scalar timestamp to tz-naive UTC (engine contract)."""
        if getattr(now, "tzinfo", None) is not None:
            return now.astimezone(datetime.UTC).replace(tzinfo=None)
        return now

    @staticmethod
    def _entry_time_iso(signal_row: dict[str, Any], strategy) -> str:
        """Shared T+I entry clock from ``_bar_time``; wall clock is fallback."""
        bar_time = signal_row.get("_bar_time")
        if bar_time is not None:
            interval = getattr(strategy.config, "INTERVAL", "15m")
            return pd.Timestamp(calculate_entry_time(bar_time, interval)).isoformat()
        return datetime.datetime.now(datetime.UTC).isoformat()
