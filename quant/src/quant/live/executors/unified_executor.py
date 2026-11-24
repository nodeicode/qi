import datetime
import logging
import threading
from typing import Any

import pandas as pd
from alpaca.trading.client import TradingClient
from alpaca.trading.models import Order
from alpaca.trading.requests import MarketOrderRequest, OrderSide, TimeInForce

from quant.core.engine import TradingEngine
from quant.core.sizing import size_for_signal
from quant.data.data_handler import AlpacaDataHandler
from quant.live.protocols import LivePositionDict, LiveStrategyProtocol
from quant.live.utils import MultiClientEquityManager
from quant.portfolio import Fill, Side
from quant.utils.time import calculate_entry_time


def _entry_time_iso(signal_row: dict[str, Any], strategy) -> str:
    """Shared T+I entry clock from ``_bar_time``; wall clock only as fallback."""
    bar_time = signal_row.get("_bar_time")
    interval = getattr(strategy.config, "INTERVAL", "15m")
    if bar_time is not None:
        return pd.Timestamp(calculate_entry_time(bar_time, interval)).isoformat()
    return datetime.datetime.now(datetime.UTC).isoformat()


class UnifiedExecutor:
    """
    Equity execution atom: multi-client order submission + in-memory equity.

    Routes equity signals through ``_enter_linear_trade`` / ``exit_trade`` and
    warns-and-skips option/IC-class signals (the IC/option live executors were
    removed with the equity-only trim).  Thread-local client binding keeps
    concurrent paper/prod strategies on their own accounts.
    """

    def __init__(
        self,
        clients: dict[str, TradingClient],
        data_handler: AlpacaDataHandler,
        logger: logging.Logger,
        *,
        portfolio=None,
    ) -> None:
        if not clients:
            raise ValueError("UnifiedExecutor requires at least one trading client.")
        # Per-thread active broker client.  This executor is shared by
        # strategies that run concurrently, one per thread; each thread arms
        # its own binding via ``set_client`` before entering or exiting.  A
        # single shared mutable ``client`` slot would let thread A's
        # set_client → submit sequence be crossed by thread B's set_client —
        # submitting A's order to B's broker account and sizing A's trade
        # from B's equity.  Threads that never arm fall back to the first
        # registered client.
        self._default_client: TradingClient = next(iter(clients.values()))
        self._tls = threading.local()
        self.data_handler = data_handler
        self.logger = logger
        self.equity_mgr = MultiClientEquityManager(clients, logger)
        # Optional live portfolio collector (spec `portfolio`): same primitives
        # as backtest; fills are derived from venue execution events.
        self.portfolio = portfolio

    def _resolve_venue(self) -> str:
        """Broker account key for the calling thread's client (the venue leg)."""
        for key, client in self.data_handler.clients.items():
            if client is self.client:
                return key
        return "live"

    # -------------------------------------------------------------------------
    # Client resolution (thread-local)
    # -------------------------------------------------------------------------

    @property
    def client(self) -> TradingClient:
        """Broker client for the calling thread.

        Set via ``set_client``; falls back to the first registered client on
        threads that never armed one.
        """
        return getattr(self._tls, "client", self._default_client)

    @client.setter
    def client(self, value: TradingClient) -> None:
        """Arm the calling thread's broker client.

        ``None`` un-arms the thread back to the default fallback.
        """
        if value is None:
            self._tls.__dict__.pop("client", None)
        else:
            self._tls.client = value

    # -------------------------------------------------------------------------
    # Client switching
    # -------------------------------------------------------------------------

    def set_client(self, client_name: str, client: TradingClient) -> bool:
        """
        Switch the active broker client and its equity tracker atomically.

        Call this before entering/exiting trades whenever the strategy client
        changes (paper ↔ prod).  Both the executor's client resolution and
        the shared ``MultiClientEquityManager`` are updated in one step so
        that sizing and debit/credit always reference the correct account.

        The binding is **per-thread**: it applies to the calling strategy
        thread only, so concurrent paper/prod strategies never cross
        accounts.  Returns ``True`` on success, ``False`` if *client_name*
        is not registered in the equity manager (signals should be skipped).
        """
        if not self.equity_mgr.set_active(client_name):
            return False
        self.client = client
        return True

    # -------------------------------------------------------------------------
    # In-memory equity management
    # -------------------------------------------------------------------------
    # Delegated entirely to MultiClientEquityManager (python/utils.py).
    # Call self.equity_mgr.get() / .debit(amount) / .credit(amount) directly.

    def enter_trade(
        self,
        strategy: LiveStrategyProtocol,
        ticker: str,
        signal_row: dict[str, Any],
        engine: TradingEngine | None = None,
    ) -> LivePositionDict | None:
        """
        Unified entry point for all asset classes.

        When *engine* is provided (from trader.py), routing and sizing use
        the shared ``TradingEngine`` — the same code path as the backtester.
        """
        try:
            signal = str(signal_row.get("Signal", "")).upper()

            if engine is None:
                self.logger.error(
                    f"[{strategy.name}] enter_trade requires TradingEngine — skipping {ticker}."
                )
                return None

            asset_class = engine.route(signal)

            self.logger.info(
                f"[{strategy.name}] Entering {asset_class}: {ticker}  signal={signal}"
            )

            if asset_class in ("IC", "OPTION", "SPREAD"):
                self.logger.warning(
                    f"[{strategy.name}] {asset_class} execution is not supported "
                    f"by the equity-only live executor for {ticker} — skipping."
                )
                return None
            return self._enter_linear_trade(
                strategy, ticker, signal_row, asset_class, engine=engine
            )

        except Exception as e:
            self.logger.exception(
                f"[{strategy.name}] Error entering trade for {ticker}: {e}"
            )
            return None

    def exit_trade(self, pos: LivePositionDict, qty: float) -> bool:
        """
        Close a position with a market order on its ``Ticker`` and credit the
        position's recorded entry value back to the active client's equity.
        """
        try:
            symbol = pos.get("Ticker")
            if not symbol:
                self.logger.error(
                    f"[{pos.get('Strategy', '?')}] exit_trade: no symbol in position for {pos.get('Ticker', '?')}."
                )
                return False

            req = MarketOrderRequest(
                symbol=symbol,
                qty=qty,
                side=OrderSide.SELL,
                time_in_force=TimeInForce.DAY,
            )
            self.client.submit_order(req)
            self.logger.info(
                f"[{pos.get('Strategy', '?')}] Exit order submitted: {symbol}  qty={qty}"
            )
            entry_price = float(pos.get("EntryPrice", 0.0))
            if entry_price > 0:
                self.equity_mgr.credit(qty * entry_price)

            # Live portfolio parity: a close fill exits the position through the
            # same Position machinery as backtest. The venue fill price is not
            # surfaced by this SDK's fill abstraction, so the close is booked at
            # entry price — mirroring the equity_mgr credit convention above.
            if self.portfolio is not None and qty > 0 and entry_price > 0:
                direction = int(pos.get("Direction", 1))
                self.portfolio.apply_fill(
                    Fill(
                        venue=self._resolve_venue(),
                        symbol=symbol,
                        side=Side.SELL if direction >= 0 else Side.BUY,
                        qty=qty,
                        price=entry_price,
                        strategy=pos.get("Strategy"),
                        timestamp=pos.get("EntryTime"),
                        order_id=pos.get("OrderId"),
                    )
                )
            return True

        except Exception as e:
            self.logger.exception(
                f"[{pos.get('Strategy', '?')}] Error closing position for {pos.get('Ticker')}: {e}"
            )
            return False

    # -------------------------------------------------------------------------
    # Specific Logic Handlers
    # -------------------------------------------------------------------------

    def _enter_linear_trade(
        self,
        strategy: LiveStrategyProtocol,
        ticker: str,
        signal_row: dict[str, Any],
        asset_type: str,
        engine: TradingEngine | None = None,
    ) -> LivePositionDict | None:
        """
        Handles Equities, Crypto, Futures (Linear instruments).
        """
        entry_price: float = float(signal_row.get("Close") or 0)
        signal: str = str(signal_row.get("Signal", "BUY")).upper()

        # Determine order side: SELL signal → short (requires margin/shorting enabled)
        order_side = OrderSide.SELL if signal == "SELL" else OrderSide.BUY

        if entry_price <= 0:
            return None

        if engine is None:
            self.logger.error(
                f"[{strategy.name}] _enter_linear_trade requires TradingEngine."
            )
            return None

        # Sizing — shared with backtester via engine
        equity = self.equity_mgr.get()
        pos_size = float(
            signal_row.get("PositionSize", signal_row.get("Position_Size", 1.0))
        )
        sizing = size_for_signal(
            signal,
            config=strategy.config,
            equity=equity,
            entry_price=entry_price,
            pos_size=pos_size,
        )
        qty = sizing["shares"]

        if qty < 1:
            self.logger.warning(f"[{strategy.name}] Qty < 1 for {ticker} — skipping.")
            return None

        # Execute
        req = MarketOrderRequest(
            symbol=ticker, qty=qty, side=order_side, time_in_force=TimeInForce.DAY
        )

        order: Order | dict[str, Any] = self.client.submit_order(req)
        order_id: str = (
            str(order.get("id", "")) if isinstance(order, dict) else str(order.id)
        )
        self.logger.info(
            f"[{strategy.name}] Entry order submitted: {ticker}  side={order_side.value}  qty={qty}  id={order_id}"
        )
        self.equity_mgr.debit(qty * entry_price)

        confidence: float = float(signal_row.get("Confidence", 0.0))
        direction: int = engine.direction(signal)
        position: LivePositionDict = engine.build_position(  # type: ignore
            _entry_time_iso(signal_row, strategy),
            entry_price,
            direction,
            signal,
            confidence,
            Strategy=strategy.name,
            Ticker=ticker,
            UnderlyingEntryPrice=entry_price,
            Qty=qty,
            PositionSize=float(
                signal_row.get("PositionSize", signal_row.get("Position_Size", 1.0))
            ),
            Vol_Ratio=float(signal_row.get("Vol_Ratio", 1.0)),
            OrderId=order_id,
            Env=strategy.env,
            AssetType=asset_type,
            StopLoss=signal_row.get("StopLoss"),
            TakeProfit=signal_row.get("TakeProfit"),
        )

        # Live portfolio parity: an entry fill opens/re-adjusts the position
        # through the same Position machinery as backtest. ``_bar_time``-derived
        # fill time is used when available.
        if self.portfolio is not None and qty > 0:
            self.portfolio.apply_fill(
                Fill(
                    venue=self._resolve_venue(),
                    symbol=ticker,
                    side=Side.BUY if direction >= 0 else Side.SELL,
                    qty=qty,
                    price=float(entry_price),
                    strategy=strategy.name,
                    timestamp=_entry_time_iso(signal_row, strategy),
                    order_id=order_id,
                )
            )
        return position
