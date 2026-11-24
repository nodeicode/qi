"""IC / ET time helpers.

``et_wall_to_utc_naive`` is the active shared helper for entry clocks
(backtest ``calculate_entry_time``).

Next-bday remapping helpers remain only while ``quant_range`` still
imports ``ic_entry_date``.  They are not used by live orchestration
(``example_equity`` + shared T+I).  Delete with ``quant_range*`` when no
backtest consumer remains.
"""

from __future__ import annotations

import datetime
import warnings
import zoneinfo

import pandas as pd

_ET = zoneinfo.ZoneInfo("America/New_York")
_IC_ENTRY_MINUTE_ET_DEFAULT = 0


def ic_uses_next_bday_entry(config) -> bool:
    """Return True when a completed daily signal bar enters next business morning.

    .. deprecated:: Prefer ``example_equity`` bar-gate entry; next-bday remapping
        is no longer used by orchestration.
    """
    warnings.warn(
        "ic_uses_next_bday_entry is deprecated; orchestration uses shared "
        "calculate_entry_time (T+I). Prefer example_equity bar-gate strategies.",
        DeprecationWarning,
        stacklevel=2,
    )
    entry_hour = int(getattr(config, "IC_ENTRY_HOUR_ET", 15))
    strat_interval = getattr(config, "INTERVAL", "1m")
    return strat_interval in ("1d", "1wk", "1mo") and entry_hour < 15


def ic_entry_date(config, signal_ts) -> datetime.date:
    """Calendar entry date for a signal-bar timestamp.

    .. deprecated:: Prefer ``example_equity`` same-day bar-gate entry.
    """
    warnings.warn(
        "ic_entry_date is deprecated; prefer same-day bar-gate entry "
        "(example_equity) and calculate_entry_time for fills.",
        DeprecationWarning,
        stacklevel=2,
    )
    sig = pd.Timestamp(signal_ts).date()
    # Inline next-bday check to avoid double-warning from ic_uses_next_bday_entry.
    entry_hour = int(getattr(config, "IC_ENTRY_HOUR_ET", 15))
    strat_interval = getattr(config, "INTERVAL", "1m")
    if strat_interval in ("1d", "1wk", "1mo") and entry_hour < 15:
        return (pd.Timestamp(sig) + pd.offsets.BDay(1)).date()
    return sig


def et_wall_to_utc_naive(
    entry_date: datetime.date,
    hour: int,
    minute: int = 0,
) -> pd.Timestamp:
    """Convert an ET wall clock on *entry_date* to UTC-naive (DST-safe).

    Naive output means **UTC wall clock**, never ET.  Summer/winter offsets
    come from ``America/New_York`` (e.g. 10:00 ET → 14:00 UTC EDT, 15:00 UTC EST).
    """
    ts_et = pd.Timestamp(
        year=entry_date.year,
        month=entry_date.month,
        day=entry_date.day,
        hour=int(hour),
        minute=int(minute),
        second=0,
        tz=_ET,
    )
    return ts_et.tz_convert("UTC").tz_localize(None)


def calc_ic_entry_time(signal_timestamp, config) -> pd.Timestamp:
    """UTC-naive entry timestamp at ``IC_ENTRY_HOUR_ET:IC_ENTRY_MINUTE_ET``.

    .. deprecated:: Prefer ``quant.backtest.utils.calculate_entry_time``.
    """
    warnings.warn(
        "calc_ic_entry_time is deprecated; use calculate_entry_time "
        "(shared T+I / next-open rule).",
        DeprecationWarning,
        stacklevel=2,
    )
    entry_hour = int(getattr(config, "IC_ENTRY_HOUR_ET", 15))
    entry_min = int(getattr(config, "IC_ENTRY_MINUTE_ET", _IC_ENTRY_MINUTE_ET_DEFAULT))
    # Avoid double-warning: inline ic_entry_date logic.
    sig = pd.Timestamp(signal_timestamp).date()
    strat_interval = getattr(config, "INTERVAL", "1m")
    if strat_interval in ("1d", "1wk", "1mo") and entry_hour < 15:
        entry_date = (pd.Timestamp(sig) + pd.offsets.BDay(1)).date()
    else:
        entry_date = sig
    return et_wall_to_utc_naive(entry_date, entry_hour, entry_min)
