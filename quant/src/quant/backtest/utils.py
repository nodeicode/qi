from __future__ import annotations

import logging

import numpy as np
import pandas as pd
import scipy.stats as ss

logger = logging.getLogger(__name__)


def calculate_deflated_sharpe_ratio(returns, strategies_tested=100):
    """
    Calculates the Deflated Sharpe Ratio (DSR).

    Parameters:
    returns (pd.Series): Daily returns of the strategy.
    strategies_tested (int): Number of strategies tested (multiple testing correction).

    Returns:
    float: The Deflated Sharpe Ratio probability (0-1).
    """
    try:
        if len(returns) < 20:
            return 0.0

        # Annualized Sharpe Ratio
        sr = (returns.mean() / returns.std()) * np.sqrt(252)

        # Skewness and Kurtosis
        skew = ss.skew(returns)
        kurt = ss.kurtosis(returns)  # Fisher kurtosis (normal = 0)

        # Number of observations
        T = len(returns)

        # Variance of the Sharpe Ratio
        # Bailey, David H., and Marcos Lopez de Prado. "The Sharpe ratio efficient frontier."
        var_sr = (1 - skew * sr + (kurt - 1) / 4 * sr**2) / (T - 1)

        if var_sr <= 0:
            return 0.0

        sigma_sr = np.sqrt(var_sr)

        psr = ss.norm.cdf(sr / sigma_sr)

        return psr

    except Exception:
        return 0.0


# TODO: make this extensiable and modular to used anywhere
def calculate_metrics(strategy_returns, benchmark_returns, positions, trades_df=None):
    """
    Calculates standard performance metrics for a strategy.

    Parameters:
    strategy_returns (pd.Series): Daily returns of the strategy.
    benchmark_returns (pd.Series): Daily returns of the benchmark.
    positions (pd.Series): Daily position/signal (-1, 0, 1).
    trades_df (pd.DataFrame, optional): DataFrame of executed trades for win rate calc.

    Returns:
    dict: Dictionary of metrics.
    """
    metrics = {}

    # 1. Total & Annualized Return
    metrics["Total_Return"] = (1 + strategy_returns).prod() - 1
    metrics["Benchmark_Return"] = (1 + benchmark_returns).prod() - 1

    days = len(strategy_returns)
    if days > 0:
        metrics["Annual_Return"] = (1 + metrics["Total_Return"]) ** (252 / days) - 1
    else:
        metrics["Annual_Return"] = 0.0

    # 2. Risk Metrics (Sharpe, Sortino, Drawdown)
    std = strategy_returns.std()
    if std > 0:
        metrics["Sharpe_Ratio"] = (strategy_returns.mean() / std) * np.sqrt(252)
    else:
        metrics["Sharpe_Ratio"] = 0.0

    downside_returns = strategy_returns[strategy_returns < 0]
    downside_std = downside_returns.std()
    if downside_std > 0:
        metrics["Sortino_Ratio"] = (strategy_returns.mean() / downside_std) * np.sqrt(
            252
        )
    else:
        metrics["Sortino_Ratio"] = 0.0

    cum_ret = (1 + strategy_returns).cumprod()
    peak = cum_ret.cummax()
    drawdown = (cum_ret - peak) / peak
    metrics["Max_Drawdown"] = drawdown.min()

    # 3. Activity Metrics
    active_days = positions[positions != 0].count()
    metrics["Exposure"] = active_days / days if days > 0 else 0

    # Calculate Win Rate (Prefer Trade-based if available)
    if trades_df is not None and not trades_df.empty and "PnL" in trades_df.columns:
        total_trades = len(trades_df)
        winning_trades = len(trades_df[trades_df["PnL"] > 0])
        metrics["Win_Rate"] = winning_trades / total_trades if total_trades > 0 else 0.0
    else:
        # Fallback to Daily Win Rate
        active_returns = strategy_returns[positions != 0]
        if len(active_returns) > 0:
            win_days = active_returns[active_returns > 0].count()
            metrics["Win_Rate"] = win_days / len(active_returns)
        else:
            metrics["Win_Rate"] = 0.0

    # 4. Monte Carlo Drawdown at 95% Confidence (MC_DD_95)
    # Bootstrap resampling of returns to estimate risk tail
    n_sims = 1000
    mc_dds = []

    # Only simulate if we have enough data
    if len(strategy_returns) > 30:
        returns_array = strategy_returns.to_numpy()
        rng = np.random.default_rng()
        for _ in range(n_sims):
            # Resample with replacement
            sim_rets = rng.choice(returns_array, size=days, replace=True)
            sim_cum = np.cumprod(1 + sim_rets)
            sim_peak = np.maximum.accumulate(sim_cum)
            sim_dd = (sim_cum - sim_peak) / sim_peak
            mc_dds.append(sim_dd.min())

        metrics["MC_DD_95"] = np.percentile(
            mc_dds, 5
        )  # 5th percentile of Drawdowns (negative number)
    else:
        metrics["MC_DD_95"] = metrics["Max_Drawdown"]

    # 5. Alpha / Beta (vs Benchmark)
    # Using simple covariance method
    # Align and clean data to avoid artifacts (e.g. fillna(0) at index 0)
    if len(benchmark_returns) > 0 and len(strategy_returns) > 0:
        df_metrics = pd.concat([strategy_returns, benchmark_returns], axis=1).dropna()
        df_metrics.columns = ["strat", "bench"]
    else:
        df_metrics = pd.DataFrame()

    if len(df_metrics) > 20:
        cov: float = df_metrics["strat"].cov(df_metrics["bench"])
        var: float = df_metrics["bench"].var()
        metrics["Beta"] = cov / var if var > 0 else 0

        # Simple Alpha (Excess return over Beta-adjusted Benchmark)
        daily_alpha = (
            df_metrics["strat"].mean() - metrics["Beta"] * df_metrics["bench"].mean()
        )
        metrics["Alpha"] = daily_alpha * 252

        # Jensen's Alpha (CAPM with Risk-Free Rate)
        # R_p = alpha + R_f + beta * (R_m - R_f)
        # alpha = R_p - [R_f + beta * (R_m - R_f)]
        rf_rate = 0.04  # Assumed 4% annual
        rf_daily = rf_rate / 252

        expected_daily_return = rf_daily + metrics["Beta"] * (
            df_metrics["bench"].mean() - rf_daily
        )
        metrics["Jensen_Alpha"] = (
            df_metrics["strat"].mean() - expected_daily_return
        ) * 252

    else:
        metrics["Beta"] = 0.0
        metrics["Alpha"] = 0.0
        metrics["Jensen_Alpha"] = 0.0

    return metrics


def display_results_table(results_dict, title="Performance Metrics"):
    """
    Displays a formatted performance table from a dictionary of results.
    """
    if not results_dict:
        logger.info("No results to display for %s", title)
        return

    # Convert dict of dicts to DataFrame
    df = pd.DataFrame(results_dict).T

    # Sort columns
    columns_order = [
        "Total_Return",
        "Benchmark_Return",
        "Alpha",
        "Jensen_Alpha",
        "Annual_Return",
        "Max_Drawdown",
        "MC_DD_95",
        "Sharpe_Ratio",
        "Sortino_Ratio",
        "DSR",
        "Beta",
        "Win_Rate",
        "Exposure",
    ]
    columns_to_show = [c for c in columns_order if c in df.columns]
    df = df[columns_to_show]

    # Define formats (for Styler)
    formats = {
        "Total_Return": "{:.1%}",
        "Benchmark_Return": "{:.1%}",
        "Annual_Return": "{:.1%}",
        "Max_Drawdown": "{:.1%}",
        "MC_DD_95": "{:.1%}",
        "Alpha": "{:.2%}",
        "Jensen_Alpha": "{:.2%}",
        "Beta": "{:.2f}",
        "Sharpe_Ratio": "{:.2f}",
        "Sortino_Ratio": "{:.2f}",
        "DSR": "{:.1%}",
        "Win_Rate": "{:.1%}",
        "Exposure": "{:.1%}",
    }

    logger.info("=== %s ===", title)

    # Text-based Output for Logs/Agent
    display_df = df.copy()
    for col, fmt in formats.items():
        if col in display_df.columns:
            display_df[col] = display_df[col].apply(
                lambda x, _fmt=fmt: _fmt.format(x) if pd.notna(x) else "N/A"
            )
    logger.info("\n%s", display_df)

    try:
        from IPython.display import display

        # Style logic
        def highlight_beat_bench(row):
            styles = ["" for _ in row.index]
            if (
                "Total_Return" in row.index
                and "Benchmark_Return" in row.index
                and row["Total_Return"] > row["Benchmark_Return"]
            ):
                # Highlight Total Return Green
                idx = row.index.get_loc("Total_Return")
                styles[idx] = (
                    "background-color: #d4edda; color: #155724; font-weight: bold"
                )
            return styles

        def highlight_pos_alpha(val):
            return (
                "background-color: #d4edda; color: #155724; font-weight: bold"
                if val > 0
                else ""
            )

        # Apply Styles
        styler = df.style.format({k: v for k, v in formats.items() if k in df.columns})

        if "Total_Return" in df.columns and "Benchmark_Return" in df.columns:
            styler = styler.apply(highlight_beat_bench, axis=1)

        if "Alpha" in df.columns:
            styler = styler.map(highlight_pos_alpha, subset=["Alpha"])

        display(styler)

    except (ImportError, Exception):  # noqa: S110
        pass  # Text output already emitted via logger above


def apply_breach_exit_cbbo(
    breach_cbbo: dict,
    leg_meta: dict,
    S_exit: float,
    exit_time,
    leg_mids: dict,
    methods: dict,
    exit_bar_ts: dict,
    *,
    is_tp_exit: bool = False,
    tv_per_short: float = 0.0,
) -> tuple[dict[str, float], dict[str, float], list[str]]:
    """Price IC breach exit legs with per-leg CBBO (no all-or-nothing clear).

    Keeps valid CBBO mids/bid/ask on available legs; only missing legs fall
    back to intrinsic (or TP time-value floor on short legs). Ask-debit is
    enabled by the caller only when the returned bid/ask maps cover all four
    legs (``missed`` empty).

    Returns ``(exit_bids, exit_asks, missed_leg_keys)``.
    """
    exit_bids: dict[str, float] = {}
    exit_asks: dict[str, float] = {}
    missed: list[str] = []

    for ln in ("lp", "sp", "sc", "lc"):
        cb = breach_cbbo.get(ln)
        if cb is not None and float(cb.get("mid", -1)) >= 0:
            leg_mids[ln] = float(cb["mid"])
            exit_bids[ln] = float(cb["bid"])
            exit_asks[ln] = float(cb["ask"])
            methods[ln] = "breach_cbbo"
            exit_bar_ts[ln] = f"cbbo@{exit_time}"
            continue

        missed.append(ln)
        ot, k = leg_meta[ln]
        if ot == "put":
            iv = max(k - S_exit, 0.0)
        else:
            iv = max(S_exit - k, 0.0)
        if is_tp_exit and ln in ("sp", "sc"):
            iv = max(iv, tv_per_short)
        methods[ln] = "tp_tv_fallback" if is_tp_exit else "breach_intrinsic_fallback"
        leg_mids[ln] = iv
        exit_bar_ts[ln] = f"S_breach={S_exit:.2f}"

    return exit_bids, exit_asks, missed
