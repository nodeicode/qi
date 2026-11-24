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
from quant.backtest.executors import (
    BacktestExecContext,
    ExecutorRouter,
    ICPrefetch,
    OptionsPrefetch,
)
from quant.backtest.prefetchers import prefetch_ic_legs, prefetch_option_contracts
from quant.backtest.utils import calculate_fill_time
from quant.core.engine import TradingEngine
from quant.core.protocols import DataHandlerProtocol
from quant.data.options_data_handler import OptionDataHandler
from quant.utils.strategy_config import BaseStrategyConfig


class Backtester(BaseBacktester):
    """Unified backtester using ``engine.process_strategy`` via ``ExecutorRouter``.

    ``engine.route(signal)`` selects the asset class per signal; the router
    forwards enter/exit to the registered child executor.  EQUITY is registered
    by default; OPTION / SPREAD / IC land as their executors are added.
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
        self.option_data_handler = OptionDataHandler(provider="databento")

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
        self._expiration_cache: dict[str, str] = {}
        self._option_data_cache: dict[tuple, pd.DataFrame] = {}
        self._df_manage: pd.DataFrame = pd.DataFrame()
        self._df_manage_1m: pd.DataFrame = (
            pd.DataFrame()
        )  # 1m bars for last-hour breach/pin scan

        # IC diagnostics (skip reasons + prefetch health) surfaced to notebooks
        self._ic_drop_stats: dict[str, int] = {}
        self._ic_leg_prices: dict = {}
        self._ic_leg_cbbo: dict = {}
        self._ic_prefetch_stats: dict[str, object] = {}
        self._ic_prefetch_detail: list[dict] = []

    # ==================================================================
    # Public API
    # ==================================================================

    def run_backtest(self) -> pd.DataFrame:
        """Single loop driving all asset classes — mirrors ``trader.py``.

        1. Prepare signals (via ``engine.run_signals`` — same as live).
        2. Detect dominant asset class and prefetch option data if needed.
        3. Iterate signals: call ``engine.process_strategy`` per bar →
           ``ExecutorRouter`` dispatches entries/exits by asset class.
        """
        # Single prepare path for all strategies (signals + Raw_Close).
        df_signals = self.prepare_signals()

        if df_signals.empty:
            return pd.DataFrame()

        self._asset_class = self._detect_asset_class(df_signals)
        self.logger.info(f"Asset class: {self._asset_class}")

        # Prefetch option/manage data upfront (same as live's data caching)
        self._prefetch_data(df_signals)

        # Main simulation loop — mirrors trader.process_strategy
        self._simulate(df_signals)

        self.log_reason_statistics()
        return pd.DataFrame(self.trade_log)

    def compute_equity_curve(self):
        if self._asset_class in ("OPTION", "SPREAD", "IC"):
            self._compute_realized_equity_curve()
        else:
            super().compute_equity_curve()

    def calculate_performance_metrics(
        self,
        benchmark_ticker: str | None = None,
    ) -> dict:
        if self._asset_class in ("OPTION", "SPREAD", "IC"):
            return self._calculate_additive_performance_metrics(benchmark_ticker)
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
        if self._asset_class in ("OPTION", "SPREAD", "IC"):
            ctx.options = OptionsPrefetch(
                expiration_cache=self._expiration_cache,
                option_data_cache=self._option_data_cache,
                option_data_handler=self.option_data_handler,
            )
        if self._asset_class == "IC":
            ctx.ic = ICPrefetch(
                expiration_cache=self._expiration_cache,
                leg_prices=self._ic_leg_prices,
                leg_cbbo=self._ic_leg_cbbo,
                drop_stats=self._ic_drop_stats,
                option_data_handler=self.option_data_handler,
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
        """Prefetch manage-resolution data + option data.

        Mirrors trader.py's ``fetch_or_update_cache`` calls — all data
        is fetched upfront before the simulation loop.
        """
        use_options = self._asset_class in ("OPTION", "SPREAD", "IC")

        # IC-specific path fetches its own manage data (with correct
        # time window and interval), so skip the generic fetch here
        # to avoid a redundant API/cache call.
        if self._asset_class != "IC":
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

        if self._asset_class == "FUTURES":
            # Fail fast: outright futures must resolve to a registered
            # contract spec.  A mistyped/missing product root is a config
            # error — surface it before the bar loop, never mid-run.
            from quant.backtest.executors.futures import resolve_futures_root
            from quant.core._utils import PRODUCT_SPECS

            _root = resolve_futures_root(self.ticker, self.config)
            PRODUCT_SPECS.get(_root, strict=True)

        if not use_options:
            return

        # Expiration cache
        actionable_signals = df_signals[
            df_signals["Signal"].apply(lambda s: self.engine.is_actionable(str(s)))
        ]
        if actionable_signals.empty:
            return

        # Expiration cache keyed by entry dates (T+I / next-open).
        trading_days = sorted(
            {
                calculate_fill_time(
                    ts, self.config.INTERVAL, self.config.DATA_DELAY
                ).date()
                for ts in actionable_signals.index
            }
        )
        _min_dte = int(getattr(self.config, "MIN_OPTION_DTE", 1))
        _max_dte_cfg = getattr(self.config, "MAX_OPTION_DTE", None)
        _max_dte = int(_max_dte_cfg) if _max_dte_cfg is not None else _min_dte + 30
        _repair_max = int(getattr(self.config, "EXPIRATION_REPAIR_MAX_DAYS", 30))
        self._expiration_cache: dict[str, str] = (
            self.option_data_handler.build_expiration_cache(
                self.ticker,
                trading_days,
                min_dte=_min_dte,
                max_dte=_max_dte,
                repair_max_days=_repair_max,
            )
        )

        # Prefetch option data for OPTION / SPREAD
        if self._asset_class in ("OPTION", "SPREAD"):
            self._option_data_cache = (
                prefetch_option_contracts(
                    self.ticker,
                    self._asset_class,
                    actionable_signals,
                    self._expiration_cache,
                    self.option_data_handler,
                    self.config,
                )
                or {}
            )

        # IC-specific: prefetch 4-leg prices
        if self._asset_class == "IC":
            (
                self._df_manage,
                self._df_manage_1m,
                self._ic_leg_prices,
                self._ic_leg_cbbo,
                self._ic_prefetch_stats,
                self._ic_prefetch_detail,
            ) = prefetch_ic_legs(
                self.ticker,
                self.logger,
                self._ic_drop_stats,
                self._expiration_cache,
                self.config,
                df_signals,
                self.option_data_handler,
                self.data_handler,
            )
