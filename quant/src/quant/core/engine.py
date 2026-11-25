"""Trading Engine — shared decision harness for backtest and live (strategy-dataflow).

``TradingEngine`` is the single deterministic decision entry point both
environments drive: the class name, import path, and public entry points
(``process_strategy(strategy, now, venue)`` and ``warmup(now)``) are
unchanged, only the internal pipeline is reshaped in place. It is no longer a
signal-string driver: it resolves the strategy's declared subscriptions,
serves closed bars from per-subscription ``_buffers`` (backtest replay or live
data source), feeds the author body ``decide(events, portfolio)`` the typed
event stream plus the injected read-only Portfolio, submits the returned typed
``Order``/``ExitIntent`` items to the venue's execution client, and records a
deterministic decision transcript — so replay of (event stream + body + venue)
reproduces identical decisions.

Gates, sizing, and the manage-pass live in the author body, not here; the venue
is the only seam that differs between backtest and live.
"""

from __future__ import annotations

import datetime
import logging
from typing import Any

import pandas as pd

from quant.observability import MonitorEmit, Span, SpanName
from quant.order import Order
from quant.portfolio import Portfolio as _Portfolio
from quant.strategies.base import (  # layering-ok (author items the engine routes)
    ExitIntent,
)
from quant.utils.strategy_config import BaseStrategyConfig
from quant.utils.time import closed_bar_asof
from quant.venue.base import to_utc_naive

__all__ = ["Events", "TradingEngine"]

logger = logging.getLogger(__name__)


def _is_daily_cadence(cadence: str) -> bool:
    """True for daily-or-longer bar cadences (``1d`` / ``1wk`` / ``1mo``).

    Daily decision bars are stamped at the session-date midnight and represent
    the whole trading session, so the intraday session-hours gate never applies
    to them.
    """
    unit = str(cadence).strip().lower()[-1:] if str(cadence).strip() else ""
    return unit in {"d", "w", "m"}


def _iso(value) -> str:
    """Deterministic ISO string for transcript/tests (tz-naive or naive-UTC).

    Normalizes through the venue boundary helper, the only sanctioned tz
    conversion on the decision path — never re-derived inline here.
    """
    try:
        return to_utc_naive(pd.Timestamp(value)).isoformat()
    except (TypeError, ValueError):
        return str(value)


class Events:
    """Typed event stream delivered to the author body on one processing tick.

    Holds the per-symbol closed-bar buffers the engine has served for the
    strategy's ``bars`` subscriptions. ``events.bars(symbol)`` returns the
    latest closed adjusted-bar frame for *symbol* (or ``None`` before that
    subscription has delivered); ``events.bars()`` with no argument returns a
    lone frame or, for a multi-subscription strategy, a dict keyed by symbol.
    """

    def __init__(self, buffers: dict[str, pd.DataFrame]):
        # ``buffers`` is the engine's per-symbol closed-bar cache (shared
        # reference) so manage-only ticks read the last served frame.
        self._buffers = buffers

    def bars(self, symbol: str | None = None):
        """Closed adjusted bars for *symbol* (or the lone/symbol-keyed frame)."""
        if symbol is None:
            if len(self._buffers) == 1:
                return next(iter(self._buffers.values()))
            return dict(self._buffers)
        return self._buffers.get(symbol)


def _order_record(order: Order) -> dict:
    """Deterministic transcript record for a typed ``Order`` (no wall-clock ts)."""
    return {
        "kind": "order",
        "side": order.side.value,
        "qty": float(order.quantity),
        "symbol": order.instrument.symbol,
        "venue": order.venue,
        "price_type": order.price_type.value,
        "price": order.price,
        "tag": order.tag,
        "strategy": order.strategy,
    }


def _exit_record(intent: ExitIntent) -> dict:
    """Deterministic transcript record for an ``ExitIntent`` manage-pass item."""
    pos = intent.position
    qty = float(getattr(pos, "qty", 0.0) or 0.0)
    return {
        "kind": "exit",
        "strategy": intent.strategy,
        "symbol": str(getattr(pos, "symbol", getattr(pos, "Ticker", "?"))),
        "qty": qty,
        "reason": intent.reason,
    }


def _item_record(item) -> dict:
    """Transcript record for one decision item (typed Order / ExitIntent / other)."""
    if isinstance(item, Order):
        return _order_record(item)
    if isinstance(item, ExitIntent):
        return _exit_record(item)
    return {"kind": "other", "value": str(item)}


def _fill_record(fill) -> dict | None:
    """Deterministic transcript record for one reported fill (or ``None``)."""
    if fill is None:
        return None
    return {
        "venue": str(getattr(fill, "venue", "")),
        "symbol": str(getattr(fill, "symbol", "")),
        "side": str(getattr(fill, "side", "")).lower(),
        "qty": float(getattr(fill, "qty", 0) or 0),
        "price": float(getattr(fill, "price", 0) or 0),
        "strategy": str(getattr(fill, "strategy", "")),
    }


class TradingEngine:
    """Shared decision harness used by both backtester and live trader.

    One per strategy. ``process_strategy`` serves the strategy's declared
    subscriptions, drives the author body over closed bars + the read-only
    Portfolio, submits typed orders/exit intents to the venue, and records a
    deterministic decision transcript. Scheduling is per-subscription cadence
    on closed data PLUS the venue's session calendar.
    """

    #: Max per-subscription buffers kept (bounded map keyed by subscription).
    _BUFFER_MAXLEN: int = 64
    #: Rolling lookback (calendar days) served to the author body per tick.
    _LOOKBACK_DAYS: int = 120

    def __init__(
        self,
        strategy,
        data_handler=None,
        *,
        monitor_emit: MonitorEmit | None = None,
        portfolio: _Portfolio | None = None,
    ):
        config = getattr(strategy, "config", None)
        if not isinstance(config, BaseStrategyConfig):
            raise TypeError(
                f"Strategy {type(strategy).__name__!r} config must be a "
                "BaseStrategyConfig instance"
            )
        self.strategy = strategy
        # Data handler fulfilling ``get_data(subscription, ...)`` (live
        # AlpacaDataHandler, or the seeded BacktestDataHandler in backtest).
        self.data_handler = data_handler
        self._monitor_emit = monitor_emit
        self._log = logging.getLogger(
            f"{__name__}.{getattr(strategy, 'name', 'engine')}"
        )
        # The account the author body sizes/exits against (read-only view is
        # injected into ``decide``). Wired by the host (Backtester / trader).
        self.portfolio: _Portfolio | None = portfolio

        # Per-subscription closed-bar cache keyed by ``Subscription.key()``
        # (bounded): feeds the author body's reads (backtest replay / live
        # source). ``_last_delivered`` tracks the bar-open served per key so a
        # subscription is driven only when its cadence advances.
        self._buffers: dict[tuple, pd.DataFrame] = {}
        self._last_delivered: dict[tuple, pd.Timestamp] = {}

        # Deterministic decision transcript (event stream + body + venue
        # effects) — the artifact replay/determinism goldens are pinned to.
        self.transcript: list[dict] = []

    # ─────────────────────────────────────────────────────────────────
    # Declared surface resolution
    # ─────────────────────────────────────────────────────────────────

    def _resolve_subscriptions(self, strategy=None) -> list[Any]:
        """The strategy's declared ``(instrument, datatype, cadence)`` list.

        Config is the source of truth, falling back to the strategy class/all
        instance attributes (the declarative ``Strategy`` base surface).
        """
        strategy = strategy or self.strategy
        config = getattr(strategy, "config", None)
        subs = list(getattr(config, "subscriptions", None) or [])
        if not subs:
            subs = list(getattr(strategy, "subscriptions", None) or [])
        return subs

    # ─────────────────────────────────────────────────────────────────
    # Warm start (off the decision path)
    # ─────────────────────────────────────────────────────────────────

    def warmup(self, now: datetime.datetime) -> bool:
        """Best-effort, off-path pre-warm of the per-subscription buffers.

        Seeds each declared subscription's window (the same lookback ``decide``
        would read) so the first live bar after market open is served from the
        warmed buffer. Runs off the decision path and emits no monitor events;
        a failure just means the first tick runs cold. Returns True if any
        subscription warmed.
        """
        if self.data_handler is None:
            return False
        get_data = getattr(self.data_handler, "get_data", None)
        if get_data is None:
            return False
        end = pd.Timestamp(now)
        warmed = False
        for sub in self._resolve_subscriptions():
            start = end - pd.Timedelta(days=self._LOOKBACK_DAYS)
            try:
                window = get_data(sub, start, end)
            except Exception as e:  # warm is best-effort
                self._log.exception("warmup failed for %s: %s", sub.key(), e)
                continue
            if window is not None and not window.empty:
                self._buffers[sub.key()] = window
                warmed = True
                self._last_delivered.setdefault(
                    sub.key(), closed_bar_asof(end, sub.cadence)
                )
        return warmed

    # ─────────────────────────────────────────────────────────────────
    # Closed-bar serving
    # ─────────────────────────────────────────────────────────────────

    def _serve_window(self, sub, now: pd.Timestamp, venue) -> pd.DataFrame:
        """Serve *sub*'s closed adjusted bars through ``closed_bar_asof(now)``.

        Reads through the venue's wired data source (or the engine's handler)
        ``get_data(subscription, start, end)``; out-of-range/empty windows
        return an empty frame without raising.
        """
        end = closed_bar_asof(now, sub.cadence)
        start = end - pd.Timedelta(days=self._LOOKBACK_DAYS)
        source = getattr(venue, "data_source", None) or self.data_handler
        get_data = getattr(source, "get_data", None)
        if get_data is None:
            get_data = getattr(self.data_handler, "get_data", None)
        if get_data is None:
            return pd.DataFrame()
        try:
            window = get_data(sub, start, end)  # ty: ignore[no-untyped-call]
        except Exception as e:  # pragma: no cover - defensive boundary
            self._log.exception(
                "[%s] data source error serving %s: %s",
                getattr(self.strategy, "name", "?"),
                sub.key(),
                e,
            )
            return pd.DataFrame()
        if window is None:
            return pd.DataFrame()
        return window if isinstance(window, pd.DataFrame) else pd.DataFrame()

    def _due_subscriptions(self, subs: list[Any], now: pd.Timestamp) -> list[Any]:
        """Subscriptions whose cadence has advanced past the last served bar.

        A subscription is due when ``closed_bar_asof(now, cadence)`` is newer
        than the last bar-open already delivered, or it has never delivered —
        so manage-only ticks naturally skip subscriptions that have not closed
        a new bar (no signal fetches, per spec `decision-engine`).
        """
        due: list[Any] = []
        for sub in subs:
            last_closed = closed_bar_asof(now, sub.cadence)
            prev = self._last_delivered.get(sub.key())
            if prev is None or last_closed > prev:
                due.append(sub)
        return due

    # ─────────────────────────────────────────────────────────────────
    # Top-level orchestration (live + backtest entrypoint)
    # ─────────────────────────────────────────────────────────────────

    def process_strategy(self, strategy, now, venue) -> None:
        """Run one processing tick for *strategy* at *now* through *venue*.

        ``venue`` is the wired ``Venue`` (backtest or live): its session
        calendar gates the run (a closed session skips the body entirely) and
        its ``execution_client`` is the fill seam. The author body decides over
        the closed bars of the due subscriptions plus the injected read-only
        portfolio; returned typed ``Order``/``ExitIntent`` items are submitted
        to the venue and recorded in the deterministic decision transcript.
        """
        s_name: str = getattr(strategy, "name", type(strategy).__name__)
        now_ts = pd.Timestamp(now)

        subs = self._resolve_subscriptions(strategy)
        if not subs:
            self._log.warning("[%s] No subscriptions declared — skipping.", s_name)
            return

        due = self._due_subscriptions(subs, now_ts)
        if not due:
            return

        # Closed-session gate (spec `decision-engine` Bar-driven scheduling):
        # an intraday decision tick outside the venue's session hours does not
        # run new entries. A daily-cadence decision bar represents its whole
        # trading session (stamped at session midnight), so it is never gated
        # by intraday session hours.
        intraday_due = [s for s in due if not _is_daily_cadence(s.cadence)]
        is_open = getattr(venue, "is_open_at", None)
        if intraday_due and callable(is_open) and not is_open(now_ts):
            return

        # Serve the due subscriptions' closed bars into the per-subscription
        # buffers; non-due subscriptions keep their last served frame.
        due_keys: list[tuple] = []
        for sub in due:
            key = sub.key()
            self._buffers[key] = self._serve_window(sub, now_ts, venue)
            self._last_delivered[key] = closed_bar_asof(now_ts, sub.cadence)
            due_keys.append(key)

        # Author body over typed events + the read-only portfolio.
        by_symbol: dict[str, pd.DataFrame] = {}
        for sub in due:
            by_symbol[sub.instrument.symbol] = self._buffers[sub.key()]
        events = Events(by_symbol)
        portfolio_view = self._portfolio_for_decision()

        decisions: list[Any] = []
        with Span(
            SpanName.DECIDE,
            emit=self._monitor_emit,
            event={"kind": "decide", "strategy": s_name},
        ):
            decisions = strategy.decide(events, portfolio_view) or []

        # Record the deterministic transcript, then submit to the venue.
        cadence = due[0].cadence if hasattr(due[0], "cadence") else "1h"
        fills = self.submit_decisions(
            venue, self.portfolio, decisions, now=now_ts, cadence=cadence
        )
        self.transcript.append(
            {
                "now": _iso(now_ts),
                "strategy": s_name,
                "due_subscriptions": [tuple(k) for k in due_keys],
                "decisions": [_item_record(d) for d in decisions],
                "fills": [None if f is None else _fill_record(f) for f in fills],
            }
        )

    def _portfolio_for_decision(self):
        """The read-only portfolio the author body receives for this tick."""
        portfolio = self.portfolio
        if portfolio is None:
            portfolio = _Portfolio(starting_equity=self._starting_equity())
        return portfolio.read_only()

    def _starting_equity(self) -> float:
        config = getattr(self.strategy, "config", None)
        return float(getattr(config, "INITIAL_CAPITAL", 100_000.0))

    # ─────────────────────────────────────────────────────────────────
    # Order submission to the venue
    # ─────────────────────────────────────────────────────────────────

    def submit_decisions(
        self,
        venue: Any,
        portfolio,
        decisions: list[Any],
        *,
        now=None,
        cadence: str | None = None,
    ) -> list[Any]:
        """Submit the author body's decisions to *venue* and report fills.

        Every typed ``Order`` is submitted to the venue's execution client;
        each ``ExitIntent`` is translated into a closing typed ``Order`` for
        the position (opposite side, open quantity) built on the position's
        instrument, then submitted. Returns one reported fill per decision
        item (``None`` for a rejected/gated order). Non-order decision items
        (unknowns) are passed through unchanged as ``None`` fills.
        """
        execution_client = getattr(venue, "execution_client", None) or venue
        submit = getattr(execution_client, "submit", None)
        if submit is None:
            self._log.warning(
                "[%s] venue %s has no execution_client.submit — skipping decisions",
                getattr(self.strategy, "name", "?"),
                getattr(venue, "id", str(venue)),
            )
            return [None] * len(decisions)

        fills: list[Any] = []
        for item in decisions:
            if isinstance(item, ExitIntent):
                order = self._exit_to_order(item)
                if order is None:
                    fills.append(None)
                    continue
            elif isinstance(item, Order):
                order = item
            else:
                fills.append(None)
                continue
            fills.append(submit(order, now=now, cadence=cadence))
        return fills

    def _exit_to_order(self, intent: ExitIntent) -> Order | None:
        """A closing typed ``Order`` for one manage-pass ``ExitIntent``."""
        position = intent.position
        if position is None:
            return None
        instrument = getattr(position, "instrument", None)
        if instrument is None:
            instrument = self._instrument_for(position)
        if instrument is None:
            self._log.warning(
                "[%s] exit intent %s has no instrument — skipping exit",
                getattr(self.strategy, "name", "?"),
                intent,
            )
            return None
        from quant.portfolio import Side

        direction = int(getattr(position, "direction", 1) or 1)
        side = Side.SELL if direction > 0 else Side.BUY
        qty = float(getattr(position, "qty", 0.0) or 0.0)
        if qty <= 0:
            return None
        return instrument.make_order(side, qty, strategy=intent.strategy)

    def _instrument_for(self, position) -> Any:
        """Resolve a position's typed instrument from the strategy declarations."""
        symbol = getattr(position, "symbol", None) or getattr(position, "Ticker", None)
        if not symbol:
            return None
        for inst in self._declared_instruments():
            if inst.symbol == symbol:
                return inst
        return None

    def _declared_instruments(self) -> list[Any]:
        infra: list[Any] = []
        config = getattr(self.strategy, "config", None)
        insts = list(getattr(config, "instruments", None) or [])
        if not insts:
            insts = list(getattr(self.strategy, "instruments", None) or [])
        infra = [sub.instrument for sub in self._resolve_subscriptions()]
        merged = list(insts) + infra
        out: list[Any] = []
        seen: set[tuple] = set()
        for i in merged:
            key = i.key()
            if key not in seen:
                seen.add(key)
                out.append(i)
        return out
