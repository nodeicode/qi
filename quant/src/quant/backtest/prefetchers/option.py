import datetime
from typing import cast

import pandas as pd

from quant.backtest.utils import build_spread_strikes, calculate_fill_time
from quant.data.options_data_handler import OptionDataHandler
from quant.utils.options import select_strike
from quant.utils.strategy_config import BaseStrategyConfig


def prefetch_option_contracts(
    ticker: str,
    asset_class: str,
    actionable_signals: pd.DataFrame,
    expiration_cache: dict[str, str],
    option_data_handler: OptionDataHandler,
    config: BaseStrategyConfig,
):
    """Build tasks and batch-fetch option contracts — OPTION + SPREAD."""
    tasks: list[dict] = []
    for ts, row in actionable_signals.iterrows():
        ts = cast("pd.Timestamp", ts)
        signal = str(row.get("Signal", ""))
        entry_time = calculate_fill_time(ts, config.INTERVAL, config.DATA_DELAY)
        day_str = pd.Timestamp(entry_time).date().strftime("%Y-%m-%d")
        expiration = expiration_cache.get(day_str)
        if not expiration:
            continue

        underlying_price = float(row.get("Raw_Close", row.get("Close", 0.0)))
        opt_type = "call" if "CALL" in signal else "put"
        opt_max_hold_days = getattr(config, "OPTION_MAX_HOLD_DAYS", 10)
        opt_max_hold_min = min(
            config.MAX_HOLDING_MINUTES,
            opt_max_hold_days * 1440,
        )
        end_dt = entry_time + datetime.timedelta(minutes=opt_max_hold_min + 10)
        interval = getattr(
            config,
            "OPTION_INTERVAL",
            getattr(config, "MANAGE_INTERVAL", "1m"),
        )

        if asset_class == "SPREAD":
            is_credit = signal in {"SELL_CALL", "SELL_PUT"}
            long_strike, short_strike, _, _ = build_spread_strikes(
                config, underlying_price, opt_type, is_credit
            )
            tasks.append(
                {
                    "ticker": ticker,
                    "expiration": expiration,
                    "long_strike": long_strike,
                    "short_strike": short_strike,
                    "opt_type": opt_type,
                    "entry_time": entry_time,
                    "end_dt": end_dt,
                    "interval": interval,
                }
            )
        else:
            strike = select_strike(underlying_price, opt_type, config)
            tasks.append(
                {
                    "ticker": ticker,
                    "expiration": expiration,
                    "strike": strike,
                    "opt_type": opt_type,
                    "entry_time": entry_time,
                    "end_dt": end_dt,
                    "interval": interval,
                }
            )

    if not tasks:
        return

    # Convert tasks to standard handler requests (spread → two legs each).
    batch_reqs: list[dict] = []
    for task in tasks:
        if asset_class == "SPREAD":
            for strike_key in ("long_strike", "short_strike"):
                req = {
                    "ticker": task["ticker"],
                    "expiration": task["expiration"],
                    "strike": task[strike_key],
                    "opt_type": task["opt_type"],
                    "start_dt": task["entry_time"],
                    "end_dt": task["end_dt"],
                    "interval": task["interval"],
                }
                batch_reqs.append(req)
        else:
            batch_reqs.append(
                {
                    "ticker": task["ticker"],
                    "expiration": task["expiration"],
                    "strike": task["strike"],
                    "opt_type": task["opt_type"],
                    "start_dt": task["entry_time"],
                    "end_dt": task["end_dt"],
                    "interval": task["interval"],
                }
            )
    return option_data_handler.fetch_cbbo_range_batch(batch_reqs)
