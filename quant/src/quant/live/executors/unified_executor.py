import datetime
import logging
import threading
import zoneinfo
from typing import Any

import pandas as pd
from alpaca.data.requests import OptionLatestQuoteRequest
from alpaca.trading.client import TradingClient
from alpaca.trading.models import Order
from alpaca.trading.requests import MarketOrderRequest, OrderSide, TimeInForce

from quant.core.engine import TradingEngine
from quant.core.sizing import size_for_signal
from quant.data.data_handler import AlpacaDataHandler
from quant.live.executors.ic_executor import IronCondorExecutor
from quant.live.protocols import LivePositionDict, LiveStrategyProtocol
from quant.live.utils import MultiClientEquityManager, get_occ_symbol
from quant.utils.options import select_strike
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
    Atomic Executor handling all asset classes (Equity, Options, Crypto, etc.)
    Routes logic based on Strategy Configuration.
    """

    def __init__(
        self,
        clients: dict[str, TradingClient],
        data_handler: AlpacaDataHandler,
        logger: logging.Logger,
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
        # Dedicated Iron Condor executor (SELL_IC / BUY_IC).
        self.ic_executor = IronCondorExecutor(
            client=self._default_client,
            data_handler=data_handler,
            equity_mgr=self.equity_mgr,
            logger=logger,
        )

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
        self.ic_executor.client = client
        return True

    # -------------------------------------------------------------------------
    # In-memory equity management
    # -------------------------------------------------------------------------
    # Delegated entirely to MultiClientEquityManager (python/utils.py).
    # Call self.equity_mgr.get() / .debit(amount) / .credit(amount) directly.

    # Signal prefixes that imply a specific asset class, regardless of config.
    # NOTE: Canonical definitions live in quant.backtest.utils.
    # These are kept for backward-compat references only.
    _OPTION_SIGNALS = frozenset(["BUY_CALL", "BUY_PUT", "BUY_CALL_MOM", "BUY_PUT_MOM"])
    _SPREAD_SIGNALS = frozenset(["BUY_CALL_SPREAD", "BUY_PUT_SPREAD"])
    _IC_SIGNALS = frozenset(["SELL_IC", "BUY_IC"])
    _EQUITY_SIGNALS = frozenset(["BUY", "SELL"])

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

            if asset_class == "IC":
                return self.ic_executor.enter(
                    strategy, ticker, signal_row, engine=engine
                )
            elif asset_class == "OPTION":
                return self._enter_option_trade(
                    strategy, ticker, signal_row, engine=engine
                )
            elif asset_class == "SPREAD":
                self.logger.warning(
                    f"[{strategy.name}] SPREAD execution not yet implemented for {ticker} — skipping."
                )
                return None
            else:
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
        Universal exit logic.

        IC positions are delegated to IronCondorExecutor.exit() which closes
        all 4 legs correctly.  All other asset classes sell the primary symbol
        (OccSymbol / Ticker) with a market order.
        """
        asset_type = str(pos.get("AssetType", "EQUITY")).upper()
        if asset_type == "IC":
            return self.ic_executor.exit(pos, qty)

        try:
            symbol = pos.get("OccSymbol") or pos.get("Ticker")
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
                self.equity_mgr.credit(
                    qty * entry_price * (100 if asset_type == "OPTION" else 1)
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
            OccSymbol=ticker,
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
        return position

    # -------------------------------------------------------------------------
    # IC trades are handled by IronCondorExecutor (python/executors/ic_executor.py).
    # Call self.ic_executor.enter() / .exit() directly.
    # -------------------------------------------------------------------------

    # TODO: need to make this smarter i.e choose strike with good liquidity and required leverage
    def _enter_option_trade(
        self,
        strategy: LiveStrategyProtocol,
        ticker: str,
        signal_row: dict[str, Any],
        engine: TradingEngine | None = None,
    ) -> LivePositionDict | None:
        """
        Handles Option specific chain lookup and strike selection.
        """
        underlying_price: float = float(
            signal_row.get("Raw_Close") or signal_row.get("Close") or 0
        )
        signal: str = str(signal_row.get("Signal", "BUY_CALL"))

        # Configurable strike selection (same logic as backtester)
        opt_type = "call" if "CALL" in str(signal).upper() else "put"
        if engine is None:
            self.logger.error(
                f"[{strategy.name}] _enter_option_trade requires TradingEngine."
            )
            return None
        strike = select_strike(underlying_price, opt_type, strategy.config)

        # Expiry Logic — look up the nearest real exchange-listed expiration,
        # mirroring backtester.py (here we do it using alpaca api itself and avoid using OptionDataHandler to maintain a single data source ).
        min_dte = getattr(strategy.config, "MIN_OPTION_DTE", 7)
        max_dte = min_dte + 30
        now_et = datetime.datetime.now(zoneinfo.ZoneInfo("America/New_York"))

        expiration_date_gte = (now_et + datetime.timedelta(days=min_dte)).strftime(
            "%Y-%m-%d"
        )
        expiration_date_lte = (now_et + datetime.timedelta(days=max_dte)).strftime(
            "%Y-%m-%d"
        )

        exps = self.data_handler.get_option_expirations(
            ticker, expiration_date_gte, expiration_date_lte
        )
        expiration = exps[0] if exps else None

        if not expiration:
            self.logger.warning(
                f"[{strategy.name}] No listed expiration for {ticker} "
                f"between {expiration_date_gte} and {expiration_date_lte} — skipping."
            )
            return None

        # Strike Search
        found_occ_symbol = ""
        found_strike = strike
        entry_price = 0.0

        # Search range: ITM/OTM depending on config? Defaulting to ATM +/- 10
        search_offsets = [0]
        if opt_type == "call":
            search_offsets.extend(range(1, 6))  # OTM Calls
        else:
            search_offsets.extend(range(-1, -6, -1))  # OTM Puts

        for offset in search_offsets:
            test_strike = strike + offset
            test_occ_symbol = get_occ_symbol(ticker, test_strike, expiration, opt_type)

            # Real-time quote via Alpaca API (TODO: using indicative feed now)
            try:
                req = OptionLatestQuoteRequest(symbol_or_symbols=test_occ_symbol)
                quotes = (
                    self.data_handler.option_historical_client.get_option_latest_quote(
                        req
                    )
                )
                qt = quotes.get(test_occ_symbol)
                if qt is not None:
                    entry_price = float(getattr(qt, "ask_price", 0) or 0)
                    if entry_price > 0:
                        found_occ_symbol = test_occ_symbol
                        found_strike = test_strike
                        break
            except Exception:
                pass

        if found_occ_symbol == "":
            self.logger.warning(
                f"[{strategy.name}] No valid option for {ticker}  exp={expiration} — skipping."
            )
            return None

        # Sizing — shared with backtester via the core dispatch
        equity = self.equity_mgr.get()
        sizing = size_for_signal(
            "BUY_CALL" if opt_type == "call" else "BUY_PUT",
            config=strategy.config,
            equity=equity,
            premium_per_contract=entry_price * 100,
        )
        qty = sizing["n_contracts"]

        if qty < 1:
            return None

        # Execute
        req = MarketOrderRequest(
            symbol=found_occ_symbol,
            qty=qty,
            side=OrderSide.BUY,
            time_in_force=TimeInForce.DAY,
        )

        order: Order | dict[str, Any] = self.client.submit_order(req)
        order_id: str = (
            str(order.get("id", "")) if isinstance(order, dict) else str(order.id)
        )
        self.logger.info(
            f"[{strategy.name}] Option entry order submitted: {found_occ_symbol}  qty={qty}  id={order_id}"
        )
        self.equity_mgr.debit(qty * entry_price * 100)

        # Build position via engine for schema parity with backtester
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
            OccSymbol=found_occ_symbol,
            UnderlyingEntryPrice=underlying_price,
            EntryBasePrice=underlying_price,
            Qty=qty,
            OptionType=opt_type,
            Strike=found_strike,
            Expiration=expiration,
            OrderId=order_id,
            Env=strategy.env,
            AssetType="OPTION",
            StopLoss=signal_row.get("StopLoss"),
            TakeProfit=signal_row.get("TakeProfit"),
        )
        return position
