"""example_equity — the public reference strategy (declarative, strategy-dataflow).

A deliberately simple moving-average crossover on the equity close. It exists
to (a) make the engine/backtest/live pipeline runnable out-of-the-box without
any private alpha, and (b) act as the canonical example for ``docs/
extensibility.md`` (add-a-strategy recipe). It is **not** tuned for
profitability — it is a harness, not an edge.

It is written against the declarative surface (spec `strategy-authoring`):
declare the typed instrument, a ``bars`` subscription, and a venue, then
implement the single author body ``decide(events, portfolio)`` which BOTH
evaluates the portfolio's open positions for exits (the folded
manage/evaluate_manage_exit role) and generates entries (the folded
generate_signals + sizing role). The MA windows stay the legacy 10/30 and the
entry sizing keeps the ~0.95 buying-power fraction so the entry decision
sequence reproduces the pre-change reference over the same synthetic fixture —
now sized on the instrument (lot/price precision) against the injected,
read-only portfolio, never via ``CAPITAL_PER_TRADE``/``SIZING_STRATEGIES``.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import ClassVar

import pandas as pd

from quant.data import Datatype, Subscription
from quant.instruments import EquityInstrument
from quant.strategies.base import ExitIntent, Strategy
from quant.strategies.risk import exit_risk_hit
from quant.strategies.sizing import size_equity_lot
from quant.utils.strategy_config import BaseStrategyConfig

_SYMBOL = "SPY"


def _equity_instrument() -> EquityInstrument:
    """The single traded equity instrument (venue ``backtest`` by default)."""
    return EquityInstrument(
        symbol=_SYMBOL,
        venue="backtest",
        price_precision=2,
        lot_size=1,
    )


def _bars_subscription() -> Subscription:
    """The strategy's single ``(SPY, BARS, "1h")`` bars subscription."""
    return Subscription(
        instrument=_equity_instrument(),
        datatype=Datatype.BARS,
        cadence="1h",
    )


def _default_instruments():
    return [_equity_instrument()]


def _default_subscriptions():
    return [_bars_subscription()]


@dataclass
class ExampleEquityConfig(BaseStrategyConfig):
    """Equity MA-cross configuration (framework risk fields + declarations).

    The framework risk/execution fields (``STOP_LOSS_PERCENT`` /
    ``TAKE_PROFIT_PERCENT`` / ``MAX_HOLDING_MINUTES`` / ``MAX_CONCURRENT_POSITIONS`` /
    ``INITIAL_CAPITAL`` / ``PRE_LOAD_DAYS``) are read by the author body to
    size entries and gate exits; ``fast_window`` / ``slow_window`` are the MA
    crossover windows (legacy 10/30); ``buying_power_fraction`` is the entry
    share of the portfolio's buying power (legacy 0.95).
    """

    PRE_LOAD_DAYS: int = 60
    INITIAL_CAPITAL: float = 100_000.0
    MAX_CONCURRENT_POSITIONS: int = 1

    STOP_LOSS_PERCENT: float = 0.05
    TAKE_PROFIT_PERCENT: float = -1.0  # disabled sentinel
    USE_TRAIL_LOSS: bool = False
    MAX_HOLDING_MINUTES: int = 60 * 24 * 30

    MIN_SIGNAL_CONFIDENCE: float = 0.0
    BASE_SLIPPAGE_FACTOR: float = 0.001

    # --- Declarative declarations (config is the source of truth) ---
    instruments: list = field(default_factory=_default_instruments)
    subscriptions: list = field(default_factory=_default_subscriptions)
    venue: str = "backtest"

    # --- Author windows / sizing ---
    fast_window: int = 10
    slow_window: int = 30
    buying_power_fraction: float = 0.95


class ExampleEquityStrategy(Strategy):
    """Moving-average crossover on the equity close (declarative reference)."""

    name: ClassVar[str] = "example_equity"
    venue: ClassVar[str] = "backtest"

    def __init__(self, config: ExampleEquityConfig | None = None) -> None:
        self.config = config or ExampleEquityConfig()
        # Mirror the declared config onto the instance/class surface the engine
        # and data port read (instruments / subscriptions / venue).
        self.instruments: list = list(self.config.instruments)
        self.subscriptions: list = list(self.config.subscriptions)
        self.venue: str = self.config.venue or "backtest"
        super().__init__()  # validates the declared venue id at construction

    # ── author body ────────────────────────────────────────────────────────
    def decide(self, events, portfolio) -> list:
        """Single decision body: exits for open positions, then entries.

        ``events.bars(_SYMBOL)`` is the closed adjusted Close series for the
        1h subscription; ``portfolio`` is the injected read-only portfolio
        holding this strategy's open positions. Returns a list of typed
        ``Order`` (entries) and ``ExitIntent`` (manage-pass exits) items.
        """
        bars = events.bars(_SYMBOL)
        if bars is None or bars.empty:
            return []
        close = bars["Close"]
        fast = close.rolling(self.config.fast_window).mean()
        slow = close.rolling(self.config.slow_window).mean()
        last_close = float(close.iloc[-1])
        at = bars.index[-1]

        instrument = self.config.instruments[0]
        out: list = []

        # Manage pass — the folded manage_trade / evaluate_manage_exit role.
        out += [
            ExitIntent(self.name, position=pos, reason="exit")
            for pos in portfolio.open_positions(self.name)
            if exit_risk_hit(pos, self.config, spot=last_close, at=at)
            or _crossed(fast, slow, down=True)
        ]

        # Signal pass — the folded generate_signals + sizing role.
        if (
            _crossed(fast, slow, down=False)
            and len(portfolio.open_positions(self.name))
            < self.config.MAX_CONCURRENT_POSITIONS
        ):
            qty = size_equity_lot(
                portfolio,
                last_close,
                instrument=instrument,
                buying_power_fraction=self.config.buying_power_fraction,
            )
            if qty > 0:
                from quant.portfolio import Side

                out.append(instrument.make_order(Side.BUY, qty, strategy=self.name))
        return out


def _crossed(fast_ma: pd.Series, slow_ma: pd.Series, *, down: bool) -> bool:
    """True when the fast/slow MA crossover occurred on the latest bar.

    ``down=False`` is a bullish cross (fast rising above slow); ``down=True``
    is a bearish cross (fast falling below slow). Requires two adjacent valid
    rows (the rolling MAs are NaN until ``slow_window`` bars accumulate).
    """
    if len(fast_ma) < 2:
        return False
    prev_fast, curr_fast = float(fast_ma.iloc[-2]), float(fast_ma.iloc[-1])
    prev_slow, curr_slow = float(slow_ma.iloc[-2]), float(slow_ma.iloc[-1])
    if any(pd.isna(v) for v in (prev_fast, curr_fast, prev_slow, curr_slow)):
        return False
    if down:
        return prev_fast >= prev_slow and curr_fast < curr_slow
    return prev_fast <= prev_slow and curr_fast > curr_slow


# Backwards-friendly alias (consumers may import by the pattern name).
StrategyConfig = ExampleEquityConfig
