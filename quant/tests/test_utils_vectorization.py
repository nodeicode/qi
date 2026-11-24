"""Parity helpers and latency regression for quant.utils.indicators.

Hard SLO (live engine bottleneck was ~2.9s p50 / ~4.8s p99 / ~66%):
  warm ``calculate_indicators`` on N=5_000 bars must finish in < 1000 ms.
"""

from __future__ import annotations

import time

import numpy as np
import pandas as pd
import pytest

from quant.utils.indicators import (
    calculate_hilbert_transform,
    calculate_indicators,
    wilder_atr,
)

LATENCY_SLO_MS = 1000.0
LIVE_PRELOAD_N = 5_000
INTRADAY_N = 50_000


def _synthetic_ohlcv(n: int, seed: int = 0) -> pd.DataFrame:
    rng = np.random.default_rng(seed)
    close = 100.0 * np.exp(np.cumsum(rng.normal(0.0, 0.01, n)))
    high = close * (1.0 + rng.uniform(0.0, 0.005, n))
    low = close * (1.0 - rng.uniform(0.0, 0.005, n))
    open_ = close * (1.0 + rng.normal(0.0, 0.001, n))
    volume = rng.integers(100_000, 1_000_000, n).astype(np.float64)
    idx = pd.date_range("2020-01-01", periods=n, freq="1min")
    return pd.DataFrame(
        {
            "Open": open_,
            "High": high,
            "Low": low,
            "Close": close,
            "Volume": volume,
        },
        index=idx,
    )


def _warm_times_ms(fn, reps: int = 7) -> list[float]:
    fn()  # discard cold / first call
    times: list[float] = []
    for _ in range(reps):
        t0 = time.perf_counter()
        fn()
        times.append((time.perf_counter() - t0) * 1000.0)
    return times


class TestIndicatorParitySmoke:
    def test_wilder_atr_finite(self):
        df = _synthetic_ohlcv(300)
        atr = wilder_atr(
            df["High"].to_numpy(),
            df["Low"].to_numpy(),
            df["Close"].to_numpy(),
            14,
        )
        assert atr.shape == (300,)
        assert np.isfinite(atr[50:]).all()

    def test_calculate_indicators_columns(self):
        df = _synthetic_ohlcv(400)
        out = calculate_indicators(df)
        for col in ("RSI", "ATR", "EMA_FAST", "H_PHASE", "HURST", "AMIHUD_ILLIQ"):
            assert col in out.columns

    def test_hilbert_causal_shape(self):
        s = _synthetic_ohlcv(200)["Close"]
        ht = calculate_hilbert_transform(s, window=60)
        assert list(ht.columns) == ["H_PHASE", "H_FREQ", "H_AMPLITUDE"]
        assert len(ht) == len(s)


@pytest.mark.latency
class TestCalculateIndicatorsLatency:
    """CI gate mirroring engine ``_last_signal_spans['strategy_calc_total_ns']``."""

    def test_warm_p50_under_1000ms_live_preload(self):
        df = _synthetic_ohlcv(LIVE_PRELOAD_N)

        def run():
            calculate_indicators(df.copy())

        times = _warm_times_ms(run, reps=7)
        p50 = float(np.median(times))
        p99 = float(np.percentile(times, 99))
        assert p50 < LATENCY_SLO_MS, (
            f"calculate_indicators warm p50={p50:.1f}ms "
            f"(p99={p99:.1f}ms, times={[round(t, 1) for t in times]}) "
            f"exceeds SLO {LATENCY_SLO_MS:.0f}ms"
        )

    def test_warm_single_call_under_1000ms(self):
        df = _synthetic_ohlcv(LIVE_PRELOAD_N)
        calculate_indicators(df.copy())  # warm
        t0 = time.perf_counter()
        calculate_indicators(df.copy())
        elapsed_ms = (time.perf_counter() - t0) * 1000.0
        assert elapsed_ms < LATENCY_SLO_MS, (
            f"warm calculate_indicators took {elapsed_ms:.1f}ms "
            f"(SLO {LATENCY_SLO_MS:.0f}ms)"
        )

    @pytest.mark.slow
    def test_intraday_50k_reports_timing(self):
        """Informational: 50k bars need not meet 1s SLO (live preload is ~5k)."""
        df = _synthetic_ohlcv(INTRADAY_N)

        def run():
            calculate_indicators(df.copy())

        times = _warm_times_ms(run, reps=3)
        p50 = float(np.median(times))
        # Soft headroom check — fail only if catastrophically slow (>15s)
        assert p50 < 15_000.0, f"N=50k p50={p50:.1f}ms unexpectedly slow"
