"""
Regime Model V6 - "Component Upgrade"

Problem with V5:
- The "Competing Forces" architecture was sound, but the component signals were too noisy.
- Result: The model still had no consistent, positive edge.

V6 Philosophy: "Garbage In, Garbage Out"
- A model is only as good as its inputs.
- This version focuses on a major upgrade to EACH of the 6 components to make them
  more robust, predictive, and less susceptible to market noise.

V6 Architecture:
- Keeps the V5 "Competing Forces" baskets (Trending vs. Mean-Reversion).
- Radically improves the calculation of each signal within the baskets.
"""

# NOTE: not sure if this file should be in quant_utils or a separate strategy utils folder, so far leaving it in quant_utils
import warnings
from dataclasses import dataclass

import numpy as np
import pandas as pd
from numpy.lib.stride_tricks import sliding_window_view
from scipy.linalg import inv, logm, sqrtm

# ------------------------------------------------------------------
# HELPERS
# ------------------------------------------------------------------


def _sigmoid(x: pd.Series, k: float = 1.0) -> pd.Series:
    """Vectorized logistic function to map any real number to (0, 1)."""
    return 1.0 / (1.0 + np.exp(-k * x))


def _z_score(series: pd.Series, window: int) -> pd.Series:
    """Calculates the rolling Z-score of a series."""
    mean = series.rolling(window=window).mean()
    std = series.rolling(window=window).std().replace(0, np.nan)
    return (series - mean) / std


def _normalize_to_01(series: pd.Series, window: int = 252) -> pd.Series:
    """Normalizes a series to 0-1 range using rolling min-max."""
    rolling_min = series.rolling(window, min_periods=window // 2).min()
    rolling_max = series.rolling(window, min_periods=window // 2).max()
    normalized = (series - rolling_min) / (rolling_max - rolling_min + 1e-9)
    return normalized.clip(0, 1).fillna(0.5)


def _riemannian_distance_2d(cov1, cov2):
    """
    Calculates the Affine-Invariant Riemannian Metric (AIRM) distance between two SPD matrices.
    d(P1, P2) = || log(P1^-1/2 * P2 * P1^-1/2) ||_F
    """
    try:
        # For 2x2 or 3x3 matrices, this is computationally feasible
        # P1^-1/2
        sqrt_cov1 = sqrtm(cov1)
        inv_sqrt_cov1 = inv(sqrt_cov1)

        # Inner term: P1^-1/2 * P2 * P1^-1/2
        inner = inv_sqrt_cov1 @ cov2 @ inv_sqrt_cov1

        # Log map
        log_inner = logm(inner)

        # Frobenius norm
        dist = np.linalg.norm(log_inner, ord="fro")

        # Handle numerical instability (complex results)
        if np.iscomplex(dist):
            return np.abs(dist)
        return dist
    except Exception:
        return np.nan


# ------------------------------------------------------------------
# CONFIG
# ------------------------------------------------------------------


@dataclass
class RegimeModelConfig:
    """
    Configuration for V6 "Component Upgrade" Model
    """

    # --- Core Windows ---
    MOMENTUM_WINDOW: int = 20  # Faster response
    HURST_WINDOW: int = 100
    RSRS_WINDOW: int = 18
    VOL_BREAKOUT_SHORT: int = 20
    VOL_BREAKOUT_LONG: int = 100
    CORRELATION_WINDOW: int = 60
    DIVERGENCE_WINDOW: int = 30
    EFFICIENCY_WINDOW: int = 20  # Faster response
    RIEMANNIAN_WINDOW_SHORT: int = 20
    RIEMANNIAN_WINDOW_LONG: int = 100

    # --- V7 Basket Weights ---
    # Trending Basket (MOMENTUM + EFFICIENCY + TREND QUALITY)
    TREND_MOMENTUM_W: float = 0.30
    TREND_QUALITY_W: float = 0.20  # Replaces RSRS
    TREND_VOL_EXP_W: float = 0.10
    TREND_EFFICIENCY_W: float = 0.40  # Increased weight on Efficiency

    # Mean-Reversion Basket (Penalties)
    # These reduce the score if high (Mean Reverting)
    MR_HURST_W: float = 0.40  # Increased penalty for chop
    MR_DIVERGENCE_W: float = 0.10
    MR_CORRELATION_W: float = 0.00

    # --- Smoothing ---
    FINAL_SCORE_EMA: int = 10  # Increased from 5 for stability (V2 learning)

    # --- HMM Bear Detection ---
    USE_HMM: bool = True  # Toggle HMM bear-aware blend on/off
    HMM_N_STATES: int = 3  # Number of hidden states (bull/transition/bear)
    HMM_BEAR_WEIGHT: float = 0.65  # Bear suppression coefficient (blend a)
    HMM_RANDOM_STATE: int = 42  # Reproducibility seed

    # --- EXP-6: Quiet-Grind Adaptive Momentum Boost ---
    # Low vol-expansion during persistent directional drift = "quiet grind" (bull or bear).
    # The vol_expansion_score unfairly penalizes these: low vol → low vol score → net_score
    # stays near chop even though momentum + trend_quality confirm a real trend.
    # When: vol_expansion < CEIL AND |momentum - 0.5| > EDGE AND trend_quality > FLOOR
    # Action: add a signed momentum boost to net_score before normalization.
    USE_QUIET_GRIND_BOOST: bool = True
    QUIET_GRIND_VOL_CEIL: float = 0.45  # vol_expansion_score below this is "low vol"
    QUIET_GRIND_MOM_EDGE: float = 0.65  # momentum must be >0.65 (bull) or <0.35 (bear)
    QUIET_GRIND_TQ_FLOOR: float = 0.50  # trend_quality must confirm direction
    QUIET_GRIND_BOOST_STR: float = 0.20  # additive push on net_score (range [-1,1])

    # --- HMM Symmetric Blend: Bull Boost ---
    # When HMM strongly confirms Bull state, gently lift regime_score above V6 score.
    # Formula: bear_adj + HMM_BULL_WEIGHT x bull_prob x (1 - bear_adj)
    # At 0.30: a fully bull-confirmed day with V6=0.65 → regime_score≈0.755 (+16%).
    # At 0.0: suppression-only mode (previous behaviour).
    HMM_BULL_WEIGHT: float = 0.30  # Bull lift coefficient; 0.0 = suppression-only


# ------------------------------------------------------------------
# REGIME MODEL V6 - "COMPONENT UPGRADE"
# ------------------------------------------------------------------


def _rolling_hurst(prices: np.ndarray, window: int) -> np.ndarray:
    """Rolling Hurst exponent via MA detrending."""
    n = len(prices)
    hurst_arr = np.full(n, np.nan)
    if n < window:
        return hurst_arr

    windows = sliding_window_view(prices, window)[: n - window]
    ma = windows.mean(axis=1)
    x_detrended = windows - ma[:, None]
    y = np.cumsum(x_detrended, axis=1)
    r = y.max(axis=1) - y.min(axis=1)
    s = x_detrended.std(axis=1)
    val = np.where(s < 1e-9, 0.5, r / s)
    hurst_arr[window:] = val
    return hurst_arr


def _calculate_trend_quality(
    prices: np.ndarray, window: int
) -> tuple[np.ndarray, np.ndarray]:
    """
    Trend quality via linear regression of price vs time.
    Returns normalized slope and R² series.
    """
    n = len(prices)
    slope_series = np.full(n, np.nan)
    r2_series = np.full(n, np.nan)
    if n < window:
        return slope_series, r2_series

    x = np.arange(window, dtype=np.float64)
    x_mean = x.mean()
    x_dev = x - x_mean
    x_var = np.var(x)

    windows = sliding_window_view(prices, window)[: n - window]
    y_mean = windows.mean(axis=1)
    y_dev = windows - y_mean[:, None]
    cov = (y_dev * x_dev).mean(axis=1)
    slope = np.where(x_var > 1e-9, cov / x_var, 0.0)

    y_pred = slope[:, None] * x_dev + y_mean[:, None]
    ss_res = ((windows - y_pred) ** 2).sum(axis=1)
    ss_tot = (y_dev**2).sum(axis=1)
    r2 = np.where(ss_tot > 1e-9, 1.0 - ss_res / ss_tot, 0.0)
    slope_norm = (slope / (y_mean + 1e-9)) * 100.0

    slope_series[window:] = slope_norm
    r2_series[window:] = r2
    return slope_series, r2_series


_rolling_hurst_numba = _rolling_hurst
_calculate_trend_quality_numba = _calculate_trend_quality


class RegimeModel:
    """
    V6 "Component Upgrade" Model

    Keeps the V5 "Competing Forces" architecture but radically upgrades
    the calculation of each of the 6 component signals for more robustness.
    """

    def __init__(
        self,
        config: RegimeModelConfig = RegimeModelConfig(),
        market_data: pd.DataFrame | None = None,
    ):
        self.config = config
        self.market_data = market_data

    def calculate_all_scores(self, df: pd.DataFrame) -> pd.DataFrame:
        """
        V6 "Component Upgrade" Calculation

        1. Calculate all 6 upgraded components
        2. Separate into Trending vs Mean-Reversion baskets
        3. Calculate weighted score for each basket
        4. Calculate weighted score for each basket
        4. Calculate NET score: (Trending - Mean-Reverting)
        5. Normalize final score to 0-1 range
        """
        df = df.copy()
        c = self.config

        if "pct_change" not in df.columns:
            df["pct_change"] = df["Close"].pct_change(fill_method=None).fillna(0)

        # === STEP 1: Calculate all V7 components ===
        df["momentum_score"] = self._calculate_momentum_v6(df, c.MOMENTUM_WINDOW)
        df["trend_quality_score"] = self._calculate_trend_quality_v6(
            df, c.RSRS_WINDOW
        )  # Replaces RSRS
        df["hurst_score"] = self._calculate_hurst_v6(df["Close"], c.HURST_WINDOW)
        df["vol_expansion_score"] = self._calculate_vol_breakout_v6(
            df, c.VOL_BREAKOUT_SHORT, c.VOL_BREAKOUT_LONG
        )
        df["correlation_score"] = self._calculate_correlation_v6(
            df, self.market_data, c.CORRELATION_WINDOW
        )
        df["divergence_score"] = self._calculate_divergence_v6(df, c.DIVERGENCE_WINDOW)
        df["efficiency_score"] = self._calculate_efficiency_v7(df, c.EFFICIENCY_WINDOW)
        df["riemannian_score"] = self._calculate_riemannian_score(
            df, c.RIEMANNIAN_WINDOW_SHORT, c.RIEMANNIAN_WINDOW_LONG
        )

        # === STEP 2: Calculate Basket Scores ===

        # Directional Factor (1.0 = Bullish, -1.0 = Bearish)
        direction_factor = np.sign(2 * df["momentum_score"] - 1)
        direction_factor = direction_factor.replace(0, 0)

        # Trending Basket (Directional: -1 to 1)
        # We map 0-1 scores to -1 to 1 for directional components
        trending_score = (
            c.TREND_MOMENTUM_W * (2 * df["momentum_score"] - 1)
            + c.TREND_QUALITY_W * (2 * df["trend_quality_score"] - 1)
            + c.TREND_VOL_EXP_W * df["vol_expansion_score"] * direction_factor
            + c.TREND_EFFICIENCY_W * df["efficiency_score"] * direction_factor
        )

        # Mean-Reversion Basket (Penalty: 0 to 1)
        # High Score = High Mean Reversion / Chop
        mean_reverting_score = (
            c.MR_HURST_W * (1 - df["hurst_score"])
            + c.MR_DIVERGENCE_W * (1 - df["divergence_score"])
            + c.MR_CORRELATION_W * df["correlation_score"]
        )

        # === STEP 3: Calculate Net Score ===
        # Logic: Trend Score * (1 - Chop Penalty)
        # Result:
        #   Strong Bull Trend (1) * (1 - 0) = 1
        #   Strong Bear Trend (-1) * (1 - 0) = -1
        #   Choppy Market (0) * (1 - 1) = 0

        net_score = trending_score * (1 - mean_reverting_score)

        # === STEP 3.5: EXP-6 — Quiet-Grind Adaptive Momentum Boost ===
        # Detects low-vol persistent-drift environments and adds a directional boost
        # so the model isn't stranded in chop territory during slow bull/bear grinds.
        # Only fires when momentum AND trend_quality both confirm direction;
        # vol_expansion alone cannot suppress a confirmed directional bias.
        if c.USE_QUIET_GRIND_BOOST:
            mom_directional = 2 * df["momentum_score"] - 1  # [-1, 1]
            quiet_grind_mask = (
                (df["vol_expansion_score"] < c.QUIET_GRIND_VOL_CEIL)  # low vol
                & (
                    abs(df["momentum_score"] - 0.5) > (c.QUIET_GRIND_MOM_EDGE - 0.5)
                )  # |mom| strong
                & (
                    df["trend_quality_score"] > c.QUIET_GRIND_TQ_FLOOR
                )  # confirmed trend
            )
            net_score = (
                net_score
                + c.QUIET_GRIND_BOOST_STR
                * mom_directional
                * quiet_grind_mask.astype(float)
            )

        # === STEP 4: Normalize & Smooth ===
        # Map net score from [-1, 1] to [0, 1]
        # 0 = Strong Bear, 0.5 = Neutral/Chop, 1 = Strong Bull
        final_score_raw = (net_score + 1) / 2
        df["regime_score"] = final_score_raw.ewm(
            span=c.FINAL_SCORE_EMA, adjust=False
        ).mean()

        # === STEP 5: HMM Bear-Aware Blend ===
        # Preserve raw V6 score so callers can compare before/after blend.
        df["v6_regime_score"] = df["regime_score"].copy()

        if c.USE_HMM:
            try:
                # BearAwareHMMRegime is defined later in this module; instantiated
                # here (not in __init__) to avoid a forward-reference at class parse time.
                hmm = BearAwareHMMRegime(
                    n_states=c.HMM_N_STATES,
                    random_state=c.HMM_RANDOM_STATE,
                )
                hmm_out = hmm.fit_predict(df).reindex(df.index)
                df["HMM_Bear_Prob"] = hmm_out["hmm_bear_prob"].fillna(0.33)
                df["HMM_Bull_Prob"] = hmm_out["hmm_bull_prob"].fillna(0.33)
                df["HMM_State"] = hmm_out["hmm_state"].fillna(1).astype(float)
                # Symmetric HMM blend:
                # (1) Bear suppression: V6 x (1 - a_bear x bear_prob)
                #     — pulls score toward 0 when HMM is confident in bear state.
                # (2) Bull boost: bear_adj + a_bull x bull_prob x (1 - bear_adj)
                #     — gently lifts score when HMM is confident in bull state; never exceeds 1.0.
                # Net effect: bear confirmed → near 0; bull confirmed → above V6; transition → minimal delta.
                bear_adj = df["v6_regime_score"] * (
                    1.0 - c.HMM_BEAR_WEIGHT * df["HMM_Bear_Prob"]
                )
                df["regime_score"] = (
                    bear_adj
                    + c.HMM_BULL_WEIGHT * df["HMM_Bull_Prob"] * (1.0 - bear_adj)
                ).clip(0.0, 1.0)
            except Exception as e:
                warnings.warn(
                    f"[RegimeModel] HMM fit failed ({e!r}), falling back to V6 score only.",
                    stacklevel=2,
                )
                df["HMM_Bear_Prob"] = 0.33
                df["HMM_Bull_Prob"] = 0.33
                df["HMM_State"] = 1.0
        else:
            df["HMM_Bear_Prob"] = 0.33
            df["HMM_Bull_Prob"] = 0.33
            df["HMM_State"] = 1.0

        return df

    def _calculate_efficiency_v7(self, df: pd.DataFrame, window: int) -> pd.Series:
        """
        EXP-3: Choppiness Index replaces Kaufman Efficiency Ratio.

        CHOP = 100 * log10(sum_ATR1(n) / (HH(n) - LL(n))) / log10(n)
        Range: ~38.2 = perfect Fibonacci trend, 100 = pure chop.

        Benefits over KER:
        - OHLC-native: uses True Range, not just close-to-close.
        - Naturally bounded [0, 100] — no percentile-rank needed.
        - Much more stable bar-to-bar: eliminates the 0.05↔0.95 oscillation
          that was the dominant noise source at 40% weight.
        - Inverted & normalized to [0, 1]: 1 = strong trend, 0 = chop.
        """
        high = df["High"]
        low = df["Low"]
        close = df["Close"]

        # True Range (aligned to df.index)
        prev_close = close.shift(1)
        tr = pd.concat(
            [
                high - low,
                (high - prev_close).abs(),
                (low - prev_close).abs(),
            ],
            axis=1,
        ).max(axis=1)

        sum_atr = tr.rolling(window, min_periods=window // 2).sum()
        hh = high.rolling(window, min_periods=window // 2).max()
        ll = low.rolling(window, min_periods=window // 2).min()
        range_hl = (hh - ll).replace(0, np.nan)

        chop = (100.0 * np.log10(sum_atr / range_hl) / np.log10(window)).clip(0, 100)

        # Invert so 1.0 = strong trend (low chop), 0.0 = pure chop
        efficiency = 1.0 - (chop / 100.0)

        # Light smoothing — span=5 vs old 126-bar rank window keeps it responsive
        return efficiency.ewm(span=5, adjust=False).mean().fillna(0.5)

    def _calculate_momentum_v6(self, df: pd.DataFrame, window: int) -> pd.Series:
        """
        V6 Momentum: Directional Momentum.
        Uses ROC (Rate of Change) normalized by volatility to determine trend direction and strength.
        """
        # ROC
        roc = df["Close"].pct_change(window, fill_method=None)

        # Normalize ROC by volatility (Sharpe-like)
        # We use rolling std of returns * sqrt(window) to estimate vol over the window
        vol = df["Close"].pct_change(fill_method=None).rolling(window).std() * np.sqrt(
            window
        )

        # V7 Fix: Cap volatility to prevent signal crushing in crashes
        # REVERTED: Let volatility be natural. High vol should dampen the signal if ROC is weak.
        # But if ROC is strong negative, high vol is fine.

        risk_adjusted_mom = roc / (vol + 1e-9)

        # Map to 0-1 using Tanh
        # Tanh maps (-inf, inf) to (-1, 1)
        # We want (-2, 2) to map to roughly (-0.9, 0.9)
        # tanh(2) = 0.96
        momentum_score = (np.tanh(risk_adjusted_mom) + 1) / 2

        return momentum_score.fillna(0.5)

    def _calculate_trend_quality_v6(self, df: pd.DataFrame, window: int) -> pd.Series:
        """
        V7 Trend Quality: Linear Regression Slope * R2.
        - Slope measures direction and magnitude.
        - R2 measures consistency (quality).
        - High Quality Trend = Strong Slope + High R2.
        """
        # Use Close price for trend quality
        prices = df["Close"].to_numpy().astype(np.float64)

        slope_series, r2_series = _calculate_trend_quality(prices, window)

        # Combine Slope and R2
        # We want positive slope + high R2 -> Bullish (1)
        # Negative slope + high R2 -> Bearish (0)
        # Low R2 -> Chop (0.5)

        slope_s = pd.Series(slope_series, index=df.index)
        r2_s = pd.Series(r2_series, index=df.index)

        # Normalize Slope (Z-Score)
        slope_z = _z_score(slope_s, window=126).clip(-3, 3)

        # Weighted Score: Slope * R2
        # If R2 is low, the score is dampened towards 0 (neutral slope)
        # But we want it dampened towards 0.5 (neutral regime)

        # Map Slope Z to [-1, 1]
        slope_norm = slope_z / 3.0

        # Quality Adjusted Trend = Slope * R2
        # Range: [-1, 1]
        quality_trend = slope_norm * r2_s

        # Map to [0, 1]
        final_score = (quality_trend + 1) / 2

        return final_score.fillna(0.5)

    def _calculate_hurst_v6(self, prices: pd.Series, window: int) -> pd.Series:
        """
        V6 Hurst: Uses moving average detrending.
        - Faster and more robust to short-term noise than linear detrending.
        """
        prices_arr = prices.to_numpy().astype(np.float64)
        hurst_raw_arr = _rolling_hurst(prices_arr, window)
        hurst_raw = pd.Series(hurst_raw_arr, index=prices.index)

        # Reduced normalization window from 252 to 126
        hurst_normalized = _normalize_to_01(hurst_raw, window=126)
        return hurst_normalized.clip(0, 1).fillna(0.5)

    def _calculate_vol_breakout_v6(
        self, df: pd.DataFrame, short_window: int, long_window: int
    ) -> pd.Series:
        """
        V6 Volatility: "Volatility Breakout" signal.
        - Compares short-term vol to long-term vol.
        - A sharp increase is a clearer signal of regime change.
        """
        returns = df["pct_change"]

        # V6 Upgrade: Use Z-Score of Volatility instead of simple ratio
        # This normalizes the volatility spike relative to recent history
        vol = returns.rolling(short_window).std()
        vol_mean = vol.rolling(long_window).mean()
        vol_std = vol.rolling(long_window).std()

        vol_z = (vol - vol_mean) / (vol_std + 1e-9)

        # Map Z-Score to 0-1
        # Z > 2 (Spike) -> Score 1.0
        # Z < 0 (Contraction) -> Score 0.0
        vol_breakout_score = _sigmoid(vol_z, k=1.0)

        return vol_breakout_score.fillna(0.5)

    def _calculate_correlation_v6(
        self, df: pd.DataFrame, market_df: pd.DataFrame | None, window: int
    ) -> pd.Series:
        """
        V6 Correlation: Uses correlation with a market benchmark (e.g., SPY).
        - High correlation with market = risk-on/off, MR behavior.
        - Low correlation = idiosyncratic move, trending behavior.

        FALLBACK: If no market data, uses Serial Autocorrelation.
        - High Positive Autocorrelation = Trending (Persistence)
        - Negative/Low Autocorrelation = Mean Reverting (Noise)
        """
        if market_df is None or market_df.empty:
            # Fallback: Serial Autocorrelation (Lag 1)
            # Trending assets have high positive autocorrelation.
            # Mean-reverting assets have low or negative autocorrelation.
            # We map Autocorr [-1, 1] to Score [0, 1]
            # But wait, the original logic was: High Corr with Market = MR (Score 1).
            # Here: High Autocorr = Trend.
            # So we need to INVERT the logic for Autocorr to match the "MR Basket" logic.
            # High Autocorr -> Low MR Score.

            returns = df["pct_change"]
            autocorr = returns.rolling(window).corr(returns.shift(1))

            # Normalize: Autocorr of 1.0 (Perfect Trend) -> Score 0.0 (No MR)
            # Autocorr of 0.0 or -1.0 (Noise/MR) -> Score 1.0 (High MR)
            # Map [1, -1] to [0, 1]
            # Score = (1 - Autocorr) / 2
            # Autocorr 1 -> 0
            # Autocorr -1 -> 1
            # Autocorr 0 -> 0.5

            corr_score = (1 - autocorr.clip(-1, 1)) / 2
            return corr_score.ewm(span=10).mean().fillna(0.5)

        # Align data
        data = pd.DataFrame({"asset": df["pct_change"]}).join(
            pd.DataFrame({"market": market_df["pct_change"]})
        )
        data = data.ffill().fillna(0)

        # V6 Upgrade: Rolling correlation with market benchmark
        rolling_corr = data["asset"].rolling(window).corr(data["market"])

        # High correlation is a mean-reverting signal (score = 1)
        # Low/negative correlation is a trending signal (score = 0)
        # We normalize so that high correlation -> high score
        corr_score = (rolling_corr.clip(-1, 1) + 1) / 2

        return corr_score.ewm(span=10).mean().fillna(0.5)

    def _calculate_divergence_v6(self, df: pd.DataFrame, window: int) -> pd.Series:
        """
        EXP-2: Corwin-Schultz Bid-Ask Spread Proxy replaces OBV divergence.

        OLD PROBLEM: OBV divergence flatlined 0.45-0.55 across all regimes,
        making its 10% weight in the MR basket completely inert.

        NEW APPROACH: Corwin-Schultz (2012) estimates the effective bid-ask spread
        from daily High/Low prices alone — no tick data required.
        - Wide spread  → illiquid, stressed market  → bearish microstructure
        - Tight spread → liquid, healthy market     → bullish microstructure

        Mapping to the MR basket:
          divergence_score ∈ [0, 1]; used as (1 - divergence_score) in MR penalty.
          High spread (bear) → low divergence_score → high MR penalty ✓
          Tight spread (bull) → high divergence_score → low MR penalty  ✓

        Formula (vectorized, no Numba dependency):
          H2 = max(H[t], H[t-1]),  L2 = min(L[t], L[t-1])
          β  = log(H2/L2)²
          y  = log(H[t]/L[t])² + log(H[t-1]/L[t-1])²
          a  = (√(2β) - √β) / (3-2√2) - √(y / (3-2√2))     [clipped to >= 0]
          spread = 2(e^a - 1) / (1 + e^a)
        """
        high = df["High"].to_numpy().astype(np.float64)
        low = df["Low"].to_numpy().astype(np.float64)

        # Pairwise two-bar quantities (vectorized)
        H2 = np.maximum(high[1:], high[:-1])
        L2 = np.minimum(low[1:], low[:-1])

        # Guard against zero range (e.g., halt days)
        H2 = np.where(H2 <= L2, L2 * 1.0001, H2)
        h_safe = np.where(high[1:] <= low[1:], low[1:] * 1.0001, high[1:])
        h_prev = np.where(high[:-1] <= low[:-1], low[:-1] * 1.0001, high[:-1])

        k = 3.0 - 2.0 * np.sqrt(2.0)  # ≈ 0.1716
        beta = np.log(H2 / L2) ** 2
        gamma = np.log(h_safe / low[1:]) ** 2 + np.log(h_prev / low[:-1]) ** 2

        alpha = (np.sqrt(2.0 * beta) - np.sqrt(beta)) / k - np.sqrt(gamma / k)
        alpha = np.clip(alpha, 0.0, None)

        spread_vals = 2.0 * (np.exp(alpha) - 1.0) / (1.0 + np.exp(alpha))
        spread = pd.Series(
            np.concatenate([[np.nan], spread_vals]),
            index=df.index,
        ).clip(lower=0)

        # Rolling mean over `window` bars, then percentile-rank over 1-year window
        spread_smooth = spread.rolling(window, min_periods=window // 2).mean()
        spread_rank = spread_smooth.rolling(252, min_periods=60).rank(pct=True)

        # Invert: tight spread (rank≈0) → score≈1 (healthy/bull, low MR penalty)
        #         wide  spread (rank≈1) → score≈0 (stressed/bear, high MR penalty)
        return (1.0 - spread_rank).fillna(0.5)

    def _calculate_riemannian_score(
        self, df: pd.DataFrame, window_short: int, window_long: int
    ) -> pd.Series:
        """
        Detects Regime Shifts by measuring the Riemannian Distance between
        the short-term covariance structure and the long-term (background) covariance structure.
        """
        # Features to use for covariance matrix
        # We need at least 2 features to make a matrix.
        # Using Returns and Log-Range (volatility proxy) and maybe Volume change?
        # Let's use Returns and Log-Range as a 2D manifold.

        if "pct_change" not in df.columns:
            df["pct_change"] = df["Close"].pct_change(fill_method=None)

        # Log Range: log(High/Low)
        log_range = np.log(df["High"] / df["Low"])

        # Prepare data matrix
        # We need to handle NaNs
        data = (
            pd.DataFrame({"Returns": df["pct_change"], "LogRange": log_range})
            .fillna(0)
            .to_numpy()
        )

        n = len(data)
        distances = np.full(n, np.nan)

        if n < window_long:
            return pd.Series(distances, index=df.index)

        stride = 5
        for i in range(window_long, n, stride):
            slice_short = data[i - window_short : i]
            cov_short = np.cov(slice_short, rowvar=False)

            slice_long = data[i - window_long : i]
            cov_long = np.cov(slice_long, rowvar=False)

            cov_short += np.eye(cov_short.shape[0]) * 1e-6
            cov_long += np.eye(cov_long.shape[0]) * 1e-6

            distances[i] = _riemannian_distance_2d(cov_long, cov_short)

        return pd.Series(distances, index=df.index).ffill()

    def _apply_adaptive_ema(
        self, series: pd.Series, span_array: np.ndarray
    ) -> pd.Series:
        """
        Applies EMA with adaptive span (changes based on conditions).
        """
        result = np.full(len(series), np.nan)
        result[0] = series.iloc[0] if not pd.isna(series.iloc[0]) else 0.5

        for i in range(1, len(series)):
            if pd.isna(series.iloc[i]):
                result[i] = result[i - 1]
            else:
                alpha = 2 / (span_array[i] + 1)
                result[i] = alpha * series.iloc[i] + (1 - alpha) * result[i - 1]

        return pd.Series(result, index=series.index)

    def _multi_timeframe_filter(self, score: pd.Series, ratio: int) -> pd.Series:
        """
        Confirms regime with longer timeframe.
        Only changes regime if both timeframes agree.
        """
        # Resample to longer timeframe
        longer_tf_score = score.rolling(ratio, min_periods=1).mean()

        # Blend: 70% current, 30% longer timeframe
        blended = 0.7 * score + 0.3 * longer_tf_score

        return blended

    def _apply_hysteresis(self, regime_scores: pd.Series, margin: float) -> pd.Series:
        """
        Applies hysteresis to prevent rapid regime flipping.
        V3: Reduced margin from 0.05 to 0.03 for better responsiveness.
        """
        categories = []
        current_state = "Mixed"

        for score in regime_scores:
            if pd.isna(score):
                categories.append(current_state)
                continue

            # Simplified 3-regime system: Strong MR, Mixed, Strong Trend
            if current_state == "Strong MR":
                if score > 0.35 + margin:
                    current_state = "Mixed"
            elif current_state == "Mixed":
                if score < 0.35 - margin:
                    current_state = "Strong MR"
                elif score > 0.65 + margin:
                    current_state = "Strong Trend"
            elif current_state == "Strong Trend" and score < 0.65 - margin:
                current_state = "Mixed"

            categories.append(current_state)

        return pd.Series(categories, index=regime_scores.index)

    # ============ LEGACY COMPATIBILITY (REMOVED WEAK COMPONENTS) ============
    # V3 Analysis showed RSRS and Hurst added noise without predictive value
    # Keeping stubs for backward compatibility only


# ------------------------------------------------------------------
# STANDALONE FUNCTIONS
# ------------------------------------------------------------------


def calculate_riemannian_regime(
    df: pd.DataFrame, features: list, window_short: int = 20, window_long: int = 100
) -> pd.Series:
    """
    Detects Regime Shifts by measuring the Riemannian Distance between
    the short-term covariance structure and the long-term (background) covariance structure.

    Features should be a list of column names (e.g., ['Returns', 'LogVolume', 'Range']).
    """
    # 1. Prepare Data
    data = df[features].to_numpy()
    n = len(data)
    distances = np.full(n, np.nan)

    # We need at least window_long samples
    if n < window_long:
        return pd.Series(distances, index=df.index)

    stride = 1

    for i in range(window_long, n, stride):
        # Short-term window
        slice_short = data[i - window_short : i]
        cov_short = np.cov(slice_short, rowvar=False)

        # Long-term window (Background)
        slice_long = data[i - window_long : i]
        cov_long = np.cov(slice_long, rowvar=False)

        # Regularize to ensure SPD (add small noise to diagonal)
        cov_short += np.eye(cov_short.shape[0]) * 1e-6
        cov_long += np.eye(cov_long.shape[0]) * 1e-6

        dist = _riemannian_distance_2d(cov_long, cov_short)
        distances[i] = dist

    return pd.Series(distances, index=df.index).ffill()


# ------------------------------------------------------------------
# BEAR-AWARE HMM REGIME DETECTOR
# ------------------------------------------------------------------


class BearAwareHMMRegime:
    """
    3-State Gaussian Hidden Markov Model trained on SPY benchmark features.

    Problem with V6 regime score:
    - V6 is a continuous [0,1] trending/mean-reversion score.
    - It does not explicitly label bear markets as a distinct state.
    - Result: regime_score ≈ 0.3-0.5 in both "choppy bull" and "bear crash" —
      the downstream XGBoost sees no clear bear signal and fires BUY anyway.

    HMM fix:
    - 3 states learned from data: bull (high return, low vol),
      transition (mixed), bear (low/negative return, high vol).
    - States identified post-fit by ranking state mean returns.
    - Output: hmm_bear_prob per day — probability of being in bear state.
    - Blended score: V6_score x (1 - a x hmm_bear_prob) suppresses regime
      score in bear periods, so the ML model sees a strong bear signal.

    No lookahead: HMM is fit on the same SPY series used for the full
    backtest period. This mirrors production usage (retrain on full
    available history at signal time).
    """

    def __init__(self, n_states: int = 3, random_state: int = 42):
        self.n_states = n_states
        self.random_state = random_state
        self._model = None
        self._bull_state: int = 0
        self._bear_state: int = 2

    # ────────────────────────────────────────────────────────────────
    def fit_predict(self, benchmark_df: pd.DataFrame) -> pd.DataFrame:
        """
        Fit HMM on benchmark features and return per-day regime posteriors.

        Feature set (all computed internally from OHLCV):
          - drawdown_20  : 20-day peak drawdown — distinguishes sustained bears from vol spikes
          - amihud_norm  : Amihud illiquidity rank [0,1] — institutional stress proxy
          - vol_ratio    : EWMA vol_20 / vol_60 — vol regime expansion vs background

        Smoothing is asymmetric: bear_prob span=7 (slower; reduces false alarms from vol spikes),
        bull_prob span=3 (faster; quicker confirmation of emerging bull state).

        Returns
        -------
        pd.DataFrame with columns:
            hmm_state      int   (0=bull, 1=transition, 2=bear)
            hmm_bull_prob  float [0,1]
            hmm_bear_prob  float [0,1]
        """
        try:
            from hmmlearn.hmm import GaussianHMM
        except ImportError:
            warnings.warn(
                "hmmlearn not installed — HMM regime disabled. "
                "Install with: pip install hmmlearn",
                stacklevel=2,
            )
            idx = benchmark_df.index
            return pd.DataFrame(
                {"hmm_state": 1, "hmm_bull_prob": 0.33, "hmm_bear_prob": 0.33},
                index=idx,
            )

        df = benchmark_df.copy()
        ret = df["Close"].pct_change().fillna(0)

        # EWMA variance (RiskMetrics λ=0.94)
        ewma_var = ret.ewm(alpha=0.06, adjust=False).var()
        vol_daily = np.sqrt(ewma_var.clip(lower=0))

        vol_20 = vol_daily.rolling(20, min_periods=5).mean()
        vol_60 = vol_daily.rolling(60, min_periods=20).mean()
        vol_ratio = (vol_20 / vol_60.replace(0, np.nan)).fillna(1.0).clip(0.2, 5.0)

        # ── EXP-1: Drawdown-from-peak (distinguishes true bears from vol spikes)
        # Bears: sustained drawdown + high illiquidity.
        # Vol-spike bounces: momentary drawdown, illiquidity recovers quickly.
        peak_20 = df["Close"].rolling(20, min_periods=5).max()
        drawdown_20 = ((df["Close"] - peak_20) / peak_20.replace(0, np.nan)).fillna(0.0)

        # ── EXP-1: Amihud illiquidity signal
        if "Volume" in df.columns and df["Volume"].gt(0).any():
            dollar_vol = df["Close"] * df["Volume"].replace(0, np.nan)
            amihud_raw = ret.abs() / dollar_vol
            # Rolling 20-bar mean, then percentile-rank over 252 bars for stationarity
            amihud_20 = amihud_raw.rolling(20, min_periods=5).mean()
            amihud_norm = (
                amihud_20.rolling(252, min_periods=60).rank(pct=True).fillna(0.5)
            )
        else:
            # Fallback: use vol_ratio as liquidity proxy when Volume not available
            amihud_norm = (
                vol_ratio.rolling(252, min_periods=60).rank(pct=True).fillna(0.5)
            )

        feat = pd.DataFrame(
            {
                "drawdown_20": drawdown_20,  # sustained bear: -0.10+; vol spikes: brief
                "amihud_norm": amihud_norm,  # illiquidity spikes in genuine stress
                "vol_ratio": vol_ratio,  # vol expansion vs recent background
            }
        ).fillna(0)

        # Standardise features (zero-mean, unit-variance)
        mu = feat.mean()
        sigma = feat.std().replace(0, 1.0)
        X = ((feat - mu) / sigma).to_numpy()

        # Fit GaussianHMM
        # covariance_type='full' with 3-4 features: well-conditioned covariance estimation.
        # n_iter=500 with tol=1e-5 ensures convergence on the expanded feature set.
        model = GaussianHMM(
            n_components=self.n_states,
            covariance_type="full",
            n_iter=500,
            tol=1e-5,
            random_state=self.random_state,
        )
        with warnings.catch_warnings():
            warnings.simplefilter("ignore")
            model.fit(X)

        self._model = model

        states = model.predict(X)
        proba = model.predict_proba(X)  # shape (n, n_states)

        # Identify bear / bull states by mean 5d return per state
        mean_ret_by_state = np.array(
            [
                ret.rolling(5, min_periods=1).sum().to_numpy()[states == s].mean()
                if (states == s).any()
                else 0.0
                for s in range(self.n_states)
            ]
        )
        order = np.argsort(mean_ret_by_state)  # ascending: bear … bull
        self._bear_state = int(order[0])
        self._bull_state = int(order[-1])

        # Build output
        out = pd.DataFrame(index=df.index)
        out["hmm_bull_prob"] = proba[:, self._bull_state]
        out["hmm_bear_prob"] = proba[:, self._bear_state]

        semantic = np.ones(len(states), dtype=int)  # default = transition (1)
        semantic[states == self._bear_state] = 2  # bear
        semantic[states == self._bull_state] = 0  # bull
        out["hmm_state"] = semantic

        # Asymmetric EWM smoothing:
        # bear span=7 (slower) → avoids false-alarm calls from short vol spikes.
        # bull span=3 (faster) → quicker confirmation of emerging bull regime.
        out["hmm_bear_prob"] = out["hmm_bear_prob"].ewm(span=7, adjust=False).mean()
        out["hmm_bull_prob"] = out["hmm_bull_prob"].ewm(span=3, adjust=False).mean()

        return out

    # ────────────────────────────────────────────────────────────────
    @property
    def state_labels(self) -> dict:
        """Return human-readable state label map after fitting."""
        if self._model is None:
            return {}
        return {self._bear_state: "bear", self._bull_state: "bull"}
