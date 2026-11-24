"""Type stubs for the compiled ``quant.utils.cpp_native.vq_kernels`` extension.

The runtime module is ``vq_kernels.cpython-<ver>-<platform>.so``, built in-place
by ``__init__.py`` on first import and deliberately gitignored (see
``utils/cpp_native/.gitignore``). Static checkers (ty) cannot introspect the
compiled binary, so this stub mirrors the ``NB_MODULE`` exports in
``vq_kernels.cpp`` so ``import quant.utils.cpp_native.vq_kernels`` and call
sites in ``quant.utils.indicators`` type-check without the build artifact.

All kernels take 1-D float64 arrays (callers cast with ``astype(np.float64)``)
and return a fresh 1-D float64 ndarray of the same length.
"""

import numpy as np

def adaptive_z_scores(a: np.ndarray, sc: np.ndarray) -> np.ndarray: ...
def kalman_filter(
    prices: np.ndarray,
    Q: float = 1e-5,
    R: float = 1e-3,
) -> np.ndarray: ...
def fisher_core(
    prices: np.ndarray,
    roll_min: np.ndarray,
    roll_max: np.ndarray,
    period: int,
) -> np.ndarray: ...
def hilbert_spectrum(
    values: np.ndarray,
    window: int,
) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    """Causal Hilbert spectrum (unwrapped phase, frequency, amplitude)."""
    ...

def hurst_exponent(a: np.ndarray, window: int) -> np.ndarray:
    """Rolling Hurst exponent (R/S at lags {2,4,8,16}, OLS slope)."""
    ...

def fractal_dimension(a: np.ndarray, window: int) -> np.ndarray:
    """Rolling fractal dimension (direction-change proxy)."""
    ...

def microstructure(
    high: np.ndarray,
    low: np.ndarray,
    close: np.ndarray,
    volume: np.ndarray,
    window: int,
) -> tuple[
    np.ndarray,
    np.ndarray,
    np.ndarray,
    np.ndarray,
    np.ndarray,
    np.ndarray,
    np.ndarray,
    np.ndarray,
]:
    """Microstructure block: (amihud_illiq, amihud_smooth, kyle_lambda,
    vpin_proxy, signed_vol, micro_clv, micro_vol_chg, micro_illiq)."""
    ...

def rolling_rank_pct(a: np.ndarray, window: int) -> np.ndarray:
    """Average-rank percentile of the last value of each window."""
    ...

def rolling_sum(a: np.ndarray, window: int) -> np.ndarray: ...
def rolling_mean(a: np.ndarray, window: int) -> np.ndarray: ...
def sliding_max(a: np.ndarray, window: int) -> np.ndarray: ...
def sliding_min(a: np.ndarray, window: int) -> np.ndarray: ...
def rolling_skew(a: np.ndarray, window: int) -> np.ndarray: ...
def rolling_kurt(a: np.ndarray, window: int) -> np.ndarray: ...
def rolling_autocorr(a: np.ndarray, window: int, lag: int) -> np.ndarray: ...
