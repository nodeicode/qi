"""Fill-space helpers shared by the backtest venue's execution client.

These utilities were extracted verbatim from the deleted per-asset-class
backtest executors (``quant/backtest/executors/*`` and
``quant/backtest/utils.py``) into this leaf module so the venue (a port,
importable by every layer) can price and clock fills without importing the
terminal ``quant.backtest`` package. Semantics are unchanged: prices come
from the *tradable* close of closed bars (``Raw_Close`` preferred, matching
the unadjusted execution book) while signals run on adjusted bars, the fill
clock is ``T+I+DATA_DELAY``, and slippage is applied per-direction on entry
vs exit.
"""

from __future__ import annotations

import datetime

import pandas as pd

from quant.utils.strategy_config import BaseStrategyConfig
from quant.utils.time import _INTRADAY_HOUR, _INTRADAY_MINUTE, calculate_entry_time


def calculate_fill_time(
    signal_timestamp,
    interval: str,
    data_delay_min: int = 0,
) -> pd.Timestamp:
    """Live-parity fill clock: bar close + feed delay for intraday bars.

    Used for both entry and exit fills.  Example (example_equity): manage/signal
    bar stamped ``T``, interval ``15m``, ``DATA_DELAY=15`` → fill at ``T+30m``
    wall (bar closes at ``T+15``, then SIP delay).  Entry: ``09:45`` → ``10:15``
    ET.  Exit: same rule on the manage bar that triggered the exit.
    """
    entry = calculate_entry_time(signal_timestamp, interval)
    delay = max(0, int(data_delay_min or 0))

    # TODO: why naive?
    if entry.tzinfo is not None:
        entry = entry.tz_convert("UTC").tz_localize(None)

    if delay <= 0:
        return entry
    if interval in _INTRADAY_MINUTE or interval in _INTRADAY_HOUR:
        return pd.Timestamp(entry) + datetime.timedelta(minutes=delay)
    return entry


def tradable_close(frame: pd.DataFrame) -> pd.Series:
    """Return the *tradable* close series for a manage/signals frame.

    Priors from attached Raw_Close (unadjusted, matches the Databento CBBO
    book) — the backtest venue prices in tradable space even when indicators
    run on adjusted ``"all"`` bars. Falls back to ``Close`` when Raw_Close
    was never attached (raw-space runs where Close == Raw_Close anyway).
    """
    if "Raw_Close" in frame.columns:
        return frame["Raw_Close"]
    if "Close" in frame.columns:
        return frame["Close"]
    raise KeyError("frame has neither Raw_Close nor Close")


def tradable_close_at(row: pd.Series) -> float:
    """Scalar tradable close for one bar row (Raw_Close preferred over Close)."""
    row_series = row.to_frame().T if isinstance(row, pd.Series) else row
    return float(tradable_close(row_series).iloc[0])


def apply_slippage(
    config: BaseStrategyConfig,
    raw_price: float,
    direction: int,
    is_entry: bool,
) -> float:
    """Apply entry or exit slippage to a raw fill price."""
    # ``SLIPPAGE_PER_SHARE`` is an optional strategy extension; the base
    # config guarantees the factor fallback but intentionally does not require
    # every strategy to declare a per-share override.
    slippage_ps = float(getattr(config, "SLIPPAGE_PER_SHARE", 0.0))
    slippage_fac = float(config.BASE_SLIPPAGE_FACTOR)
    sign = direction if is_entry else -direction
    if slippage_ps > 0:
        return raw_price + slippage_ps * sign
    if slippage_fac > 0:
        return raw_price * (1.0 + slippage_fac * sign)
    return raw_price
