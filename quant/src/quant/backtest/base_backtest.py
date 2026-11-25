import logging
from typing import cast

import numpy as np
import pandas as pd

from quant.backtest.utils import (
    calculate_deflated_sharpe_ratio,
    calculate_metrics,
)
from quant.data._times import parse_naive_utc, to_naive_index, to_naive_utc
from quant.utils.strategy_config import BaseStrategyConfig


class BaseBacktester:
    """
    Base class for backtesting engines.
    """

    def __init__(
        self,
        config: BaseStrategyConfig,
        strategy,
        start_date: str,
        end_date: str,
        ticker: str,
        verbose: bool = False,
        data_handler=None,
    ):
        self.config = config
        self.strategy = strategy
        # Underlying bar feed. Defaults to the equity Alpaca feed for existing
        # callers (behind the ``quant[alpaca]`` extra); callers may inject any
        # ``DataHandlerProtocol`` implementation. Imported lazily so the base
        # package import graph never requires alpaca-py (brokerless base).
        if data_handler is not None:
            self.data_handler = data_handler
        else:
            from quant.data.data_handler import AlpacaDataHandler  # quant[alpaca] extra

            self.data_handler = AlpacaDataHandler()
        _s0, _s1 = parse_naive_utc(start_date, end_date)
        self.start_date = _s0.isoformat()
        self.end_date = _s1.isoformat()

        # Use provided ticker
        self.ticker = ticker

        self.trade_log = []  # list to store simulated trades
        self.initial_capital = config.INITIAL_CAPITAL
        self.equity_curve: pd.Series = pd.Series(dtype=float)
        self.reason_stats = {}  # dict to track performance by reason
        self.all_signals = pd.DataFrame()  # Store all generated signals for analysis

        # Set up logger based on verbosity
        self.logger = logging.getLogger(self.__class__.__name__)
        if verbose:
            self.logger.setLevel(logging.INFO)
        else:
            self.logger.setLevel(logging.WARNING)
        handler = logging.StreamHandler()
        formatter = logging.Formatter(
            "%(asctime)s - %(levelname)s - %(name)s - %(message)s"
        )
        handler.setFormatter(formatter)
        if not self.logger.handlers:
            self.logger.addHandler(handler)
        self.logger.propagate = False

    def compute_equity_curve(self):
        """
        Computes the daily equity curve including unrealized PnL (Mark-to-Market).
        """
        if not self.trade_log:
            self.equity_curve = pd.Series(
                [self.initial_capital], index=[pd.to_datetime(self.start_date)]
            )
            return

        # 1. Fetch Daily Data for MTM Calculation
        try:
            df_daily = self.data_handler.get_historical_data(
                ticker=self.ticker,
                start=self.start_date,
                end=self.end_date,
                interval="1d",
            )
        except Exception as e:
            self.logger.error(f"Failed to fetch daily data for equity curve: {e}")
            df_daily = pd.DataFrame()

        # Fallback to realized-only if no data
        if df_daily.empty:
            self.logger.warning(
                "No daily data available. Reverting to Realized PnL equity curve."
            )
            timestamps = []
            equities = []
            current_eq = self.initial_capital
            # Start point
            timestamps.append(pd.to_datetime(self.start_date))
            equities.append(current_eq)

            for trade in sorted(self.trade_log, key=lambda x: x["ExitTime"]):
                current_eq += trade["PnL"]
                timestamps.append(trade["ExitTime"])
                equities.append(current_eq)
            self.equity_curve = pd.Series(equities, index=timestamps)
            return

        # 2. Mark-to-Market Calculation
        trades_df = pd.DataFrame(self.trade_log)

        # Normalize timezones
        if isinstance(df_daily.index, pd.DatetimeIndex):
            df_daily = to_naive_index(df_daily)

        if "EntryTime" in trades_df.columns:
            trades_df["EntryTime"] = trades_df["EntryTime"].map(
                lambda v: to_naive_utc(v) if pd.notna(v) else v
            )
        if "ExitTime" in trades_df.columns:
            trades_df["ExitTime"] = trades_df["ExitTime"].map(
                lambda v: to_naive_utc(v) if pd.notna(v) else v
            )

        daily_equities = []
        daily_timestamps = []

        # Iterate daily close prices
        for date, row in df_daily.iterrows():
            date = cast("pd.Timestamp", date)
            close_price = row["Close"]

            # Define End of Day (to verify which trades are active)
            day_end = date + pd.Timedelta(days=1) - pd.Timedelta(seconds=1)

            # A. Realized PnL (Closed before or on this day)
            realized_mask = trades_df["ExitTime"] <= day_end
            realized_pnl = trades_df.loc[realized_mask, "PnL"].sum()

            # B. Unrealized PnL (Open at EOD)
            # Active: Entered <= day_end AND Exits > day_end
            active_mask = (trades_df["EntryTime"] <= day_end) & (
                trades_df["ExitTime"] > day_end
            )
            active_trades = trades_df.loc[active_mask]

            unrealized_pnl = 0.0

            if not active_trades.empty:
                for _, trade in active_trades.iterrows():
                    entry_price = trade["EntryPrice"]
                    qty = trade["Contracts"]
                    t_type = trade.get("Type", 1)

                    # Heuristic: Check Signal Name for 'PUT' or 'SHORT'
                    signal_name = str(trade.get("Signal", "")).upper()

                    is_short = False
                    if (
                        t_type == 2
                        or t_type == -1
                        or "PUT" in signal_name
                        or "SHORT" in signal_name
                    ):
                        is_short = True

                    if is_short:
                        unrealized_pnl += (entry_price - close_price) * qty
                    else:
                        unrealized_pnl += (close_price - entry_price) * qty

            total_equity = self.initial_capital + realized_pnl + unrealized_pnl
            daily_equities.append(total_equity)
            daily_timestamps.append(date)

        self.equity_curve = pd.Series(daily_equities, index=daily_timestamps)

    def calculate_performance_metrics(
        self, benchmark_ticker: str | None = None
    ) -> dict:
        """
        Calculate comprehensive performance metrics (Sharpe, Sortino, Alpha, Beta, etc.)
        using the underlying asset (or specified ticker) as the benchmark.
        Standardizes to DAILY returns for accurate metric calculation.
        """
        # Ensure equity curve is computed
        if self.equity_curve.empty:
            if len(self.trade_log) > 0:
                self.compute_equity_curve()
            else:
                return {}

        # 1. Prepare Equity Curve (Resample to Daily)
        eq_curve = self.equity_curve.copy()
        # Ensure Naive for consistent resampling
        if (
            isinstance(eq_curve.index, pd.DatetimeIndex)
            and eq_curve.index.tz is not None
        ):
            eq_curve.index = eq_curve.index.tz_convert(None)

        # Add Start Point (Initial Capital) if missing
        start_dt = pd.to_datetime(self.start_date)
        if start_dt.tz is not None:
            start_dt = start_dt.tz_convert(None)

        # Sort just in case
        eq_curve = eq_curve.sort_index()

        # Anchor with Initial Capital at start_dt - 1 day to ensure first day return is captured in pct_change
        anchor_dt = start_dt - pd.Timedelta(days=1)
        if eq_curve.empty or eq_curve.index[0] > anchor_dt:
            start_series = pd.Series([self.initial_capital], index=[anchor_dt])
            eq_curve = pd.concat([start_series, eq_curve]).sort_index()

        # Resample to Daily (End of Day) and Forward Fill
        # This gives us a daily time series of equity
        daily_equity = eq_curve.resample("D").last().ffill()

        # Calculate Daily Returns
        strategy_returns = daily_equity.pct_change().fillna(0)

        # 2. Benchmark Returns (Default to self.ticker)
        target_benchmark = benchmark_ticker if benchmark_ticker else self.ticker

        # Fetch Benchmark Data (1d)
        bench_df = self.data_handler.get_historical_data(
            target_benchmark, self.start_date, self.end_date, "1d"
        )

        if not bench_df.empty:
            if (
                isinstance(bench_df.index, pd.DatetimeIndex)
                and bench_df.index.tz is not None
            ):
                bench_df.index = bench_df.index.tz_convert(None)

            # Reindex Benchmark PRICES to match Strategy Daily Index
            bench_prices = bench_df["Close"].reindex(
                strategy_returns.index, method="ffill"
            )
            bench_returns = bench_prices.pct_change().fillna(0)
        else:
            self.logger.warning(
                f"Could not fetch benchmark data for {target_benchmark}. Using Zero return."
            )
            bench_returns = pd.Series(0, index=strategy_returns.index)

        # 3. Positions (Exposure)
        positions = pd.Series(0, index=strategy_returns.index)
        trades_df = pd.DataFrame(self.trade_log)

        if (
            not trades_df.empty
            and "EntryTime" in trades_df.columns
            and "ExitTime" in trades_df.columns
        ):
            entries = trades_df["EntryTime"]
            exits = trades_df["ExitTime"]
            if entries.dt.tz is not None:
                entries = entries.dt.tz_convert(None)
            if exits.dt.tz is not None:
                exits = exits.dt.tz_convert(None)
            # Mark active days
            for i in range(len(trades_df)):
                entry = entries.iloc[i]
                exit = exits.iloc[i]
                mask = (positions.index >= entry) & (positions.index <= exit)
                positions[mask] = 1

        # 4. Calculate Metrics
        metrics = calculate_metrics(
            strategy_returns,
            bench_returns,
            positions,
            trades_df if not trades_df.empty else None,
        )

        # Override Benchmark Return with robust Price-based Calculation
        # This handles cases where reindexing/resampling misses the first period return
        if not bench_df.empty:
            # Robust Index filtering ensuring timezone compatibility
            # We use a temporary index wrapper to avoid in-place mutation ambiguities
            bench_idx = bench_df.index
            if isinstance(bench_idx, pd.DatetimeIndex) and bench_idx.tz is not None:
                bench_idx = bench_idx.tz_convert(None)

            s_date = pd.Timestamp(self.start_date)
            if s_date.tz is not None:
                s_date = s_date.tz_convert(None)

            e_date = pd.Timestamp(self.end_date)
            if e_date.tz is not None:
                e_date = e_date.tz_convert(None)

            # Boolean mask on the aligned index
            mask = (bench_idx >= s_date) & (bench_idx <= e_date)
            valid_bench = bench_df.loc[mask]

            if not valid_bench.empty:
                # Use Open price for the start to capture the full period return
                start_price = (
                    valid_bench["Open"].iloc[0]
                    if "Open" in valid_bench.columns
                    else valid_bench["Close"].iloc[0]
                )
                end_price = valid_bench["Close"].iloc[-1]
                metrics["Benchmark_Return"] = (end_price / start_price) - 1

        # Add DSR (Deflated Sharpe Ratio)
        # Using default strategies_tested=100 from utils
        if len(strategy_returns) > 20:
            metrics["DSR"] = calculate_deflated_sharpe_ratio(strategy_returns)
        else:
            metrics["DSR"] = 0.0

        return metrics

    def update_reason_stats(self, trade: dict):
        """Update performance statistics for each reason"""
        reason = trade.get("Reason", "unknown")
        if not reason or reason == "":
            reason = "no_reason"

        if reason not in self.reason_stats:
            self.reason_stats[reason] = {
                "count": 0,
                "profitable": 0,
                "total_pnl": 0.0,
                "total_duration": 0.0,
                "pnls": [],
                "durations": [],
            }

        stats = self.reason_stats[reason]
        stats["count"] += 1
        if trade["PnL"] > 0:
            stats["profitable"] += 1
        stats["total_pnl"] += trade["PnL"]
        stats["total_duration"] += trade["DurationMinutes"]
        stats["pnls"].append(trade["PnL"])
        stats["durations"].append(trade["DurationMinutes"])

    def log_reason_statistics(self):
        """Log detailed statistics grouped by reason"""
        if not self.reason_stats:
            self.logger.info("No reason statistics available.")
            return

        total_trades = sum(s["count"] for s in self.reason_stats.values())

        self.logger.info("TRADE PERFORMANCE BY REASON")

        # Sort by count (most common reasons first)
        sorted_reasons = sorted(
            self.reason_stats.items(), key=lambda x: x[1]["count"], reverse=True
        )

        for reason, stats in sorted_reasons:
            count = stats["count"]
            profitable = stats["profitable"]
            win_rate = (profitable / count * 100) if count > 0 else 0
            pct_of_total = (count / total_trades * 100) if total_trades > 0 else 0
            avg_pnl = stats["total_pnl"] / count if count > 0 else 0
            avg_duration = stats["total_duration"] / count if count > 0 else 0

            # Calculate additional metrics
            pnls = stats["pnls"]
            median_pnl = float(np.median(pnls)) if pnls else 0
            max_pnl = max(pnls) if pnls else 0
            min_pnl = min(pnls) if pnls else 0

            durations = stats["durations"]
            median_duration = float(np.median(durations)) if durations else 0

            self.logger.info(f"  Reason: '{reason}'")
            self.logger.info(f"  Trades: {count} ({pct_of_total:.1f}% of total)")
            self.logger.info(f"  Win Rate: {win_rate:.1f}% ({profitable}/{count})")
            self.logger.info(f"  Total PnL: ${stats['total_pnl']:.2f}")
            self.logger.info(f"  Avg PnL: ${avg_pnl:.2f} | Median: ${median_pnl:.2f}")
            self.logger.info(f"  Best: ${max_pnl:.2f} | Worst: ${min_pnl:.2f}")
            self.logger.info(
                f"  Avg Duration: {avg_duration:.1f} min | Median: {median_duration:.1f} min"
            )

        self.logger.info("REASON SUMMARY")
        self.logger.info(f"Total Trades: {total_trades}")
        self.logger.info(f"Unique Reasons: {len(self.reason_stats)}")

        # Overall statistics
        total_profitable = sum(s["profitable"] for s in self.reason_stats.values())
        overall_win_rate = (
            (total_profitable / total_trades * 100) if total_trades > 0 else 0
        )
        total_pnl = sum(s["total_pnl"] for s in self.reason_stats.values())

        self.logger.info(f"Overall Win Rate: {overall_win_rate:.1f}%")
        self.logger.info(f"Overall PnL: ${total_pnl:.2f}")
        self.logger.info("=" * 80)

    def run_backtest(self) -> pd.DataFrame:
        raise NotImplementedError("Subclasses must implement run_backtest")
