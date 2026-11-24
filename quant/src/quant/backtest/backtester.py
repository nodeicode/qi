"""
Unified Backtester — environment adapter via ``ExecutorRouter``.
================================================================

Architecture:

  1. **Data fetch** — signal-resolution + manage-resolution bars prefetched
     upfront via ``_prefetch_data``.
  2. **Per-bar loop** — ``engine.process_strategy(strategy, now, executor)``
     drives scheduling, signals, manage, entry gating.  ``ExecutorRouter``
     is the ``ExecutorProtocol`` boundary and dispatches fills by asset class.
  3. **Fill simulation** — per-asset-class executors under
     ``quant.backtest.executors`` (``EquityExecutor``, …) own enter/exit.

A single ``run_backtest`` drives everything.

Usage
-----
>>> from quant.backtest import Backtester
>>> bt = Backtester(config, strategy, "2024-01-01", "2024-06-01", "AAPL")
>>> trades = bt.run_backtest()
>>> bt.compute_equity_curve()
>>> metrics = bt.calculate_performance_metrics()
"""

from __future__ import annotations

import logging
from typing import Any, Literal, cast

import pandas as pd

from quant.backtest.backtest_data_handler import BacktestDataHandler
from quant.backtest.base_backtest import BaseBacktester
from quant.backtest.executors import BacktestExecContext, ExecutorRouter
from quant.core.engine import TradingEngine
from quant.core.protocols import DataHandlerProtocol
from quant.utils.strategy_config import BaseStrategyConfig


class Backtester(BaseBacktester):
    """Unified backtester using ``engine.process_strategy`` via ``ExecutorRouter``.

    ``engine.route(signal)`` selects the asset class per signal; the router
    forwards enter/exit to the registered child executor.  EQUITY
    are registered by default.
    """

    def __init__(
        self,
        config: BaseStrategyConfig,
        strategy,
        start_date: str,
        end_date: str,
        ticker: str,
        verbose: bool = False,
        data_handler=None,
    ):
        super().__init__(
            config, strategy, start_date, end_date, ticker, verbose, data_handler
        )

        # When verbose, also surface logs from engine + data + strategy loggers so
        # internal decisions (signal gating, manage_trade, fills) are visible
        # alongside backtest logs — makes it easy to spot errors end-to-end.
        if verbose:
            _verbose_handler = logging.StreamHandler()
            _verbose_handler.setFormatter(
                logging.Formatter(
                    "%(asctime)s - %(levelname)s - %(name)s - %(message)s"
                )
            )
            for _log_name in ("quant.core.engine", "quant.backtest", "quant.data"):
                _lg = logging.getLogger(_log_name)
                _lg.setLevel(logging.WARNING)
                if not any(
                    isinstance(h, logging.StreamHandler)
                    and "%(name)s" in getattr(h.formatter, "_fmt", "")
                    for h in _lg.handlers
                ):
                    _lg.addHandler(_verbose_handler)
            # Strategy logger (supports .logger or ._log attribute)
            for _attr in ("logger", "_log"):
                _strat_log = getattr(self.strategy, _attr, None)
                if isinstance(_strat_log, logging.Logger):
                    _strat_log.setLevel(logging.INFO)
                    if not any(
                        isinstance(h, logging.StreamHandler)
                        and "%(name)s" in getattr(h.formatter, "_fmt", "")
                        for h in _strat_log.handlers
                    ):
                        _strat_log.addHandler(_verbose_handler)
                    break

        self._asset_class: str = "EQUITY"

        # Simulation state (mirrors trader.active_positions / running equity)
        self._active_positions: list[dict] = []
        self._running_equity: float = float(self.initial_capital)
        self._collateral_in_use: float = 0.0

        data_handler = BacktestDataHandler(
            df_signals=pd.DataFrame(),  # placeholder, engine caches full df in _simulate
            df_manage=pd.DataFrame(),  # placeholder, engine caches full df in _simulate
            signal_interval="",
            manage_interval="",
        )

        # Initialize engine to use
        self.engine = TradingEngine(
            self.strategy, cast("DataHandlerProtocol", data_handler)
        )

        # Pre-fetched caches (populated in _prepare_data)
        self._df_manage: pd.DataFrame = pd.DataFrame()
        self._df_manage_1m: pd.DataFrame = pd.DataFrame()

    # ==================================================================
    # Public API
    # ==================================================================

    def run_backtest(self) -> pd.DataFrame:
        """Single loop driving all asset classes — mirrors ``trader.py``.

        1. Prepare signals (via ``engine.run_signals`` — same as live).
        2. Detect dominant asset class and prefetch manage data if needed.
        3. Iterate signals: call ``engine.process_strategy`` per bar →
           ``ExecutorRouter`` dispatches entries/exits by asset class.
        """
        # Single prepare path for all strategies (signals + Raw_Close).
        df_signals = self.prepare_signals()

        if df_signals.empty:
            return pd.DataFrame()

        self._asset_class = self._detect_asset_class(df_signals)
        self.logger.info(f"Asset class: {self._asset_class}")

        # Prefetch manage-data upfront (same as live's data caching)
        self._prefetch_data(df_signals)

        # Main simulation loop — mirrors trader.process_strategy
        self._simulate(df_signals)

        self.log_reason_statistics()
        return pd.DataFrame(self.trade_log)

    def compute_equity_curve(self):
        super().compute_equity_curve()

    def calculate_performance_metrics(
        self,
        benchmark_ticker: str | None = None,
    ) -> dict:
        return super().calculate_performance_metrics(benchmark_ticker)

    # ==================================================================
    # Core loop — drives engine.process_strategy per bar
    # ==================================================================

    def _build_exec_context(self, df_signals: pd.DataFrame) -> BacktestExecContext:
        """Bundle shared state + optional prefetch bags for ``ExecutorRouter``."""
        ctx = BacktestExecContext(
            config=getattr(self.strategy, "config", self.config),
            engine=self.engine,
            ticker=self.ticker,
            logger=self.logger,
            df_signals=df_signals,
            df_manage=self._df_manage,
            df_manage_1m=self._df_manage_1m,
            active_positions=self._active_positions,
            trade_log=self.trade_log,
            starting_equity=self._running_equity,
            running_equity=self._running_equity,
            collateral_in_use=self._collateral_in_use,
            update_reason_stats=self.update_reason_stats,
        )
        return ctx

    def _simulate(self, df_signals: pd.DataFrame):
        """Per-bar loop calling ``engine.process_strategy`` — full live parity.

        ``ExecutorRouter`` exposes ``ExecutorProtocol`` so the engine's
        orchestration runs identically here and in ``trader.py``.  Per-asset
        fill simulation lives in child executors behind the router.
        """

        # Engine fetches bar data via ``self.data_handler``; in backtest
        # that's a thin wrapper over the pre-loaded DataFrames.
        signal_interval: Any | Literal["1h"] = getattr(
            self.strategy.config, "INTERVAL", "1h"
        )
        manage_interval = getattr(
            self.strategy.config, "MANAGE_INTERVAL", signal_interval
        )

        data_handler = BacktestDataHandler(
            df_signals=df_signals,
            df_manage=self._df_manage,
            signal_interval=signal_interval,
            manage_interval=manage_interval,
        )
        self.engine.data_handler = cast(
            "DataHandlerProtocol", data_handler
        )  # inject preloaded data handler

        # Cache full signal df for indicator injection inside call_manage_trade.
        self.engine.cache_signals(self.ticker, df_signals)

        executor = ExecutorRouter(self._build_exec_context(df_signals))

        # engine.process_strategy reads strategy.config.TICKERS — restrict
        # to the single backtest ticker for the duration of the run so
        # other tickers (if any) don't trigger empty fetches.
        cfg = self.strategy.config
        prev_tickers = getattr(cfg, "TICKERS", None)
        try:
            cfg.TICKERS = [self.ticker]
            for ts, _row in df_signals.iterrows():
                ts = cast("pd.Timestamp", ts)
                self.engine.process_strategy(self.strategy, ts, executor)
        finally:
            if prev_tickers is None:
                if hasattr(cfg, "TICKERS"):
                    try:
                        delattr(cfg, "TICKERS")
                    except AttributeError:
                        cfg.TICKERS = []
            else:
                cfg.TICKERS = prev_tickers

        # Close positions still active at end of backtest window.
        executor.force_close_remaining()
        # Sync ledger back from shared context.
        self._running_equity = executor.running_equity
        self._collateral_in_use = executor.collateral_in_use

    # ==================================================================
    # Data preparation — mirrors trader.py data fetching
    # ==================================================================

    def _detect_asset_class(self, df_signals: pd.DataFrame) -> str:
        for sig in df_signals["Signal"].dropna().unique():
            ac = self.engine.route(str(sig))
            if ac != "EQUITY":
                return ac
        return "EQUITY"

    def _prefetch_data(self, df_signals: pd.DataFrame):
        """Prefetch manage-resolution data before the simulation loop.

        Mirrors trader.py's ``fetch_or_update_cache`` calls — all data is
        fetched upfront before the simulation loop.
        """
        # Manage-resolution data (same as trader.py's manage_data_cache)
        manage_interval = getattr(self.config, "MANAGE_INTERVAL", "1h")
        self._df_manage = self.data_handler.get_historical_data(
            ticker=self.ticker,
            start=df_signals.index.min().isoformat(),
            end=self.end_date,
            interval=manage_interval,
        )
        if not self._df_manage.empty:
            self._df_manage = self._df_manage.sort_index()
