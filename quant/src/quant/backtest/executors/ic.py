from typing import cast

import numpy as np
import pandas as pd

from quant.backtest.executors.base_executor import (
    BaseBacktestExecutor,
    tradable_close,
    tradable_close_at,
)
from quant.backtest.utils import apply_breach_exit_cbbo, calculate_fill_time
from quant.core.protocols import StrategyProtocol
from quant.core.sizing import size_for_signal
from quant.utils.options import (
    build_ic_position,
    check_ic_credit_gates,
    check_ic_spread_gate,
)


class IronCondorExecutor(BaseBacktestExecutor):
    """Fill simulator for IC signals (BUY_IC / SELL_IC)."""

    def enter(
        self,
        strategy: StrategyProtocol,
        ticker: str,
        signal_row: dict,
        engine,
        *,
        now=None,
    ) -> dict | None:
        """Open ic position. Returns the new position dict (or None)."""
        ic_prefetch = self.ctx.require_ic()
        expiration_cache = ic_prefetch.expiration_cache
        ic_leg_prices = ic_prefetch.leg_prices
        _ic_leg_cbbo = ic_prefetch.leg_cbbo
        ic_drop_stats = ic_prefetch.drop_stats

        def _ic_drop(reason: str, message: str, *args):
            """Log and count IC drops with a canonical reason key."""
            ic_drop_stats[reason] = ic_drop_stats.get(reason, 0) + 1
            self.logger.info(message, *args)

        signal_time = now
        entry_time = calculate_fill_time(
            signal_time, self.config.INTERVAL, self.config.DATA_DELAY
        )
        try:
            row = self._df_signals.loc[now]
        except KeyError:
            row = pd.Series(signal_row)

        required = ["K_LP", "K_SP", "K_SC", "K_LC"]
        if any(col not in row.index or pd.isna(row[col]) for col in required):
            missing = [c for c in required if c not in row.index or pd.isna(row[c])]
            _ic_drop(
                "missing_strikes",
                "IC-DROP [missing_strikes] %s missing=%s",
                entry_time,
                missing,
            )
            return

        day_str = (
            entry_time.date().strftime("%Y-%m-%d")
            if hasattr(entry_time, "date")
            else str(entry_time)[:10]
        )
        expiration = expiration_cache.get(day_str)
        if not expiration:
            _ic_drop(
                "no_expiration",
                "IC-DROP [no_expiration] %s day=%s",
                entry_time,
                day_str,
            )
            return

        _max_dte_cfg = getattr(self.config, "MAX_OPTION_DTE", None)
        if (
            _max_dte_cfg is not None
            and int(_max_dte_cfg) == 0
            and expiration != day_str
        ):
            _ic_drop(
                "expiry_slide",
                "IC-DROP [expiry_slide] %s day=%s exp=%s (0-DTE requires same-day)",
                entry_time,
                day_str,
                expiration,
            )
            return

        K_lp, K_sp = float(row["K_LP"]), float(row["K_SP"])
        K_sc, K_lc = float(row["K_SC"]), float(row["K_LC"])
        atr = float(row.get("ATR", 0))
        _raw_dir = row.get("IC_Direction", 1)
        ic_direction = int(_raw_dir) if pd.notna(_raw_dir) else 1
        signal = str(signal_row.get("Signal", ""))
        _raw_conf = signal_row.get("Confidence", 0.5)
        confidence = float(_raw_conf) if pd.notna(_raw_conf) else 0.5

        task_idx = row.get("_ic_task_idx")
        if task_idx is None or (isinstance(task_idx, float) and pd.isna(task_idx)):
            _ic_drop("no_task_idx", "IC-DROP [no_task_idx] %s", entry_time)
            return
        task_idx = int(task_idx)
        p_lp = ic_leg_prices.get((task_idx, "lp"))
        p_sp = ic_leg_prices.get((task_idx, "sp"))
        p_sc = ic_leg_prices.get((task_idx, "sc"))
        p_lc = ic_leg_prices.get((task_idx, "lc"))

        _leg_prices = {"lp": p_lp, "sp": p_sp, "sc": p_sc, "lc": p_lc}
        _leg_methods = {"lp": "cbbo", "sp": "cbbo", "sc": "cbbo", "lc": "cbbo"}
        _missing_legs = [lg for lg, p in _leg_prices.items() if p is None]

        # Mirrors live ic_executor: abort on any missing CBBO — no synthetic fallback.
        # Retrying with a time offset has not improved fill coverage in practice.
        if _missing_legs:
            _ic_drop(
                "missing_leg_cbbo",
                "IC-DROP [missing_leg_cbbo] %s task=%s missing=%s"
                " — all 4 legs require real CBBO (matches live abort)",
                entry_time,
                task_idx,
                _missing_legs,
            )
            return

        p_lp = _leg_prices["lp"]
        p_sp = _leg_prices["sp"]
        p_sc = _leg_prices["sc"]
        p_lc = _leg_prices["lc"]

        p_lp_f = cast("float", p_lp)
        p_sp_f = cast("float", p_sp)
        p_sc_f = cast("float", p_sc)
        p_lc_f = cast("float", p_lc)

        def _leg_detail(leg_name: str, strike: float, price: float) -> str:
            cb = _ic_leg_cbbo.get((task_idx, leg_name))
            if cb:
                return (
                    f"{leg_name.upper()}=K{strike:.0f} mid={price:.3f} "
                    f"bid={cb.get('bid', '?'):.3f} ask={cb.get('ask', '?'):.3f} "
                    f"@{cb.get('ts_used', '?')}"
                )
            return f"{leg_name.upper()}=K{strike:.0f} mid={price:.3f} (no-cbbo)"

        self.logger.info(
            "IC-STRIKES [task=%d] %s exp=%s | %s | %s | %s | %s",
            task_idx,
            entry_time,
            expiration,
            _leg_detail("lp", K_lp, p_lp_f),
            _leg_detail("sp", K_sp, p_sp_f),
            _leg_detail("sc", K_sc, p_sc_f),
            _leg_detail("lc", K_lc, p_lc_f),
        )

        is_short = ic_direction == 1
        mid_credit = (p_sp_f + p_sc_f) - (p_lp_f + p_lc_f)
        ic_fill_step_pct = float(getattr(self.config, "IC_FILL_STEP_PCT", 0.25))

        # Extract CBBO bid/ask up-front — used for both fill-slip and worst-credit clamp.
        _all_cbbo = all(m == "cbbo" for m in _leg_methods.values())
        _cbbo_lp = _ic_leg_cbbo.get((task_idx, "lp"))
        _cbbo_sp = _ic_leg_cbbo.get((task_idx, "sp"))
        _cbbo_sc = _ic_leg_cbbo.get((task_idx, "sc"))
        _cbbo_lc = _ic_leg_cbbo.get((task_idx, "lc"))
        _b_lp = _a_lp = _b_sp = _a_sp = _b_sc = _a_sc = _b_lc = _a_lc = 0.0
        _cbbo_complete = False
        if _all_cbbo and _cbbo_lp and _cbbo_sp and _cbbo_sc and _cbbo_lc:
            _b_lp = float(_cbbo_lp.get("bid", 0) or 0)
            _a_lp = float(_cbbo_lp.get("ask", 0) or 0)
            _b_sp = float(_cbbo_sp.get("bid", 0) or 0)
            _a_sp = float(_cbbo_sp.get("ask", 0) or 0)
            _b_sc = float(_cbbo_sc.get("bid", 0) or 0)
            _a_sc = float(_cbbo_sc.get("ask", 0) or 0)
            _b_lc = float(_cbbo_lc.get("bid", 0) or 0)
            _a_lc = float(_cbbo_lc.get("ask", 0) or 0)
            _cbbo_complete = (
                _b_lp > 0
                and _a_lp > 0
                and _b_sp > 0
                and _a_sp > 0
                and _b_sc > 0
                and _a_sc > 0
                and _b_lc > 0
                and _a_lc > 0
            )

        # Fill-price model mirrors live ic_executor: slip = step_pct * combo_spread.
        # Fall back to mid-magnitude only when CBBO bid/ask are unavailable.
        # TODO: centralize fill formula with live ic_executor._enter_ic_trade.
        if _cbbo_complete:
            combo_spread = (
                (_a_lp - _b_lp) + (_a_sp - _b_sp) + (_a_sc - _b_sc) + (_a_lc - _b_lc)
            )
            combo_slip = max(0.01, ic_fill_step_pct * combo_spread)
        else:
            combo_spread = 0.0
            combo_slip = max(0.01, ic_fill_step_pct * abs(mid_credit))
        credit = mid_credit - combo_slip if is_short else mid_credit + combo_slip

        # Worst-credit clamp: post-slip fill cannot exceed combo_bid (SHORT) or combo_ask (BUY).
        if _cbbo_complete:
            if is_short:
                _worst_credit = (_b_sp + _b_sc) - (_a_lp + _a_lc)
                credit = min(credit, _worst_credit)
            else:
                _worst_debit = (_a_sp + _a_sc) - (_b_lp + _b_lc)
                credit = max(credit, _worst_debit)
        if credit <= 0.01:
            _ic_drop(
                "credit_too_thin",
                "IC-DROP [credit_too_thin] %s mid=%.4f slip=%.4f net=%.4f legs=[%.2f,%.2f,%.2f,%.2f]",
                entry_time,
                mid_credit,
                combo_slip,
                credit,
                p_lp_f,
                p_sp_f,
                p_sc_f,
                p_lc_f,
            )
            return

        wing = max(K_sp - K_lp, K_lc - K_sc)
        if wing <= 0:
            _ic_drop(
                "wing_invalid",
                "IC-DROP [wing_invalid] %s wing=%.2f credit=%.4f",
                entry_time,
                wing,
                credit,
            )
            return

        comm_per_leg = float(getattr(self.config, "IC_COMM_PER_LEG", 0.02))
        net_credit = credit - (4 * comm_per_leg / 100)
        min_net_credit = float(getattr(self.config, "IC_MIN_NET_CREDIT", 0.08))
        if min_net_credit > 0 and net_credit < min_net_credit:
            _ic_drop(
                "net_credit_floor",
                "IC-DROP [net_credit_floor] %s net_credit=%.4f < min_net=%.4f "
                "(credit=%.4f comm=%.4f)",
                entry_time,
                net_credit,
                min_net_credit,
                credit,
                4 * comm_per_leg / 100,
            )
            return

        # TODO: centralize entry gates with live ic_executor._fetch_entry_quotes.
        ic_min_credit = float(getattr(self.config, "IC_MIN_CREDIT", -1))
        ic_max_credit = float(getattr(self.config, "IC_MAX_CREDIT", -1))
        credit_rej = check_ic_credit_gates(
            credit, min_credit=ic_min_credit, max_credit=ic_max_credit
        )
        if credit_rej == "credit_too_low":
            _ic_drop(
                "credit_too_low",
                "IC-DROP [credit_too_low] %s credit=%.4f < min=%.4f "
                "(insufficient premium, low-IV day with no edge)",
                entry_time,
                credit,
                ic_min_credit,
            )
            return
        if credit_rej == "credit_too_high":
            _ic_drop(
                "credit_too_high",
                "IC-DROP [credit_too_high] %s credit=%.4f > max=%.4f "
                "(market-implied breach risk too high)",
                entry_time,
                credit,
                ic_max_credit,
            )
            return

        max_spread_pct = float(getattr(self.config, "IC_MAX_SPREAD_PCT", 0.80))
        if (
            check_ic_spread_gate(credit, combo_spread, max_spread_pct) is not None
            and _cbbo_complete
        ):
            spread_pct = combo_spread / credit if credit > 0 else float("inf")
            _ic_drop(
                "spread_too_wide",
                "IC-DROP [spread_too_wide] %s spread_pct=%.0f%% > cap=%.0f%% "
                "(combo_spread=%.4f credit=%.4f)",
                entry_time,
                spread_pct * 100,
                max_spread_pct * 100,
                combo_spread,
                credit,
            )
            return

        bpr_ps = (wing - credit) if is_short else credit
        if bpr_ps <= 0:
            _ic_drop(
                "bpr_zero",
                "IC-DROP [bpr_zero] %s bpr=%.4f wing=%.2f credit=%.4f",
                entry_time,
                bpr_ps,
                wing,
                credit,
            )
            return
        collateral_ps = wing

        sizing = size_for_signal(
            signal,
            config=self.config,
            equity=self._constant_equity(),
            confidence=confidence,
            collateral_per_contract=collateral_ps * 100,
        )
        n_contracts = sizing["n_contracts"]
        if n_contracts == 0:
            _ic_drop(
                "sizing_zero",
                "IC-DROP [sizing_zero] %s equity=%.0f conf=%.2f collateral=%.0f",
                entry_time,
                self._constant_equity(),
                confidence,
                collateral_ps * 100,
            )
            return

        trade_collateral = n_contracts * collateral_ps * 100
        available_equity = self._constant_equity() - self._collateral_in_use
        if trade_collateral > available_equity:
            n_contracts = max(1, int(available_equity / (collateral_ps * 100)))
            trade_collateral = n_contracts * collateral_ps * 100
            if trade_collateral > available_equity:
                _ic_drop(
                    "insufficient_equity",
                    "IC-DROP [insufficient_equity] %s need=%.0f avail=%.0f",
                    entry_time,
                    trade_collateral,
                    available_equity,
                )
                return

        df_entry = self._slice_manage(start=entry_time)
        if df_entry.empty:
            _ic_drop("no_manage_data", "IC-DROP [no_manage_data] %s", entry_time)
            return
        underlying_entry = tradable_close_at(df_entry.iloc[0])

        position = dict(
            build_ic_position(
                entry_time=entry_time,
                underlying_price=underlying_entry,
                ic_direction=ic_direction,
                signal=signal,
                confidence=confidence,
                strikes={"K_SP": K_sp, "K_SC": K_sc, "K_LP": K_lp, "K_LC": K_lc},
                config=self.config,
                atr=atr,
            )
        )
        position["Ticker"] = self.ticker
        position["_asset_class"] = "IC"
        position["_credit"] = credit
        position["_n_contracts"] = n_contracts
        position["_collateral"] = trade_collateral
        position["_wing"] = wing
        position["_bpr_ps"] = bpr_ps
        position["_is_short"] = is_short
        position["_expiration"] = expiration
        position["_row"] = row
        position["_ic_task_idx"] = task_idx
        position["_signal_time"] = signal_time
        position["_entry_leg_methods"] = _leg_methods
        position["_entry_combo_spread"] = combo_spread

        self._active_positions.append(position)
        self._collateral_in_use += trade_collateral

        self.logger.info(
            "IC open [task=%s]: %s  credit=%.3f  conf=%.2f  ctr=%d  "
            "collateral=$%.0f  equity=$%.0f  "
            "K_LP=%.1f K_SP=%.1f K_SC=%.1f K_LC=%.1f  exp=%s",
            task_idx,
            entry_time,
            credit,
            confidence,
            n_contracts,
            trade_collateral,
            self._constant_equity(),
            K_lp,
            K_sp,
            K_sc,
            K_lc,
            expiration,
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
        """Close IC *position*. Returns True on success."""
        ic_prefetch = self.ctx.require_ic()
        _ic_leg_cbbo = ic_prefetch.leg_cbbo
        option_data_handler = ic_prefetch.option_data_handler

        pos = sig.get("_position", position)

        if manage_df is None:
            manage_df = self._df_manage

        entry_time = pos.get("EntryTime")
        if entry_time is not None:
            _et = pd.Timestamp(entry_time)
            if pd.notna(_et):
                _trimmed = manage_df[manage_df.index >= _et]
                if not _trimmed.empty:
                    manage_df = _trimmed

        if manage_df.empty:
            self.logger.warning(
                "EquityExecutor.exit: manage_df empty after EntryTime trim for %s — skipping",
                pos.get("Ticker"),
            )
            if pos in self._active_positions:
                self._active_positions.remove(pos)
            return True

        exit_idx = sig.get("exit_idx", len(manage_df) - 1)
        if exit_idx >= len(manage_df):
            exit_idx = len(manage_df) - 1
        exit_row = manage_df.iloc[exit_idx]
        exit_bar = cast("pd.Timestamp", exit_row.name)
        exit_bar = pd.Timestamp(exit_bar)

        exit_time = calculate_fill_time(
            exit_bar,
            self.config.MANAGE_INTERVAL or self.config.INTERVAL,
            self.config.DATA_DELAY,
        )

        entry_time = pos["EntryTime"]

        credit = pos["_credit"]
        n_contracts = pos["_n_contracts"]
        trade_collateral = pos["_collateral"]
        wing = pos["_wing"]
        bpr_ps = pos["_bpr_ps"]
        is_short = pos["_is_short"]

        _s = pos.get("Strikes") or pos
        K_lp = float(_s.get("K_LP", pos.get("K_LP", 0)))
        K_sp = float(_s.get("K_SP", pos.get("K_SP", 0)))
        K_sc = float(_s.get("K_SC", pos.get("K_SC", 0)))
        K_lc = float(_s.get("K_LC", pos.get("K_LC", 0)))

        ic_fill_step_pct = float(getattr(self.config, "IC_FILL_STEP_PCT", 0.25))
        comm_per_leg = getattr(self.config, "IC_COMM_PER_LEG", 0.02)

        duration = (exit_time - entry_time).total_seconds() / 60.0
        S_exit = tradable_close_at(exit_row)

        _task_idx = pos.get("_ic_task_idx")
        expiration = pos.get("_expiration", "")

        _leg_meta = {
            "lp": ("put", K_lp),
            "sp": ("put", K_sp),
            "sc": ("call", K_sc),
            "lc": ("call", K_lc),
        }

        _is_time_close = forced or (duration >= self.config.MAX_HOLDING_MINUTES - 1)

        leg_mids: dict[str, float] = {}
        _methods: dict[str, str] = {}
        _exit_bar_ts: dict[str, str] = {}
        _alpaca_sellout = False
        _last_hr_min_put_dist: float | None = None
        _last_hr_min_call_dist: float | None = None

        if _is_time_close:
            _exp_date = pd.Timestamp(expiration)
            _day_start = _exp_date + pd.Timedelta(hours=18)
            _day_end = _exp_date + pd.Timedelta(hours=21, minutes=30)
            _expiry_bars = self._df_manage.loc[_day_start:_day_end]

            if len(_expiry_bars) >= 2:
                S_settle = tradable_close_at(_expiry_bars.iloc[-1])
                _settle_ts = _expiry_bars.index[-1]
            elif len(_expiry_bars) == 1:
                S_settle = tradable_close_at(_expiry_bars.iloc[0])
                _settle_ts = _expiry_bars.index[0]
            else:
                S_settle = S_exit
                _settle_ts = exit_time

            _scan_anchor_ts = _settle_ts
            if not self._df_manage_1m.empty:
                _expiry_1m = self._df_manage_1m.loc[_day_start:_day_end]
                if not _expiry_1m.empty:
                    _scan_anchor_ts = _expiry_1m.index[-1]
            _scan_before = int(getattr(self.config, "IC_SELLOUT_SCAN_BEFORE_MIN", 45))
            _scan_after = int(getattr(self.config, "IC_SELLOUT_SCAN_AFTER_MIN", 15))
            _scan_start = _scan_anchor_ts - pd.Timedelta(minutes=_scan_before)
            _scan_end = _scan_anchor_ts + pd.Timedelta(minutes=_scan_after)
            _scan_source = (
                self._df_manage_1m if not self._df_manage_1m.empty else self._df_manage
            )
            _scan_bars = _scan_source.loc[_scan_start:_scan_end]

            _scan_spot = tradable_close(_scan_bars) if not _scan_bars.empty else None
            _sp_breach_ts = None
            _sc_breach_ts = None
            if not _scan_bars.empty:
                _low_col = cast(
                    "pd.Series",
                    _scan_bars["Low"] if "Low" in _scan_bars.columns else _scan_spot,
                )
                _high_col = cast(
                    "pd.Series",
                    _scan_bars["High"] if "High" in _scan_bars.columns else _scan_spot,
                )
                _sp_hits = _scan_bars.index[_low_col < K_sp]
                _sc_hits = _scan_bars.index[_high_col > K_sc]
                if len(_sp_hits) > 0:
                    _sp_breach_ts = _sp_hits[0]
                if len(_sc_hits) > 0:
                    _sc_breach_ts = _sc_hits[0]

            _sp_itm = _sp_breach_ts is not None
            _sc_itm = _sc_breach_ts is not None
            _alpaca_sellout = _sp_itm or _sc_itm

            if _alpaca_sellout:
                _breach_candidates = [
                    t for t in (_sp_breach_ts, _sc_breach_ts) if t is not None
                ]
                _check_ts = min(_breach_candidates)
                S_check = tradable_close_at(_scan_bars.loc[_check_ts])
                S_exit = S_check
                # Alpaca auto-liquidate / sellout fires at the breach print
                # (e.g. 15:45 ET) — broker closes the book, no manage order and
                # no SIP/DATA_DELAY fill lag. CBBO at_time = breach bar open.
                exit_time = pd.Timestamp(_check_ts)
                self.logger.info(
                    "IC Alpaca-sellout [task=%s]: first breach @ %s (fill=breach, "
                    "no delay) S=%.2f "
                    "(SP_ITM=%s K_sp=%.1f | SC_ITM=%s K_sc=%.1f) "
                    "scan_window=[%s, %s]",
                    _task_idx,
                    _check_ts,
                    S_check,
                    _sp_itm,
                    K_sp,
                    _sc_itm,
                    K_sc,
                    _scan_start,
                    _scan_end,
                )
            else:
                S_check = S_settle
                _check_ts = _settle_ts
                S_exit = S_settle
                # Safe expiry settlement still uses manage fill clock.
                exit_time = calculate_fill_time(
                    _settle_ts, self.config.INTERVAL, self.config.DATA_DELAY
                )
            duration = (exit_time - entry_time).total_seconds() / 60.0

            _pin_tol = float(getattr(self.config, "PIN_RISK_TOLERANCE", 1.0))
            _near_pin_floor = float(getattr(self.config, "IC_NEAR_PIN_FLOOR", 0.10))
            _cbbo_window = int(getattr(self.config, "CBBO_WINDOW_MIN", 10))
            _odh = option_data_handler

            if not _alpaca_sellout and not _scan_bars.empty:
                _s_low = cast(
                    "pd.Series",
                    _scan_bars["Low"] if "Low" in _scan_bars.columns else _scan_spot,
                )
                _s_high = cast(
                    "pd.Series",
                    _scan_bars["High"] if "High" in _scan_bars.columns else _scan_spot,
                )
                _last_hr_min_put_dist = float((_s_low - K_sp).min())
                _last_hr_min_call_dist = float((K_sc - _s_high).min())

            if _alpaca_sellout:
                _sellout_legs = []
                if _sp_itm:
                    _sellout_legs.extend(["sp", "lp"])
                if _sc_itm:
                    _sellout_legs.extend(["sc", "lc"])

                _batch_reqs = [
                    {
                        "ticker": self.ticker,
                        "strike": _leg_meta[_ln][1],
                        "expiration": expiration,
                        "opt_type": _leg_meta[_ln][0],
                        "at_time": exit_time,
                        "leg_key": _ln,
                    }
                    for _ln in _sellout_legs
                ]
                _batch_out = (
                    _odh.prefetch_ic_cbbo(
                        _batch_reqs,
                        window_min=_cbbo_window,
                    )
                    if _batch_reqs
                    else {}
                )

                for _ln in _sellout_legs:
                    _ot, _k = _leg_meta[_ln]
                    _is_liquidated = (_ln == "sp" and _sp_itm) or (
                        _ln == "sc" and _sc_itm
                    )

                    cbbo = _batch_out.get(_ln)

                    if cbbo is not None and float(cbbo.get("mid", -1)) >= 0:
                        # Store executable close-side prices directly. For a
                        # short IC, buy inner shorts at ask and sell long wings
                        # at bid; reverse those sides for a reverse IC.
                        is_inner = _ln in ("sp", "sc")
                        buy_to_close = is_inner if is_short else not is_inner
                        if bool(getattr(self.config, "IC_SELLOUT_EXECUTABLE", True)):
                            leg_mids[_ln] = float(
                                cbbo["ask"] if buy_to_close else cbbo["bid"]
                            )
                        else:
                            leg_mids[_ln] = float(cbbo["mid"])
                        if _is_liquidated:
                            _methods[_ln] = "alpaca_sellout_executable_cbbo"
                        else:
                            _methods[_ln] = "wing_sellout_executable_cbbo"
                        _exit_bar_ts[_ln] = f"cbbo@{exit_time}"
                    else:
                        if _ot == "put":
                            iv = max(_k - S_check, 0.0)
                        else:
                            iv = max(S_check - _k, 0.0)

                        if _is_liquidated:
                            _tv_floor = float(
                                getattr(self.config, "IC_SELLOUT_TV_FLOOR", 0.15)
                            )
                            _tv = max(_tv_floor, 0.15 * iv) if iv > 0 else _tv_floor
                            iv = iv + _tv
                            _methods[_ln] = "alpaca_sellout_intrinsic_fallback"
                        else:
                            _methods[_ln] = "wing_sellout_intrinsic_fallback"

                        leg_mids[_ln] = iv
                        _exit_bar_ts[_ln] = f"S_check={S_check:.2f}"
                        self.logger.warning(
                            "IC sellout [task=%s] %s: no CBBO at breach ts %s, "
                            "fallback intrinsic=%.4f (S=%.2f K=%.1f)",
                            _task_idx,
                            _ln,
                            _check_ts,
                            iv,
                            S_check,
                            _k,
                        )

            for _ln in ("lp", "sp", "sc", "lc"):
                if _ln in leg_mids:
                    continue

                _ot, _k = _leg_meta[_ln]
                if _ot == "put":
                    iv = max(_k - S_settle, 0.0)
                else:
                    iv = max(S_settle - _k, 0.0)

                if iv == 0.0:
                    if _ot == "put":
                        dist_otm = S_settle - _k
                    else:
                        dist_otm = _k - S_settle

                    if dist_otm >= _pin_tol:
                        leg_mids[_ln] = 0.0
                        _methods[_ln] = "otm_expires_worthless"
                        _exit_bar_ts[_ln] = f"S_settle={S_settle:.2f}"
                        continue

                    if _ln in ("sp", "sc") and 0 < dist_otm < _pin_tol:
                        _residual = _near_pin_floor * (1.0 - dist_otm / _pin_tol)
                        _residual = max(_near_pin_floor * 0.5, _residual)
                        iv = min(_residual, wing)

                leg_mids[_ln] = iv
                if iv == 0.0:
                    _methods[_ln] = "otm_expires_worthless"
                elif _ln in ("sp", "sc") and iv < _near_pin_floor:
                    _methods[_ln] = "near_pin_residual"
                else:
                    _methods[_ln] = "itm_settle"
                _exit_bar_ts[_ln] = f"S_settle={S_settle:.2f}"

        else:
            # Intraday breach / proximity exit — price with exit-time CBBO.
            _odh = option_data_handler
            _cbbo_window = int(getattr(self.config, "CBBO_WINDOW_MIN", 10))
            _breach_reqs = [
                {
                    "ticker": self.ticker,
                    "strike": _leg_meta[_ln][1],
                    "expiration": expiration,
                    "opt_type": _leg_meta[_ln][0],
                    "at_time": exit_time,
                    "leg_key": _ln,
                }
                for _ln in ("lp", "sp", "sc", "lc")
            ]
            _breach_cbbo = _odh.prefetch_ic_cbbo(
                _breach_reqs,
                window_min=_cbbo_window,
            )

            # Per-leg CBBO: keep valid quotes; intrinsic/TV only for missing legs.
            # Ask-debit floor requires all four bid/ask (_exit_cbbo_ok).
            _is_tp_exit = bool(pos.get("_tp_exit", False))
            _tv_per_short = 0.0
            if _is_tp_exit and credit > 0:
                _max_hold_min = float(getattr(self.config, "MAX_HOLDING_MINUTES", 1605))
                _T_total_h = _max_hold_min / 60.0
                _T_elapsed_h = (exit_time - entry_time).total_seconds() / 3600.0
                _tv_frac = float(
                    np.sqrt(max(0.0, (_T_total_h - _T_elapsed_h) / _T_total_h))
                )
                _tv_per_short = credit * _tv_frac / 2.0

            _exit_bids, _exit_asks, _missed = apply_breach_exit_cbbo(
                _breach_cbbo,
                _leg_meta,
                S_exit,
                exit_time,
                leg_mids,
                _methods,
                _exit_bar_ts,
                is_tp_exit=_is_tp_exit,
                tv_per_short=_tv_per_short,
            )
            _breach_cbbo_ok = len(_missed) == 0
            pos["_exit_bids"] = _exit_bids
            pos["_exit_asks"] = _exit_asks
            pos["_exit_cbbo_ok"] = _breach_cbbo_ok

            if _missed:
                self.logger.warning(
                    "IC breach close [task=%s]: missing CBBO on %s at %s S=%.2f "
                    "— kept CBBO on other legs; fallback=%s",
                    _task_idx,
                    ",".join(_missed),
                    exit_time,
                    S_exit,
                    "tp_tv" if _is_tp_exit else "intrinsic",
                )

        _put_used_fallback = any(
            "fallback" in _methods[_ln]
            or "intrinsic" in _methods[_ln]
            or "settle" in _methods[_ln]
            or "worthless" in _methods[_ln]
            or "pin" in _methods[_ln]
            for _ln in ("sp", "lp")
        )
        _call_used_fallback = any(
            "fallback" in _methods[_ln]
            or "intrinsic" in _methods[_ln]
            or "settle" in _methods[_ln]
            or "worthless" in _methods[_ln]
            or "pin" in _methods[_ln]
            for _ln in ("sc", "lc")
        )
        _put_spread = leg_mids["sp"] - leg_mids["lp"]
        _call_spread = leg_mids["sc"] - leg_mids["lc"]

        if _put_used_fallback and _put_spread > wing:
            _excess = _put_spread - wing
            leg_mids["sp"] -= _excess
            self.logger.info(
                "IC wing-cap [task=%s]: put spread %.4f > wing %.1f, "
                "reduced SP %.4f→%.4f",
                _task_idx,
                _put_spread,
                wing,
                leg_mids["sp"] + _excess,
                leg_mids["sp"],
            )

        if _call_used_fallback and _call_spread > wing:
            _excess = _call_spread - wing
            leg_mids["sc"] -= _excess
            self.logger.info(
                "IC wing-cap [task=%s]: call spread %.4f > wing %.1f, "
                "reduced SC %.4f→%.4f",
                _task_idx,
                _call_spread,
                wing,
                leg_mids["sc"] + _excess,
                leg_mids["sc"],
            )

        close_credit = (leg_mids["sp"] + leg_mids["sc"]) - (
            leg_mids["lp"] + leg_mids["lc"]
        )
        mid_close_credit = close_credit

        _exit_bids = pos.get("_exit_bids") or {}
        _exit_asks = pos.get("_exit_asks") or {}
        _exit_cbbo_ok = bool(pos.get("_exit_cbbo_ok")) and all(
            lg in _exit_bids and lg in _exit_asks for lg in ("lp", "sp", "sc", "lc")
        )

        # Exit combo spread from exit-time bid/ask when available; else entry proxy.
        _exit_combo_spread = 0.0
        _ask_debit = 0.0
        _bid_credit_exit = 0.0
        if _exit_cbbo_ok:
            _exit_combo_spread = (
                (_exit_asks["lp"] - _exit_bids["lp"])
                + (_exit_asks["sp"] - _exit_bids["sp"])
                + (_exit_asks["sc"] - _exit_bids["sc"])
                + (_exit_asks["lc"] - _exit_bids["lc"])
            )
            # Cost to buy back short IC / credit to close long IC at market.
            _ask_debit = (_exit_asks["sp"] + _exit_asks["sc"]) - (
                _exit_bids["lp"] + _exit_bids["lc"]
            )
            _bid_credit_exit = (_exit_bids["sp"] + _exit_bids["sc"]) - (
                _exit_asks["lp"] + _exit_asks["lc"]
            )

        _entry_combo_spread = float(pos.get("_entry_combo_spread") or 0.0)
        _slip_base = (
            _exit_combo_spread
            if _exit_combo_spread > 0
            else (
                _entry_combo_spread
                if _entry_combo_spread > 0
                else max(abs(credit), abs(close_credit))
            )
        )

        combo_slip = 0.0
        if not _is_time_close:
            _slip_mult = float(getattr(self.config, "IC_EXIT_SLIP_MULT", 2.0))
            combo_slip = max(0.02, _slip_mult * ic_fill_step_pct * _slip_base)
            if _exit_cbbo_ok:
                if is_short:
                    _filled = mid_close_credit + combo_slip
                    if bool(getattr(self.config, "IC_ASK_DEBIT_EXIT", True)):
                        close_credit = max(_filled, _ask_debit)
                    else:
                        close_credit = _filled
                else:
                    _filled = mid_close_credit - combo_slip
                    if bool(getattr(self.config, "IC_ASK_DEBIT_EXIT", True)):
                        close_credit = min(_filled, _bid_credit_exit)
                    else:
                        close_credit = _filled
                combo_slip = 0.0  # already embedded in close_credit

        if is_short:
            pnl_ps = credit - close_credit - combo_slip
        else:
            pnl_ps = close_credit - credit - combo_slip

        commission = 2 * 4 * comm_per_leg * n_contracts
        total_pnl = pnl_ps * 100 * n_contracts - commission

        self.logger.info(
            "IC close (%s) [task=%s]: ctr=%d  credit=%.3f close_credit=%.3f pnl_ps=%.3f "
            "total=$%.2f S_exit=%.2f | "
            "lp=%.3f(%s) sp=%.3f(%s) sc=%.3f(%s) lc=%.3f(%s)",
            exit_time,
            _task_idx,
            n_contracts,
            credit,
            close_credit,
            pnl_ps,
            total_pnl,
            S_exit,
            leg_mids["lp"],
            _methods["lp"],
            leg_mids["sp"],
            _methods["sp"],
            leg_mids["sc"],
            _methods["sc"],
            leg_mids["lc"],
            _methods["lc"],
        )

        if pd.isna(total_pnl):
            self.logger.warning(
                "IC close: NaN PnL for task %s — skipping equity update", _task_idx
            )
            total_pnl = 0.0

        self._running_equity += total_pnl
        self._collateral_in_use -= trade_collateral

        if _is_time_close:
            if _alpaca_sellout:
                reason = "Alpaca_Sellout_345PM"
                expiry_type = "Breach"
            elif (
                _last_hr_min_put_dist is not None and _last_hr_min_call_dist is not None
            ):
                _pin_tol_r = float(getattr(self.config, "PIN_RISK_TOLERANCE", 1.0))
                if (
                    _last_hr_min_put_dist >= _pin_tol_r
                    and _last_hr_min_call_dist >= _pin_tol_r
                ):
                    reason = "Expiry_Safe"
                    expiry_type = "Safe"
                else:
                    reason = "Expiry_Risky"
                    expiry_type = "Risky"
            else:
                reason = "Expiry_Settlement"
                expiry_type = "Settlement"
        else:
            reason = "Breach_Exit_MTM"
            expiry_type = "Breach"

        _entry_cbbo_ts: dict[str, str] = {}
        _entry_bid: dict[str, float | None] = {}
        _entry_ask: dict[str, float | None] = {}
        _entry_mid: dict[str, float | None] = {}
        for _ln2 in ("lp", "sp", "sc", "lc"):
            entry_cbbo = _ic_leg_cbbo.get((_task_idx, _ln2))
            if entry_cbbo is not None:
                _entry_cbbo_ts[_ln2] = entry_cbbo["ts_used"]
                _entry_bid[_ln2] = entry_cbbo.get("bid")
                _entry_ask[_ln2] = entry_cbbo.get("ask")
                _entry_mid[_ln2] = entry_cbbo.get("mid")
            else:
                _entry_cbbo_ts[_ln2] = "no_data"
                _entry_bid[_ln2] = None
                _entry_ask[_ln2] = None
                _entry_mid[_ln2] = None

        def _fv(d: dict, k: str) -> float:
            v = d.get(k)
            return float(v) if v is not None and v == v else 0.0

        _cm_sp = _fv(_entry_mid, "sp")
        _cm_sc = _fv(_entry_mid, "sc")
        _cm_lp = _fv(_entry_mid, "lp")
        _cm_lc = _fv(_entry_mid, "lc")
        _cb_sp = _fv(_entry_bid, "sp")
        _cb_sc = _fv(_entry_bid, "sc")
        _ca_lp = _fv(_entry_ask, "lp")
        _ca_lc = _fv(_entry_ask, "lc")
        _ca_sp = _fv(_entry_ask, "sp")
        _ca_sc = _fv(_entry_ask, "sc")
        _cb_lp = _fv(_entry_bid, "lp")
        _cb_lc = _fv(_entry_bid, "lc")

        _mid_credit_raw = (_cm_sp + _cm_sc) - (_cm_lp + _cm_lc)
        _bid_credit = (_cb_sp + _cb_sc) - (_ca_lp + _ca_lc)
        _combo_spread = (
            (_ca_sp - _cb_sp)
            + (_ca_sc - _cb_sc)
            + (_ca_lp - _cb_lp)
            + (_ca_lc - _cb_lc)
        )
        _credit_wing_pct = (credit / wing * 100) if wing > 0 else 0.0
        _spread_pct = (
            (_combo_spread / _mid_credit_raw * 100) if _mid_credit_raw > 0 else 0.0
        )
        _fill_slippage = _mid_credit_raw - credit
        _bid_mid_gap = _mid_credit_raw - _bid_credit

        self.trade_log.append(
            {
                "EntryTime": entry_time,
                "ExitTime": exit_time,
                "Signal": pos.get("Signal", ""),
                "Type": "SHORT_IC" if is_short else "REVERSE_IC",
                "Confidence": pos.get("Confidence", 0),
                "EntryPrice": credit,
                "ExitPrice": close_credit,
                "Contracts": n_contracts,
                "PnL": total_pnl,
                "PnLPercent": (total_pnl / trade_collateral) * 100
                if trade_collateral > 0
                else 0.0,
                "DurationMinutes": duration,
                "S_exit": S_exit,
                "K_LP": K_lp,
                "K_SP": K_sp,
                "K_SC": K_sc,
                "K_LC": K_lc,
                "Strikes": f"{K_lp}/{K_sp}|{K_sc}/{K_lc}",
                "Wing": wing,
                "BPR_PerShare": bpr_ps,
                "Collateral": trade_collateral,
                "Reason": reason,
                "ExpiryType": expiry_type,
                "LastHour_MinPutDist": _last_hr_min_put_dist,
                "LastHour_MinCallDist": _last_hr_min_call_dist,
                "CloseMethod": "|".join(
                    f"{lg}={_methods[lg]}" for lg in ("lp", "sp", "sc", "lc")
                ),
                "EntryBid_LP": _entry_bid.get("lp"),
                "EntryBid_SP": _entry_bid.get("sp"),
                "EntryBid_SC": _entry_bid.get("sc"),
                "EntryBid_LC": _entry_bid.get("lc"),
                "EntryAsk_LP": _entry_ask.get("lp"),
                "EntryAsk_SP": _entry_ask.get("sp"),
                "EntryAsk_SC": _entry_ask.get("sc"),
                "EntryAsk_LC": _entry_ask.get("lc"),
                "EntryMid_LP": _entry_mid.get("lp"),
                "EntryMid_SP": _entry_mid.get("sp"),
                "EntryMid_SC": _entry_mid.get("sc"),
                "EntryMid_LC": _entry_mid.get("lc"),
                "LegMid_LP": leg_mids.get("lp"),
                "LegMid_SP": leg_mids.get("sp"),
                "LegMid_SC": leg_mids.get("sc"),
                "LegMid_LC": leg_mids.get("lc"),
                "Expiration": pos.get("_expiration", ""),
                "SignalDate": str(pos.get("_signal_time", ""))[:10],
                "EntryLegMethods": "|".join(
                    f"{lg}={pos.get('_entry_leg_methods', {}).get(lg, '?')}"
                    for lg in ("lp", "sp", "sc", "lc")
                ),
                "EntryBarTS": "|".join(
                    f"{lg}={_entry_cbbo_ts.get(lg, '?')}"
                    for lg in ("lp", "sp", "sc", "lc")
                ),
                "ExitBarTS": "|".join(
                    f"{lg}={_exit_bar_ts.get(lg, '?')}"
                    for lg in ("lp", "sp", "sc", "lc")
                ),
                "PositionSize": 1.0,
                "CapitalAllocated": trade_collateral,
                "RunningEquity": self._running_equity,
                "MidCredit": round(_mid_credit_raw, 4),
                "BidCredit": round(_bid_credit, 4),
                "ComboSpread": round(_combo_spread, 4),
                "CreditWingRatio": round(_credit_wing_pct, 2),
                "SpreadPct": round(_spread_pct, 2),
                "FillSlippage": round(_fill_slippage, 4),
                "BidMidGap": round(_bid_mid_gap, 4),
                "ExitMidDebit": round(mid_close_credit, 4),
                "ExitAskDebit": round(_ask_debit, 4) if _exit_cbbo_ok else None,
                "ExitComboSpread": round(_exit_combo_spread, 4)
                if _exit_combo_spread > 0
                else None,
                "ExitMethod": reason,
                "UsedFallback": any(
                    any(
                        tok in str(_methods.get(lg, ""))
                        for tok in (
                            "fallback",
                            "full_wing",
                        )
                    )
                    for lg in ("lp", "sp", "sc", "lc")
                ),
            }
        )
        self._update_reason_stats(self.trade_log[-1])

        if pos in self._active_positions:
            self._active_positions.remove(pos)
        return True

    def force_close(self, position: dict) -> None:
        """Force-close equity position at last available manage bar."""
        if self._df_manage.empty:
            return
        sig = {
            "_position": position,
            "exit_idx": len(self._df_manage) - 1,
        }
        self.exit("", position, sig, forced=True, manage_df=self._df_manage)
