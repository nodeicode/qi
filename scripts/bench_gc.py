#!/usr/bin/env python3
"""bench_gc.py — local, instance-agnostic GC-mode benchmark for the compute stages.

Measures `calculate_indicators` + `generate_signals` under three GC modes:
  baseline — GC left on (current production behavior)
  freeze   — gc.freeze() after the warmup (stage-2/3 candidate, currently live)
  disable  — gc.disable() around the hot path (stage-3 candidate, suspend_gc)

Run ONE mode per invocation (fresh process => clean GC state):
  python scripts/bench_gc.py --mode baseline --rows 20000 --warmup 2 --iters 10
  python scripts/bench_gc.py --mode freeze ...
  python scripts/bench_gc.py --mode disable ...

This is the measured basis for the GC-mode decision; the winner decides
whether to rely on the default GC or enable the suspend_gc() hot-path toggle.
"""

import argparse
import gc
import json
import statistics
import time

import numpy as np
import pandas as pd


def build_frame(rows: int) -> pd.DataFrame:
    """Synthetic 15m OHLCV frame exercising the same pandas machinery as live."""
    idx = pd.date_range("2025-01-01", periods=rows, freq="15min", tz="UTC")
    rng = np.random.default_rng(42)
    close = 100 * np.exp(np.cumsum(rng.normal(0, 0.002, rows)))
    op = np.roll(close, 1)
    op[0] = close[0]
    hi = np.maximum(op, close) * (1 + np.abs(rng.normal(0, 0.001, rows)))
    lo = np.minimum(op, close) * (1 - np.abs(rng.normal(0, 0.001, rows)))
    vol = rng.integers(100_000, 5_000_000, rows)
    df = pd.DataFrame(
        {"Open": op, "High": hi, "Low": lo, "Close": close, "Volume": vol}
    )
    df.index = idx
    return df


def stats_us(arr):
    s = sorted(arr)
    n = len(s)
    p95 = s[min(int(0.95 * n), n - 1)]
    return {
        "median_ms": round(statistics.median(arr) / 1e3, 3),
        "p95_ms": round(p95 / 1e3, 3),
        "mean_ms": round(statistics.mean(arr) / 1e3, 3),
    }


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--mode", choices=["baseline", "freeze", "disable"], required=True)
    ap.add_argument("--rows", type=int, default=20000)
    ap.add_argument("--warmup", type=int, default=2)
    ap.add_argument("--iters", type=int, default=10)
    a = ap.parse_args()

    from quant.strategies import get_strategy

    strategy = get_strategy("example_equity")()
    df = build_frame(a.rows)

    # Warmup — mirrors live warm-then-tick so the strategy caches are hot before
    # any measurement (and before gc.freeze() freezes them).
    for _ in range(a.warmup):
        strategy.calculate_indicators(df, ticker="SPY")
        strategy.generate_signals(strategy.calculate_indicators(df, ticker="SPY"))

    if a.mode == "freeze":
        gc.freeze()
    elif a.mode == "disable":
        gc.disable()

    calc_us, sig_us = [], []
    try:
        for _ in range(a.iters):
            t0 = time.perf_counter()
            df_ind = strategy.calculate_indicators(df, ticker="SPY")
            t1 = time.perf_counter()
            strategy.generate_signals(df_ind)
            t2 = time.perf_counter()
            calc_us.append((t1 - t0) * 1e6)
            sig_us.append((t2 - t1) * 1e6)
    finally:
        if a.mode == "disable":
            gc.enable()

    print(
        json.dumps(
            {
                "mode": a.mode,
                "rows": a.rows,
                "warmup": a.warmup,
                "iters": a.iters,
                "calculate_indicators": stats_us(calc_us),
                "generate_signals": stats_us(sig_us),
            }
        )
    )


if __name__ == "__main__":
    main()
