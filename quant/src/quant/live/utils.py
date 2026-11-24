# Unified Trader Utils
# Helper functions for the Unified Trader
import contextlib
import datetime
import json
import logging
import os
import threading
import time
from pathlib import Path
from typing import Any

import pandas as pd
from alpaca.trading.client import TradingClient
from alpaca.trading.models import TradeAccount

from quant.live.protocols import DataHandlerProtocol


@contextlib.contextmanager
def suspend_gc():
    """Pause the Python generational GC for the duration of the block.

    Deferred, not disabled-forever: re-enables (letting the next allocation
    trigger a normal collection) on exit, so memory stays bounded. Safe,
    reversible, and semantically a no-op for the compute itself — it only
    removes GC pauses from the wrapped span.

    Wired into the live per-tick decision path behind the ``GC_PAUSE_HOTPATH``
    env toggle (see `python/trader.py`), so it can be A/B measured against
    `strategy_calc_total_ns` / `generate_signals_ns` without a redeploy changing
    the rest of the pipeline. Stage-3 compute-structural tuning.
    """
    import gc

    was_enabled = gc.isenabled()
    if was_enabled:
        gc.disable()
    try:
        yield
    finally:
        if was_enabled and not gc.isenabled():
            gc.enable()


# -----------------------------------------------------------------------------
# Equity Manager
# -----------------------------------------------------------------------------


class EquityManager:
    """
    Lightweight in-memory account equity tracker.

    Strategy
    --------
    - Fetches real equity from the broker **once** at construction.
    - `debit(amount)` / `credit(amount)` keep the value current after every
      order so back-to-back sizing is always accurate with zero API calls.
    - A TTL-based re-sync (default 5 min) corrects drift from market-driven
      P&L, dividends, and fill-price slippage.
    """

    SYNC_TTL: int = 300  # seconds between broker re-syncs

    def __init__(self, client: TradingClient, logger: logging.Logger) -> None:
        self._lock = threading.RLock()
        self._client = client
        self._logger = logger
        self._equity: float = 0.0
        self._ts: float = 0.0
        self._sync(initial=True)

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def get(self) -> float:
        """Return current equity, re-syncing from broker if TTL has elapsed.

        Serialized with ``debit`` / ``credit`` / ``_sync``: several strategy
        threads may trade the same broker account concurrently, and an
        unguarded read-modify-write on ``_equity`` would lose updates.
        """
        with self._lock:
            if (time.monotonic() - self._ts) > self.SYNC_TTL:
                self._sync()
            return self._equity

    def debit(self, amount: float) -> None:
        """Subtract deployed capital after an entry order."""
        with self._lock:
            self._equity = max(0.0, self._equity - amount)
        self._logger.debug(
            f"[EquityManager] debited ${amount:,.2f} → ${self._equity:,.2f}"
        )

    def credit(self, amount: float) -> None:
        """Add freed capital back after an exit order."""
        with self._lock:
            self._equity += amount
        self._logger.debug(
            f"[EquityManager] credited ${amount:,.2f} → ${self._equity:,.2f}"
        )

    # ------------------------------------------------------------------
    # Internal
    # ------------------------------------------------------------------

    def _sync(self, initial: bool = False) -> None:
        with self._lock:
            try:
                account: TradeAccount | dict[str, Any] = self._client.get_account()
                equity_raw: str | float | None = (
                    account.get("equity")
                    if isinstance(account, dict)
                    else account.equity
                )
                self._equity = float(equity_raw or 0)
                self._ts = time.monotonic()
                label = "Initial" if initial else "Re-synced"
                self._logger.info(
                    f"[EquityManager] {label} equity: ${self._equity:,.2f}"
                )
            except Exception as e:
                if initial:
                    self._logger.error(
                        f"[EquityManager] Could not fetch initial equity: {e}. Defaulting to 0."
                    )
                else:
                    self._logger.warning(
                        f"[EquityManager] Re-sync failed, using last known value: {e}"
                    )
                self._ts = time.monotonic()  # reset timer so we don't hammer the API


# -----------------------------------------------------------------------------
# Multi-Client Equity Manager
# -----------------------------------------------------------------------------


class MultiClientEquityManager:
    """
    Multi-client equity tracker.

    Holds one ``EquityManager`` per registered broker client.  The "active"
    tracker is selected via ``set_active(client_name)``; all proxy methods
    (``get`` / ``debit`` / ``credit``) then delegate to it.

    This ensures that paper and live accounts are tracked independently:
    sizing decisions always read the correct account balance, and
    debit/credit adjustments never bleed across accounts.

    **Threading** — the active-client selection is **thread-local**.
    Callers invoke ``set_active`` immediately before an order or sizing
    decision, and this manager may be used concurrently from multiple
    threads.  A process-global selection would let one thread's
    ``set_active`` redirect another thread's later ``get`` / ``debit`` /
    ``credit`` to the wrong broker account — reading the wrong equity for
    sizing and booking profit/loss against the wrong account.  Thread-local
    selection keeps each thread pinned to the account it last selected.
    The per-account ``EquityManager`` instances are shared across threads:
    each holds only its own balance, so concurrent access is safe.
    """

    def __init__(
        self, clients: dict[str, TradingClient], logger: logging.Logger
    ) -> None:
        self._logger = logger
        self._managers: dict[str, EquityManager] = {
            name: EquityManager(client, logger) for name, client in clients.items()
        }
        # Thread-local active selection: each thread remembers the client it
        # selected via ``set_active``.  Threads that never call it fall back
        # to the first registered client.
        self._default_active: str | None = next(iter(self._managers), None)
        self._tls = threading.local()

    # ------------------------------------------------------------------
    # Active-client selection
    # ------------------------------------------------------------------

    def set_active(self, client_name: str) -> bool:
        """Point all subsequent proxy calls at *client_name*'s tracker.

        Applies to the calling thread only.  Returns ``True`` on success,
        ``False`` if the client is not registered.
        """
        if client_name not in self._managers:
            self._logger.warning(
                f"[MultiClientEquityManager] Unknown client '{client_name}'; "
                f"keeping current active ('{self.active_client}')."
            )
            return False
        self._tls.active = client_name
        return True

    @property
    def active_client(self) -> str | None:
        """Name of the currently active client (for the calling thread)."""
        return getattr(self._tls, "active", self._default_active)

    # ------------------------------------------------------------------
    # Direct access
    # ------------------------------------------------------------------

    def for_client(self, client_name: str) -> EquityManager:
        """Return the ``EquityManager`` for a specific client by name."""
        return self._managers[client_name]

    # ------------------------------------------------------------------
    # Proxy API — mirrors EquityManager so call sites need no changes
    # ------------------------------------------------------------------

    def get(self) -> float:
        return self._active_mgr.get()

    def debit(self, amount: float) -> None:
        self._active_mgr.debit(amount)

    def credit(self, amount: float) -> None:
        self._active_mgr.credit(amount)

    # ------------------------------------------------------------------
    # Internal
    # ------------------------------------------------------------------

    @property
    def _active_mgr(self) -> EquityManager:
        active = self.active_client
        if active is None:
            raise RuntimeError("No active client configured")
        return self._managers[active]


# -----------------------------------------------------------------------------
# Configuration & State Management Helpers
# -----------------------------------------------------------------------------

# Resolve project root: parent of the ``python/`` package directory.
_PROJECT_ROOT = Path(__file__).resolve().parent.parent
_ENV_FILE = _PROJECT_ROOT / ".environment"
_VALID_ENVIRONMENTS = {"staging", "prod"}


def resolve_env(logger: logging.Logger) -> str:
    """Return the deployment environment (``staging`` or ``prod``).

    Reads the environment from ``<project_root>/.environment`` (written by
    bootstrap).  Defaults to ``staging`` when the marker file is absent
    (local dev / tests).
    """
    env = "staging"
    if os.path.exists(_ENV_FILE):
        with open(_ENV_FILE) as f:
            env = f.read().strip().lower() or env
        if env not in _VALID_ENVIRONMENTS:
            raise ValueError(
                f"Invalid environment '{env}'. Must be one of {sorted(_VALID_ENVIRONMENTS)}."
            )
    else:
        logger.warning(
            f"Environment marker {_ENV_FILE} not found, defaulting to '{env}'."
        )
    return env


def resolve_config_file(base_name: str, logger: logging.Logger) -> str:
    """
    Resolve an environment-specific config file.

    Reads the environment from ``<project_root>/.environment`` (written by bootstrap)
    and returns ``<base_name>.<env>.json`` (e.g. ``strategies_config.staging.json``).
    Defaults to ``staging`` when the marker file is absent (local dev).
    """
    env = resolve_env(logger)

    config_path: Path = (_PROJECT_ROOT / f"{base_name}.{env}.json").resolve()
    if not config_path.is_relative_to(_PROJECT_ROOT):
        raise ValueError(f"Resolved config path {config_path} escapes project root.")

    if not config_path.exists():
        raise FileNotFoundError(
            f"Config file {config_path} not found for environment '{env}'."
        )

    logger.info(f"Using environment config: {config_path}")
    return str(config_path)


def load_config(config_file: str, logger: logging.Logger) -> dict[str, Any]:
    """
    Load strategies configuration from JSON file.
    """
    if not os.path.exists(config_file):
        logger.error(f"Config file {config_file} not found.")
        return {"strategies": []}

    try:
        with open(config_file) as f:
            config = json.load(f)
        logger.info("Configuration loaded.")
        return config
    except Exception as e:
        logger.error(f"Error loading config: {e}")
        return {"strategies": []}


def save_state(
    state_file: str,
    state: dict[str, Any],
    lock: threading.RLock,
    logger: logging.Logger,
) -> None:
    """
    Save trader state to file atomically.
    """
    with lock:
        try:
            # Atomic write
            temp_file = f"{state_file}.tmp"
            with open(temp_file, "w") as f:
                json.dump(state, f, indent=4, default=str)
            os.replace(temp_file, state_file)
        except Exception as e:
            logger.error(f"Error saving state: {e}")


def load_state(state_file: str, logger: logging.Logger) -> dict[str, Any]:
    """
    Load trader state from file.
    """
    if not os.path.exists(state_file):
        return {}
    try:
        with open(state_file) as f:
            state = json.load(f)
        logger.info("State loaded successfully.")
        return state
    except Exception as e:
        logger.error(f"Error loading state: {e}")
        return {}


# -----------------------------------------------------------------------------
# Date & Time Helpers
# -----------------------------------------------------------------------------


def parse_interval(interval: str) -> datetime.timedelta:
    """
    Parse an interval string (e.g., '1m', '1h', '1d') into a timedelta.
    """
    try:
        unit = interval[-1].lower()
        value = int(interval[:-1])
        if unit == "m":
            return datetime.timedelta(minutes=value)
        elif unit == "h":
            return datetime.timedelta(hours=value)
        elif unit == "d":
            return datetime.timedelta(days=value)
    except Exception:
        pass
    return datetime.timedelta(minutes=60)


# -----------------------------------------------------------------------------
# System Helpers
# -----------------------------------------------------------------------------


def setup_logging(
    name: str = "UnifiedTrader", log_file: str = "trader.log"
) -> logging.Logger:
    logger = logging.getLogger(name)
    _env_level = os.environ.get("LOG_LEVEL", "INFO").upper()
    _level = getattr(logging, _env_level, logging.INFO)
    logger.setLevel(_level)
    formatter = logging.Formatter(
        "%(asctime)s - %(levelname)s - %(name)s - %(message)s"
    )

    # Clear existing handlers to prevent duplicate output and fd leaks on re-calls
    for _h in logger.handlers[:]:
        _h.close()
        logger.removeHandler(_h)

    # Console Handler
    sh = logging.StreamHandler()
    sh.setFormatter(formatter)
    logger.addHandler(sh)

    # File Handler
    fh = logging.FileHandler(log_file)
    fh.setFormatter(formatter)
    logger.addHandler(fh)

    # quant.core — full LOG_LEVEL; display name remapped to "Engine"
    class _EngineNameFilter(logging.Filter):
        def filter(self, record: logging.LogRecord) -> bool:
            record.name = "Engine"
            return True

    _eng_filter = _EngineNameFilter()
    sh_eng = logging.StreamHandler()
    sh_eng.setFormatter(formatter)
    sh_eng.addFilter(_eng_filter)
    fh_eng = logging.FileHandler(log_file)
    fh_eng.setFormatter(formatter)
    fh_eng.addFilter(_eng_filter)

    engine_log = logging.getLogger("quant.core")
    engine_log.setLevel(_level)
    engine_log.propagate = False
    for _h in engine_log.handlers[:]:
        _h.close()
        engine_log.removeHandler(_h)
    engine_log.addHandler(sh_eng)
    engine_log.addHandler(fh_eng)

    # quant.strategies — WARNING and ERROR logs only; INFO is intentionally
    # suppressed to reduce noise.  propagate=False prevents fallthrough to the
    # root logger so WARNING/ERROR still reach both console and trader.log.
    strat_log = logging.getLogger("quant.strategies")
    strat_log.setLevel(logging.WARNING)
    strat_log.propagate = False
    for _h in strat_log.handlers[:]:
        _h.close()
        strat_log.removeHandler(_h)
    strat_log.addHandler(sh)
    strat_log.addHandler(fh)

    return logger


def fetch_or_update_cache(
    cache: dict[str, dict[str, pd.DataFrame]],
    s_name: str,
    ticker: str,
    data_handler: DataHandlerProtocol,
    data_end_dt: datetime.datetime,
    lookback: int,
    interval: str,
) -> pd.DataFrame:
    """
    Fetch historical data for *ticker* into *cache[s_name][ticker]*,
    or incrementally update it if already present.
    NOTE: Dict in python is pass-by-reference, so this function modifies the cache globally (in-place).

    Works with any cache dict (signal data, manage data, etc.) —
    callers pass the specific cache they want populated.

    Returns the (possibly updated) DataFrame.
    """
    if s_name not in cache:
        cache[s_name] = {}

    # Normalize data_end_dt to a tz-aware pd.Timestamp (UTC) so all
    # downstream comparisons are tz-consistent regardless of what the
    # data_handler returns (Alpaca uses America/New_York or UTC).
    data_end_ts = (
        pd.Timestamp(data_end_dt, tz="UTC")
        if pd.Timestamp(data_end_dt).tz is None
        else pd.Timestamp(data_end_dt).tz_convert("UTC")
    )

    if ticker not in cache[s_name]:
        # Initial full load
        start_dt = data_end_ts - datetime.timedelta(days=lookback)
        df = data_handler.get_historical_data(
            ticker=ticker,
            start=start_dt.isoformat(),
            end=data_end_ts.isoformat(),
            interval=interval,
        )
        cache[s_name][ticker] = df
    else:
        # Incremental update — fetch overlap window then merge
        last_ts = cache[s_name][ticker].index[-1]

        # Ensure last_ts is tz-aware UTC for safe comparison
        if hasattr(last_ts, "tz") and last_ts.tz is not None:
            last_ts = last_ts.tz_convert("UTC")
        else:
            last_ts = pd.Timestamp(last_ts, tz="UTC")

        update_start = data_end_ts - datetime.timedelta(days=1)
        if update_start < last_ts:
            update_start = last_ts

        new_df = data_handler.get_historical_data(
            ticker=ticker,
            start=update_start.isoformat(),
            end=data_end_ts.isoformat(),
            interval=interval,
        )

        if not new_df.empty:
            combined = pd.concat([cache[s_name][ticker], new_df])
            combined = combined[~combined.index.duplicated(keep="last")]
            cache[s_name][ticker] = combined.sort_index()

    return cache[s_name].get(ticker, pd.DataFrame())
