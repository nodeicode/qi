from dataclasses import dataclass


@dataclass
class BaseStrategyConfig:
    """
    Base configuration for all strategies.
    Defines required parameters for backtesting and execution.
    Strategies must override these fields with concrete values.
    """

    # --- Data & Timeframe ---
    INTERVAL: str  # Timeframe for underlying data (e.g., "1h", "15m")
    PRE_LOAD_DAYS: int  # Days of data to preload for indicator warmup
    MANAGE_INTERVAL: str  # Timeframe for trade management (usually matches INTERVAL)

    # --- Capital Management ---
    INITIAL_CAPITAL: float  # Starting portfolio value
    CAPITAL_PER_TRADE: float  # Fraction of current equity per trade (0.0-1.0)
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
    TICKERS: list[str]  # List of tickers this strategy is designed/tuned for

    # --- Option Selection ---
    # if MIN_OPTION_DTE is set, strategy and backtest use single-leg options
    MIN_OPTION_DTE: int | None = (
        None  # Minimum Days to Expiration for selecting contracts
    )

    # if MIN_OPTION_DTE and MIN_SPREAD_WIDTH are set, strategy and backtest will use spreads instead of single-leg options
    MIN_SPREAD_WIDTH: int | None = None  # Minimum width for spreads (if applicable)

    # --- Option Strike Selection (these apply to both single-leg and spread options) ---
    # 'atm' = round to nearest $5, 'itm' = deep ITM for high delta, 'otm' = OTM for leverage
    OPTION_STRIKE_METHOD: str = "itm"
    # How far ITM as fraction of underlying price (e.g. 0.05 = 5% ITM)
    OPTION_ITM_DEPTH: float = 0.05
    # How far OTM as fraction of underlying price (e.g. 0.05 = 5% OTM)
    OPTION_OTM_DEPTH: float = 0.05
    # Strike rounding increment (e.g. 5 for $5 increments, 1 for $1)
    OPTION_STRIKE_INCREMENT: float = 5.0
    # Max hold time for option trades in days (caps equity MAX_HOLDING_MINUTES)
    OPTION_MAX_HOLD_DAYS: int = 10

    # SIP / feed delay in minutes. Source of truth for live *and* backtest fill
    # clocks (``T+I+DATA_DELAY``). Framework default is 0; strategies that need
    # delayed SIP parity (e.g. example_equity) override explicitly.
    DATA_DELAY: int = 0

    def __post_init__(self) -> None:
        """Fail fast on obviously-invalid config rather than surfacing a
        confusing error deep inside the engine/backtester.

        Deliberately basic/non-exhaustive: sentinel values used to disable a
        feature (e.g. ``TAKE_PROFIT_PERCENT=-1``, ``STOP_LOSS_PERCENT=999``)
        are intentionally left unconstrained.
        """
        errors: list[str] = []

        def _require(condition: bool, message: str) -> None:
            if not condition:
                errors.append(message)

        _require(bool(self.INTERVAL), "INTERVAL must be a non-empty string")
        _require(
            bool(self.MANAGE_INTERVAL), "MANAGE_INTERVAL must be a non-empty string"
        )
        _require(self.PRE_LOAD_DAYS >= 0, "PRE_LOAD_DAYS must be >= 0")

        _require(self.INITIAL_CAPITAL > 0, "INITIAL_CAPITAL must be > 0")
        _require(
            0 < self.CAPITAL_PER_TRADE <= 1,
            "CAPITAL_PER_TRADE must be in (0, 1] (fraction of equity per trade)",
        )
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
        _require(bool(self.TICKERS), "TICKERS must be a non-empty list")

        _require(
            self.MIN_OPTION_DTE is None or self.MIN_OPTION_DTE >= 0,
            "MIN_OPTION_DTE must be None or >= 0",
        )
        _require(
            self.MIN_SPREAD_WIDTH is None or self.MIN_SPREAD_WIDTH > 0,
            "MIN_SPREAD_WIDTH must be None or > 0",
        )
        # Spreads only make sense once single-leg option selection is enabled.
        _require(
            self.MIN_SPREAD_WIDTH is None or self.MIN_OPTION_DTE is not None,
            "MIN_SPREAD_WIDTH requires MIN_OPTION_DTE to also be set",
        )

        _require(
            self.OPTION_STRIKE_METHOD in ("atm", "itm", "otm"),
            "OPTION_STRIKE_METHOD must be one of 'atm', 'itm', 'otm'",
        )
        _require(
            self.OPTION_STRIKE_INCREMENT > 0, "OPTION_STRIKE_INCREMENT must be > 0"
        )
        _require(self.OPTION_MAX_HOLD_DAYS > 0, "OPTION_MAX_HOLD_DAYS must be > 0")

        _require(self.DATA_DELAY >= 0, "DATA_DELAY must be >= 0")

        if errors:
            joined = "\n  - ".join(errors)
            raise ValueError(f"Invalid {type(self).__name__}:\n  - {joined}")
