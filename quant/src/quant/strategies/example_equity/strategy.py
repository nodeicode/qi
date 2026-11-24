"""example_equity — the public reference strategy.

A deliberately simple moving-average crossover on the equity close. It exists
to (a) make the engine/backtest/live pipeline runnable out-of-the-box without
any private alpha, and (b) act as the canonical example for ``docs/
extensibility.md`` (add-a-strategy recipe). It is **not** tuned for
profitability — it is a harness, not an edge.

Contract (StrategyProtocol):
  * ``config``  — ``ExampleEquityConfig(BaseStrategyConfig)``
  * ``calculate_indicators(df, **kw) -> df``
  * ``generate_signals(df) -> df``  (columns ``Signal``, ``Confidence``,
    ``Reason`` as the engine/executors expect)
  * ``manage_trade(position, df) -> int``
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import ClassVar

import numpy as np
import pandas as pd

from quant.utils.strategy_config import BaseStrategyConfig

# Column names this strategy injects; the engine uses these to bind indicators
# into manage frames.
_FAST = "MA_FAST"
_SLOW = "MA_SLOW"


@dataclass
class ExampleEquityConfig(BaseStrategyConfig):
    """Equity MA-cross configuration."""

    INTERVAL: str = "1h"
    MANAGE_INTERVAL: str = "1h"
    PRE_LOAD_DAYS: int = 60

    INITIAL_CAPITAL: float = 100_000.0
    CAPITAL_PER_TRADE: float = 0.95
    MAX_CONCURRENT_POSITIONS: int = 1

    STOP_LOSS_PERCENT: float = 0.05
    TAKE_PROFIT_PERCENT: float = -1.0  # disabled
    USE_TRAIL_LOSS: bool = False
    MAX_HOLDING_MINUTES: int = 60 * 24 * 30

    MIN_SIGNAL_CONFIDENCE: float = 0.0
    BASE_SLIPPAGE_FACTOR: float = 0.001

    TICKERS: list[str] = field(default_factory=lambda: ["SPY"])

    # moving-average windows
    FAST_WINDOW: int = 10
    SLOW_WINDOW: int = 30

    # SIP / feed delay: zero by default (see base class).
    DATA_DELAY: int = 0


class ExampleEquityStrategy:
    """Moving-average crossover on the equity close."""

    INDICATOR_COLUMNS: ClassVar[list[str]] = [_FAST, _SLOW]

    def __init__(self, config: ExampleEquityConfig | None = None) -> None:
        self.config = config or ExampleEquityConfig()
        self.name = "example_equity"

    # ── indicators ──────────────────────────────────────────────────────────

    def calculate_indicators(self, df: pd.DataFrame, **kw) -> pd.DataFrame:
        close = df["Close"]
        out = df.copy()
        out[_FAST] = close.rolling(self.config.FAST_WINDOW).mean()
        out[_SLOW] = close.rolling(self.config.SLOW_WINDOW).mean()
        return out

    # ── signals ─────────────────────────────────────────────────────────────

    def generate_signals(self, df: pd.DataFrame) -> pd.DataFrame:
        out = df.copy()
        out["Signal"] = "NEUTRAL"
        out["Confidence"] = 0.0
        out["Reason"] = ""

        if out.empty or _SLOW not in out.columns:
            return out

        fast = out[_FAST].to_numpy()
        slow = out[_SLOW].to_numpy()
        if fast.size < 2 or slow.size < 2:
            return out

        signal = np.full(fast.size, "NEUTRAL", dtype=object)
        confidence = np.zeros(fast.size)
        reason = np.array([""] * fast.size, dtype=object)

        prev_fast = np.roll(fast, 1)
        prev_slow = np.roll(slow, 1)
        valid = ~(
            np.isnan(fast) | np.isnan(slow) | np.isnan(prev_fast) | np.isnan(prev_slow)
        )

        cross_up = valid & (prev_fast <= prev_slow) & (fast > slow)
        cross_down = valid & (prev_fast >= prev_slow) & (fast < slow)

        signal[cross_up] = "BUY"
        confidence[cross_up] = 1.0
        reason[cross_up] = "MA cross up"

        signal[cross_down] = "SELL"
        confidence[cross_down] = 1.0
        reason[cross_down] = "MA cross down"

        out["Signal"] = signal
        out["Confidence"] = confidence
        out["Reason"] = reason
        return out

    # ── manage ──────────────────────────────────────────────────────────────

    def manage_trade(self, position: dict, df: pd.DataFrame) -> int:
        """Simple manage hook.

        Returns the trade-action code the engine understands (``0`` = hold).
        Since the strategy is a harness, it lets the engine's
        stop/take/max-hold rules drive exits; it only forces an exit if the
        current bar's signal says SELL.
        """
        # Existing exits are signal-driven (generate_signals emits SELL on the
        # downtrend); manage simply holds so we do not double-exit. The engine's
        # stop/take/max-hold rules still guard the position.
        return 0


# Backwards-friendly alias (consumers may import by the pattern name).
StrategyConfig = ExampleEquityConfig
