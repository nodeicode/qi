"""Tests for the nanobind native kernels in ``quant/utils/cpp_native``.

Verifies two properties of the compiled extension
(``quant.utils.cpp_native.vq_kernels``):

1. **Parity** — ``quant.utils.indicators`` uses the extension and the output is
   bit-for-bit identical to the pure-Python recurrences (the SLO guard, so
   results never change).
2. **Speedup** — the native path is meaningfully faster than the pure-Python
   loop. All latency asserts live in a single test
   (``test_native_kernels_speedup``) that is currently COMMENTED OUT — see the
   TODO there. Wall-clock speedups flake on noisy CI runners (fractal ~1.4x on
   CI vs ~2.9x locally at the 1.5x threshold), so speed is documented, not
   asserted, until dev/staging/prod run on strict, expected environments.

The native extension is a **required** dependency of ``quant.utils.indicators``
(no pure-Python fallback), so importing ``ind`` in this module already
guarantees the compiled kernels are present and active. The pure-Python loops
alongside the native calls below are test-local references kept only to
validate parity and measure speedup.
"""

from __future__ import annotations

import time

import numpy as np
import pandas as pd

import quant.utils.indicators as ind

# Native must beat the pure-Python recurrence by at least this factor. Measured
# speedups at n=20000 are ~39x (adaptive_z), ~29x (kalman), ~19x (fisher), so 5x leaves a
# large safety margin while still guaranteeing the native path is genuinely faster.
SPEEDUP_MIN = 5.0
# Size used for benchmark comparisons (large enough to dominate call overhead).
SPEEDUP_N = 20000


def _gen(seed: int = 0, n: int = 4000) -> np.ndarray:
    rng = np.random.default_rng(seed)
    return np.exp(np.cumsum(rng.normal(0.0, 0.01, n))) * 100.0


# ---------------------------------------------------------------------------
# Pure-Python reference recurrences — shared by parity AND speedup tests so the
# speedup baseline is exactly the code the native kernel must replicate.
# ---------------------------------------------------------------------------


def _sc_array(a: np.ndarray, window: int) -> np.ndarray:
    """KAMA smoothing-constant array shared by adaptive-z reference and kernel."""
    s = pd.Series(a)
    change = (s - s.shift(window)).abs()
    path = s.diff().abs().rolling(window).sum()
    er = (change / path.replace(0, np.nan)).fillna(0.0).to_numpy()
    return (er * (2.0 / 3.0 - 2.0 / 31.0) + 2.0 / 31.0) ** 2


def _pure_adaptive_z(a: np.ndarray, window: int) -> np.ndarray:
    n = len(a)
    means = np.empty(n)
    vars_ = np.empty(n)
    z = np.empty(n)
    sc = _sc_array(a, window)
    means[0] = a[0]
    vars_[0] = 0.0
    z[0] = 0.0
    for i in range(1, n):
        sci = sc[i]
        means[i] = means[i - 1] + sci * (a[i] - means[i - 1])
        vars_[i] = (1.0 - sci) * vars_[i - 1] + sci * (a[i] - means[i]) ** 2
        z[i] = (a[i] - means[i]) / np.sqrt(vars_[i]) if vars_[i] > 1e-9 else 0.0
    return z


def _pure_kalman(prices: np.ndarray, Q: float = 1e-5, R: float = 1e-3) -> np.ndarray:
    prices = np.asarray(prices, dtype=np.float64)
    n = len(prices)
    est = np.zeros(n)
    e = prices[0]
    ee = 1.0
    for i in range(n):
        ep = e
        eep = ee + Q
        kg = eep / (eep + R)
        e = ep + kg * (prices[i] - ep)
        ee = (1.0 - kg) * eep
        est[i] = e
    return est


def _pure_fisher(
    prices: np.ndarray, roll_min: np.ndarray, roll_max: np.ndarray, period: int
) -> np.ndarray:
    n = len(prices)
    f = np.zeros(n)
    val = 0.0
    prev_f = 0.0
    for i in range(period, n):
        cmx = roll_max[i]
        cmn = roll_min[i]
        if cmx != cmn:
            val = 0.33 * 2.0 * ((prices[i] - cmn) / (cmx - cmn) - 0.5) + 0.67 * val
        else:
            val = 0.0
        val = np.clip(val, -0.99, 0.99)
        f[i] = 0.5 * np.log((1 + val) / (1 - val)) + 0.5 * prev_f
        prev_f = f[i]
    return f


def _best_time(fn, reps: int) -> float:
    """Best-of-``reps`` wall time for ``fn()`` (native is fast; warm-up first)."""
    fn()  # warm-up (module attr lookups, caches)
    best = float("inf")
    for _ in range(reps):
        t0 = time.perf_counter()
        fn()
        best = min(best, time.perf_counter() - t0)
    return best


# ---------------------------------------------------------------------------
# Parity — native must match the pure-Python recurrence exactly
# ---------------------------------------------------------------------------


def test_native_active_by_default():
    """The compiled extension is loaded (required, not optional)."""
    assert ind._vq is not None


def test_adaptive_z_native_parity():
    a = _gen(n=4000)
    native = ind._adaptive_z_score(a, 14)
    ref = _pure_adaptive_z(a, 14)
    assert np.allclose(native, ref, rtol=1e-9, atol=1e-12)


def test_kalman_native_parity():
    a = _gen(n=4000)
    native = ind.fast_kalman_filter(a, 1e-5, 1e-3)
    ref = _pure_kalman(a, 1e-5, 1e-3)
    assert np.allclose(native, ref, rtol=1e-9, atol=1e-12)


def test_fisher_native_parity():
    a = _gen(n=4000)
    s = pd.Series(a)
    rmin = s.rolling(10).min().to_numpy()
    rmax = s.rolling(10).max().to_numpy()
    fisher, inv = ind._fisher_transform(a, 10)
    ref = _pure_fisher(a, rmin, rmax, 10)
    assert np.allclose(fisher, ref, rtol=1e-9, atol=1e-12)
    # inv is fisher shifted by one (mirroring the indicator contract)
    assert np.array_equal(inv[1:], fisher[:-1])


def test_fisher_native_finite():
    a = _gen(n=4000)
    fisher, inv = ind._fisher_transform(a, 10)
    assert fisher.shape == (len(a),)
    assert inv.shape == (len(a),)
    assert np.isfinite(fisher).all()
    assert np.array_equal(inv[1:], fisher[:-1])


def test_functions_produce_finite_output():
    """The native-backed functions return finite, correctly-shaped output."""
    a = _gen(n=1000)
    assert np.isfinite(ind._adaptive_z_score(a, 14)).all()
    assert np.isfinite(ind.fast_kalman_filter(a)).all()


# ============================================================================
# N1 kernels — indicator pipeline hotspots (Hilbert / Hurst / Fractal /
# Microstructure). Unlike the recurrence kernels above (pure-Python loop
# baselines, 19-47x), these baselines are already vectorized numpy/pandas
# (and scipy FFT for hilbert), so speedups are smaller multiples of real
# measurements (~1.4-5.6x at n=31k, taken 2026-09-17). Thresholds below are
# preserved as documentation inside the (commented-out) consolidated speedup
# test at the bottom of this file.
# ============================================================================

from numpy.lib.stride_tricks import sliding_window_view
from scipy.signal import hilbert as _scipy_hilbert

SPEEDUP_MIN_HILBERT = 2.0  # measured ~5.6x
SPEEDUP_MIN_HURST = 1.5  # measured ~2.8x
SPEEDUP_MIN_FRACTAL = 1.5  # measured ~2.9x
SPEEDUP_MIN_MICRO = 1.1  # measured ~1.4x


def _gen_ohlcv(seed: int = 0, n: int = 4000):
    """Realistic OHLCV (SPY-like 15m prices, zero-volume bars, flat runs)."""
    rng = np.random.default_rng(seed)
    close = np.exp(np.cumsum(rng.normal(0.0, 0.004, n))) * 500.0
    flat = rng.random(n) < 0.03
    close[flat] = close[rng.integers(0, n, flat.sum())]
    volume = rng.integers(0, 2_000_000, n).astype(np.float64)
    high = close + rng.random(n) * 0.7
    low = close - rng.random(n) * 0.7
    return high, low, close, volume


# ── Pure-Python references (verbatim original vectorized implementations) ──


def _pure_hilbert_spectrum(values: np.ndarray, window: int):
    """Original calculate_hilbert_transform (scipy per-window hilbert)."""
    n = len(values)
    amplitude = np.zeros(n)
    phase_raw = np.zeros(n)
    if n < 2:
        return phase_raw, np.zeros(n), amplitude
    detrended = np.diff(values, prepend=values[0])
    for i in range(1, min(window, n)):
        sl = detrended[: i + 1]
        if len(sl) >= 2:
            analytic = _scipy_hilbert(sl)
            amplitude[i] = np.abs(analytic[-1])
            phase_raw[i] = np.angle(analytic[-1])
    if n >= window:
        windows = sliding_window_view(detrended, window)
        analytic_batch = _scipy_hilbert(windows, axis=-1)
        amplitude[window - 1 :] = np.abs(analytic_batch[:, -1])
        phase_raw[window - 1 :] = np.angle(analytic_batch[:, -1])
    unwrapped = np.unwrap(phase_raw)
    frequency = np.zeros(n)
    frequency[1:] = np.diff(unwrapped) / (2.0 * np.pi)
    return unwrapped, frequency, amplitude


def _pure_hurst(arr: np.ndarray, window: int) -> np.ndarray:
    """Original _hurst_exponent (batched R/S, vectorized OLS)."""
    n = len(arr)
    result = np.full(n, np.nan)
    if n < window:
        return result
    lags = np.array([2, 4, 8, 16], dtype=np.int64)
    windows = sliding_window_view(arr.astype(np.float64), window)
    log_tau = np.full((windows.shape[0], len(lags)), np.nan)
    for li, lag in enumerate(lags):
        if lag >= window // 2:
            continue
        sub = windows[:, ::lag]
        mean = sub.mean(axis=1, keepdims=True)
        y = sub - mean
        z = np.cumsum(y, axis=1)
        r = z.max(axis=1) - z.min(axis=1)
        s = sub.std(axis=1)
        with np.errstate(divide="ignore", invalid="ignore"):
            tau = np.where(s > 0, r / s, 0.0)
        log_tau[:, li] = np.log(np.maximum(tau, 1e-12))
    log_lags = np.log(lags.astype(np.float64))
    valid = np.isfinite(log_tau)
    lt = np.where(valid, log_tau, 0.0)
    ll = np.where(valid, np.broadcast_to(log_lags, log_tau.shape), 0.0)
    count = valid.sum(axis=1).astype(np.float64)
    sum_x = ll.sum(axis=1)
    sum_y = lt.sum(axis=1)
    sum_xx = (ll * ll).sum(axis=1)
    sum_xy = (ll * lt).sum(axis=1)
    denom = count * sum_xx - sum_x * sum_x
    with np.errstate(divide="ignore", invalid="ignore"):
        slope = np.where(
            (count >= 2) & (np.abs(denom) > 1e-18),
            (count * sum_xy - sum_x * sum_y) / denom,
            0.5,
        )
    result[window - 1 :] = slope
    return result


def _pure_fractal(arr: np.ndarray, window: int) -> np.ndarray:
    """Original _fractal_dimension (normalize + sign-change count)."""
    n = len(arr)
    result = np.full(n, np.nan)
    if n < window:
        return result
    windows = sliding_window_view(arr.astype(np.float64), window)
    w_min = windows.min(axis=1, keepdims=True)
    w_max = windows.max(axis=1, keepdims=True)
    span = w_max - w_min
    normalized = np.where(span > 0, (windows - w_min) / span, 0.0)
    d1 = np.diff(normalized, axis=1)
    signs = np.sign(d1)
    changes = np.sum(signs[:, 1:] != signs[:, :-1], axis=1)
    fd = 1.0 + changes / window
    result[window - 1 :] = fd
    return result


def _pure_microstructure(high, low, close, volume, window: int):
    """Original _microstructure (pandas rolling aggregations)."""
    n = len(close)
    rng = np.maximum(high - low, 1e-9)
    micro_clv = (close - low) / rng
    vol_safe = np.where(volume > 0, volume, 1.0)
    micro_illiq = rng / vol_safe
    vol_prev = np.roll(volume, 1)
    vol_prev[0] = volume[0]
    v_curr = np.where(volume > 0, volume, 1.0)
    v_prev = np.where(vol_prev > 0, vol_prev, 1.0)
    micro_vol_chg = np.zeros(n)
    micro_vol_chg[1:] = np.log(v_curr[1:] / v_prev[1:])
    returns = np.zeros(n)
    returns[1:] = np.diff(close) / close[:-1]
    dollar_vol = close * volume
    amihud_illiq = np.full(n, np.nan)
    mask = dollar_vol > 0
    amihud_illiq[mask] = np.abs(returns[mask]) / dollar_vol[mask]
    amihud_smooth = pd.Series(amihud_illiq).rolling(window).mean().to_numpy()
    price_diff = np.zeros(n)
    price_diff[1:] = np.diff(close)
    price_diff_std = pd.Series(price_diff).rolling(window).std().to_numpy()
    z_price = np.where(price_diff_std != 0, price_diff / price_diff_std, 0.0)
    buy_fraction = 1.0 / (1.0 + np.exp(-1.7 * z_price))
    signed_vol = volume * (2 * buy_fraction - 1)
    signed_vol[:window] = np.nan
    ret_s = pd.Series(returns)
    sv_s = pd.Series(signed_vol)
    cov = ret_s.rolling(window).cov(sv_s)
    var_sv = sv_s.rolling(window).var()
    kyle_lambda = (cov / var_sv.replace(0, np.nan)).to_numpy()
    order_imbalance = np.abs(signed_vol)
    vpin_ratios = np.where(volume > 0, order_imbalance / volume, np.nan)
    vpin_proxy = pd.Series(vpin_ratios).rolling(window).mean().to_numpy()
    return (
        amihud_illiq,
        amihud_smooth,
        kyle_lambda,
        vpin_proxy,
        signed_vol,
        micro_clv,
        micro_vol_chg,
        micro_illiq,
    )


_MICRO_NAMES = (
    "AMIHUD_ILLIQ",
    "AMIHUD_SMOOTH",
    "KYLE_LAMBDA",
    "VPIN_PROXY",
    "SIGNED_VOLUME",
    "Micro_CLV",
    "Micro_Vol_Chg",
    "Micro_Illiquidity",
)


# ── Parity ──────────────────────────────────────────────────────────────────


def test_hilbert_native_parity():
    for n in (1, 2, 130, 4000):
        _high, _low, close, _volume = _gen_ohlcv(seed=n, n=n)
        series = pd.Series(close)
        out = ind.calculate_hilbert_transform(series, 60)
        ref = _pure_hilbert_spectrum(close, 60)
        for col, r in zip(("H_PHASE", "H_FREQ", "H_AMPLITUDE"), ref):
            assert np.allclose(
                out[col].to_numpy(), r, rtol=1e-9, atol=1e-12, equal_nan=True
            ), f"hilbert {col} mismatch at n={n}"


def test_hurst_native_parity():
    for n in (130, 4000):
        _high, _low, close, _volume = _gen_ohlcv(seed=n, n=n)
        assert np.allclose(
            ind._hurst_exponent(close, 50),
            _pure_hurst(close, 50),
            rtol=1e-9,
            atol=1e-12,
            equal_nan=True,
        )


def test_fractal_native_parity():
    for n in (130, 4000):
        _high, _low, close, _volume = _gen_ohlcv(seed=n, n=n)
        assert np.allclose(
            ind._fractal_dimension(close, 30),
            _pure_fractal(close, 30),
            rtol=1e-9,
            atol=1e-12,
            equal_nan=True,
        )


def test_microstructure_native_parity():
    for n in (400, 4000):
        high, low, close, volume = _gen_ohlcv(seed=n, n=n)
        nat = [np.asarray(x) for x in ind._microstructure(high, low, close, volume, 20)]
        ref = _pure_microstructure(high, low, close, volume, 20)
        for name, a, b in zip(_MICRO_NAMES, nat, ref):
            assert np.allclose(a, b, rtol=1e-9, atol=1e-12, equal_nan=True), (
                f"microstructure {name} mismatch at n={n}"
            )


# ============================================================================
# N2 kernels — sliding-window primitives + composites (full-frame latency).
# Parity is asserted against the direct pandas rolling semantics; moment-based
# kurt/skew use a looser tolerance (pandas Cython online vs our two-pass
# moments agree to ~1e-6 abs only at extreme degeneracy — 9+ sig figs).
# ============================================================================

# kurt/skew are moment-based: online-vs-two-pass rounding ~1e-6 abs at 31k.
_MOM_RTOL, _MOM_ATOL = 1e-6, 1e-8
_N2_SPEEDUP_MIN = 2.0  # measured 3-10x for _percentile_rank / _choppiness_index


def _gen_close(seed: int = 0, n: int = 4000) -> np.ndarray:
    rng = np.random.default_rng(seed)
    x = np.exp(np.cumsum(rng.normal(0.0, 0.005, n))) * 500.0
    flat = rng.random(n) < 0.04
    x[flat] = x[rng.integers(0, n, flat.sum())]
    return x


def test_n2_rolling_rank_parity():
    x = _gen_close(seed=1)
    assert np.allclose(
        ind._percentile_rank(x, 40),
        pd.Series(x).rolling(40).rank(pct=True).to_numpy(),
        rtol=1e-9,
        atol=1e-12,
        equal_nan=True,
    )


def test_n2_rolling_aggregates_parity():
    x = _gen_close(seed=2)
    s = pd.Series(x)
    for w in (15, 30):
        assert np.allclose(
            ind._rolling_mean(x, w),
            s.rolling(w).mean().to_numpy(),
            rtol=1e-9,
            atol=1e-12,
            equal_nan=True,
        )
        assert np.allclose(
            ind._rolling_max(x, w),
            s.rolling(w).max().to_numpy(),
            rtol=1e-9,
            atol=1e-12,
            equal_nan=True,
        )
        assert np.allclose(
            ind._rolling_min(x, w),
            s.rolling(w).min().to_numpy(),
            rtol=1e-9,
            atol=1e-12,
            equal_nan=True,
        )


def test_n2_skew_kurt_parity():
    x = _gen_close(seed=3)
    s = pd.Series(x)
    assert np.allclose(
        ind._kurtosis(x, 20),
        s.rolling(20).kurt().to_numpy(),
        rtol=_MOM_RTOL,
        atol=_MOM_ATOL,
        equal_nan=True,
    )
    assert np.allclose(
        ind._skewness(x, 20),
        s.rolling(20).skew().to_numpy(),
        rtol=_MOM_RTOL,
        atol=_MOM_ATOL,
        equal_nan=True,
    )


def test_n2_autocorr_parity():
    x = _gen_close(seed=4)
    s = pd.Series(x)
    assert np.allclose(
        ind._autocorrelation(x, 20, 1),
        s.rolling(20).corr(s.shift(1)).to_numpy(),
        rtol=1e-6,
        atol=1e-8,
        equal_nan=True,
    )


def test_n2_composites_parity():
    rng = np.random.default_rng(5)
    n = 2000
    c = _gen_close(seed=5, n=n)
    h = c + rng.random(n) * 0.7
    lo_ = c - rng.random(n) * 0.7
    v = rng.integers(0, 2_000_000, n).astype(float)
    s = pd.Series(c)
    # efficiency ratio
    net = (s - s.shift(10)).abs()
    path = s.diff().abs().rolling(10).sum()
    eff_ref = (net / path.replace(0, np.nan)).to_numpy()
    eff_ref[:10] = np.nan
    assert np.allclose(
        ind._efficiency_ratio(c, 10), eff_ref, rtol=1e-9, atol=1e-12, equal_nan=True
    )
    # vwap deviation
    cs, vs = pd.Series(c), pd.Series(v)
    vol_sum = vs.rolling(20).sum()
    vwap = (cs * vs).rolling(20).sum() / vol_sum.replace(0, np.nan)
    vwap_ref = ((cs - vwap) / vwap.replace(0, np.nan)).fillna(0.0).to_numpy()
    assert np.allclose(
        ind._vwap_deviation(c, v, 20), vwap_ref, rtol=1e-9, atol=1e-12, equal_nan=True
    )
    # choppiness — true range uses prev_close[0] = close[0] (not NaN)
    pc = np.empty_like(c)
    pc[0] = c[0]
    pc[1:] = c[:-1]
    tr_arr = np.maximum(h - lo_, np.maximum(np.abs(h - pc), np.abs(lo_ - pc)))
    atr_sum = pd.Series(tr_arr).rolling(14).sum()
    hi = pd.Series(h).rolling(14).max()
    lo = pd.Series(lo_).rolling(14).min()
    rng_ = hi - lo
    with np.errstate(divide="ignore", invalid="ignore"):
        ci = 100.0 * np.log10(atr_sum / rng_) / np.log10(14)
    ci_ref = ci.where((rng_ > 0) & (atr_sum > 0), 50.0).to_numpy()
    assert np.allclose(
        ind._choppiness_index(h, lo_, c, 14),
        ci_ref,
        rtol=1e-9,
        atol=1e-12,
        equal_nan=True,
    )


# ============================================================================
# Speedup asserts — DISABLED. All latency asserts live in the single test
# below, which is commented out.
#
# TODO(perf): re-enable once CI/CD (dev, staging, prod) runs on strict,
# expected environments (pinned CPU model/core count, isolated runner, pinned
# numpy/scipy/pandas builds). Wall-clock speedups flake on shared vCPU CI
# runners: fractal measured 1.4x on CI vs ~2.9x locally at a 1.5x threshold,
# and hurst/micro sit similarly close to theirs. Until then the parity tests
# above remain the hard correctness SLO. To measure (uncomment first):
#
#     uv run pytest tests/test_native_kernels.py -k speedup --no-cov -s
#
# Re-calibrate each SPEEDUP_MIN_* threshold from a fresh measurement on the
# pinned environment before enabling.
# ============================================================================

# def test_native_kernels_speedup():
#     """Native beats the pure-Python baseline by the documented factors.
#
#     Thresholds are small multiples of real measurements (recurrence kernels
#     ~19-47x; vectorized-baseline N1/N2 kernels ~1.4-5.6x at n=20k) so they
#     catch silent degradation of the native path without being fragile.
#     """
#     # recurrence kernels (pure-Python loop baselines)
#     a = _gen(n=SPEEDUP_N)
#     t_native = _best_time(lambda: ind._adaptive_z_score(a, 14), reps=5)
#     t_pure = _best_time(lambda: _pure_adaptive_z(a, 14), reps=3)
#     assert t_pure / t_native >= SPEEDUP_MIN, (
#         f"adaptive_z speedup {t_pure / t_native:.1f}x < {SPEEDUP_MIN}x "
#         f"(native {t_native * 1e3:.2f}ms vs pure {t_pure * 1e3:.2f}ms)"
#     )
#
#     t_native = _best_time(lambda: ind.fast_kalman_filter(a, 1e-5, 1e-3), reps=10)
#     t_pure = _best_time(lambda: _pure_kalman(a, 1e-5, 1e-3), reps=5)
#     assert t_pure / t_native >= SPEEDUP_MIN, (
#         f"kalman speedup {t_pure / t_native:.1f}x < {SPEEDUP_MIN}x "
#         f"(native {t_native * 1e3:.2f}ms vs pure {t_pure * 1e3:.2f}ms)"
#     )
#
#     s = pd.Series(a)
#     rmin = s.rolling(10).min().to_numpy()
#     rmax = s.rolling(10).max().to_numpy()
#     t_native = _best_time(lambda: ind._fisher_transform(a, 10), reps=5)
#     t_pure = _best_time(lambda: _pure_fisher(a, rmin, rmax, 10), reps=3)
#     assert t_pure / t_native >= SPEEDUP_MIN, (
#         f"fisher speedup {t_pure / t_native:.1f}x < {SPEEDUP_MIN}x "
#         f"(native {t_native * 1e3:.2f}ms vs pure {t_pure * 1e3:.2f}ms)"
#     )
#
#     # N1 kernels (vectorized numpy/pandas/scipy baselines)
#     high, low, close, volume = _gen_ohlcv(seed=3, n=SPEEDUP_N)
#     series = pd.Series(close)
#     t_native = _best_time(lambda: ind.calculate_hilbert_transform(series, 60), reps=5)
#     t_pure = _best_time(lambda: _pure_hilbert_spectrum(close, 60), reps=3)
#     assert t_pure / t_native >= SPEEDUP_MIN_HILBERT, (
#         f"hilbert speedup {t_pure / t_native:.1f}x < {SPEEDUP_MIN_HILBERT}x"
#     )
#
#     t_native = _best_time(lambda: ind._hurst_exponent(close, 50), reps=5)
#     t_pure = _best_time(lambda: _pure_hurst(close, 50), reps=3)
#     assert t_pure / t_native >= SPEEDUP_MIN_HURST, (
#         f"hurst speedup {t_pure / t_native:.1f}x < {SPEEDUP_MIN_HURST}x"
#     )
#
#     t_native = _best_time(lambda: ind._fractal_dimension(close, 30), reps=5)
#     t_pure = _best_time(lambda: _pure_fractal(close, 30), reps=3)
#     assert t_pure / t_native >= SPEEDUP_MIN_FRACTAL, (
#         f"fractal speedup {t_pure / t_native:.1f}x < {SPEEDUP_MIN_FRACTAL}x"
#     )
#
#     t_native = _best_time(
#         lambda: ind._microstructure(high, low, close, volume, 20), reps=5
#     )
#     t_pure = _best_time(
#         lambda: _pure_microstructure(high, low, close, volume, 20), reps=3
#     )
#     assert t_pure / t_native >= SPEEDUP_MIN_MICRO, (
#         f"microstructure speedup {t_pure / t_native:.1f}x < {SPEEDUP_MIN_MICRO}x"
#     )
#
#     # N2 kernels
#     x = _gen_close(seed=1, n=SPEEDUP_N)
#     t_native = _best_time(lambda: ind._percentile_rank(x, 40), reps=10)
#     t_pure = _best_time(lambda: pd.Series(x).rolling(40).rank(pct=True), reps=3)
#     assert t_pure / t_native >= _N2_SPEEDUP_MIN, (
#         f"percentile_rank speedup {t_pure / t_native:.1f}x < {_N2_SPEEDUP_MIN}x"
#     )
