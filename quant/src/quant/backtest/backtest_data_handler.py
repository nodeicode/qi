"""
BacktestDataHandler — adapter exposing ``data_handler`` interface
=================================================================

``engine.process_strategy`` fetches bar data via
``self.data_handler.get_historical_data(ticker, start, end, interval)``.
The live trader plugs in ``AlpacaDataHandler`` (disk-cached via
``StockDataCache``).  The backtester plugs in this class, which wraps
the pre-loaded ``df_signals`` / ``df_manage`` DataFrames and returns
``searchsorted``-based prefix views \u2014 no copy per bar.

Why a wrapper instead of using ``AlpacaDataHandler`` directly?
    * The backtester already loads the full window upfront and runs
      ``calculate_indicators`` once.  Re-fetching per bar (even from
      disk) would re-run indicators and dwarf the per-bar work.
    * Returning a ``.iloc[:right]`` view per bar is O(log n) and
      allocation-free.
"""

from __future__ import annotations

import pandas as pd

from quant.core._utils import _trim_incomplete_daily_bar
from quant.data._times import assert_canonical_index, parse_naive_utc
from quant.utils.time import closed_bar_asof


class BacktestDataHandler:
    """Slice pre-loaded backtest DataFrames as if they were live fetches.

    Parameters
    ----------
    df_signals
        Indicator + signal DataFrame at the signal interval.
    df_manage
        Bar DataFrame at the manage interval (used by
        ``call_manage_trade`` for exit detection).
    signal_interval
        Interval string matching ``strategy.config.INTERVAL``.
    manage_interval
        Interval string matching ``strategy.config.MANAGE_INTERVAL``.
    """

    def __init__(
        self,
        df_signals: pd.DataFrame,
        df_manage: pd.DataFrame,
        signal_interval: str,
        manage_interval: str,
    ):
        self._df_signals = df_signals
        self._df_manage = df_manage
        self._signal_interval = signal_interval
        self._manage_interval = manage_interval

    def get_historical_data(
        self,
        ticker: str,
        start,
        end,
        interval: str,
        use_cache: bool = True,
        adjustment: str = "all",
    ) -> pd.DataFrame:
        """Return a window view of the pre-loaded DataFrame for *interval*.

        Both *start* and *end* are honoured via ``searchsorted`` so the
        returned slice matches the rolling lookback window computed by
        ``fetch_data``.  No copy is made; ``df.iloc[left:right]`` is a
        zero-allocation view.

        For daily intervals, applies the same incomplete-bar trim as the
        live ``fetch_data`` path so mid-session cutoffs cannot include
        today's still-forming bar (D1/D2).

        For intraday intervals, trims to ``closed_bar_asof(end, interval)``
        so manage/signal fetches never include the bar still open at *end*
        (``searchsorted(..., side="right")`` alone would).
        """
        if interval == self._signal_interval:
            df = self._df_signals
        elif interval == self._manage_interval:
            df = self._df_manage
        else:
            return pd.DataFrame()

        if df.empty:
            return df

        idx_tz = getattr(df.index, "tz", None)

        # Boundary: canonical naive-UTC bounds via ``_times`` (never re-derive
        # tz semantics per call).  When the (daily) preloaded index is tz-aware
        # we only align the bound for the slice — a transient, not a conversion
        # of the data.
        start_t, end_t = parse_naive_utc(start, end)
        if idx_tz is not None:
            start_t = start_t.tz_localize(idx_tz)
            end_t = end_t.tz_localize(idx_tz)

        left = df.index.searchsorted(start_t, side="left")
        right = df.index.searchsorted(end_t, side="right")
        out = df.iloc[left:right]

        if interval == "1d":
            return _trim_incomplete_daily_bar(out, end_t)

        if out.empty:
            return out

        last_closed = closed_bar_asof(end_t, interval)
        if idx_tz is None and last_closed.tz is not None:
            last_closed = last_closed.tz_localize(None)
        elif idx_tz is not None and last_closed.tz is None:
            last_closed = last_closed.tz_localize(idx_tz)
        elif idx_tz is not None and last_closed.tz is not None:
            last_closed = last_closed.tz_convert(idx_tz)

        right_closed = out.index.searchsorted(last_closed, side="right")
        out = out.iloc[:right_closed]
        assert_canonical_index(out, interval=interval)
        return out
