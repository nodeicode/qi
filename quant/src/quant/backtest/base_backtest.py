import logging
from typing import cast

import numpy as np
import pandas as pd

from quant.backtest.utils import (
    calculate_deflated_sharpe_ratio,
    calculate_metrics,
)
from quant.data._times import parse_naive_utc, to_naive_index, to_naive_utc
from quant.data.data_handler import AlpacaDataHandler
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
        # callers; futures runs inject a futures-capable handler (e.g.
        # ``FuturesDataHandler``) — both implement ``DataHandlerProtocol``.
        self.data_handler = (
            data_handler if data_handler is not None else AlpacaDataHandler()
        )
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

    # ------------------------------------------------------------------
    # Shared helpers for option / spread subclasses
    # ------------------------------------------------------------------

    def _compute_realized_equity_curve(self):
        """Realized-PnL-only equity curve (no MTM unrealized).

        Used by option/spread backtests where EntryPrice is option premium,
        not underlying price, making MTM unrealized PnL meaningless.
        """
        if not self.trade_log:
            self.equity_curve = pd.Series(
                [self.initial_capital], index=[pd.to_datetime(self.start_date)]
            )
            return

        trades_df = pd.DataFrame(self.trade_log)

        if trades_df["ExitTime"].dt.tz is not None:
            trades_df["ExitTime"] = trades_df["ExitTime"].dt.tz_convert(None)
        if trades_df["EntryTime"].dt.tz is not None:
            trades_df["EntryTime"] = trades_df["EntryTime"].dt.tz_convert(None)

        trades_df = trades_df.sort_values("ExitTime")
        trades_df["ExitDate"] = trades_df["ExitTime"].dt.normalize()
        daily_pnl = trades_df.groupby("ExitDate")["PnL"].sum()

        start_dt = pd.to_datetime(self.start_date)
        if start_dt.tz is not None:
            start_dt = start_dt.tz_convert(None)
        end_dt = pd.to_datetime(self.end_date)
        if end_dt.tz is not None:
            end_dt = end_dt.tz_convert(None)

        full_range = pd.date_range(start=start_dt, end=end_dt, freq="D")
        daily_pnl = daily_pnl.reindex(full_range, fill_value=0.0)
        equity = self.initial_capital + daily_pnl.cumsum()
        self.equity_curve = equity
        self._daily_pnl = daily_pnl

    def _calculate_additive_performance_metrics(
        self,
        benchmark_ticker: str | None = None,
    ) -> dict:
        """Performance metrics for option/spread/IC portfolios.

        Equity is realized-PnL only (no option MTM). When equity stays
        comfortably above zero we use **daily equity pct_change** (same
        economics as the stock path) so Sharpe / Jensen / annualization
        are consistent. Additive ``pnl / initial_capital`` is retained only
        as a fallback when equity can approach zero (pct_change blows up).

        Prior bug: annualizing arithmetic mean of ``pnl/initial_capital``
        (``mean * 252``) while ``Annual_Return`` used geometric compounding
        produced inflated Jensen Alpha (e.g. ~60% vs ~28% CAGR).
        """
        if self.equity_curve.empty:
            if len(self.trade_log) > 0:
                self.compute_equity_curve()
            else:
                return {}

        if self.equity_curve.empty:
            return {}

        eq = self.equity_curve.copy().sort_index()
        if isinstance(eq.index, pd.DatetimeIndex) and eq.index.tz is not None:
            eq.index = eq.index.tz_convert(None)

        # Business-day series (drop weekend zeros that dilute means)
        daily_eq = eq.resample("B").last().ffill()
        min_eq = float(daily_eq.min()) if len(daily_eq) else 0.0
        use_pct = min_eq > 0.1 * float(self.initial_capital)

        if use_pct:
            strategy_returns = daily_eq.pct_change().dropna()
        else:
            daily_pnl = getattr(self, "_daily_pnl", None)
            if daily_pnl is None or daily_pnl.empty:
                strategy_returns = daily_eq.diff().fillna(0.0) / self.initial_capital
            else:
                pnl_b = (
                    daily_pnl.resample("B")
                    .sum()
                    .reindex(daily_eq.index, fill_value=0.0)
                )
                strategy_returns = (pnl_b / self.initial_capital).iloc[1:]

        # Benchmark on the same business-day index
        target_benchmark = benchmark_ticker if benchmark_ticker else self.ticker
        bench_df = self.data_handler.get_historical_data(
            target_benchmark, self.start_date, self.end_date, "1d"
        )
        if not bench_df.empty:
            if (
                isinstance(bench_df.index, pd.DatetimeIndex)
                and bench_df.index.tz is not None
            ):
                bench_df.index = bench_df.index.tz_convert(None)
            bench_prices = bench_df["Close"].reindex(
                strategy_returns.index, method="ffill"
            )
            bench_returns = bench_prices.pct_change().fillna(0.0)
        else:
            bench_returns = pd.Series(0.0, index=strategy_returns.index)

        # Exposure
        positions = pd.Series(0, index=strategy_returns.index)
        trades_df = pd.DataFrame(self.trade_log)
        if not trades_df.empty:
            entries = trades_df["EntryTime"]
            exits = trades_df["ExitTime"]
            if entries.dt.tz is not None:
                entries = entries.dt.tz_convert(None)
            if exits.dt.tz is not None:
                exits = exits.dt.tz_convert(None)
            for i in range(len(trades_df)):
                mask = (positions.index >= entries.iloc[i]) & (
                    positions.index <= exits.iloc[i]
                )
                positions[mask] = 1

        metrics: dict = {}
        n = len(strategy_returns)
        final_eq = float(self.equity_curve.iloc[-1])
        metrics["Total_Return"] = (final_eq / self.initial_capital) - 1.0

        if n > 0:
            metrics["Annual_Return"] = (1.0 + metrics["Total_Return"]) ** (
                252 / n
            ) - 1.0
        else:
            metrics["Annual_Return"] = 0.0

        std = float(strategy_returns.std()) if n else 0.0
        metrics["Sharpe_Ratio"] = (
            (float(strategy_returns.mean()) / std) * np.sqrt(252) if std > 0 else 0.0
        )

        downside = strategy_returns[strategy_returns < 0]
        ds_std = float(downside.std()) if len(downside) else 0.0
        metrics["Sortino_Ratio"] = (
            (float(strategy_returns.mean()) / ds_std) * np.sqrt(252)
            if ds_std > 0
            else 0.0
        )

        peak = daily_eq.cummax()
        dd = (daily_eq - peak) / peak.replace(0, np.nan)
        metrics["Max_Drawdown"] = float(dd.min()) if len(dd) else 0.0
        metrics["Calmar_Ratio"] = (
            float(metrics["Annual_Return"] / abs(metrics["Max_Drawdown"]))
            if metrics["Max_Drawdown"] not in (0.0, None)
            and abs(metrics["Max_Drawdown"]) > 1e-12
            else 0.0
        )

        active_days = int(positions[positions != 0].count())
        metrics["Exposure"] = active_days / n if n > 0 else 0.0

        if not trades_df.empty and "PnL" in trades_df.columns:
            total = len(trades_df)
            winners = int((trades_df["PnL"] > 0).sum())
            metrics["Win_Rate"] = winners / total if total else 0.0
        else:
            metrics["Win_Rate"] = 0.0

        # CAPM Jensen alpha (annualized) — same return definition as Sharpe
        aligned = pd.DataFrame(
            {"strat": strategy_returns, "bench": bench_returns}
        ).dropna()
        if len(aligned) > 30:
            var_bench = float(aligned["bench"].var())
            cov = float(aligned["strat"].cov(aligned["bench"]))
            beta = cov / var_bench if var_bench > 0 else 0.0
            metrics["Beta"] = beta
            rf_daily = 0.04 / 252.0
            # Simple alpha (no Rf)
            metrics["Alpha"] = (
                float(aligned["strat"].mean()) - beta * float(aligned["bench"].mean())
            ) * 252.0
            # Jensen: R_p - [R_f + β(R_m - R_f)]
            expected = rf_daily + beta * (float(aligned["bench"].mean()) - rf_daily)
            metrics["Jensen_Alpha"] = (
                float(aligned["strat"].mean()) - expected
            ) * 252.0
        else:
            metrics["Beta"] = 0.0
            metrics["Alpha"] = 0.0
            metrics["Jensen_Alpha"] = 0.0

        # Benchmark total return (price path)
        if not bench_df.empty:
            s_dt = pd.Timestamp(self.start_date)
            e_dt = pd.Timestamp(self.end_date)
            if s_dt.tz is not None:
                s_dt = s_dt.tz_convert(None)
            if e_dt.tz is not None:
                e_dt = e_dt.tz_convert(None)
            bidx = bench_df.index
            if isinstance(bidx, pd.DatetimeIndex) and bidx.tz is not None:
                bidx = bidx.tz_convert(None)
            valid = bench_df.loc[(bidx >= s_dt) & (bidx <= e_dt)]
            if not valid.empty:
                sp = (
                    valid["Open"].iloc[0]
                    if "Open" in valid.columns
                    else valid["Close"].iloc[0]
                )
                ep = valid["Close"].iloc[-1]
                metrics["Benchmark_Return"] = float(ep / sp) - 1.0
            else:
                metrics["Benchmark_Return"] = float((1.0 + bench_returns).prod() - 1.0)
        else:
            metrics["Benchmark_Return"] = 0.0

        # Monte Carlo DD on strategy return series
        if n > 30:
            arr = strategy_returns.to_numpy(dtype=float)
            rng = np.random.default_rng()
            mc_dds = []
            for _ in range(1000):
                sim = rng.choice(arr, size=n, replace=True)
                if use_pct:
                    sim_eq = np.cumprod(1.0 + sim)
                    sim_peak = np.maximum.accumulate(sim_eq)
                    sim_dd = (sim_eq - sim_peak) / sim_peak
                else:
                    sim_eq = (
                        self.initial_capital + np.cumsum(sim) * self.initial_capital
                    )
                    sim_peak = np.maximum.accumulate(sim_eq)
                    sim_dd = (sim_eq - sim_peak) / sim_peak
                mc_dds.append(float(sim_dd.min()))
            metrics["MC_DD_95"] = float(np.percentile(mc_dds, 5))
        else:
            metrics["MC_DD_95"] = metrics["Max_Drawdown"]

        if n > 20:
            metrics["DSR"] = calculate_deflated_sharpe_ratio(strategy_returns)
        else:
            metrics["DSR"] = 0.0

        # Stash for report charts (optional consumers)
        self._daily_equity_bd = daily_eq
        self._strategy_returns_bd = strategy_returns
        self._bench_returns_bd = bench_returns
        self._drawdown_bd = dd

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

    def prepare_signals(self) -> pd.DataFrame:
        self.logger.info("Starting backtest...")

        # 1. Fetch underlying data with pre-loading period
        actual_start_date = pd.to_datetime(self.start_date)
        preload_start_date = (
            actual_start_date - pd.Timedelta(days=self.config.PRE_LOAD_DAYS)
        ).isoformat()

        df_underlying = self.data_handler.get_historical_data(
            ticker=self.ticker,
            start=preload_start_date,
            end=self.end_date,
            interval=self.config.INTERVAL,
        )
        if df_underlying.empty:
            self.logger.error("No underlying data retrieved. Aborting backtest.")
            return pd.DataFrame()

        self.logger.info(
            f"Fetched {len(df_underlying)} bars including {self.config.PRE_LOAD_DAYS} days of pre-load data"
            f" (adjustment=all)"
        )

        # Pass ticker= when the strategy supports it (IC / multi-asset).
        import inspect as _inspect

        _params = _inspect.signature(self.strategy.calculate_indicators).parameters
        if "ticker" in _params:
            preload_df_with_indicators = self.strategy.calculate_indicators(
                df_underlying, ticker=self.ticker
            )
        else:
            preload_df_with_indicators = self.strategy.calculate_indicators(
                df_underlying
            )

        backtest_mask = preload_df_with_indicators.index >= pd.to_datetime(
            self.start_date
        ).tz_localize(preload_df_with_indicators.index.tzinfo)
        df_signals = self.strategy.generate_signals(preload_df_with_indicators)
        df_signals = df_signals[backtest_mask].copy()

        # Always attach Raw_Close for historical strike / order placement (tradable
        # space) — backtest-only: the live path never carries Raw_Close (adjusted
        # decision-bar Close == Raw_Close by retroactive adjustment).
        from quant.core._utils import attach_raw_close

        df_signals = attach_raw_close(
            df_signals,
            self.data_handler,
            self.ticker,
            preload_start_date,
            self.end_date,
            self.config.INTERVAL,
            log=self.logger,
        )

        # Edge case: adjusted Close equals Raw_Close on the latest bar because
        # adjustments are retroactive — the newest bar always sits after the most
        # recent corporate action, so its adjustment factor is 1.0. If they ever
        # diverge here, a corporate action landed mid-session and that assumption
        # is violated for this bar. Log-only watch (fills still use Raw_Close).
        if "Raw_Close" in df_signals.columns and len(df_signals):
            import numpy as _np

            _tail = df_signals.iloc[-1]
            _close = _tail.get("Close")
            _raw = _tail.get("Raw_Close")
            if (
                _close is not None
                and _raw is not None
                and not _np.isclose(float(_raw), float(_close))
            ):
                self.logger.warning(
                    "[edge_case] Raw_Close=%.6f != Close=%.6f on latest bar %s — "
                    "corporate action may have landed mid-session",
                    float(_raw),
                    float(_close),
                    df_signals.index[-1],
                )

        self.all_signals = df_signals.copy()
        df_signals = df_signals.sort_index()

        return df_signals

    def run_backtest(self) -> pd.DataFrame:
        raise NotImplementedError("Subclasses must implement run_backtest")
