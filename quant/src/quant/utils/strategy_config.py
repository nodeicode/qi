"""Declarative strategy configuration (spec `strategy-sdk` Config validation).

``BaseStrategyConfig`` carries two groups of fields:

* the **framework risk/execution fields** the author body reads (initial
  capital, position caps, stop/take/max-hold, confidence, slippage, pre-load
  days, data-delay), and
* the **declarative declarations** ``instruments`` / ``subscriptions`` /
  ``venue`` that name what the strategy trades and where its orders route.

The engine-driven knobs the author no longer controls are gone: ``INTERVAL`` /
``MANAGE_INTERVAL`` / ``CAPITAL_PER_TRADE`` / ``TICKERS`` and the option-strike
set are removed. Cadence is per-subscription (the engine serves closed bars at
the declared cadence), sizing is the author body's own job from the instrument
contract facts + the injected portfolio, and instrument selection is declared
directly. Validation below rejects an obviously-invalid config at construction,
naming the offending field, rather than surfacing a confusing error deep inside
the engine/backtester.
"""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class BaseStrategyConfig:
    """Base configuration for all strategies.

    Strategies override the framework risk/execution fields with concrete
    values and fill in the declarative declarations (``instruments``,
    ``subscriptions``, ``venue``). The declaration fields default to empty so a
    config that only sets risk/execution still constructs; the venue id is
    registry-checked at the strategy/engine boundary (this module is a leaf and
    may not import ``quant.venue``).
    """

    # --- Data & Timeframe ---
    PRE_LOAD_DAYS: int  # Days of data to preload for indicator warmup

    # --- Capital Management ---
    INITIAL_CAPITAL: float  # Starting portfolio value
    MAX_CONCURRENT_POSITIONS: int  # Max number of open positions allowed

    # --- Risk Management ---
    STOP_LOSS_PERCENT: float  # Hard stop loss as a decimal (e.g., 0.30 for 30%)
    TAKE_PROFIT_PERCENT: float  # Take profit target as a decimal (e.g., 2.0 for 200%)
    USE_TRAIL_LOSS: bool  # Whether to use trailing stops once in profit
    MAX_HOLDING_MINUTES: int  # Max duration to hold a position before forced exit

    # --- Execution & Filtering ---
    MIN_SIGNAL_CONFIDENCE: (
        float  # Minimum model confidence score to take a trade (0.0-1.0)
    )
    BASE_SLIPPAGE_FACTOR: (
        float  # Slippage multiplier for realistic execution simulation
    )

    # SIP / feed delay in minutes. Source of truth for live *and* backtest fill
    # clocks (``T+I+DATA_DELAY``). Framework default is 0; strategies that need
    # delayed SIP parity (e.g. example_equity) override explicitly.
    DATA_DELAY: int = 0

    # --- Declarative declarations (spec `strategy-sdk`) ---
    #: Typed instruments the strategy trades (contract facts live on each).
    instruments: list = field(default_factory=list)
    #: ``(instrument, datatype, cadence)`` subscriptions the engine serves closed bars for.
    subscriptions: list = field(default_factory=list)
    #: Registered venue id the strategy's orders route to (e.g. ``"backtest"``).
    venue: str = ""

    def __post_init__(self) -> None:
        """Fail fast on invalid config rather than surfacing a confusing error
        deep inside the engine/backtester.

        ``venue`` registry membership is deliberately NOT checked here: this
        module is a leaf (it may not import ``quant.venue``), so the strategy
        base and the engine reject an unknown venue at construction. The checks
        below are structural/basic and non-exhaustive: sentinel values used to
        disable a feature (e.g. ``TAKE_PROFIT_PERCENT=-1``,
        ``STOP_LOSS_PERCENT=999``) are intentionally left unconstrained.
        """
        errors: list[str] = []

        def _require(condition: bool, message: str) -> None:
            if not condition:
                errors.append(message)

        _require(self.PRE_LOAD_DAYS >= 0, "PRE_LOAD_DAYS must be >= 0")

        _require(self.INITIAL_CAPITAL > 0, "INITIAL_CAPITAL must be > 0")
        _require(
            self.MAX_CONCURRENT_POSITIONS >= 1,
            "MAX_CONCURRENT_POSITIONS must be >= 1",
        )

        _require(self.MAX_HOLDING_MINUTES > 0, "MAX_HOLDING_MINUTES must be > 0")

        _require(
            0 <= self.MIN_SIGNAL_CONFIDENCE <= 1,
            "MIN_SIGNAL_CONFIDENCE must be in [0, 1]",
        )
        _require(self.BASE_SLIPPAGE_FACTOR >= 0, "BASE_SLIPPAGE_FACTOR must be >= 0")

        _require(self.DATA_DELAY >= 0, "DATA_DELAY must be >= 0")

        _require(
            isinstance(self.instruments, list),
            "instruments must be a list of Instrument declarations",
        )
        _require(
            isinstance(self.subscriptions, list),
            "subscriptions must be a list of Subscription declarations",
        )
        _require(
            isinstance(self.venue, str),
            "venue must be a str identifying a registered venue (e.g. 'backtest')",
        )

        if errors:
            joined = "\n  - ".join(errors)
            raise ValueError(f"Invalid {type(self).__name__}:\n  - {joined}")
