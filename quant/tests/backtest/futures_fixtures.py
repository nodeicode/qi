"""Offline futures fixtures for validating the outright-futures backtest path.

The fixture bars are produced by feeding synthetic Databento-format frames
through the **real** ``FuturesDataHandler._process_databento_df`` /
``_finalize_futures_index`` / ``_to_title_case`` pipeline, so the bar shape,
index convention and synthesized bid/ask exactly match what a live futures
fetch would return — with zero network.

The strategy registers a FUTURES signal at class definition (the documented
extension pattern) so the whole story — new signal → asset class → router →
executor → contract math — is exercised end-to-end.
"""

from __future__ import annotations

import datetime
from dataclasses import dataclass, field
from typing import Any, ClassVar

import numpy as np
import pandas as pd

from quant.core._utils import SIGNAL_REGISTRY, AssetClass
from quant.core.protocols import PositionDict
from quant.data.futures_data_handler import FuturesDataHandler
from quant.utils.strategy_config import BaseStrategyConfig

MES_SYMBOL = "MES"


def make_databento_raw_frame(
    *, start: pd.Timestamp, n_minutes: int = 60 * 24, level: float = 5600.0
) -> pd.DataFrame:
    """Synthetic Databento raw trades/ohlc frame (pre-processing shape)."""
    ts = pd.date_range(start, periods=n_minutes, freq="1min", tz="UTC")
    drift = np.linspace(0.0, 12.0, n_minutes)  # +12 points over the window
    mid = level + drift
    f = {
        "ts_event": ts,
        "open": mid,
        "high": mid + 0.40,
        "low": mid - 0.40,
        "close": mid + 0.05,
        "volume": np.full(n_minutes, 900, dtype=int),
    }
    return pd.DataFrame(f)


def _apply_realistic_quotes(df: pd.DataFrame, tick: float = 0.25) -> pd.DataFrame:
    """Override the handler's placeholder OHLCV spread with a tick-sized one.

    ``FuturesDataHandler`` synthesizes bid/ask from OHLCV as ``mid * 0.05`` —
    a crude placeholder (a $280 spread on a $5,600 index).  Real quotes come
    from the handler's CBBO/quote paths; for offline validation the fixture
    applies a realistic 1-tick-each-side spread so fills round to the product
    tick and P&L is meaningful.
    """
    out = df.copy()
    half = tick / 2.0
    out["mid"] = out["Close"]
    out["bid"] = out["Close"] - half
    out["ask"] = out["Close"] + half
    return out


def make_futures_bars(
    *,
    symbol: str = MES_SYMBOL,
    start: str = "2024-01-02",
    n_minutes: int = 60 * 24,
    level: float = 5600.0,
) -> pd.DataFrame:
    """Futures bars as ``FuturesDataHandler.get_historical_data`` returns.

    Applies the real processing chain (synthesized mid/bid/ask, 1m resolved as
    returned by the handler) then the realistic tick-spread override.  UTC-naive
    intraday index.
    """
    raw = make_databento_raw_frame(
        start=pd.Timestamp(start, tz="UTC"), n_minutes=n_minutes, level=level
    )
    df = FuturesDataHandler._process_databento_df(raw, "1m")
    df = FuturesDataHandler._finalize_futures_index(df, "1m")
    df = FuturesDataHandler._to_title_case(df, symbol)
    return _apply_realistic_quotes(df)


class FakeFuturesHandler:
    """DataHandlerProtocol-compatible handler over synthetic Databento raw data.

    Each ``get_historical_data`` call runs the **real** ``FuturesDataHandler``
    processing chain for the requested interval (1m base + 5m/15m/30m resample)
    and finalize/title-case — so bars, index convention and synthesized
    bid/ask exactly match a live futures fetch, offline.
    """

    def __init__(self, raw_df: pd.DataFrame):
        self._raw = raw_df

    def get_historical_data(
        self,
        ticker: str,
        start,
        end,
        interval: str = "1m",
        use_cache: bool = True,
        adjustment: str | None = None,
    ) -> pd.DataFrame:
        df = FuturesDataHandler._process_databento_df(self._raw, interval)
        df = FuturesDataHandler._finalize_futures_index(df, interval)
        df = FuturesDataHandler._to_title_case(df, ticker)
        df = _apply_realistic_quotes(df)
        s = pd.Timestamp(start)
        e = pd.Timestamp(end)
        return df.loc[(df.index >= s) & (df.index <= e)].copy()


@dataclass
class FuturesConfig(BaseStrategyConfig):
    """Minimal config for a 15m MES outright-futures strategy."""

    INTERVAL: str = "15m"
    MANAGE_INTERVAL: str = "15m"
    PRE_LOAD_DAYS: int = 3
    INITIAL_CAPITAL: float = 100_000.0
    CAPITAL_PER_TRADE: float = 0.10
    MAX_CONCURRENT_POSITIONS: int = 2
    STOP_LOSS_PERCENT: float = 0.05
    TAKE_PROFIT_PERCENT: float = -1.0  # disabled
    USE_TRAIL_LOSS: bool = False
    MAX_HOLDING_MINUTES: int = 600
    MIN_SIGNAL_CONFIDENCE: float = 0.5
    BASE_SLIPPAGE_FACTOR: float = 0.0
    TICKERS: list[str] = field(default_factory=lambda: [MES_SYMBOL])

    # --- Outright futures extensions (strategy-subclass fields) ---
    FUTURES_ROOT: str = MES_SYMBOL
    FUTURES_ROUND_TO_TICK: bool = True
    FUTURES_MARGIN_FRACTION: float = 0.10
    DATA_DELAY: int = 0


class GoldenFuturesStrategy:
    """Deterministic 15m MES strategy: BUY_MES every N-th bar, manage-exit after K bars.

    Registers ``BUY_MES`` → FUTURES at class creation — the documented
    extension point for new assets (no core edit; router + sizing resolve by
    asset class).
    """

    SIGNALS: ClassVar[tuple] = ("BUY_MES",)

    name = "golden_futures"
    client_name = "golden"

    def __init__(self, config: BaseStrategyConfig, *, n: int = 48, exit_after: int = 6):
        self.config = config
        self._n = n
        self._exit_after = exit_after

    def calculate_indicators(self, df: pd.DataFrame, **kw) -> pd.DataFrame:
        out = df.copy()
        out["sma"] = out["Close"].rolling(4, min_periods=1).mean()
        return out

    def generate_signals(self, df: pd.DataFrame) -> pd.DataFrame:
        out = df.copy()
        sig = np.where(np.arange(len(out)) % self._n == 0, "BUY_MES", "NEUTRAL")
        out["Signal"] = sig
        out["Confidence"] = 0.9
        out["Reason"] = "golden_futures"
        return out

    def manage_trade(self, position: PositionDict, df: pd.DataFrame) -> int:
        """Exit at the first bar >= EntryTime + exit_after * INTERVAL (or hold)."""
        interval_min = int(self.config.INTERVAL[:-1])
        entry_raw = position.get("EntryTime")
        if entry_raw is None:
            entry_raw = pd.NaT
        # Engine guarantees EntryTime on positions; defensive fallback only.
        entry = pd.Timestamp(entry_raw)  # ty: ignore[invalid-argument-type]
        if entry.tzinfo is not None:
            entry = entry.tz_convert("UTC").tz_localize(None)
        cutoff = entry + pd.Timedelta(minutes=interval_min * self._exit_after)
        hit = df.index[df.index >= cutoff]
        if len(hit) == 0:
            return -1
        return df.index.get_indexer([hit[0]])[0]


# Extension point exercised at import: a brand-new signal routes to FUTURES.
for _sig in GoldenFuturesStrategy.SIGNALS:
    SIGNAL_REGISTRY.register(_sig, AssetClass.FUTURES, 1)
