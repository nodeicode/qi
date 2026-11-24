---
description: "Use when reviewing ML feature engineering, walk-forward training, or label generation code. Covers anti-lookahead rules, feature conventions, and the walk-forward pipeline."
applyTo: utils/ml_utils.py, strategies/**/utils.py, strategies/**/features.py
---

# ML Feature & Walk-Forward Standards

## Walk-Forward Pipeline

1. **Feature computation** — hourly/daily indicators (1000+ samples typical).
2. **Warmup skip** — drop first `WARMUP_DAYS` (e.g., 60 bars) to stabilize rolling indicators.
3. **Feature pruning** — XGBoost importance on **initial window only** (`WF_INITIAL`). Select top-N features (`N_TOP_FEATURES`).
4. **Walk-forward loop**:
   - Train: `[i, i + WF_WINDOW]`
   - Embargo: `[i + WF_WINDOW, i + WF_WINDOW + WF_EMBARGO]`
   - Test: `[i + WF_WINDOW + WF_EMBARGO, ...]`
   - Step forward by `WF_STEP`
5. **Per-target XGBoost** — separate model per target with per-target configs (`TARGET_CONFIG`).
6. **Optional Platt calibration** on test fold.
7. **Output** — OOS probabilities aligned to daily dates.

## Anti-Lookahead Rules (Critical)

These are the most common sources of data leakage in quantitative ML. **Every PR must be checked against this list:**

- Feature pruning must use only the first `WF_INITIAL` window — never full-sample importance.
- Labels must be forward-shifted (`shift(-1)`) so features don't include their own outcome bar.
- `WF_EMBARGO` must be >= max label horizon to break autocorrelation between train/test.
- HMM (`hmmlearn`) forward-backward fits the entire series — do **not** use `USE_HMM=True` unless the fit is strictly expanding-window.
- Centered rolling windows (e.g., `rolling().apply(center=True)`) are forbidden — use causal (trailing) windows only.
- Avoid `.fillna(method='bfill')` on features — backfill propagates future values.

## Feature Naming Conventions

| Category            | Prefix/Pattern        | Examples                                |
| ------------------- | --------------------- | --------------------------------------- |
| Hourly rolling      | descriptive           | `atr_7h`, `rsi_14h`, `vol_ratio`        |
| Session context     | `session_` or `gap_`  | `gap_pct`, `session_cum_range`          |
| Previous day        | `prev_`               | `prev_session_range`, `prev_day_return` |
| Probability outputs | `P_`                  | `P_next_range_narrow`, `P_vol_crush`    |
| Regime              | `regime_` or `Regime` | `Regime`, `regime_z_score`              |

## Feature Utilities (`utils/ml_utils.py`)

- **Volume Profile**: `compute_volume_profile()` → VP_POC, VP_VAH, VP_VAL, VP_Width (Numba-accelerated).
- **Fair Value Gaps**: `detect_fair_value_gaps()` → bullish/bearish gaps with ATR-normalized sizing.
- **Support/Resistance**: `detect_sr_levels()` → swing pivots with clustering. Pivot lookahead fixed by shifting confirmation bar.
- **Level Proximity**: `compute_level_features()` → ATR-normalized distances to nearest levels.
- **Context Features**: `compute_context_features()` → RSI, ADX, GARCH vol, efficiency ratio, etc.

## Target Categories (v3 ensemble)

| Category    | Purpose                   | Count      |
| ----------- | ------------------------- | ---------- |
| CONTAINMENT | "Sell premium" confidence | 17 targets |
| EXPANSION   | "Kill-switch" veto        | 7 targets  |

Entry gate: `P_contain > threshold AND P_expand < veto_threshold`.

## Per-Target Config

Each target can have independent `window` and `platt` settings in `TARGET_CONFIG`:

```python
TARGET_CONFIG = {
    "combo_sell_premium": {"window": 180, "platt": True},
    "vol_crush": {"window": 252, "platt": False},
}
```
