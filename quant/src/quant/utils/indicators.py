import warnings
from typing import Any

import numpy as np
import pandas as pd
import talib as ta
from numpy.lib.stride_tricks import sliding_window_view

# Native (nanobind) kernels for sequential recurrence hot paths. These replicate
# the pure-Python loops bit-for-bit but run in C++ (μs-level) with identical
# output. The extension is a REQUIRED dependency (no pure-Python fallback): the
# .so is auto-built on first import by ``quant/utils/cpp_native/__init__.py``
# (setup.py build_ext --inplace), so this import cannot fail on wheels.
from quant.utils.cpp_native import vq_kernels as _vq

warnings.filterwarnings("ignore", category=UserWarning)

_SQRT2 = np.sqrt(2.0)
_CS_K = 3.0 - 2.0 * _SQRT2  # Corwin-Schultz constant ≈ 0.1716


# ============================================================================
# CORE HELPERS (pandas / NumPy / TA-Lib)
# ============================================================================


def wilder_atr(
    high: np.ndarray, low: np.ndarray, close: np.ndarray, period: int = 14
) -> np.ndarray:
    """Wilder-smoothed Average True Range (TA-Lib)."""
    return ta.ATR(
        np.asarray(high, dtype=np.float64),
        np.asarray(low, dtype=np.float64),
        np.asarray(close, dtype=np.float64),
        timeperiod=period,
    )


def _true_range(high: np.ndarray, low: np.ndarray, close: np.ndarray) -> np.ndarray:
    prev_close = np.empty_like(close)
    prev_close[0] = close[0]
    prev_close[1:] = close[:-1]
    return np.maximum(
        high - low,
        np.maximum(np.abs(high - prev_close), np.abs(low - prev_close)),
    )


def rolling_mean(
    arr: np.ndarray, window: int, min_periods: int | None = None
) -> np.ndarray:
    """Public rolling mean (pandas backend). ``min_periods`` defaults to *window*."""
    mp = int(window) if min_periods is None else int(min_periods)
    return (
        pd.Series(arr, dtype=np.float64)
        .rolling(int(window), min_periods=mp)
        .mean()
        .to_numpy()
    )


def _rolling_mean(arr: np.ndarray, window: int) -> np.ndarray:
    return np.asarray(_vq.rolling_mean(np.asarray(arr, dtype=np.float64), int(window)))


def _rolling_max(arr: np.ndarray, window: int) -> np.ndarray:
    return np.asarray(_vq.sliding_max(np.asarray(arr, dtype=np.float64), int(window)))


def _rolling_min(arr: np.ndarray, window: int) -> np.ndarray:
    return np.asarray(_vq.sliding_min(np.asarray(arr, dtype=np.float64), int(window)))


def _rolling_std(arr: np.ndarray, window: int) -> np.ndarray:
    return pd.Series(arr, dtype=np.float64).rolling(window).std().to_numpy()


def _ema(arr: np.ndarray, span: int) -> np.ndarray:
    return (
        pd.Series(arr, dtype=np.float64).ewm(span=span, adjust=False).mean().to_numpy()
    )


def _rsi(close: np.ndarray, period: int) -> np.ndarray:
    return ta.RSI(np.asarray(close, dtype=np.float64), timeperiod=period)


def _percentile_rank(arr: np.ndarray, window: int) -> np.ndarray:
    """Rolling percentile of the last value in each window (0-1, native)."""
    return np.asarray(
        _vq.rolling_rank_pct(np.asarray(arr, dtype=np.float64), int(window))
    )


def _linear_regression_slope(arr: np.ndarray, window: int) -> np.ndarray:
    """Rolling OLS slope via TA-Lib (C)."""
    return ta.LINEARREG_SLOPE(np.asarray(arr, dtype=np.float64), timeperiod=window)


def _kurtosis(arr: np.ndarray, window: int) -> np.ndarray:
    return np.asarray(_vq.rolling_kurt(np.asarray(arr, dtype=np.float64), int(window)))


def _skewness(arr: np.ndarray, window: int) -> np.ndarray:
    return np.asarray(_vq.rolling_skew(np.asarray(arr, dtype=np.float64), int(window)))


def _rate_of_change(arr: np.ndarray, period: int) -> np.ndarray:
    s = pd.Series(arr, dtype=np.float64)
    return (s.pct_change(periods=period) * 100.0).to_numpy()


def _efficiency_ratio(arr: np.ndarray, window: int) -> np.ndarray:
    a = np.asarray(arr, dtype=np.float64)
    n = len(a)
    net = np.full(n, np.nan)
    net[window:] = np.abs(a[window:] - a[:-window])
    diff = np.empty(n)
    diff[0] = np.nan
    diff[1:] = a[1:] - a[:-1]
    path = np.asarray(_vq.rolling_sum(np.abs(diff), int(window)))
    path = np.where(path == 0.0, np.nan, path)  # .replace(0, np.nan)
    with np.errstate(divide="ignore", invalid="ignore"):
        out = net / path
    out[:window] = np.nan
    return out


def _choppiness_index(
    high: np.ndarray, low: np.ndarray, close: np.ndarray, window: int
) -> np.ndarray:
    h = np.asarray(high, dtype=np.float64)
    lo_ = np.asarray(low, dtype=np.float64)
    c = np.asarray(close, dtype=np.float64)
    tr = _true_range(h, lo_, c)
    atr_sum = np.asarray(_vq.rolling_sum(tr, int(window)))
    hi = np.asarray(_vq.sliding_max(h, int(window)))
    lo = np.asarray(_vq.sliding_min(lo_, int(window)))
    rng = hi - lo
    with np.errstate(divide="ignore", invalid="ignore"):
        ci = 100.0 * np.log10(atr_sum / rng) / np.log10(window)
    ci = np.where((rng > 0) & (atr_sum > 0), ci, 50.0)
    return ci


def _autocorrelation(arr: np.ndarray, window: int, lag: int) -> np.ndarray:
    return np.asarray(
        _vq.rolling_autocorr(np.asarray(arr, dtype=np.float64), int(window), int(lag))
    )


def _vwap_deviation(close: np.ndarray, volume: np.ndarray, window: int) -> np.ndarray:
    c = np.asarray(close, dtype=np.float64)
    v = np.asarray(volume, dtype=np.float64)
    vol_sum = np.asarray(_vq.rolling_sum(v, int(window)))
    cv_sum = np.asarray(_vq.rolling_sum(c * v, int(window)))
    vol_sum = np.where(vol_sum == 0.0, np.nan, vol_sum)  # .replace(0, nan)
    with np.errstate(divide="ignore", invalid="ignore"):
        vwap = cv_sum / vol_sum
        dev = (c - vwap) / np.where(vwap == 0.0, np.nan, vwap)
    return np.where(np.isnan(dev), 0.0, dev)  # .fillna(0.0) — NaN only, keeps inf


def _money_flow_index(
    high: np.ndarray,
    low: np.ndarray,
    close: np.ndarray,
    volume: np.ndarray,
    period: int,
) -> np.ndarray:
    return ta.MFI(
        np.asarray(high, dtype=np.float64),
        np.asarray(low, dtype=np.float64),
        np.asarray(close, dtype=np.float64),
        np.asarray(volume, dtype=np.float64),
        timeperiod=period,
    )


def _adaptive_z_score(arr: np.ndarray, window: int) -> np.ndarray:
    """KAMA-like adaptive z-score using ER from rolling path length (vectorized ER)."""
    a = np.asarray(arr, dtype=np.float64)
    n = len(a)
    if n == 0:
        return a.copy()

    s = pd.Series(a)
    change = (s - s.shift(window)).abs()
    path = s.diff().abs().rolling(window).sum()
    er = (change / path.replace(0, np.nan)).fillna(0.0).to_numpy()

    fast_sc = 2.0 / 3.0
    slow_sc = 2.0 / 31.0
    sc = (er * (fast_sc - slow_sc) + slow_sc) ** 2

    return np.asarray(
        _vq.adaptive_z_scores(
            a.astype(np.float64, copy=False),
            sc.astype(np.float64, copy=False),
        )
    )
    """
     --------------------------------------------------------------------
     DEPRECATED / UNREACHABLE — pure-Python recurrence removed. Kept only
     as a reference; native is now mandatory.
     --------------------------------------------------------------------
     means = np.empty(n)
     vars_ = np.empty(n)
     z_scores = np.empty(n)
     means[0] = a[0]
     vars_[0] = 0.0
     z_scores[0] = 0.0
     for i in range(1, n):
         sci = sc[i]
         means[i] = means[i - 1] + sci * (a[i] - means[i - 1])
         vars_[i] = (1.0 - sci) * vars_[i - 1] + sci * (a[i] - means[i]) ** 2
         z_scores[i] = (
             (a[i] - means[i]) / np.sqrt(vars_[i]) if vars_[i] > 1e-9 else 0.0
         )
     return z_scores
    """


def _hurst_exponent(arr: np.ndarray, window: int) -> np.ndarray:
    """Rolling Hurst via batched R/S at multiple lags (native kernel)."""
    a = np.asarray(arr, dtype=np.float64)
    return np.asarray(_vq.hurst_exponent(a.astype(np.float64, copy=False), int(window)))


def _fractal_dimension(arr: np.ndarray, window: int) -> np.ndarray:
    """Rolling fractal dimension via direction-change proxy (native kernel)."""
    a = np.asarray(arr, dtype=np.float64)
    return np.asarray(
        _vq.fractal_dimension(a.astype(np.float64, copy=False), int(window))
    )


def _fisher_transform(prices: np.ndarray, period: int) -> tuple[np.ndarray, np.ndarray]:
    n = len(prices)
    s = pd.Series(prices, dtype=np.float64)
    roll_min = s.rolling(period).min().to_numpy()
    roll_max = s.rolling(period).max().to_numpy()

    fisher_v = np.asarray(
        _vq.fisher_core(
            np.asarray(prices, dtype=np.float64),
            roll_min.astype(np.float64, copy=False),
            roll_max.astype(np.float64, copy=False),
            int(period),
        )
    )
    inv_fisher = np.zeros(n, dtype=np.float64)
    if n > 1:
        inv_fisher[1:] = fisher_v[:-1]
    return fisher_v, inv_fisher

    """
    --------------------------------------------------------------------
    DEPRECATED (UNREACHABLE) pure-Python fallback removed; native is the
    only implementation. Reference kept for parity archaeology.
    --------------------------------------------------------------------
    for i in range(period, n):
        curr_max = roll_max[i]
        curr_min = roll_min[i]
        if curr_max != curr_min:
            val[i] = (
                0.33 * 2 * ((prices[i] - curr_min) / (curr_max - curr_min) - 0.5)
                + 0.67 * val[i - 1]
            )
        else:
            val[i] = 0.0
        val[i] = np.clip(val[i], -0.99, 0.99)
        fisher[i] = (
            0.5 * np.log((1 + val[i]) / (1 - val[i])) + 0.5 * fisher[i - 1]
        )
        inv_fisher[i] = fisher[i - 1]
    return fisher, inv_fisher
    """


def _synthetic_vix(closes: np.ndarray, lows: np.ndarray, period: int) -> np.ndarray:
    highest = pd.Series(closes, dtype=np.float64).rolling(period).max().to_numpy()
    with np.errstate(divide="ignore", invalid="ignore"):
        wvf = np.where(highest > 0, (highest - lows) / highest * 100.0, 0.0)
    wvf[:period] = 0.0
    return wvf


def _shannon_entropy(returns: np.ndarray, window: int, bins: int = 10) -> np.ndarray:
    n = len(returns)
    entropy_arr = np.zeros(n)
    if n < window:
        return entropy_arr

    windows = sliding_window_view(returns.astype(np.float64), window)
    w_min = windows.min(axis=1, keepdims=True)
    w_max = windows.max(axis=1, keepdims=True)
    span = w_max - w_min
    flat = np.where(span > 0, (windows - w_min) / span, 0.0)
    # histogram along axis 1
    hist = np.zeros((flat.shape[0], bins))
    for b in range(bins):
        lo = b / bins
        hi = (b + 1) / bins
        if b == bins - 1:
            hist[:, b] = np.sum((flat >= lo) & (flat <= hi), axis=1)
        else:
            hist[:, b] = np.sum((flat >= lo) & (flat < hi), axis=1)
    probs = hist / window
    with np.errstate(divide="ignore", invalid="ignore"):
        ent = -np.nansum(np.where(probs > 0, probs * np.log(probs), 0.0), axis=1)
    entropy_arr[window:] = ent
    return entropy_arr


def _microstructure(
    high: np.ndarray,
    low: np.ndarray,
    close: np.ndarray,
    volume: np.ndarray,
    window: int,
) -> tuple:
    n = len(close)
    del n  # native kernel owns sizing; kept param for signature compat
    high = np.asarray(high, dtype=np.float64)
    low = np.asarray(low, dtype=np.float64)
    close = np.asarray(close, dtype=np.float64)
    volume = np.asarray(volume, dtype=np.float64)
    return tuple(
        np.asarray(x)
        for x in _vq.microstructure(
            high.astype(np.float64, copy=False),
            low.astype(np.float64, copy=False),
            close.astype(np.float64, copy=False),
            volume.astype(np.float64, copy=False),
            int(window),
        )
    )


# ============================================================================
# BACKWARD-COMPATIBILITY ALIASES (former Numba names)
# ============================================================================

_rolling_mean_numba = _rolling_mean
_rolling_max_numba = _rolling_max
_rolling_min_numba = _rolling_min
_rolling_std_numba = _rolling_std
_ema_numba = _ema
_rsi_numba = _rsi
_atr_numba = wilder_atr
_percentile_rank_numba = _percentile_rank
_linear_regression_slope_numba = _linear_regression_slope
_kurtosis_numba = _kurtosis
_skewness_numba = _skewness
_autocorrelation_numba = _autocorrelation
_hurst_exponent_numba = _hurst_exponent
_fractal_dimension_numba = _fractal_dimension
_efficiency_ratio_numba = _efficiency_ratio
_choppiness_index_numba = _choppiness_index
_vwap_deviation_numba = _vwap_deviation
_money_flow_index_numba = _money_flow_index
_rate_of_change_numba = _rate_of_change
_microstructure_numba = _microstructure
_adaptive_z_score_numba = _adaptive_z_score
_fisher_transform_numba = _fisher_transform
_synthetic_vix_numba = _synthetic_vix
_shannon_entropy_numba = _shannon_entropy


# ============================================================================
# REGIME UTILITIES
# ============================================================================


def fast_kalman_filter(prices, Q=1e-5, R=1e-3):
    """1D Kalman filter (random-walk model), native nanobind kernel."""
    prices = np.asarray(prices, dtype=np.float64)
    return np.asarray(
        _vq.kalman_filter(prices.astype(np.float64, copy=False), float(Q), float(R))
    )
    """
    --------------------------------------------------------------------
    DEPRECATED (UNREACHABLE) pure-Python fallback; native is the kernel.
    --------------------------------------------------------------------
    n = len(prices)
    estimates = np.zeros(n)
    estimate = prices[0]
    error_est = 1.0
    for i in range(n):
        estimate_pred = estimate
        error_est_pred = error_est + Q
        k_gain = error_est_pred / (error_est_pred + R)
        estimate = estimate_pred + k_gain * (prices[i] - estimate_pred)
        error_est = (1 - k_gain) * error_est_pred
        estimates[i] = estimate
    return estimates
    """


def parkinson_rv(high: np.ndarray, low: np.ndarray, n_bars: int) -> float:
    """Parkinson HL realized vol over *n_bars*, scaled by ``sqrt(n_bars)``.

    Pure NumPy — used by session AM features for short morning windows where
    close-to-close std of a single return is degenerate.
    """
    hi = np.asarray(high, dtype=np.float64)
    lo = np.asarray(low, dtype=np.float64)
    mask = np.isfinite(hi) & np.isfinite(lo) & (hi > 0) & (lo > 0) & (hi >= lo)
    if not np.any(mask):
        return float("nan")
    hl = np.log(hi[mask] / lo[mask])
    var = float(np.mean(hl * hl) / (4.0 * np.log(2.0)))
    if not np.isfinite(var) or var < 0:
        return float("nan")
    return float(np.sqrt(var) * np.sqrt(max(int(n_bars), 1)))


def fast_corwin_schultz(high, low):
    """Vectorized Corwin-Schultz bid-ask spread estimate."""
    high = np.asarray(high, dtype=np.float64)
    low = np.asarray(low, dtype=np.float64)
    n = len(high)
    spreads = np.zeros(n)
    if n < 2:
        return spreads

    H2 = np.maximum(high[1:], high[:-1])
    L2 = np.minimum(low[1:], low[:-1])
    H2 = np.where(H2 <= L2, L2 * 1.0001, H2)
    h_safe = np.where(high[1:] <= low[1:], low[1:] * 1.0001, high[1:])
    h_prev = np.where(high[:-1] <= low[:-1], low[:-1] * 1.0001, high[:-1])

    beta = np.log(H2 / L2) ** 2
    gamma = np.log(h_safe / low[1:]) ** 2 + np.log(h_prev / low[:-1]) ** 2
    alpha = (np.sqrt(2.0 * beta) - np.sqrt(beta)) / _CS_K - np.sqrt(gamma / _CS_K)
    alpha = np.clip(alpha, 0.0, None)
    spread_vals = 2.0 * (np.exp(alpha) - 1.0) / (1.0 + np.exp(alpha))
    spreads[1:] = spread_vals
    return spreads


def yang_zhang_volatility(df, window=20):
    """Yang-Zhang Volatility Estimator."""
    try:
        log_ho = np.log(df["High"] / df["Open"])
        log_lo = np.log(df["Low"] / df["Open"])
        log_co = np.log(df["Close"] / df["Open"])
        log_oc = np.log(df["Open"] / df["Close"].shift(1))
        rs_overnight = log_oc.rolling(window).var()
        rs_open_close = log_co.rolling(window).var()
        rs = (log_ho * (log_ho - log_co)) + (log_lo * (log_lo - log_co))
        rs_var = rs.rolling(window).mean()
        k = 0.34 / (1.34 + (window + 1) / (window - 1))
        yz_var = rs_overnight + k * rs_open_close + (1 - k) * rs_var
        return np.sqrt(yz_var)
    except Exception:
        return pd.Series(np.nan, index=df.index)


# ============================================================================
# CONFIGURATION CLASS
# ============================================================================


class IndicatorConfig:
    """
    Extensible configuration for all technical indicators.
    All parameters can be overridden by subclasses for strategy-specific tuning.
    """

    # ========== MOVING AVERAGES ==========
    EMA_FAST = 10  # Fast EMA for trend following
    EMA_MEDIUM = 21  # Medium EMA
    EMA_SLOW = 50  # Slow EMA
    SMA_SHORT = 15  # Short SMA for trend confirmation
    SMA_MEDIUM = 30  # Medium SMA for confidence
    SMA_LONG = 60  # Long SMA for major trend

    # ========== BOLLINGER BANDS ==========
    BOLL_PERIOD = 20  # Standard is 20
    BOLL_STDDEV = 2.0  # Standard is 2.0
    BB_DISTANCE_FACTOR = 0.3  # Sensitivity factor

    # ========== RSI (Relative Strength Index) ==========
    RSI_PERIOD = 14  # Standard RSI period
    FAST_RSI_PERIOD = 7  # Short-term RSI
    RSI_OVERBOUGHT = 70  # Overbought threshold
    RSI_OVERSOLD = 30  # Oversold threshold

    # ========== MACD (Moving Average Convergence Divergence) ==========
    MACD_FASTPERIOD = 12  # Standard fast period
    MACD_SLOWPERIOD = 26  # Standard slow period
    MACD_SIGNALPERIOD = 9  # Standard signal period
    MACD_THRESHOLD = 0.5  # Threshold for signal strength

    # ========== STOCHASTIC OSCILLATOR ==========
    STOCH_K_PERIOD = 14  # %K period
    STOCH_D_PERIOD = 3  # %D smoothing period

    # ========== ADAPTIVE MATH METHODS ==========
    USE_HILBERT_TRANSFORM = True
    USE_RIEMANNIAN_REGIME = True
    STOCH_OVERBOUGHT = 80  # Overbought level
    STOCH_OVERSOLD = 20  # Oversold level

    # ========== DMI/ADX (Directional Movement Index) ==========
    DMI_PERIOD = 14  # Standard DMI period
    ADX_PERIOD = 14  # Standard ADX period
    ADX_STRONG_TREND = 25  # Strong trend threshold
    MAX_ADX = 30  # Maximum ADX for filtering

    # ========== VOLATILITY INDICATORS ==========
    ATR_PERIOD = 14  # Average True Range period
    ATR_PERCENTILE_WINDOW = 40  # Window for ATR percentile calculation
    VOL_MA_PERIOD = 20  # Volume moving average
    VOL_EXPAND_MULT = 1.05  # Volume expansion multiplier

    # ========== MOMENTUM INDICATORS ==========
    ROC_PERIOD = 12  # Rate of Change period
    MFI_PERIOD = 14  # Money Flow Index period
    CCI_PERIOD = 20  # Commodity Channel Index period
    MOMENTUM_PERIOD = 10  # Generic momentum period

    # ========== OSCILLATORS ==========
    ULTOSC_PERIOD1 = 7  # Ultimate Oscillator short-term
    ULTOSC_PERIOD2 = 14  # Ultimate Oscillator medium-term
    ULTOSC_PERIOD3 = 28  # Ultimate Oscillator long-term
    WILLR_PERIOD = 14  # Williams %R period

    # ========== RANGE & BREAKOUT ==========
    BREAKOUT_LOOKBACK = 15  # Lookback for range highs/lows
    PULLBACK_LOOKBACK = 8  # Lookback for pullback reference
    RANGE_TOLERANCE_PCT = 0.01  # Tolerance for breakout detection

    # ========== STATISTICAL INDICATORS ==========
    KURTOSIS_WINDOW = 20  # Rolling kurtosis window
    SKEWNESS_WINDOW = 20  # Rolling skewness window
    HURST_WINDOW = 50  # Hurst exponent window
    FRACTAL_WINDOW = 30  # Fractal dimension window
    EFFICIENCY_RATIO_WINDOW = 10  # Kaufman efficiency ratio
    CHOPPINESS_WINDOW = 14  # Choppiness index window
    AUTOCORR_WINDOW = 20  # Autocorrelation window
    AUTOCORR_LAG = 1  # Autocorrelation lag
    LINEAR_REG_WINDOW = 14  # Linear regression slope window
    VWAP_WINDOW = 20  # VWAP deviation window

    # ========== HILBERT TRANSFORM ==========
    HILBERT_TRANSFORM_ENABLED = True
    HILBERT_WINDOW = 60

    # ========== ON-BALANCE VOLUME ==========
    OBV_MA_PERIOD = 20  # OBV moving average for smoothing


# ============================================================================
# HILBERT TRANSFORM (causal, batched — no per-bar Python loop)
# ============================================================================


def calculate_hilbert_transform(series: pd.Series, window: int = 60) -> pd.DataFrame:
    """
    Causal Hilbert transform: batched rolling windows via native kernel.

    The native ``hilbert_spectrum`` kernel computes the last-sample analytic
    signal per sliding window by precomputed projection weights (mathematically
    identical to scipy's fft-based ``hilbert``, ~1e-13 relative agreement),
    plus the np.unwrap-equivalent phase unwrap and frequency derivation — a
    single fused pass with no 2D window allocation.
    """
    values = series.to_numpy(dtype=np.float64)
    phase, frequency, amplitude = _vq.hilbert_spectrum(
        values.astype(np.float64, copy=False), int(window)
    )
    return pd.DataFrame(
        {
            "H_PHASE": np.asarray(phase),
            "H_FREQ": np.asarray(frequency),
            "H_AMPLITUDE": np.asarray(amplitude),
        },
        index=series.index,
    )


# ============================================================================
# MAIN INDICATOR CALCULATION FUNCTION
# ============================================================================


def calculate_indicators(
    df: pd.DataFrame,
    config=IndicatorConfig,
    indicators_to_calculate: dict[str, bool] | None = None,
) -> pd.DataFrame:
    """
    Calculate technical indicators based on configuration using Numba-optimized functions.

    Args:
        df: DataFrame with OHLCV data (columns: Open, High, Low, Close, Volume)
        config: Configuration object with indicator parameters (class or instance)
        indicators_to_calculate: Optional dictionary specifying which indicators to calculate
                                {indicator_name: True/False}

    Returns:
        DataFrame with added indicators

    Available indicator groups:
        - MOVING_AVERAGES: EMA_FAST, EMA_MEDIUM, EMA_SLOW, SMA_SHORT, SMA_MEDIUM, SMA_LONG
        - BOLLINGER: BB_UPPER, BB_MID, BB_LOWER, BB_WIDTH, BB_POSITION
        - RSI: RSI, FAST_RSI
        - MACD: MACD, MACD_SIGNAL, MACD_HIST
        - STOCHASTIC: STOCH_K, STOCH_D
        - ADX: ADX
        - DMI: +DI, -DI, ATR (also calculates TR components)
        - ATR: ATR
        - VOLUME: Vol_MA, RVOL (relative volume), OBV, OBV_MA, VOL_EXPAND
        - MOMENTUM: ROC, MFI, CCI
        - OSCILLATORS: ULTOSC, WILLR
        - RANGE: RANGE_HIGH, RANGE_LOW, PULLBACK_REF_HIGH, PULLBACK_REF_LOW
        - STATISTICAL: KURTOSIS, SKEWNESS, HURST, FRACTAL_DIM, EFFICIENCY_RATIO,
                      CHOPPINESS, AUTOCORR, LINEAR_SLOPE, VWAP_DEV
        - PERCENTILE: ATR_PCT, ATR_PCTL (ATR as percentage and percentile)
        - PATTERNS: PCT_FROM_HIGH, PCT_FROM_LOW, PCT_FROM_PB_HIGH, PCT_FROM_PB_LOW,
                   BREAKOUT_CAND, BREAKDOWN_CAND
        - MICROSTRUCTURE: AMIHUD_ILLIQ, AMIHUD_SMOOTH, KYLE_LAMBDA, VPIN_PROXY, SIGNED_VOL
        - HILBERT: H_PHASE, H_FREQ, H_AMPLITUDE
    """
    # Handle config as class or instance
    if not hasattr(config, "__dict__"):
        config = config()

    # Default to calculating all indicators if not specified
    if indicators_to_calculate is None:
        indicators_to_calculate = {
            "MOVING_AVERAGES": True,
            "BOLLINGER": True,
            "RSI": True,
            "MACD": True,
            "STOCHASTIC": True,
            "ADX": True,
            "DMI": True,
            "ATR": True,
            "VOLUME": True,
            "MOMENTUM": True,
            "OSCILLATORS": True,
            "RANGE": True,
            "STATISTICAL": True,
            "PERCENTILE": True,
            "PATTERNS": True,
            "MICROSTRUCTURE": True,
            "HILBERT": True,
        }

    close_arr = df["Close"].to_numpy(dtype=np.float64)
    high_arr = df["High"].to_numpy(dtype=np.float64)
    low_arr = df["Low"].to_numpy(dtype=np.float64)
    volume_arr = df["Volume"].to_numpy(dtype=np.float64)
    n = len(df)

    # Single-pass assembly (N2.3): compute all outputs as arrays/series into
    # ``cols`` (dict preserves the original column-append order), then one
    # DataFrame constructor. No per-column manager insert churn, no input copy.
    cols: dict[str, Any] = {c: df[c] for c in df.columns}

    if indicators_to_calculate.get("MOVING_AVERAGES", False):
        cols["EMA_FAST"] = _ema(close_arr, config.EMA_FAST)
        cols["EMA_MEDIUM"] = _ema(close_arr, config.EMA_MEDIUM)
        cols["EMA_SLOW"] = _ema(close_arr, config.EMA_SLOW)
        cols["SMA_SHORT"] = _rolling_mean(close_arr, config.SMA_SHORT)
        cols["SMA_MEDIUM"] = _rolling_mean(close_arr, config.SMA_MEDIUM)
        cols["SMA_LONG"] = _rolling_mean(close_arr, config.SMA_LONG)

    if indicators_to_calculate.get("BOLLINGER", False):
        bb_u, bb_m, bb_l = ta.BBANDS(
            close_arr,
            timeperiod=config.BOLL_PERIOD,
            nbdevup=config.BOLL_STDDEV,
            nbdevdn=config.BOLL_STDDEV,
        )
        cols["BB_UPPER"] = bb_u
        cols["BB_MID"] = bb_m
        cols["BB_LOWER"] = bb_l
        cols["BB_WIDTH"] = (bb_u - bb_l) / bb_m
        cols["BB_POSITION"] = (close_arr - bb_l) / (bb_u - bb_l)

    if indicators_to_calculate.get("RSI", False):
        cols["RSI"] = _rsi(close_arr, config.RSI_PERIOD)
        cols["FAST_RSI"] = _rsi(close_arr, config.FAST_RSI_PERIOD)

    if indicators_to_calculate.get("MACD", False):
        macd, macd_sig, macd_hist = ta.MACD(
            close_arr,
            fastperiod=config.MACD_FASTPERIOD,
            slowperiod=config.MACD_SLOWPERIOD,
            signalperiod=config.MACD_SIGNALPERIOD,
        )
        cols["MACD"] = macd
        cols["MACD_SIGNAL"] = macd_sig
        cols["MACD_HIST"] = macd_hist

    if indicators_to_calculate.get("STOCHASTIC", False):
        sto_k, sto_d = ta.STOCH(
            high_arr,
            low_arr,
            close_arr,
            fastk_period=config.STOCH_K_PERIOD,
            slowk_period=config.STOCH_D_PERIOD,
            slowd_period=config.STOCH_D_PERIOD,
        )
        cols["STOCH_K"] = sto_k
        cols["STOCH_D"] = sto_d

    if indicators_to_calculate.get("ADX", False):
        cols["ADX"] = ta.ADX(high_arr, low_arr, close_arr, timeperiod=config.ADX_PERIOD)

    if indicators_to_calculate.get("DMI", False) or indicators_to_calculate.get(
        "ATR_DMI", False
    ):
        # Fused (N2.2): replicate the df-chain bit-for-bit in one numpy pass —
        # H-L / H-PC / L-PC / TR (skip-NaN row max via fmax) / +DM / -DM, then
        # native rolling sums and the IEEE DI ratios.
        cshift = np.empty(n)
        cshift[0] = np.nan
        cshift[1:] = close_arr[:-1]
        hl = high_arr - low_arr
        hpc = np.abs(high_arr - cshift)
        lpc = np.abs(low_arr - cshift)
        tr = np.fmax(hl, np.fmax(hpc, lpc))
        cols["H-L"] = hl
        cols["H-PC"] = hpc
        cols["L-PC"] = lpc
        cols["TR"] = tr
        cols["ATR"] = wilder_atr(high_arr, low_arr, close_arr, config.ATR_PERIOD)
        dmi_period = config.DMI_PERIOD
        hshift = np.empty(n)
        hshift[0] = np.nan
        hshift[1:] = high_arr[:-1]
        lshift = np.empty(n)
        lshift[0] = np.nan
        lshift[1:] = low_arr[:-1]
        dh = high_arr - hshift  # NaN at 0 -> comparisons False -> where picks 0
        dl = lshift - low_arr
        pdm = np.where(dh > dl, np.maximum(dh, 0.0), 0.0)
        ndm = np.where(dl > dh, np.maximum(dl, 0.0), 0.0)
        cols["+DM"] = pdm
        cols["-DM"] = ndm
        tr_dmi = _vq.rolling_sum(tr, dmi_period)
        pdm_dmi = _vq.rolling_sum(pdm, dmi_period)
        ndm_dmi = _vq.rolling_sum(ndm, dmi_period)
        cols["TR_DMI"] = tr_dmi
        cols["+DM_DMI"] = pdm_dmi
        cols["-DM_DMI"] = ndm_dmi
        with np.errstate(divide="ignore", invalid="ignore"):
            cols["+DI"] = 100.0 * (pdm_dmi / tr_dmi)
            cols["-DI"] = 100.0 * (ndm_dmi / tr_dmi)

    if indicators_to_calculate.get("ATR", False) and "ATR" not in cols:
        cols["ATR"] = wilder_atr(high_arr, low_arr, close_arr, config.ATR_PERIOD)

    if indicators_to_calculate.get("VOLUME", False):
        vol_ma = _rolling_mean(volume_arr, config.VOL_MA_PERIOD)
        cols["Vol_MA"] = vol_ma
        with np.errstate(divide="ignore", invalid="ignore"):
            cols["RVOL"] = volume_arr / vol_ma
        cols["VOL_EXPAND"] = (volume_arr > config.VOL_EXPAND_MULT * vol_ma).astype(
            np.int64
        )
        d = np.empty(n)
        d[0] = np.nan
        d[1:] = close_arr[1:] - close_arr[:-1]
        obv = np.cumsum(np.nan_to_num(np.sign(d) * volume_arr, nan=0.0))
        cols["OBV"] = obv
        obv_ma = _rolling_mean(obv, config.OBV_MA_PERIOD)
        cols["OBV_MA"] = obv_ma
        s5 = np.empty(n)
        s5[:5] = np.nan
        s5[5:] = obv_ma[:-5]
        with np.errstate(divide="ignore", invalid="ignore"):
            slope = (obv_ma - s5) / s5
            slope = np.where(np.isinf(slope), 0.0, slope)
        cols["OBV_Slope"] = np.where(np.isnan(slope), 0.0, slope)

    if indicators_to_calculate.get("MOMENTUM", False):
        cols["ROC"] = _rate_of_change(close_arr, config.ROC_PERIOD)
        cols["MFI"] = _money_flow_index(
            high_arr, low_arr, close_arr, volume_arr, config.MFI_PERIOD
        )
        cols["CCI"] = ta.CCI(high_arr, low_arr, close_arr, timeperiod=config.CCI_PERIOD)

    if indicators_to_calculate.get("OSCILLATORS", False):
        cols["ULTOSC"] = ta.ULTOSC(
            high_arr,
            low_arr,
            close_arr,
            timeperiod1=config.ULTOSC_PERIOD1,
            timeperiod2=config.ULTOSC_PERIOD2,
            timeperiod3=config.ULTOSC_PERIOD3,
        )
        cols["WILLR"] = ta.WILLR(
            high_arr, low_arr, close_arr, timeperiod=config.WILLR_PERIOD
        )

    if indicators_to_calculate.get("RANGE", False):
        cols["RANGE_HIGH"] = _rolling_max(close_arr, config.BREAKOUT_LOOKBACK)
        cols["RANGE_LOW"] = _rolling_min(close_arr, config.BREAKOUT_LOOKBACK)
        cols["PULLBACK_REF_HIGH"] = _rolling_max(close_arr, config.PULLBACK_LOOKBACK)
        cols["PULLBACK_REF_LOW"] = _rolling_min(close_arr, config.PULLBACK_LOOKBACK)

    if indicators_to_calculate.get("PATTERNS", False):
        if "RANGE_HIGH" not in cols:
            cols["RANGE_HIGH"] = _rolling_max(close_arr, config.BREAKOUT_LOOKBACK)
            cols["RANGE_LOW"] = _rolling_min(close_arr, config.BREAKOUT_LOOKBACK)
            cols["PULLBACK_REF_HIGH"] = _rolling_max(
                close_arr, config.PULLBACK_LOOKBACK
            )
            cols["PULLBACK_REF_LOW"] = _rolling_min(close_arr, config.PULLBACK_LOOKBACK)
        cols["PCT_FROM_HIGH"] = close_arr / cols["RANGE_HIGH"] - 1
        cols["PCT_FROM_LOW"] = close_arr / cols["RANGE_LOW"] - 1
        cols["PCT_FROM_PB_HIGH"] = close_arr / cols["PULLBACK_REF_HIGH"] - 1
        cols["PCT_FROM_PB_LOW"] = close_arr / cols["PULLBACK_REF_LOW"] - 1
        cols["BREAKOUT_CAND"] = (
            close_arr >= (1 - config.RANGE_TOLERANCE_PCT) * cols["RANGE_HIGH"]
        ).astype(np.int64)
        cols["BREAKDOWN_CAND"] = (
            close_arr <= (1 + config.RANGE_TOLERANCE_PCT) * cols["RANGE_LOW"]
        ).astype(np.int64)

    if indicators_to_calculate.get("PERCENTILE", False):
        if "ATR" not in cols:
            cols["ATR"] = wilder_atr(high_arr, low_arr, close_arr, config.ATR_PERIOD)
        atr_pct = np.where(cols["ATR"] == 0.0, np.nan, cols["ATR"] / close_arr)
        cols["ATR_PCT"] = atr_pct
        atr_pctl = _percentile_rank(
            np.where(np.isnan(atr_pct), 0.0, atr_pct),
            config.ATR_PERCENTILE_WINDOW,
        )
        cols["ATR_PCTL"] = np.where(np.isnan(atr_pctl), 0.5, atr_pctl)

    if indicators_to_calculate.get("STATISTICAL", False):
        cols["KURTOSIS"] = _kurtosis(close_arr, config.KURTOSIS_WINDOW)
        cols["SKEWNESS"] = _skewness(close_arr, config.SKEWNESS_WINDOW)
        cols["HURST"] = _hurst_exponent(close_arr, config.HURST_WINDOW)
        cols["FRACTAL_DIM"] = _fractal_dimension(close_arr, config.FRACTAL_WINDOW)
        cols["EFFICIENCY_RATIO"] = _efficiency_ratio(
            close_arr, config.EFFICIENCY_RATIO_WINDOW
        )
        cols["CHOPPINESS"] = _choppiness_index(
            high_arr, low_arr, close_arr, config.CHOPPINESS_WINDOW
        )
        cols["AUTOCORR"] = _autocorrelation(
            close_arr, config.AUTOCORR_WINDOW, config.AUTOCORR_LAG
        )
        cols["LINEAR_SLOPE"] = _linear_regression_slope(
            close_arr, config.LINEAR_REG_WINDOW
        )
        cols["VWAP_DEV"] = _vwap_deviation(close_arr, volume_arr, config.VWAP_WINDOW)

    hilbert_window = getattr(config, "HILBERT_WINDOW", 60)
    if indicators_to_calculate.get("HILBERT", False) and getattr(
        config, "HILBERT_TRANSFORM_ENABLED", True
    ):
        ht_df = calculate_hilbert_transform(
            pd.Series(close_arr, index=df.index), window=hilbert_window
        )
        cols["H_PHASE"] = ht_df["H_PHASE"]
        cols["H_FREQ"] = ht_df["H_FREQ"]
        cols["H_AMPLITUDE"] = ht_df["H_AMPLITUDE"]

    if indicators_to_calculate.get("MICROSTRUCTURE", False):
        amihud, amihud_smooth, kyle, vpin, signed_vol, m_clv, m_vol_chg, m_illiq = (
            _microstructure(high_arr, low_arr, close_arr, volume_arr, 20)
        )
        cols["AMIHUD_ILLIQ"] = amihud
        cols["AMIHUD_SMOOTH"] = amihud_smooth
        cols["SIGNED_VOLUME"] = signed_vol
        cols["KYLE_LAMBDA"] = kyle
        cols["VPIN_PROXY"] = vpin
        cols["Micro_CLV"] = m_clv
        cols["Micro_Vol_Chg"] = m_vol_chg
        cols["Micro_Illiquidity"] = m_illiq

    out = pd.DataFrame(cols, index=df.index)
    return out.dropna()

    close_arr = df["Close"].to_numpy(dtype=np.float64)
    high_arr = df["High"].to_numpy(dtype=np.float64)
    low_arr = df["Low"].to_numpy(dtype=np.float64)
    volume_arr = df["Volume"].to_numpy(dtype=np.float64)

    if indicators_to_calculate.get("MOVING_AVERAGES", False):
        df["EMA_FAST"] = _ema(close_arr, config.EMA_FAST)
        df["EMA_MEDIUM"] = _ema(close_arr, config.EMA_MEDIUM)
        df["EMA_SLOW"] = _ema(close_arr, config.EMA_SLOW)
        df["SMA_SHORT"] = _rolling_mean(close_arr, config.SMA_SHORT)
        df["SMA_MEDIUM"] = _rolling_mean(close_arr, config.SMA_MEDIUM)
        df["SMA_LONG"] = _rolling_mean(close_arr, config.SMA_LONG)

    if indicators_to_calculate.get("BOLLINGER", False):
        df["BB_UPPER"], df["BB_MID"], df["BB_LOWER"] = ta.BBANDS(
            close_arr,
            timeperiod=config.BOLL_PERIOD,
            nbdevup=config.BOLL_STDDEV,
            nbdevdn=config.BOLL_STDDEV,
        )
        df["BB_WIDTH"] = (df["BB_UPPER"] - df["BB_LOWER"]) / df["BB_MID"]
        df["BB_POSITION"] = (df["Close"] - df["BB_LOWER"]) / (
            df["BB_UPPER"] - df["BB_LOWER"]
        )

    if indicators_to_calculate.get("RSI", False):
        df["RSI"] = _rsi(close_arr, config.RSI_PERIOD)
        df["FAST_RSI"] = _rsi(close_arr, config.FAST_RSI_PERIOD)

    if indicators_to_calculate.get("MACD", False):
        df["MACD"], df["MACD_SIGNAL"], df["MACD_HIST"] = ta.MACD(
            close_arr,
            fastperiod=config.MACD_FASTPERIOD,
            slowperiod=config.MACD_SLOWPERIOD,
            signalperiod=config.MACD_SIGNALPERIOD,
        )

    if indicators_to_calculate.get("STOCHASTIC", False):
        df["STOCH_K"], df["STOCH_D"] = ta.STOCH(
            high_arr,
            low_arr,
            close_arr,
            fastk_period=config.STOCH_K_PERIOD,
            slowk_period=config.STOCH_D_PERIOD,
            slowd_period=config.STOCH_D_PERIOD,
        )

    if indicators_to_calculate.get("ADX", False):
        df["ADX"] = ta.ADX(high_arr, low_arr, close_arr, timeperiod=config.ADX_PERIOD)

    if indicators_to_calculate.get("DMI", False) or indicators_to_calculate.get(
        "ATR_DMI", False
    ):
        df["H-L"] = df["High"] - df["Low"]
        df["H-PC"] = (df["High"] - df["Close"].shift(1)).abs()
        df["L-PC"] = (df["Low"] - df["Close"].shift(1)).abs()
        df["TR"] = df[["H-L", "H-PC", "L-PC"]].max(axis=1)
        df["ATR"] = wilder_atr(high_arr, low_arr, close_arr, config.ATR_PERIOD)
        dmi_period = config.DMI_PERIOD
        df["+DM"] = np.where(
            (df["High"] - df["High"].shift(1)) > (df["Low"].shift(1) - df["Low"]),
            np.maximum(df["High"] - df["High"].shift(1), 0),
            0,
        )
        df["-DM"] = np.where(
            (df["Low"].shift(1) - df["Low"]) > (df["High"] - df["High"].shift(1)),
            np.maximum(df["Low"].shift(1) - df["Low"], 0),
            0,
        )
        df["TR_DMI"] = df["TR"].rolling(window=dmi_period).sum()
        df["+DM_DMI"] = df["+DM"].rolling(window=dmi_period).sum()
        df["-DM_DMI"] = df["-DM"].rolling(window=dmi_period).sum()
        df["+DI"] = 100 * (df["+DM_DMI"] / df["TR_DMI"])
        df["-DI"] = 100 * (df["-DM_DMI"] / df["TR_DMI"])

    if indicators_to_calculate.get("ATR", False) and "ATR" not in df.columns:
        df["ATR"] = wilder_atr(high_arr, low_arr, close_arr, config.ATR_PERIOD)

    if indicators_to_calculate.get("VOLUME", False):
        df["Vol_MA"] = _rolling_mean(volume_arr, config.VOL_MA_PERIOD)
        df["RVOL"] = df["Volume"] / df["Vol_MA"]
        df["VOL_EXPAND"] = (
            df["Volume"] > config.VOL_EXPAND_MULT * df["Vol_MA"]
        ).astype(int)
        df["OBV"] = (np.sign(df["Close"].diff()) * df["Volume"]).fillna(0).cumsum()
        df["OBV_MA"] = _rolling_mean(df["OBV"].to_numpy(), config.OBV_MA_PERIOD)
        df["OBV_Slope"] = (
            df["OBV_MA"].pct_change(5).replace([np.inf, -np.inf], 0).fillna(0)
        )

    if indicators_to_calculate.get("MOMENTUM", False):
        df["ROC"] = _rate_of_change(close_arr, config.ROC_PERIOD)
        df["MFI"] = _money_flow_index(
            high_arr, low_arr, close_arr, volume_arr, config.MFI_PERIOD
        )
        df["CCI"] = ta.CCI(high_arr, low_arr, close_arr, timeperiod=config.CCI_PERIOD)

    if indicators_to_calculate.get("OSCILLATORS", False):
        df["ULTOSC"] = ta.ULTOSC(
            high_arr,
            low_arr,
            close_arr,
            timeperiod1=config.ULTOSC_PERIOD1,
            timeperiod2=config.ULTOSC_PERIOD2,
            timeperiod3=config.ULTOSC_PERIOD3,
        )
        df["WILLR"] = ta.WILLR(
            high_arr, low_arr, close_arr, timeperiod=config.WILLR_PERIOD
        )

    if indicators_to_calculate.get("RANGE", False):
        df["RANGE_HIGH"] = _rolling_max(close_arr, config.BREAKOUT_LOOKBACK)
        df["RANGE_LOW"] = _rolling_min(close_arr, config.BREAKOUT_LOOKBACK)
        df["PULLBACK_REF_HIGH"] = _rolling_max(close_arr, config.PULLBACK_LOOKBACK)
        df["PULLBACK_REF_LOW"] = _rolling_min(close_arr, config.PULLBACK_LOOKBACK)

    if indicators_to_calculate.get("PATTERNS", False):
        if "RANGE_HIGH" not in df.columns:
            df["RANGE_HIGH"] = _rolling_max(close_arr, config.BREAKOUT_LOOKBACK)
            df["RANGE_LOW"] = _rolling_min(close_arr, config.BREAKOUT_LOOKBACK)
            df["PULLBACK_REF_HIGH"] = _rolling_max(close_arr, config.PULLBACK_LOOKBACK)
            df["PULLBACK_REF_LOW"] = _rolling_min(close_arr, config.PULLBACK_LOOKBACK)
        df["PCT_FROM_HIGH"] = df["Close"] / df["RANGE_HIGH"] - 1
        df["PCT_FROM_LOW"] = df["Close"] / df["RANGE_LOW"] - 1
        df["PCT_FROM_PB_HIGH"] = df["Close"] / df["PULLBACK_REF_HIGH"] - 1
        df["PCT_FROM_PB_LOW"] = df["Close"] / df["PULLBACK_REF_LOW"] - 1
        df["BREAKOUT_CAND"] = (
            df["Close"] >= (1 - config.RANGE_TOLERANCE_PCT) * df["RANGE_HIGH"]
        ).astype(int)
        df["BREAKDOWN_CAND"] = (
            df["Close"] <= (1 + config.RANGE_TOLERANCE_PCT) * df["RANGE_LOW"]
        ).astype(int)

    if indicators_to_calculate.get("PERCENTILE", False):
        if "ATR" not in df.columns:
            df["ATR"] = wilder_atr(high_arr, low_arr, close_arr, config.ATR_PERIOD)
        atr_pct = (df["ATR"] / df["Close"]).replace(0, np.nan)
        df["ATR_PCT"] = atr_pct
        df["ATR_PCTL"] = _percentile_rank(
            atr_pct.fillna(0).to_numpy(), config.ATR_PERCENTILE_WINDOW
        )
        df["ATR_PCTL"] = df["ATR_PCTL"].fillna(0.5)

    if indicators_to_calculate.get("STATISTICAL", False):
        df["KURTOSIS"] = _kurtosis(close_arr, config.KURTOSIS_WINDOW)
        df["SKEWNESS"] = _skewness(close_arr, config.SKEWNESS_WINDOW)
        df["HURST"] = _hurst_exponent(close_arr, config.HURST_WINDOW)
        df["FRACTAL_DIM"] = _fractal_dimension(close_arr, config.FRACTAL_WINDOW)
        df["EFFICIENCY_RATIO"] = _efficiency_ratio(
            close_arr, config.EFFICIENCY_RATIO_WINDOW
        )
        df["CHOPPINESS"] = _choppiness_index(
            high_arr, low_arr, close_arr, config.CHOPPINESS_WINDOW
        )
        df["AUTOCORR"] = _autocorrelation(
            close_arr, config.AUTOCORR_WINDOW, config.AUTOCORR_LAG
        )
        df["LINEAR_SLOPE"] = _linear_regression_slope(
            close_arr, config.LINEAR_REG_WINDOW
        )
        df["VWAP_DEV"] = _vwap_deviation(close_arr, volume_arr, config.VWAP_WINDOW)

    hilbert_window = getattr(config, "HILBERT_WINDOW", 60)
    if indicators_to_calculate.get("HILBERT", False) and getattr(
        config, "HILBERT_TRANSFORM_ENABLED", True
    ):
        ht_df = calculate_hilbert_transform(df["Close"], window=hilbert_window)
        df["H_PHASE"] = ht_df["H_PHASE"]
        df["H_FREQ"] = ht_df["H_FREQ"]
        df["H_AMPLITUDE"] = ht_df["H_AMPLITUDE"]

    if indicators_to_calculate.get("MICROSTRUCTURE", False):
        amihud, amihud_smooth, kyle, vpin, signed_vol, m_clv, m_vol_chg, m_illiq = (
            _microstructure(high_arr, low_arr, close_arr, volume_arr, 20)
        )
        df["AMIHUD_ILLIQ"] = amihud
        df["AMIHUD_SMOOTH"] = amihud_smooth
        df["SIGNED_VOLUME"] = signed_vol
        df["KYLE_LAMBDA"] = kyle
        df["VPIN_PROXY"] = vpin
        df["Micro_CLV"] = m_clv
        df["Micro_Vol_Chg"] = m_vol_chg
        df["Micro_Illiquidity"] = m_illiq

    return df.dropna()
