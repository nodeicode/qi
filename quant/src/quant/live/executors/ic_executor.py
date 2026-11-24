"""
Iron Condor Executor
====================
Dedicated executor for SELL_IC / BUY_IC (0-1 DTE Iron Condor) positions.

Responsibilities
----------------
- Expiration resolution via Alpaca get_option_contracts (single data feed).
- Parallel bid/ask quote fetching for all 4 legs.
- MLEG (multi-leg combo) limit-order submission with adaptive fill loop:
    * poll → replace limit (or initial submit) until filled or budget exhausted.
- Position and collateral accounting via the shared EquityManager.
- Full IC exit: close all 4 leg OCC symbols with market orders.

Usage
-----
    from quant.live.executors.ic_executor import IronCondorExecutor

    ic_exec = IronCondorExecutor(
        client=trading_client,
        data_handler=alpaca_data_handler,
        equity_mgr=equity_mgr,          # MultiClientEquityManager (or EquityManager)
        logger=logger,
    )

    position = ic_exec.enter(strategy, ticker, signal_row)
    ok        = ic_exec.exit(position, n_contracts)
"""

import datetime
import logging
import threading
import time
import zoneinfo
from typing import Any

import pandas as pd
from alpaca.common.exceptions import APIError
from alpaca.data import OptionHistoricalDataClient, Trade
from alpaca.data.requests import (
    OptionLatestQuoteRequest,
    OptionLatestTradeRequest,
    OptionSnapshotRequest,
)
from alpaca.trading.client import TradingClient
from alpaca.trading.requests import (
    LimitOrderRequest,
    OptionLegRequest,
    OrderClass,
    OrderSide,
    ReplaceOrderRequest,
    TimeInForce,
)

from quant.core.engine import TradingEngine
from quant.core.sizing import size_for_signal
from quant.data.data_handler import AlpacaDataHandler
from quant.live.protocols import (
    EquityManagerProtocol,
    LivePositionDict,
    LiveStrategyProtocol,
    is_live_ic_position,
)
from quant.live.utils import get_occ_symbol
from quant.utils.options import (
    build_ic_position,
    check_ic_credit_gates,
    snap_ic_strikes,
)
from quant.utils.time import calculate_entry_time


class IronCondorExecutor:
    """
    Atomic executor for Iron Condor (IC) option strategies.

    Accepts a shared ``EquityManager`` and ``AlpacaDataHandler`` so it can be
    composed inside ``UnifiedExecutor`` without duplicating state.
    """

    def __init__(
        self,
        client: TradingClient,
        data_handler: AlpacaDataHandler,
        equity_mgr: EquityManagerProtocol,
        logger: logging.Logger,
    ) -> None:
        # Per-thread active broker client, armed by ``UnifiedExecutor.set_client``.
        # Concurrent strategy threads share one executor; a shared mutable
        # client slot would let one thread's order submission use another
        # thread's broker account.  Threads that never arm fall back to the
        # client passed here at construction.
        self._default_client: TradingClient = client
        self._tls = threading.local()
        self.data_handler = data_handler
        self.equity_mgr = equity_mgr
        self.logger = logger

    @property
    def client(self) -> TradingClient:
        """Broker client for the calling thread.

        Set via ``UnifiedExecutor.set_client``; falls back to the client
        passed at construction on threads that never armed one.
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
    # Safe order-attribute helpers
    # Alpaca's submit_order / get_order_by_id return Order | dict[str, Any].
    # These helpers normalise both shapes so call-sites are type-clean.
    # -------------------------------------------------------------------------

    @staticmethod
    def _order_id(order: Any) -> str:
        if isinstance(order, dict):
            return str(order.get("id") or order.get("order_id") or "")
        return str(getattr(order, "id", "") or "")

    @staticmethod
    def _order_status(order: Any) -> str:
        if isinstance(order, dict):
            status = order.get("status", "")
            return str(getattr(status, "value", status))
        status = getattr(order, "status", "")
        return str(getattr(status, "value", status))

    @staticmethod
    def _order_fill_price(order: Any, default: float = 0.0) -> float:
        if isinstance(order, dict):
            return float(order.get("filled_avg_price") or default)
        return float(getattr(order, "filled_avg_price", None) or default)

    _ORDER_DEAD_STATUSES = frozenset({"canceled", "expired", "rejected"})

    def _poll_order_after_replace_error(
        self,
        order: Any,
        is_credit: bool,
        fill_default: float,
    ) -> tuple[str, float | None]:
        """Poll after replace 4xx — order may have filled or still be working.

        Per Alpaca, a replace can be rejected while the original order remains
        open (e.g. pending_replace) or fills at the prior limit.
        Returns ``('filled', credit)``, ``('working', None)``, or ``('dead', None)``.
        """
        updated = self.client.get_order_by_id(self._order_id(order))
        polled = self._order_status(updated)
        if polled == "filled":
            raw_fill = self._order_fill_price(updated, fill_default)
            credit = abs(raw_fill) if is_credit else -abs(raw_fill)
            return "filled", credit
        if polled in self._ORDER_DEAD_STATUSES:
            return "dead", None
        return "working", None

    def _recover_from_mleg_api_error(
        self,
        error: APIError,
        working_order: Any | None,
        *,
        ticker: str,
        limit: float,
        last_submitted_limit: float | None,
        is_credit: bool,
        attempt: int,
        op: str,
    ) -> tuple[str, float | None]:
        """Shared MLEG submit/replace APIError handling for entry and exit.

        Returns ``(action, fill_credit)`` where action is one of:
        ``abort``, ``continue``, ``filled``, ``clear_order``.
        """
        code = getattr(error, "code", None)
        status = getattr(error, "status_code", None)

        if code == 40310000:
            self.logger.warning(
                f"[IC] {ticker} insufficient options buying power — "
                f"aborting {op} (limit={limit:.2f}): {error}"
            )
            return "abort", None

        if isinstance(status, int) and 400 <= status < 500:
            recovered = False
            if working_order is not None:
                try:
                    outcome, fill_credit = self._poll_order_after_replace_error(
                        working_order,
                        is_credit,
                        last_submitted_limit or limit,
                    )
                    if outcome == "filled":
                        return "filled", fill_credit
                    if outcome == "dead":
                        self.logger.warning(
                            f"[IC] {ticker} MLEG replace rejected and order is "
                            f"dead — repricing."
                        )
                        return "clear_order", None
                    self.logger.info(
                        f"[IC] {ticker} replace rejected (status={status}, "
                        f"code={code}) but order still working — continuing: "
                        f"{error}"
                    )
                    recovered = True
                except Exception as poll_err:
                    self.logger.warning(
                        f"[IC] {ticker} poll after replace error: {poll_err} "
                        f"— assuming order still working, continuing"
                    )
                    recovered = True
            if not recovered:
                self.logger.warning(
                    f"[IC] {ticker} MLEG submit rejected by broker "
                    f"(status={status}, code={code}) — aborting {op}: {error}"
                )
                return "abort", None
            return "continue", None

        self.logger.error(
            f"[IC] {ticker} MLEG submit failed (attempt {attempt + 1}, "
            f"status={status}, code={code}): {error}"
        )
        # Entry aborts on first transient failure; exit keeps repricing.
        if attempt == 0 and op == "entry":
            return "abort", None
        return "continue", None

    def _fail_ic_exit(self, ticker: str) -> bool:
        self.logger.error(
            f"[IC] exit: MLEG did not fill after all attempts for {ticker}. "
            "Manual close may be required."
        )
        return False

    # -------------------------------------------------------------------------
    # Public API
    # -------------------------------------------------------------------------

    def enter(
        self,
        strategy: LiveStrategyProtocol,
        ticker: str,
        signal_row: dict[str, Any],
        engine: TradingEngine,
    ) -> LivePositionDict | None:
        """
        Enter an Iron Condor position.

        Reads strikes K_LP / K_SP / K_SC / K_LC from *signal_row* (populated by
        the strategy's ``generate_signals``), resolves the nearest listed
        expiration, fetches live quotes, and submits a single MLEG limit order
        with an adaptive fill loop.

        Returns a position dict on success, ``None`` on failure / abort.
        """
        try:
            return self._enter_ic_trade(strategy, ticker, signal_row, engine=engine)
        except Exception as e:
            self.logger.exception(f"[IC] Unhandled error entering {ticker}: {e}")
            return None

    def exit(self, pos: LivePositionDict, qty: float) -> bool:
        """
        Close all 4 legs of a live IC position as a single MLEG limit order.

        Submitting as one combo avoids the per-leg margin requirement that
        would be imposed on individual naked-short close orders.

        Falls back to individual market orders only when all 4 OCC symbols
        are missing (legacy positions).
        """
        qty = float(qty or pos.get("NContracts") or 1)

        occ_lp = str(pos.get("OccSymbol_LP") or "")
        occ_sp = str(pos.get("OccSymbol_SP") or "")
        occ_sc = str(pos.get("OccSymbol_SC") or "")
        occ_lc = str(pos.get("OccSymbol_LC") or "")

        ic_direction = int(pos.get("IC_Direction", 1))
        ticker = str(pos.get("Ticker", ""))
        expiration = str(pos.get("Expiration", ""))

        # ── Expiry guard ──────────────────────────────────────────────────────
        # If the expiration date has already passed, Alpaca will have
        # auto-settled any remaining contracts.  Skip order submission,
        # release the collateral in equity accounting, and return True so
        # the caller removes this position from state.
        if expiration:
            try:
                exp_date = datetime.date.fromisoformat(expiration)
                today = datetime.datetime.now(
                    zoneinfo.ZoneInfo("America/New_York")
                ).date()
                if today > exp_date:
                    self.logger.info(
                        f"[IC] {ticker} position expired on {expiration} "
                        "— Alpaca auto-settled; updating state."
                    )
                    self._account_expiry(pos, qty)
                    return True
            except ValueError:
                pass

        # ── Close legs are exact reversal of entry legs ────────────────────────
        # SELL_IC entry:  BUY LP, SELL SP, SELL SC, BUY LC  → net credit
        # SELL_IC close:  SELL LP, BUY SP, BUY SC, SELL LC  → net debit
        # BUY_IC  entry:  SELL LP, BUY SP, BUY SC, SELL LC  → net debit
        # BUY_IC  close:  BUY LP, SELL SP, SELL SC, BUY LC  → net credit
        if ic_direction == 1:
            close_legs = [
                OptionLegRequest(symbol=occ_lp, ratio_qty=1, side=OrderSide.SELL),
                OptionLegRequest(symbol=occ_sp, ratio_qty=1, side=OrderSide.BUY),
                OptionLegRequest(symbol=occ_sc, ratio_qty=1, side=OrderSide.BUY),
                OptionLegRequest(symbol=occ_lc, ratio_qty=1, side=OrderSide.SELL),
            ]
            is_debit_close = True  # paying to buy back the short IC
        else:
            close_legs = [
                OptionLegRequest(symbol=occ_lp, ratio_qty=1, side=OrderSide.BUY),
                OptionLegRequest(symbol=occ_sp, ratio_qty=1, side=OrderSide.SELL),
                OptionLegRequest(symbol=occ_sc, ratio_qty=1, side=OrderSide.SELL),
                OptionLegRequest(symbol=occ_lc, ratio_qty=1, side=OrderSide.BUY),
            ]
            is_debit_close = False  # receiving credit to close the long IC

        # ── Fetch initial live quotes via batch helper ─────────────────────────
        occ_symbols = [occ_lp, occ_sp, occ_sc, occ_lc]

        # ── Price the close order ──────────────────────────────────────────────
        # Exit config — persisted in the position dict at entry time.
        exit_fill_step = float(pos.get("IC_FILL_STEP_PCT", 0.15))
        max_attempts = int(pos.get("IC_EXIT_MAX_ATTEMPTS", 4))
        max_wait_secs = int(pos.get("IC_EXIT_MAX_WAIT_SECS", 90))
        min_poll_secs = int(pos.get("IC_EXIT_MIN_POLL_SECS", 15))
        poll_secs = max(min_poll_secs, max_wait_secs // (max_attempts + 1))

        self.logger.info(
            f"[IC] exit MLEG {ticker} direction={ic_direction} "
            f"qty={qty} ({'debit' if is_debit_close else 'credit'} close) "
            f"step={exit_fill_step:.0%} max_attempts={max_attempts}"
        )

        # ── Fallback price from position's entry credit ─────────────────────
        # Used when live quotes are unavailable so we can still submit a close.
        entry_credit = abs(float(pos.get("FillCredit", 0.0)))
        if is_debit_close:
            fallback_base = round(max(0.05, entry_credit * 1.20), 2)
        else:
            fallback_base = -round(max(0.05, entry_credit * 0.80), 2)

        # ── Progressive fill loop with per-attempt quote re-fetch ──────────────
        close_order = None
        success = False
        start_time = time.monotonic()
        limit_price = 0.0
        last_submitted_limit: float | None = None

        for attempt in range(max_attempts):
            elapsed = time.monotonic() - start_time
            if attempt > 0 and elapsed > max_wait_secs:
                break

            # Re-fetch live quotes for fresh mid/spread
            has_fresh_quotes = False
            combo_mid = 0.0
            combo_spread = 0.0
            concession = 0.0
            fresh: dict[str, dict[str, float]] = {}
            try:
                fresh = self._fetch_combo_data(occ_symbols)
                if all(sym in fresh and fresh[sym]["mid"] > 0 for sym in occ_symbols):
                    combo_mid = (
                        fresh[occ_sp]["mid"]
                        + fresh[occ_sc]["mid"]
                        - fresh[occ_lp]["mid"]
                        - fresh[occ_lc]["mid"]
                    )
                    combo_spread = sum(
                        fresh[sym]["ask"] - fresh[sym]["bid"] for sym in occ_symbols
                    )
                    has_fresh_quotes = True
                else:
                    self.logger.warning(
                        f"[IC] exit: incomplete quotes (attempt {attempt + 1}), using fallback price"
                    )
            except Exception as e:
                self.logger.warning(
                    f"[IC] exit: quote re-fetch error (attempt {attempt + 1}): {e} — using fallback price"
                )

            if has_fresh_quotes:
                # Progressive concession: attempt * step * spread
                concession = attempt * exit_fill_step * combo_spread

                if is_debit_close:
                    # Paying to buy back: start at mid, concede upward
                    combo_ask = sum(
                        fresh.get(sym, {}).get("ask", 0) for sym in [occ_sp, occ_sc]
                    ) - sum(
                        fresh.get(sym, {}).get("bid", 0) for sym in [occ_lp, occ_lc]
                    )
                    if combo_ask > 0:
                        # Market still prices this as a debit — cap at combo ask,
                        # floor at 0.01 so we never flip to credit when the
                        # spread edge is still positive.
                        target = min(combo_ask, combo_mid + concession)
                        limit_price = round(max(0.01, target), 2)
                    else:
                        # Both mid and ask are non-positive: wings richer than
                        # shorts.  Submit as credit so Alpaca interprets the
                        # sign correctly.
                        target = combo_mid + concession
                        self.logger.warning(
                            f"[IC] exit {ticker}: debit-close target is negative "
                            f"(ask_edge={combo_ask:.3f} target={target:.3f}), submitting as credit"
                        )
                        limit_price = -round(max(0.01, abs(target)), 2)
                else:
                    # Receiving credit: start at mid, concede downward
                    combo_bid = sum(
                        fresh.get(sym, {}).get("bid", 0) for sym in [occ_sp, occ_sc]
                    ) - sum(
                        fresh.get(sym, {}).get("ask", 0) for sym in [occ_lp, occ_lc]
                    )
                    target = (
                        max(combo_bid, combo_mid - concession)
                        if combo_bid > 0
                        else combo_mid - concession
                    )
                    if target > 0:
                        limit_price = -round(target, 2)  # credit
                    else:
                        # Net flipped to debit (adverse move) — pay to close
                        limit_price = round(max(0.01, abs(target)), 2)
            elif limit_price != 0.0:
                # No fresh quotes but we have a prior limit — concede one more step.
                # Always make the order more fillable: increase debit (positive limit),
                # reduce credit demanded (negative limit toward zero).
                step = 0.05
                limit_price = round(limit_price + step, 2)
                if limit_price == 0.0:
                    # Brokers reject zero-priced orders; cross through to small debit
                    limit_price = 0.01
                self.logger.info(
                    f"[IC] exit: using prior limit + step → {limit_price:.2f} (attempt {attempt + 1})"
                )
            else:
                # No prior price either — derive from entry credit
                limit_price = fallback_base
                self.logger.info(
                    f"[IC] exit: using FillCredit fallback → {limit_price:.2f} (attempt {attempt + 1})"
                )

            if has_fresh_quotes:
                price_detail = f" mid={combo_mid:.3f} spread={combo_spread:.3f} concession={concession:.3f}"
            else:
                price_detail = " (fallback pricing)"
            self.logger.info(
                f"[IC] exit attempt={attempt + 1}/{max_attempts} "
                f"limit={limit_price:.2f}{price_detail}"
            )

            if close_order is not None and limit_price == last_submitted_limit:
                self.logger.info(
                    f"[IC] exit {ticker}: reprice skipped — limit unchanged at "
                    f"{limit_price:.2f} (attempt {attempt + 1})"
                )
            else:
                try:
                    if close_order is None:
                        close_order = self.client.submit_order(
                            LimitOrderRequest(
                                qty=float(qty),
                                order_class=OrderClass.MLEG,
                                time_in_force=TimeInForce.DAY,
                                limit_price=limit_price,
                                legs=close_legs,
                            )
                        )
                        self.logger.info(
                            f"[IC] exit MLEG submitted id={self._order_id(close_order)} "
                            f"attempt={attempt + 1}"
                        )
                    else:
                        close_order = self.client.replace_order_by_id(
                            self._order_id(close_order),
                            ReplaceOrderRequest(limit_price=limit_price),
                        )
                    last_submitted_limit = limit_price
                except APIError as e:
                    action, _ = self._recover_from_mleg_api_error(
                        e,
                        close_order,
                        ticker=ticker,
                        limit=limit_price,
                        last_submitted_limit=last_submitted_limit,
                        is_credit=not is_debit_close,
                        attempt=attempt,
                        op="exit",
                    )
                    if action == "abort":
                        return self._fail_ic_exit(ticker)
                    if action == "filled":
                        success = True
                        break
                    if action == "clear_order":
                        close_order = None
                    elif action == "continue":
                        time.sleep(poll_secs)
                        continue
                except Exception as e:
                    self.logger.exception(
                        f"[IC] exit MLEG submit failed (attempt {attempt + 1}): {e}"
                    )
                    if attempt == max_attempts - 1:
                        return self._fail_ic_exit(ticker)
                    time.sleep(poll_secs)
                    continue

            if success:
                break

            time.sleep(poll_secs)

            # Poll fill status
            if close_order is None:
                continue
            try:
                updated = self.client.get_order_by_id(self._order_id(close_order))
                status = self._order_status(updated)
                self.logger.debug(
                    f"[IC] exit MLEG {self._order_id(close_order)} status={status}"
                )
                if status == "filled":
                    success = True
                    break
                elif status in ("canceled", "expired", "rejected"):
                    self.logger.warning(
                        f"[IC] exit MLEG {self._order_id(close_order)} {status} — repricing."
                    )
                    close_order = None
            except Exception as e:
                self.logger.warning(f"[IC] exit MLEG status check error: {e}")

        if not success:
            # Re-check expiry — position may have expired during the fill loop
            if expiration:
                try:
                    exp_date = datetime.date.fromisoformat(expiration)
                    today = datetime.datetime.now(
                        zoneinfo.ZoneInfo("America/New_York")
                    ).date()
                    if today > exp_date:
                        self.logger.warning(
                            f"[IC] Exit loop exhausted but {ticker} has expired ({expiration}). "
                            "Alpaca auto-settled — updating state."
                        )
                        self._account_expiry(pos, qty)
                        return True
                except ValueError:
                    pass

            # One final aggressive attempt at spread edge
            self.logger.info(
                f"[IC] exit {ticker}: normal attempts exhausted — escalating to spread edge"
            )
            try:
                fresh = self._fetch_combo_data(occ_symbols)
                if all(sym in fresh and fresh[sym]["mid"] > 0 for sym in occ_symbols):
                    if is_debit_close:
                        # Full ask: worst case debit
                        edge = sum(fresh[sym]["ask"] for sym in [occ_sp, occ_sc]) - sum(
                            fresh[sym]["bid"] for sym in [occ_lp, occ_lc]
                        )
                        if edge >= 0:
                            limit_price = round(max(0.01, edge), 2)
                        else:
                            # Market flipped to net credit — submit as credit order
                            self.logger.warning(
                                f"[IC] exit {ticker}: debit-close edge is negative "
                                f"({edge:.3f}), submitting as credit"
                            )
                            limit_price = -round(max(0.01, abs(edge)), 2)
                    else:
                        # Full bid: worst case credit
                        edge = sum(fresh[sym]["bid"] for sym in [occ_sp, occ_sc]) - sum(
                            fresh[sym]["ask"] for sym in [occ_lp, occ_lc]
                        )
                        if edge >= 0:
                            # Still net credit — submit as credit order
                            limit_price = -round(max(0.01, edge), 2)
                        else:
                            # Market flipped to net debit — submit as debit order
                            self.logger.warning(
                                f"[IC] exit {ticker}: credit-close edge is negative "
                                f"({edge:.3f}), submitting as debit"
                            )
                            limit_price = round(max(0.01, abs(edge)), 2)
                else:
                    # No quotes — use an aggressive fallback from entry credit
                    if is_debit_close:
                        limit_price = round(max(0.05, entry_credit * 1.50), 2)
                    else:
                        limit_price = -round(max(0.05, entry_credit * 0.50), 2)
                    self.logger.warning(
                        f"[IC] exit {ticker}: no quotes for aggressive attempt, "
                        f"using FillCredit-derived fallback={limit_price:.2f}"
                    )

                self.logger.info(
                    f"[IC] exit {ticker} aggressive limit={limit_price:.2f}"
                )
                poll_after_aggressive = False
                try:
                    if close_order is None:
                        close_order = self.client.submit_order(
                            LimitOrderRequest(
                                qty=float(qty),
                                order_class=OrderClass.MLEG,
                                time_in_force=TimeInForce.DAY,
                                limit_price=limit_price,
                                legs=close_legs,
                            )
                        )
                    else:
                        close_order = self.client.replace_order_by_id(
                            self._order_id(close_order),
                            ReplaceOrderRequest(limit_price=limit_price),
                        )
                    poll_after_aggressive = True
                except APIError as e:
                    action, _ = self._recover_from_mleg_api_error(
                        e,
                        close_order,
                        ticker=ticker,
                        limit=limit_price,
                        last_submitted_limit=last_submitted_limit,
                        is_credit=not is_debit_close,
                        attempt=max_attempts,
                        op="exit",
                    )
                    if action == "filled":
                        success = True
                    elif action == "abort":
                        return self._fail_ic_exit(ticker)
                    elif action == "clear_order":
                        close_order = None
                    elif action == "continue":
                        # Replace rejected but MLEG still working — poll before fail.
                        poll_after_aggressive = True
                except Exception as e:
                    self.logger.exception(
                        f"[IC] exit {ticker} aggressive submit failed: {e}"
                    )
                if not success and close_order is not None and poll_after_aggressive:
                    time.sleep(poll_secs)
                    updated = self.client.get_order_by_id(self._order_id(close_order))
                    status = self._order_status(updated)
                    if status == "filled":
                        success = True
                    else:
                        order_id = self._order_id(close_order)
                        try:
                            self.client.cancel_order_by_id(order_id)
                        except Exception as e:
                            self.logger.warning(
                                f"[IC] exit: MLEG cancel failed id={order_id}: {e}"
                            )
            except Exception as e:
                self.logger.exception(
                    f"[IC] exit {ticker} aggressive attempt failed: {e}"
                )

        if not success:
            return self._fail_ic_exit(ticker)

        # ── Collateral accounting ──────────────────────────────────────────────
        collateral = float(pos.get("Collateral", 0.0))
        fill_credit = float(pos.get("FillCredit", 0.0))
        contracts = float(pos.get("NContracts", qty))
        net_return = collateral - fill_credit * 100 * contracts
        if net_return != 0:
            self.equity_mgr.credit(abs(net_return))

        return True

    # -------------------------------------------------------------------------
    # Expiry accounting helper
    # -------------------------------------------------------------------------

    def _account_expiry(self, pos: LivePositionDict, qty: float) -> None:
        """
        Release collateral for a position that expired without a close order.

        Uses the same formula as the normal exit path so equity accounting
        stays consistent.  The caller is responsible for removing the position
        from active state.
        """
        collateral = float(pos.get("Collateral", 0.0))
        fill_credit = float(pos.get("FillCredit", 0.0))
        contracts = float(pos.get("NContracts", qty))
        net_return = collateral - fill_credit * 100 * contracts
        if net_return != 0:
            self.equity_mgr.credit(abs(net_return))

    # -------------------------------------------------------------------------
    # Quote helpers
    # -------------------------------------------------------------------------

    def fetch_leg_quote(
        self, ticker: str, strike: float, expiration: str, opt_type: str
    ) -> tuple[float, float, float]:
        """
        Fetch real-time bid/ask/mid for a single option leg via Alpaca OPRA data.

        Primary:  ``get_option_latest_quote``  — real-time NBBO bid/ask.
        Fallback: ``get_option_snapshot``      — latest quote + latest trade.

        Returns ``(bid, ask, mid)`` or ``(0.0, 0.0, 0.0)`` on failure.
        """
        occ_symbol = get_occ_symbol(ticker, strike, expiration, opt_type)
        opt_client = self.data_handler.option_historical_client

        # ── 1. Latest real-time quote ──────────────────────────────────────────
        # TODO: currently using indicative feed, not full OPRA. Switch feed when
        #       Alpaca enables OPRA on this account tier.
        try:
            req = OptionLatestQuoteRequest(symbol_or_symbols=occ_symbol)
            quotes = opt_client.get_option_latest_quote(req)
            quote = quotes.get(occ_symbol)
            if quote is not None:
                bid = float(getattr(quote, "bid_price", 0) or 0)
                ask = float(getattr(quote, "ask_price", 0) or 0)
                if bid > 0 and ask > 0:
                    mid = (bid + ask) / 2.0
                    self.logger.debug(
                        f"[IC] latest_quote {occ_symbol}: bid={bid:.3f} ask={ask:.3f} mid={mid:.3f}"
                    )
                    return bid, ask, mid
        except Exception as e:
            self.logger.debug(f"[IC] latest_quote failed for {occ_symbol}: {e}")

        # ── 2. Snapshot fallback ───────────────────────────────────────────────
        try:
            req = OptionSnapshotRequest(symbol_or_symbols=occ_symbol)
            snaps = opt_client.get_option_snapshot(req)
            snap = snaps.get(occ_symbol)
            if snap is not None:
                lq = getattr(snap, "latest_quote", None)
                lt = getattr(snap, "latest_trade", None)
                bid = ask = mid = 0.0
                if lq is not None:
                    bid = float(getattr(lq, "bid_price", 0) or 0)
                    ask = float(getattr(lq, "ask_price", 0) or 0)
                if bid > 0 and ask > 0:
                    mid = (bid + ask) / 2.0
                elif lt is not None:
                    price = float(getattr(lt, "price", 0) or 0)
                    if price > 0:
                        mid = price
                        bid = round(price * 0.97, 3)
                        ask = round(price * 1.03, 3)
                if mid > 0:
                    self.logger.debug(
                        f"[IC] snapshot {occ_symbol}: bid={bid:.3f} ask={ask:.3f} mid={mid:.3f}"
                    )
                    return bid, ask, mid
        except Exception as e:
            self.logger.warning(f"[IC] snapshot failed for {occ_symbol}: {e}")

        self.logger.warning(
            f"[IC] No quote for {ticker} {opt_type} K={strike} exp={expiration} ({occ_symbol})"
        )
        return 0.0, 0.0, 0.0

    # -------------------------------------------------------------------------
    # Combo-level quote + trade helpers
    # -------------------------------------------------------------------------

    def _fetch_combo_data(self, occ_symbols: list[str]) -> dict[str, dict[str, float]]:
        """
        Batch-fetch quotes AND latest trades for all OCC symbols (2 API calls).

        Returns dict keyed by OCC symbol:
            ``{symbol: {"bid": …, "ask": …, "mid": …, "last_trade": …}}``
        """
        opt_client: OptionHistoricalDataClient = (
            self.data_handler.option_historical_client
        )
        result: dict[str, dict[str, float]] = {}

        # 1. Batch latest quotes
        try:
            req = OptionLatestQuoteRequest(symbol_or_symbols=occ_symbols)
            quotes = opt_client.get_option_latest_quote(req)
            for sym in occ_symbols:
                q = quotes.get(sym)
                if q is not None:
                    bid = float(getattr(q, "bid_price", 0) or 0)
                    ask = float(getattr(q, "ask_price", 0) or 0)
                    mid = (bid + ask) / 2.0 if bid > 0 and ask > 0 else 0.0
                    result[sym] = {
                        "bid": bid,
                        "ask": ask,
                        "mid": mid,
                        "last_trade": 0.0,
                    }
        except Exception as e:
            self.logger.debug(f"[IC] batch quote fetch failed: {e}")

        # 2. Batch latest trades — real transaction prices
        try:
            req_t = OptionLatestTradeRequest(symbol_or_symbols=occ_symbols)
            trades: dict[str, Trade] = opt_client.get_option_latest_trade(req_t)
            for sym in occ_symbols:
                t = trades.get(sym)
                if t is not None:
                    price = float(getattr(t, "price", 0) or 0)
                    if sym not in result:
                        result[sym] = {
                            "bid": 0.0,
                            "ask": 0.0,
                            "mid": 0.0,
                            "last_trade": 0.0,
                        }
                    result[sym]["last_trade"] = price
                    # If no quote mid but we have a trade, use it as estimate
                    if result[sym]["mid"] <= 0 and price > 0:
                        result[sym]["mid"] = price
                        result[sym]["bid"] = round(price * 0.97, 3)
                        result[sym]["ask"] = round(price * 1.03, 3)
        except Exception as e:
            self.logger.debug(f"[IC] batch trade fetch failed: {e}")

        return result

    def _compute_combo_credit(
        self,
        data: dict[str, dict[str, float]],
        occ_symbols: list[str],
    ) -> float:
        """
        Signed combo value: inner legs minus wings, before commissions.

        Per-leg price: last trade when it falls within bid/ask, else mid.
        Returns ``SP + SC - LP - LC``.  Positive when inner-leg premiums
        exceed wing premiums, negative when wings are more expensive.
        Direction-agnostic — callers decide how to interpret the sign.
        """
        occ_lp, occ_sp, occ_sc, occ_lc = occ_symbols
        prices: dict[str, float] = {}
        for sym in occ_symbols:
            d = data[sym]
            trade = d["last_trade"]
            if trade > 0 and d["bid"] > 0 and d["bid"] <= trade <= d["ask"]:
                prices[sym] = trade
            else:
                prices[sym] = d["mid"]
        return prices[occ_sp] + prices[occ_sc] - prices[occ_lp] - prices[occ_lc]

    def _fetch_entry_quotes(
        self,
        ticker: str,
        occ_symbols: list[str],
        wing_width: float,
        min_net_credit: float,
        max_spread_pct: float,
        comm_per_leg: float,
        min_credit: float = 0.0,
        max_credit: float = float("inf"),
    ) -> tuple[dict[str, dict[str, float]], float] | None:
        """
        Single-fetch entry quote validation with quality gates.

        Fetches quotes + last trades once, computes combo credit, and applies
        three quality gates:
          1. Gross credit band ``[min_credit, max_credit]`` (mirrors the
             backtester's IC_MIN_CREDIT / IC_MAX_CREDIT regime filter —
             rejects too-cheap or too-rich days).
          2. Net credit floor (must cover commissions).
          3. Combo spread cap (spread as fraction of credit).

        CWR is logged as a metric but does **not** gate the trade — the
        absolute credit floor and spread quality are more robust filters.

        Returns ``(data, credit)`` or ``None``.
        """
        max_quote_retries = 3
        retry_delay_secs = 5
        data: dict[str, dict[str, float]] = {}

        for attempt in range(1, max_quote_retries + 1):
            data = self._fetch_combo_data(occ_symbols)
            if all(sym in data and data[sym]["mid"] > 0 for sym in occ_symbols):
                break
            missing = [
                sym for sym in occ_symbols if sym not in data or data[sym]["mid"] <= 0
            ]
            self.logger.warning(
                f"[IC] {ticker} incomplete quotes (attempt {attempt}/{max_quote_retries}) "
                f"— missing: {missing}"
            )
            if attempt < max_quote_retries:
                time.sleep(retry_delay_secs)
        else:
            self.logger.warning(
                f"[IC] {ticker} incomplete quotes after {max_quote_retries} attempts — skipping"
            )
            return None

        credit = self._compute_combo_credit(data, occ_symbols)
        if credit <= 0:
            self.logger.warning(f"[IC] {ticker} credit={credit:.4f} <= 0 — skipping")
            return None

        # Gate 1: gross credit band (backtest parity)
        if credit < min_credit or credit > max_credit:
            self.logger.warning(
                f"[IC] {ticker} credit={credit:.4f} outside band "
                f"[{min_credit:.4f}, {max_credit:.4f}] — skipping"
            )
            return None

        # Gate 2: net credit must exceed commission breakeven
        net_credit = credit - (4 * comm_per_leg / 100)
        if net_credit < min_net_credit:
            self.logger.warning(
                f"[IC] {ticker} net_credit={net_credit:.4f} < min={min_net_credit:.4f} — skipping"
            )
            return None

        # Gate 3: combo spread must be reasonable relative to credit
        combo_spread = sum(data[sym]["ask"] - data[sym]["bid"] for sym in occ_symbols)
        spread_pct = combo_spread / credit if credit > 0 else 999.0
        if spread_pct > max_spread_pct:
            self.logger.warning(
                f"[IC] {ticker} spread_pct={spread_pct:.2f} > max={max_spread_pct:.2f} — skipping"
            )
            return None

        # CWR: log-only metric (not a hard gate)
        cwr = credit / wing_width if wing_width > 0 else 0
        self.logger.info(
            f"[IC] {ticker} credit={credit:.4f} CWR={cwr:.3f} "
            f"net_credit={net_credit:.4f} spread_pct={spread_pct:.2f}"
        )

        return data, credit

    # -------------------------------------------------------------------------
    # Internal helpers
    # -------------------------------------------------------------------------

    def _resolve_expiration(self, ticker: str, min_dte: int) -> str | None:
        """Return the nearest listed expiration within [min_dte, min_dte+5] days.

        Delegates to AlpacaDataHandler.get_option_expirations (single data feed).
        """

        now_et = datetime.datetime.now(zoneinfo.ZoneInfo("America/New_York"))
        exp_gte = (now_et + datetime.timedelta(days=min_dte)).strftime("%Y-%m-%d")
        exp_lte = (now_et + datetime.timedelta(days=min_dte + 5)).strftime("%Y-%m-%d")
        exps = self.data_handler.get_option_expirations(ticker, exp_gte, exp_lte)
        if exps:
            return exps[0]
        self.logger.warning(
            f"[IC] No expiration for {ticker} between {exp_gte} and {exp_lte}."
        )
        return None

    def _size_contracts(
        self,
        strategy: LiveStrategyProtocol,
        ticker: str,
        K_LP: float,
        K_SP: float,
        K_SC: float,
        K_LC: float,
        confidence: float,
    ) -> tuple[int, float, float]:
        """
        Compute n_contracts, capital_for_trade, collateral_per_contract.

        Delegates to ``size_for_signal`` (IC) via the strategy's config — the
        same code path used by the backtester. Returns ``(n_contracts,
        capital_for_trade, collateral_per_contract)``, or ``(0, 0, 0)`` when
        collateral is invalid.
        """
        wing_width = max(K_SP - K_LP, K_LC - K_SC)
        collateral_per = wing_width * 100
        if collateral_per <= 0:
            self.logger.error(
                f"[IC] {ticker} collateral_per_contract={collateral_per:.2f} ≤ 0."
            )
            return 0, 0.0, 0.0

        equity = self.equity_mgr.get()

        sizing = size_for_signal(
            "SELL_IC",
            config=strategy.config,
            equity=equity,
            confidence=confidence,
            collateral_per_contract=collateral_per,
        )
        return (
            sizing["n_contracts"],
            sizing["capital"],
            sizing["collateral_per_contract"],
        )

    def _build_mleg_legs(
        self,
        occ_lp: str,
        occ_sp: str,
        occ_sc: str,
        occ_lc: str,
        ic_direction: int,
    ) -> tuple[list[OptionLegRequest], bool]:
        """
        Return ``(legs, is_credit)`` for the given IC direction.

        ic_direction == 1  → SELL_IC: sell inner, buy wings  → net credit
        ic_direction == -1 → BUY_IC:  buy  inner, sell wings → net debit
        """
        if ic_direction == 1:
            legs = [
                OptionLegRequest(symbol=occ_lp, ratio_qty=1, side=OrderSide.BUY),
                OptionLegRequest(symbol=occ_sp, ratio_qty=1, side=OrderSide.SELL),
                OptionLegRequest(symbol=occ_sc, ratio_qty=1, side=OrderSide.SELL),
                OptionLegRequest(symbol=occ_lc, ratio_qty=1, side=OrderSide.BUY),
            ]
            return legs, True
        else:
            legs = [
                OptionLegRequest(symbol=occ_lp, ratio_qty=1, side=OrderSide.SELL),
                OptionLegRequest(symbol=occ_sp, ratio_qty=1, side=OrderSide.BUY),
                OptionLegRequest(symbol=occ_sc, ratio_qty=1, side=OrderSide.BUY),
                OptionLegRequest(symbol=occ_lc, ratio_qty=1, side=OrderSide.SELL),
            ]
            return legs, False

    def _latest_underlying_spot(self, ticker: str, fallback: float) -> float:
        """Live/paper spot for IC strike re-center.

        Prefers Alpaca stock snapshot mid (else last trade). Falls back to
        *fallback* (typically signal-bar ``Raw_Close``) when the quote is
        unavailable — historical bars remain subject to ``DATA_DELAY``.
        """
        try:
            quotes = self.data_handler.get_latest_stock_quotes([ticker])
        except Exception as exc:
            self.logger.debug("[IC] latest stock quote failed %s: %s", ticker, exc)
            return float(fallback) if fallback and fallback > 0 else 0.0

        if quotes is None or getattr(quotes, "empty", True):
            return float(fallback) if fallback and fallback > 0 else 0.0
        try:
            if ticker not in quotes.index:
                return float(fallback) if fallback and fallback > 0 else 0.0
            row = quotes.loc[ticker]
            mid = float(row.get("mid", 0) or 0)
            price = float(row.get("price", 0) or 0)
            spot = mid if mid > 0 else price
            if spot > 0:
                return spot
        except (TypeError, ValueError, KeyError, AttributeError) as exc:
            self.logger.debug("[IC] parse stock quote failed %s: %s", ticker, exc)
        return float(fallback) if fallback and fallback > 0 else 0.0

    # -------------------------------------------------------------------------
    # Core entry logic
    # -------------------------------------------------------------------------

    def _enter_ic_trade(
        self,
        strategy: LiveStrategyProtocol,
        ticker: str,
        signal_row: dict[str, Any],
        engine: TradingEngine,
    ) -> LivePositionDict | None:
        """
        Full IC entry flow:
          1. Validate strikes.
          2. Resolve expiration.
          3. Build OCC symbols.
          4. Fetch all 4 leg quotes in parallel.
          5. Size contracts.
          6. Submit MLEG limit order with adaptive fill loop.
          7. Book position and return dict.
        """
        # ── 1. Validate strikes ───────────────────────────────────────────────
        K_LP = float(signal_row.get("K_LP", 0))
        K_SP = float(signal_row.get("K_SP", 0))
        K_SC = float(signal_row.get("K_SC", 0))
        K_LC = float(signal_row.get("K_LC", 0))
        ic_direction = int(signal_row.get("IC_Direction", 1))
        bar_underlying = float(
            signal_row.get("Raw_Close", signal_row.get("Close", 0)) or 0
        )
        underlying = bar_underlying
        confidence = float(signal_row.get("Confidence", 0.5))
        atr = float(signal_row.get("ATR", underlying * 0.01 if underlying > 0 else 1.0))
        signal = str(signal_row.get("Signal", "SELL_IC"))

        # Default ON: re-snap strikes at fill around live spot.
        # Live/paper uses the latest Alpaca stock quote (mid/trade) so strike
        # geometry hops SIP DATA_DELAY on historical bars. Bar Raw_Close is
        # fallback only when the quote fetch fails.
        if bool(getattr(strategy.config, "IC_RECENTER_AT_FILL", True)):
            try:
                spot = self._latest_underlying_spot(ticker, bar_underlying)
                if spot > 0:

                    def _cfg_f(row_key: str, cfg_key: str, default: float) -> float:
                        v = signal_row.get(row_key)
                        try:
                            if v is not None and float(v) == float(v):  # not NaN
                                return float(v)
                        except (TypeError, ValueError):
                            pass
                        try:
                            cfg_v = getattr(strategy.config, cfg_key, default)
                            f = float(cfg_v)
                            if f == f:
                                return f
                        except (TypeError, ValueError):
                            pass
                        return float(default)

                    _off = _cfg_f("IC_Offset_Used", "IC_OFFSET", 5.0)
                    _wing = _cfg_f("IC_Wing_Used", "IC_WING", 2.0)
                    _inc = 1.0
                    _si = getattr(strategy.config, "STRIKE_INCREMENT", None)
                    if isinstance(_si, dict):
                        _inc = float(_si.get(ticker, _si.get("_default", 1.0)))
                    K_LP, K_SP, K_SC, K_LC = snap_ic_strikes(spot, _off, _wing, _inc)
                    underlying = spot
                    self.logger.info(
                        "[IC] %s recenter spot=%.2f (bar=%.2f) → "
                        "LP=%.0f SP=%.0f SC=%.0f LC=%.0f",
                        ticker,
                        spot,
                        bar_underlying,
                        K_LP,
                        K_SP,
                        K_SC,
                        K_LC,
                    )
            except Exception as _re_exc:
                self.logger.debug("[IC] recenter skipped %s: %s", ticker, _re_exc)

        if any(k <= 0 for k in (K_LP, K_SP, K_SC, K_LC)):
            self.logger.error(
                f"[IC] {ticker} missing/invalid strikes "
                f"(K_LP={K_LP}, K_SP={K_SP}, K_SC={K_SC}, K_LC={K_LC}). Skipping."
            )
            return None

        # ── 2. Resolve expiration ─────────────────────────────────────────────
        min_dte = int(getattr(strategy.config, "MIN_OPTION_DTE", 1))
        now_utc = datetime.datetime.now(datetime.UTC)
        expiration = self._resolve_expiration(ticker, min_dte)
        if not expiration:
            return None
        self.logger.info(f"[IC] {ticker} expiration: {expiration}")

        # ── 3. Build OCC symbols ──────────────────────────────────────────────
        occ_lp = get_occ_symbol(ticker, K_LP, expiration, "put")
        occ_sp = get_occ_symbol(ticker, K_SP, expiration, "put")
        occ_sc = get_occ_symbol(ticker, K_SC, expiration, "call")
        occ_lc = get_occ_symbol(ticker, K_LC, expiration, "call")
        self.logger.info(
            f"[IC] Legs: LP={occ_lp}  SP={occ_sp}  SC={occ_sc}  LC={occ_lc}"
            f"  bar={signal_row.get('_bar_time', '?')}  underlying={underlying:.2f}"
        )

        # ── 4. Fetch entry quotes + apply quality gates ────────────────────
        wing_width = max(K_SP - K_LP, K_LC - K_SC)
        occ_symbols = [occ_lp, occ_sp, occ_sc, occ_lc]

        if engine is None:
            self.logger.error(
                f"[IC] {ticker} engine is required — refusing entry without TradingEngine."
            )
            return None

        # Read config from the strategy (TradingEngine.cfg was removed in
        # quant commit 73373c9 — config now lives on strategy.config).
        comm_per_leg = float(getattr(strategy.config, "IC_COMM_PER_LEG", 0.02))
        min_net_credit = float(getattr(strategy.config, "IC_MIN_NET_CREDIT", 0.08))
        max_spread_pct = float(getattr(strategy.config, "IC_MAX_SPREAD_PCT", 0.80))
        min_credit = float(getattr(strategy.config, "IC_MIN_CREDIT", 0.0))
        max_credit = float(getattr(strategy.config, "IC_MAX_CREDIT", float("inf")))

        entry_result = self._fetch_entry_quotes(
            ticker,
            occ_symbols,
            wing_width,
            min_net_credit,
            max_spread_pct,
            comm_per_leg,
            min_credit=min_credit,
            max_credit=max_credit,
        )
        if entry_result is None:
            return None

        best_data, mid_credit = entry_result
        ref_credit = mid_credit  # save for slippage-from-ref tracking

        credit_rej = check_ic_credit_gates(
            mid_credit, min_credit=min_credit, max_credit=max_credit
        )
        if credit_rej is not None:
            self.logger.warning(
                f"[IC] {ticker} dropped ({credit_rej}): mid_credit={mid_credit:.4f} "
                f"band=[{min_credit}, {max_credit}]"
            )
            return None

        # Unpack into per-leg values
        mid_lp = best_data[occ_lp]["mid"]
        mid_sp = best_data[occ_sp]["mid"]
        mid_sc = best_data[occ_sc]["mid"]
        mid_lc = best_data[occ_lc]["mid"]

        bid_lp = best_data[occ_lp]["bid"]
        ask_lp = best_data[occ_lp]["ask"]
        bid_sp = best_data[occ_sp]["bid"]
        ask_sp = best_data[occ_sp]["ask"]
        bid_sc = best_data[occ_sc]["bid"]
        ask_sc = best_data[occ_sc]["ask"]
        bid_lc = best_data[occ_lc]["bid"]
        ask_lc = best_data[occ_lc]["ask"]

        self.logger.info(
            f"[IC] {ticker} quotes — SP:{mid_sp:.3f} SC:{mid_sc:.3f} "
            f"LP:{mid_lp:.3f} LC:{mid_lc:.3f} → mid_credit={mid_credit:.3f}"
        )

        # ── 5. Sizing ─────────────────────────────────────────────────────────
        n_contracts, capital_for_trade, collateral_per = self._size_contracts(
            strategy,
            ticker,
            K_LP,
            K_SP,
            K_SC,
            K_LC,
            confidence,
        )
        if n_contracts == 0:
            return None

        equity_available = self.equity_mgr.get()
        active_client = getattr(self.equity_mgr, "active_client", None) or "unknown"

        # Explain sizing: show the ratio and whether the hard-dollar cap was hit.
        equity_ratio = (
            capital_for_trade / equity_available if equity_available > 0 else 0.0
        )
        max_cap = float(
            getattr(strategy.config, "MAX_CAPITAL_PER_TRADE", equity_available)
        )
        cap_note = (
            " (cap hit)"
            if capital_for_trade >= max_cap - 1
            else f" (cap=${max_cap:,.0f})"
        )

        self.logger.info(
            f"[IC] {ticker} n_contracts={n_contracts} "
            f"collateral/contract=${collateral_per:.0f} "
            f"capital=${capital_for_trade:,.0f} "
            f"equity_ratio={equity_ratio:.1%}{cap_note} "
            f"equity_available=${equity_available:,.0f} "
            f"client={active_client}"
        )

        # ── 6. MLEG fill loop (quote-aware repricing) ─────────────────────────
        # On each attempt: re-fetch live quotes, compute fresh combo mid/spread,
        # then concede IC_FILL_STEP_PCT of the combo bid-ask spread per step.
        # Default 10%/step: attempt-0 = mid, attempt-3 = mid-30% of spread,
        # so all tries stay in the top half of the spread (close to mid).

        # ensure fill_step_pct is a float between 0 and 1, defaulting to 0.10 (10%)
        fill_step_pct = max(
            0.0, min(1.0, float(getattr(strategy.config, "IC_FILL_STEP_PCT", 0.10)))
        )
        max_wait_secs = int(getattr(strategy.config, "IC_MAX_WAIT_SECS", 120))
        min_poll_secs = int(getattr(strategy.config, "IC_MIN_POLL_SECS", 15))
        max_attempts = int(getattr(strategy.config, "IC_MAX_ATTEMPTS", 4))
        fill_poll_secs = max(min_poll_secs, max_wait_secs // (max_attempts + 1))

        self.logger.debug(
            f"[IC] {ticker} fill loop: step_pct={fill_step_pct:.0%} "
            f"max_attempts={max_attempts} poll={fill_poll_secs}s timeout={max_wait_secs}s"
        )

        legs, is_credit = self._build_mleg_legs(
            occ_lp, occ_sp, occ_sc, occ_lc, ic_direction
        )

        # Working bid/ask state — seeded from step-4 quotes, refreshed each attempt.
        _bid_lp_w, _ask_lp_w = bid_lp, ask_lp
        _bid_sp_w, _ask_sp_w = bid_sp, ask_sp
        _bid_sc_w, _ask_sc_w = bid_sc, ask_sc
        _bid_lc_w, _ask_lc_w = bid_lc, ask_lc

        # Initial limit estimate (refined before first submit using fresh quotes).
        mleg_limit = -round(mid_credit, 2) if is_credit else round(mid_credit, 2)

        # Track the last fresh mid we saw — used to measure entry slippage.
        last_combo_mid: float = mid_credit

        start_time = time.monotonic()
        attempt = 0
        mleg_order = None
        success = False
        actual_credit: float | None = None
        last_submitted_limit: float | None = None

        while True:
            elapsed = time.monotonic() - start_time

            # ── Poll existing order ───────────────────────────────────────────
            if mleg_order is not None:
                try:
                    updated = self.client.get_order_by_id(self._order_id(mleg_order))
                    status = self._order_status(updated)
                    self.logger.debug(
                        f"[IC] {ticker} MLEG {self._order_id(mleg_order)} status={status}"
                    )
                    if status == "filled":
                        raw_fill = self._order_fill_price(updated, mleg_limit)
                        actual_credit = abs(raw_fill) if is_credit else -abs(raw_fill)
                        success = True
                        break
                    elif status in ("canceled", "expired", "rejected"):
                        self.logger.warning(
                            f"[IC] {ticker} MLEG {self._order_id(mleg_order)} {status} — repricing."
                        )
                        mleg_order = None
                except Exception as e:
                    self.logger.warning(f"[IC] {ticker} MLEG status check error: {e}")

            # ── Abort guard ───────────────────────────────────────────────────
            if attempt > 0 and (attempt >= max_attempts or elapsed > max_wait_secs):
                if mleg_order is not None:
                    order_id = self._order_id(mleg_order)
                    try:
                        self.client.cancel_order_by_id(order_id)
                    except Exception as e:
                        self.logger.warning(
                            f"[IC] {ticker} MLEG cancel failed id={order_id}: {e}"
                        )
                self.logger.warning(
                    f"[IC] {ticker} MLEG aborted after {attempt}/{max_attempts} attempts "
                    f"(elapsed={elapsed:.0f}s)"
                )
                return None

            # ── Re-fetch live quotes + trades for this attempt ─────────────
            try:
                fresh = self._fetch_combo_data(occ_symbols)
                if all(sym in fresh and fresh[sym]["bid"] > 0 for sym in occ_symbols):
                    _bid_lp_w, _ask_lp_w = fresh[occ_lp]["bid"], fresh[occ_lp]["ask"]
                    _bid_sp_w, _ask_sp_w = fresh[occ_sp]["bid"], fresh[occ_sp]["ask"]
                    _bid_sc_w, _ask_sc_w = fresh[occ_sc]["bid"], fresh[occ_sc]["ask"]
                    _bid_lc_w, _ask_lc_w = fresh[occ_lc]["bid"], fresh[occ_lc]["ask"]
                    self.logger.debug(
                        f"[IC] {ticker} fresh quotes attempt={attempt + 1}: "
                        f"SP({_bid_sp_w:.3f}/{_ask_sp_w:.3f}) "
                        f"SC({_bid_sc_w:.3f}/{_ask_sc_w:.3f}) "
                        f"LP({_bid_lp_w:.3f}/{_ask_lp_w:.3f}) "
                        f"LC({_bid_lc_w:.3f}/{_ask_lc_w:.3f})"
                    )
            except Exception as e:
                self.logger.debug(
                    f"[IC] {ticker} quote re-fetch failed (attempt {attempt + 1}): "
                    f"{e} — using last known quotes"
                )

            # ── Compute limit: fresh mid minus progressive spread concession ──
            combo_mid_fresh = (
                (_bid_sp_w + _ask_sp_w) / 2
                + (_bid_sc_w + _ask_sc_w) / 2
                - (_bid_lp_w + _ask_lp_w) / 2
                - (_bid_lc_w + _ask_lc_w) / 2
            )
            last_combo_mid = combo_mid_fresh
            combo_spread = (
                (_ask_sp_w - _bid_sp_w)
                + (_ask_sc_w - _bid_sc_w)
                + (_ask_lp_w - _bid_lp_w)
                + (_ask_lc_w - _bid_lc_w)
            )
            concession = attempt * fill_step_pct * combo_spread

            if is_credit:
                # SELL_IC: reduce credit demanded; floor at combo bid so we
                # never price below what the market will immediately pay.
                combo_bid_price = _bid_sp_w + _bid_sc_w - _ask_lp_w - _ask_lc_w
                target = max(combo_bid_price, combo_mid_fresh - concession)
                mleg_limit = -round(max(0.01, target), 2)
            else:
                # BUY_IC: increase debit offered; cap at combo ask so we
                # never pay more than the market immediately asks.
                combo_ask_price = _ask_sp_w + _ask_sc_w - _bid_lp_w - _bid_lc_w
                target = min(combo_ask_price, combo_mid_fresh + concession)
                mleg_limit = round(max(0.01, target), 2)

            # Bottom snap (backtest parity): the limit is the credit backtest
            # validates against IC_MIN_CREDIT (credit_too_low drop) — if
            # repricing would fill below it, snap up to min_credit instead of
            # filling a trade that can't afford its own loss.
            if min_credit > 0 and abs(mleg_limit) < min_credit:
                self.logger.warning(
                    f"[IC] {ticker} limit {abs(mleg_limit):.3f} below "
                    f"min_credit {min_credit:.3f} — snapping to floor"
                )
                mleg_limit = -min_credit if is_credit else min_credit

            # ── Submit or replace MLEG (one broker call per loop) ─────────────
            limit_rounded = round(mleg_limit, 2)
            if mleg_order is not None and limit_rounded == last_submitted_limit:
                self.logger.info(
                    f"[IC] {ticker} reprice skipped — limit unchanged at "
                    f"{limit_rounded:.2f} (attempt {attempt + 1})"
                )
            else:
                if mleg_order is None:
                    self.logger.info(
                        f"[IC] {ticker} MLEG submit attempt={attempt + 1} "
                        f"limit={limit_rounded:.2f} ({'credit' if is_credit else 'debit'}) "
                        f"mid={combo_mid_fresh:.3f} spread={combo_spread:.3f} "
                        f"contracts={n_contracts}"
                    )
                else:
                    self.logger.info(
                        f"[IC] {ticker} reprice → limit={limit_rounded:.2f} "
                        f"(attempt {attempt + 1} "
                        f"concession={concession:.3f} "
                        f"spread={combo_spread:.3f} "
                        f"mid={combo_mid_fresh:.3f})"
                    )
                try:
                    if mleg_order is None:
                        mleg_order = self.client.submit_order(
                            LimitOrderRequest(
                                qty=float(n_contracts),
                                order_class=OrderClass.MLEG,
                                time_in_force=TimeInForce.DAY,
                                limit_price=limit_rounded,
                                legs=legs,
                            )
                        )
                        self.logger.info(
                            f"[IC] {ticker} MLEG order id={self._order_id(mleg_order)}"
                        )
                    else:
                        mleg_order = self.client.replace_order_by_id(
                            self._order_id(mleg_order),
                            ReplaceOrderRequest(limit_price=limit_rounded),
                        )
                    last_submitted_limit = limit_rounded
                except APIError as e:
                    action, fill_credit = self._recover_from_mleg_api_error(
                        e,
                        mleg_order,
                        ticker=ticker,
                        limit=limit_rounded,
                        last_submitted_limit=last_submitted_limit,
                        is_credit=is_credit,
                        attempt=attempt,
                        op="entry",
                    )
                    if action == "abort":
                        return None
                    if action == "filled":
                        actual_credit = fill_credit
                        success = True
                        break
                    if action == "clear_order":
                        mleg_order = None
                except Exception as e:
                    self.logger.exception(
                        f"[IC] {ticker} MLEG submit failed (attempt {attempt + 1}): {e}"
                    )
                    if attempt == 0:
                        return None

            attempt += 1
            self.logger.info(
                f"[IC] {ticker} waiting {fill_poll_secs}s "
                f"(elapsed={elapsed:.0f}s / max={max_wait_secs}s) ..."
            )
            time.sleep(fill_poll_secs)

        if not success or actual_credit is None:
            return None

        # ── 7. Book position ──────────────────────────────────────────────────
        total_commission = 4 * comm_per_leg * n_contracts
        collateral_total = collateral_per * n_contracts
        credit_dollars = actual_credit * 100 * n_contracts
        net_credit_dollars = credit_dollars - total_commission

        # Slippage paid = how far our fill deviated from last observed mid.
        # SELL_IC: positive = received less than mid (gave away premium).
        # BUY_IC:  positive = paid more than mid.
        slippage_paid = (
            last_combo_mid - actual_credit
            if is_credit
            else abs(actual_credit) - last_combo_mid
        )
        ref_slippage = (
            ref_credit - actual_credit if is_credit else abs(actual_credit) - ref_credit
        )
        self.logger.info(
            f"[IC] {ticker} FILLED — credit/share={actual_credit:.3f} "
            f"mid_at_fill={last_combo_mid:.3f} "
            f"ref_credit={ref_credit:.3f} "
            f"slippage/share={slippage_paid:+.3f} "
            f"slippage_from_ref={ref_slippage:+.3f} "
            f"contracts={n_contracts} net_credit_$={net_credit_dollars:.2f} "
            f"commission=${total_commission:.2f}"
        )

        self.equity_mgr.debit(collateral_total - credit_dollars)

        # Build position via engine for schema parity with backtester
        live_extras = {
            "Strategy": strategy.name,
            "Ticker": ticker,
            "Env": strategy.env,
            "OccSymbol": occ_sp,
            "OccSymbol_LP": occ_lp,
            "OccSymbol_SP": occ_sp,
            "OccSymbol_SC": occ_sc,
            "OccSymbol_LC": occ_lc,
            "Expiration": expiration,
            "NContracts": n_contracts,
            "Qty": n_contracts,
            "FillCredit": actual_credit,
            "FillCredit_SP": mid_sp,
            "FillCredit_SC": mid_sc,
            "FillDebit_LP": mid_lp,
            "FillDebit_LC": mid_lc,
            "NetCreditDollars": net_credit_dollars,
            "Collateral": collateral_total,
            "OrderIds": {"MLEG": self._order_id(mleg_order) if mleg_order else ""},
            "EntryMid": round(last_combo_mid, 4),
            "EntrySlippage": round(slippage_paid, 4),
            "EntryRefCredit": round(ref_credit, 4),
            "EntrySlippageFromRef": round(ref_slippage, 4),
            # Exit fill-loop config — persisted so close_ic uses the same
            # parameters that were active when the position was opened.
            "IC_FILL_STEP_PCT": fill_step_pct,
            "IC_EXIT_MAX_ATTEMPTS": max_attempts,
            "IC_EXIT_MAX_WAIT_SECS": max_wait_secs,
            "IC_EXIT_MIN_POLL_SECS": min_poll_secs,
        }

        bar_time = signal_row.get("_bar_time")
        interval = getattr(strategy.config, "INTERVAL", "15m")
        if bar_time is not None:
            entry_time_iso = pd.Timestamp(
                calculate_entry_time(bar_time, interval)
            ).isoformat()
        else:
            entry_time_iso = now_utc.isoformat()
            self.logger.warning(
                f"[IC] {ticker} missing _bar_time — falling back to wall-clock EntryTime"
            )

        position = build_ic_position(
            entry_time_iso,
            underlying,
            ic_direction,
            signal,
            confidence,
            strikes={"K_LP": K_LP, "K_SP": K_SP, "K_SC": K_SC, "K_LC": K_LC},
            config=strategy.config,
            atr=atr,
            **live_extras,
        )

        # statically ensure position is LivePositionDict
        if not is_live_ic_position(position):
            raise TypeError("build_ic_position returned an incomplete live position")

        return position
