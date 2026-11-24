import datetime
from logging import Logger
from typing import cast

import pandas as pd

from quant.backtest.utils import calculate_fill_time
from quant.data.data_handler import AlpacaDataHandler
from quant.data.options_data_handler import OptionDataHandler
from quant.utils.strategy_config import BaseStrategyConfig


def prefetch_ic_legs(
    ticker: str,
    logger: Logger,
    _ic_drop_stats: dict[str, int],
    expiration_cache: dict[str, str],
    config: BaseStrategyConfig,
    df_signals: pd.DataFrame,
    option_data_handler: OptionDataHandler,
    data_handler: AlpacaDataHandler,
) -> tuple[pd.DataFrame, pd.DataFrame, dict, dict, dict, list[dict]]:
    """Prefetch 4-leg option bid/ask via Databento CBBO for all IC signals.

    Uses CBBO (consolidated best bid/offer) snapshots at the exact entry
    timestamp instead of OHLCV bars.  This gives real bid/ask prices that
    mirror what you'd see in live trading and eliminates bar-gap issues.

    Entry credit for SHORT IC:
      credit = (bid_sp + bid_sc) - (ask_lp + ask_lc) - slippage

    Returns ``(df_manage, df_manage_1m, ic_leg_prices, ic_leg_cbbo,
    ic_prefetch_stats, ic_prefetch_detail)`` — callers must persist all six
    onto the backtester/executor; ``ic_leg_prices``/``ic_leg_cbbo`` are
    required for entry fills, the rest is diagnostics.
    """
    _ic_leg_prices: dict = {}
    _ic_leg_cbbo: dict = {}  # {(task, leg): {bid, ask, mid, ts_used}}

    # IC diagnostics (skip reasons + prefetch health) surfaced to notebooks
    _ic_prefetch_stats: dict[str, object] = {}
    _ic_prefetch_detail: list[dict] = []  # per-task strike + quote coverage

    _df_manage = pd.DataFrame()
    _df_manage_1m = pd.DataFrame()

    ic_types = {"SELL_IC", "BUY_IC"}
    df_ic = df_signals[df_signals["Signal"].isin(ic_types)]
    if df_ic.empty:
        # return empty
        return (
            _df_manage,
            _df_manage_1m,
            _ic_leg_prices,
            _ic_leg_cbbo,
            _ic_prefetch_stats,
            _ic_prefetch_detail,
        )

    logger.info(
        f"IC signals: {len(df_ic)} "
        f"({int((df_ic['Signal'] == 'SELL_IC').sum())} SELL, "
        f"{int((df_ic['Signal'] == 'BUY_IC').sum())} BUY)"
    )

    # Build CBBO requests for entry pricing (shared T+I entry clock)
    cbbo_requests: list[dict] = []
    task_idx = 0
    _skip_no_exp = 0
    _skip_no_strikes = 0
    _entry_times: list[pd.Timestamp] = []

    for ts, row in df_ic.iterrows():
        ts = cast("pd.Timestamp", ts)
        entry_time = calculate_fill_time(ts, config.INTERVAL, config.DATA_DELAY)
        day_str = entry_time.date().strftime("%Y-%m-%d")
        expiration = expiration_cache.get(day_str)
        if not expiration:
            _skip_no_exp += 1
            continue
        required = ["K_LP", "K_SP", "K_SC", "K_LC"]
        if any(col not in row.index or pd.isna(row[col]) for col in required):
            _skip_no_strikes += 1
            continue

        _entry_times.append(entry_time)

        # Tag this row in df_signals so _fill_ic can find its leg prices
        df_signals.loc[ts, "_ic_task_idx"] = task_idx

        # Strategy K_* — default re-center around spot at fill.
        # Live: Alpaca stock quote mid at wall fill (EntryTime includes
        # strategy.config.DATA_DELAY). Backtest: Raw_Close of the last
        # completed bar before fill clock.
        # Opt out with IC_RECENTER_AT_FILL=False.
        _K_lp = float(row["K_LP"])
        _K_sp = float(row["K_SP"])
        _K_sc = float(row["K_SC"])
        _K_lc = float(row["K_LC"])
        if bool(getattr(config, "IC_RECENTER_AT_FILL", True)):
            try:
                from quant.utils.options import snap_ic_strikes

                _spot = None
                _px_col = "Raw_Close" if "Raw_Close" in df_signals.columns else "Close"
                _prior = df_signals.index[df_signals.index < entry_time]
                if len(_prior) and pd.notna(df_signals.loc[_prior[-1], _px_col]):
                    _spot = float(df_signals.loc[_prior[-1], _px_col])
                if _spot is not None and _spot > 0:
                    _off = float(
                        row["IC_Offset_Used"]
                        if "IC_Offset_Used" in row.index
                        and pd.notna(row.get("IC_Offset_Used"))
                        else getattr(config, "IC_OFFSET", 5)
                    )
                    _wing = float(
                        row["IC_Wing_Used"]
                        if "IC_Wing_Used" in row.index
                        and pd.notna(row.get("IC_Wing_Used"))
                        else getattr(config, "IC_WING", 2)
                    )
                    _inc = 1.0
                    _si = getattr(config, "STRIKE_INCREMENT", None)
                    if isinstance(_si, dict):
                        _inc = float(_si.get(ticker, _si.get("_default", 1.0)))
                    _K_lp, _K_sp, _K_sc, _K_lc = snap_ic_strikes(
                        _spot, _off, _wing, _inc
                    )
                    df_signals.loc[ts, "K_LP"] = _K_lp
                    df_signals.loc[ts, "K_SP"] = _K_sp
                    df_signals.loc[ts, "K_SC"] = _K_sc
                    df_signals.loc[ts, "K_LC"] = _K_lc
            except Exception as _re_exc:
                logger.debug("IC-RECENTER skipped task=%s: %s", task_idx, _re_exc)

        for leg, strike, ot in [
            ("lp", _K_lp, "put"),
            ("sp", _K_sp, "put"),
            ("sc", _K_sc, "call"),
            ("lc", _K_lc, "call"),
        ]:
            leg_key = (task_idx, leg)
            cbbo_requests.append(
                {
                    "ticker": ticker,
                    "strike": strike,
                    "expiration": expiration,
                    "opt_type": ot,
                    "at_time": entry_time,
                    "leg_key": leg_key,
                }
            )
        task_idx += 1

    logger.info(
        "IC-PREFETCH signal summary: %d signals → %d tasks assigned, "
        "%d skipped (no_expiration=%d, no_strikes=%d)",
        len(df_ic),
        task_idx,
        _skip_no_exp + _skip_no_strikes,
        _skip_no_exp,
        _skip_no_strikes,
    )
    if _skip_no_exp > 0:
        _ic_drop_stats["prefetch_no_expiration"] = (
            _ic_drop_stats.get("prefetch_no_expiration", 0) + _skip_no_exp
        )
    if _skip_no_strikes > 0:
        _ic_drop_stats["prefetch_no_strikes"] = (
            _ic_drop_stats.get("prefetch_no_strikes", 0) + _skip_no_strikes
        )

    if not cbbo_requests:
        return (
            _df_manage,
            _df_manage_1m,
            _ic_leg_prices,
            _ic_leg_cbbo,
            _ic_prefetch_stats,
            _ic_prefetch_detail,
        )

    logger.info(
        f"Fetching CBBO for {len(cbbo_requests)} entry legs ({task_idx} tasks)..."
    )

    # ONE parallel-batch call for all entry CBBO quotes
    _cbbo_window = int(getattr(config, "CBBO_WINDOW_MIN", 2))
    cbbo_results = option_data_handler.prefetch_ic_cbbo(
        cbbo_requests,
        window_min=_cbbo_window,
    )
    # Surface cache efficiency stats from the handler into _ic_prefetch_stats
    # so notebooks can show how many quotes came from cache vs API.
    _cbbo_cache_stats = getattr(option_data_handler, "_last_cbbo_prefetch_stats", {})

    # Process results → entry prices
    for req in cbbo_requests:
        key = req["leg_key"]
        cbbo = cbbo_results.get(key)
        if cbbo is not None:
            _ic_leg_cbbo[key] = cbbo
            _ic_leg_prices[key] = cbbo["mid"]

    # Build a request lookup keyed by leg_key for diagnostics
    _req_by_key: dict[tuple, dict] = {r["leg_key"]: r for r in cbbo_requests}

    # Per-task prefetch diagnostics
    _total_tasks = task_idx
    _complete = 0
    _miss_by_leg: dict[str, int] = {"lp": 0, "sp": 0, "sc": 0, "lc": 0}
    _miss_unlisted: int = 0
    _odh_missing = getattr(option_data_handler, "_missing_symbols", set())
    _prefetch_detail: list[dict] = []
    for t in range(_total_tasks):
        legs_ok = [lg for lg in ("lp", "sp", "sc", "lc") if (t, lg) in _ic_leg_prices]
        legs_miss = [
            lg for lg in ("lp", "sp", "sc", "lc") if (t, lg) not in _ic_leg_prices
        ]
        if len(legs_ok) == 4:
            _complete += 1
        elif legs_miss:
            for lg in legs_miss:
                _miss_by_leg[lg] += 1
                # Check if the symbol was flagged as unlisted on Databento
                req = _req_by_key.get((t, lg))
                if req:
                    sym = option_data_handler._format_databento_symbol(
                        req["ticker"],
                        req["strike"],
                        req["expiration"],
                        req["opt_type"],
                    )
                    if sym in _odh_missing:
                        _miss_unlisted += 1
            logger.info(
                "IC-PREFETCH task=%d missing_legs=%s (no CBBO data)", t, legs_miss
            )
        # Build per-task detail row (strikes, OTM distances, quote status)
        _treqs = {lg: _req_by_key.get((t, lg)) for lg in ("lp", "sp", "sc", "lc")}
        _any_req = next((r for r in _treqs.values() if r is not None), None)
        if _any_req is not None:
            _K = {lg: (r["strike"] if r else None) for lg, r in _treqs.items()}
            _sp, _sc = _K["sp"], _K["sc"]
            _mid_est = ((_sp + _sc) / 2.0) if (_sp and _sc) else None
            _day_str_t = _any_req["at_time"].strftime("%Y-%m-%d")
            _ep_t = _mid_est

            def _otm(k, price):
                return abs(k - price) if (k is not None and price) else None

            _prefetch_detail.append(
                {
                    "task_idx": t,
                    "entry_date": _day_str_t,
                    "expiration": _any_req.get("expiration", ""),
                    "underlying": round(_ep_t, 2) if _ep_t else None,
                    "K_LP": _K["lp"],
                    "K_SP": _K["sp"],
                    "K_SC": _K["sc"],
                    "K_LC": _K["lc"],
                    "lp_otm": _otm(_K["lp"], _ep_t),
                    "sp_otm": _otm(_K["sp"], _ep_t),
                    "sc_otm": _otm(_K["sc"], _ep_t),
                    "lc_otm": _otm(_K["lc"], _ep_t),
                    "lp_otm_pct": (_otm(_K["lp"], _ep_t) / _ep_t * 100)
                    if (_otm(_K["lp"], _ep_t) and _ep_t)
                    else None,
                    "sp_otm_pct": (_otm(_K["sp"], _ep_t) / _ep_t * 100)
                    if (_otm(_K["sp"], _ep_t) and _ep_t)
                    else None,
                    "sc_otm_pct": (_otm(_K["sc"], _ep_t) / _ep_t * 100)
                    if (_otm(_K["sc"], _ep_t) and _ep_t)
                    else None,
                    "lc_otm_pct": (_otm(_K["lc"], _ep_t) / _ep_t * 100)
                    if (_otm(_K["lc"], _ep_t) and _ep_t)
                    else None,
                    "legs_found": legs_ok,
                    "legs_missing": legs_miss,
                    "n_found": len(legs_ok),
                    "complete": len(legs_ok) == 4,
                    "short_missing": any(lg in legs_miss for lg in ("sp", "sc")),
                }
            )
    _ic_prefetch_detail = _prefetch_detail
    _total_missing = sum(_miss_by_leg.values())
    logger.info(
        f"IC leg prices: {len(_ic_leg_prices)}/{len(cbbo_requests)} "
        f"| complete tasks: {_complete}/{_total_tasks}"
    )
    logger.info(
        "IC missing-leg breakdown: lp=%d sp=%d sc=%d lc=%d "
        "| %d/%d unlisted on exchange (too far OTM)",
        _miss_by_leg["lp"],
        _miss_by_leg["sp"],
        _miss_by_leg["sc"],
        _miss_by_leg["lc"],
        _miss_unlisted,
        _total_missing,
    )

    _ic_prefetch_stats = {
        "signal_count": len(df_ic),
        "task_count": int(task_idx),
        "request_count": len(cbbo_requests),
        "skip_no_expiration": int(_skip_no_exp),
        "skip_no_strikes": int(_skip_no_strikes),
        "legs_found": len(_ic_leg_prices),
        "complete_tasks": int(_complete),
        "missing_by_leg": {k: int(v) for k, v in _miss_by_leg.items()},
        "missing_unlisted": int(_miss_unlisted),
        "missing_total": int(_total_missing),
        "cbbo_window_min": int(_cbbo_window),
        "cbbo_cache": _cbbo_cache_stats,
    }

    # Fetch underlying intraday for manage_trade
    manage_interval = getattr(config, "MANAGE_INTERVAL", "15m")
    signal_interval = getattr(config, "INTERVAL", manage_interval)
    if _entry_times:
        earliest = min(_entry_times)
        latest_exp = max(
            expiration_cache.get(et.date().strftime("%Y-%m-%d"), "")
            for et in _entry_times
        )
    else:
        earliest = calculate_fill_time(
            df_ic.index.min(), config.INTERVAL, config.DATA_DELAY
        )

        latest_exp = max(
            expiration_cache.get(
                calculate_fill_time(ts, config.INTERVAL, config.DATA_DELAY)
                .date()
                .strftime("%Y-%m-%d"),
                "",
            )
            for ts in df_ic.index
        )
    if latest_exp:
        latest_end = datetime.datetime.strptime(latest_exp, "%Y-%m-%d").replace(
            hour=21, minute=0, second=0
        )
    else:
        latest_end = df_ic.index.max() + datetime.timedelta(days=2)

    # Same-interval IC (e.g. example_equity 15m/15m): reuse prepared signal
    # bars — one stream for signals and manage, no redundant fetch.
    if signal_interval == manage_interval and not df_signals.empty:
        df_ul = df_signals.loc[
            (df_signals.index >= earliest) & (df_signals.index <= latest_end)
        ].copy()
        if df_ul.empty:
            df_ul = df_signals.copy()
        logger.info(
            "IC manage: reusing %d %s signal bars (INTERVAL==MANAGE_INTERVAL)",
            len(df_ul),
            manage_interval,
        )
    else:
        # Raw (unadjusted) prices — must match Databento CBBO space.
        df_ul = data_handler.get_historical_data(
            ticker=ticker,
            start=earliest.isoformat(),
            end=latest_end.isoformat(),
            interval=manage_interval,
            adjustment="raw",
        )

    if not df_ul.empty:
        _df_manage = df_ul.sort_index()

        # Fetch 1m bars for accurate last-hour breach/pin-risk classification.
        # Covers the 3:15-4:15 ET scan window on each expiration day.
        # If manage_interval is already "1m", alias directly (no extra call).
        if manage_interval == "1m":
            _df_manage_1m = _df_manage
        else:
            df_1m = data_handler.get_historical_data(
                ticker=ticker,
                start=earliest.isoformat(),
                end=latest_end.isoformat(),
                interval="1m",
                adjustment="raw",
            )
            if not df_1m.empty:
                _df_manage_1m = df_1m.sort_index()
                logger.info(
                    "Fetched %d 1m bars for last-hour breach/pin-risk scan",
                    len(_df_manage_1m),
                )
    return (
        _df_manage,
        _df_manage_1m,
        _ic_leg_prices,
        _ic_leg_cbbo,
        _ic_prefetch_stats,
        _ic_prefetch_detail,
    )
