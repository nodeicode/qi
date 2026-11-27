"""
Unified Backtester — environment adapter over the backtest venue.
================================================================

Architecture (declarative, strategy-dataflow):

  1. **Data prefetch** — each declared ``(instrument, datatype, cadence)``
     subscription is fetched once upfront via ``_prefetch_data`` and seeded
     into a per-run ``BacktestDataHandler`` (replay, no re-parse per bar).
  2. **Per-bar loop** — the union of the subscriptions' closed-bar timestamps
     drives ``engine.process_strategy(strategy, now, venue)`` per tick: the
     engine serves closed bars from the seeded handler, drives the author body
     ``decide``, and submits typed orders to the backtest venue's execution
     client (the fill seam) — the exact same code path the live trader runs.
  3. **Fill simulation** — ``BacktestExecutionClient.submit`` prices fills in
     tradable space, end-of-run ``force_close_remaining`` closes stragglers,
     and the run ledger (positions, trade log, running equity) feeds
     ``Backtester.compute_equity_curve`` / ``calculate_performance_metrics``.

A single ``run_backtest`` drives everything.

Usage
-----
>>> from quant.backtest import Backtester
>>> bt = Backtester(config, strategy, "2024-01-01", "2024-06-01", "SPY")
>>> trades = bt.run_backtest()
>>> bt.compute_equity_curve()
>>> metrics = bt.calculate_performance_metrics()
"""

from __future__ import annotations

import logging
from typing import cast

import pandas as pd

from quant.backtest.backtest_data_handler import BacktestDataHandler
from quant.backtest.base_backtest import BaseBacktester
from quant.core.engine import TradingEngine
from quant.core.protocols import DataHandlerProtocol
from quant.data.subscription import Subscription
from quant.utils.strategy_config import BaseStrategyConfig
from quant.venue import (
    BacktestExecutionClient,
    registered_venues,
    venue_by_name,
    venue_registered,
)
from quant.venue.base import to_utc_naive


class Backtester(BaseBacktester):
    """Unified backtester driving ``engine.process_strategy`` via the backtest venue."""

    def __init__(
        self,
        config: BaseStrategyConfig,
        strategy,
        start_date: str,
        end_date: str,
        ticker: str,
        verbose: bool = False,
        data_handler=None,
        portfolio=None,
        venue: str = "backtest",
    ):
        super().__init__(
            config, strategy, start_date, end_date, ticker, verbose, data_handler
        )

        # The account the author body sizes/exits against (spec `portfolio`):
        # simulated fills stream into this Portfolio through the venue client,
        # and the engine injects its read-only view into ``decide``. Defaults to
        # a private portfolio so non-portfolio runs still close positions.
        self.portfolio = portfolio
        if self.portfolio is None:
            from quant.portfolio import Portfolio

            self.portfolio = Portfolio(starting_equity=float(self.initial_capital))

        # Resolve the declared venue id at construction (spec `execution` Venue
        # resolved from the strategy declaration): a registered id builds a run
        # and an unknown id fails loudly with the registered ids.
        _declared = getattr(strategy, "venue", "") or getattr(
            getattr(strategy, "config", None), "venue", ""
        )
        _declared_id = _declared or venue or "backtest"
        if not venue_registered(_declared_id):
            raise ValueError(
                f"Unknown venue {_declared_id!r}; registered venues: "
                f"{registered_venues()}"
            )
        # The run always fills through the deterministic simulation-timeline
        # venue; its data source / execution client are wired in ``_simulate``.
        self._venue = venue_by_name("backtest")
        self.venue = self._venue.id

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

        # Simulation state.
        self._running_equity: float = float(self.initial_capital)

        # Per-run data handler: seeded once with the subscription replay
        # buffers, then serves window slices with no re-parse per bar.
        self.engine = TradingEngine(
            self.strategy, cast("DataHandlerProtocol", self.data_handler)
        )
        self.engine.portfolio = self.portfolio

        # Raw 1h manage frame the execution client prices fills from (tradable).
        self._df_manage: pd.DataFrame = pd.DataFrame()
        # ``(Subscription, frame)`` pairs fed by ``_prefetch_data`` and seeded
        # into the per-run data handler in ``_simulate``.
        self._sub_frames: list[tuple[Subscription, pd.DataFrame]] = []

    # ==================================================================
    # Public API
    # ==================================================================

    def run_backtest(self) -> pd.DataFrame:
        """Drive the declarative engine over the subscription timeline.

        1. Prefetch each declared subscription's windows (replay buffers).
        2. Seed the per-run data handler + wire the venue.
        3. Per bar: ``engine.process_strategy`` → the venue simulates fills.
        """
        self._prefetch_data()
        self._simulate()
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
    # Declared surface resolution
    # ==================================================================

    def _resolve_subscriptions(self) -> list[Subscription]:
        config = getattr(self.strategy, "config", None)
        subs = list(getattr(config, "subscriptions", None) or [])
        if not subs:
            subs = list(getattr(self.strategy, "subscriptions", None) or [])
        return [s for s in subs if isinstance(s, Subscription)]

    # ==================================================================
    # Data preparation — mirrors the live data source, per subscription
    # ==================================================================

    def _prefetch_data(self) -> None:
        """Fetch each declared subscription's window once (no re-parse per bar).

        The signal cadence's bars also seed the raw manage frame the execution
        client prices tradable-space fills from.
        """
        subs = self._resolve_subscriptions()
        preload_start = (
            pd.Timestamp(self.start_date) - pd.Timedelta(days=self.config.PRE_LOAD_DAYS)
        ).isoformat()
        for sub in subs:
            frame = self._fulfill(sub, preload_start, self.end_date)
            self._sub_frames.append((sub, frame))
        # Tradable-space manage frame is the signal subscription's own bars (the
        # execution client prices raw fills from it). No subscription -> nothing
        # to trade -> an empty manage frame.
        self._df_manage = (
            self.data_handler.get_data(subs[0], preload_start, self.end_date)
            if subs
            else pd.DataFrame()
        )
        if self._df_manage is not None and not self._df_manage.empty:
            self._df_manage = self._df_manage.sort_index()
        elif self._df_manage is None:
            self._df_manage = pd.DataFrame()

    def _fulfill(self, sub: Subscription, start: str, end: str) -> pd.DataFrame:
        """Fulfill one subscription over ``[start, end]`` on the injected handler."""
        frame = self.data_handler.get_data(sub, start, end)
        if frame is None:
            return pd.DataFrame()
        return frame.sort_index()

    # ==================================================================
    # Core loop — drives engine.process_strategy per bar
    # ==================================================================

    def _build_execution_client(self) -> BacktestExecutionClient:
        """Bundle the run ledger + raw bar frames into the backtest fill engine."""
        client = BacktestExecutionClient(
            config=getattr(self.strategy, "config", self.config),
            ticker=self.ticker,
            logger=self.logger,
            df_signals=self._df_manage,
            df_manage=self._df_manage,
            active_positions=[],
            trade_log=self.trade_log,
            starting_equity=self._running_equity,
            running_equity=self._running_equity,
            portfolio=self.portfolio,
            venue=self.venue,
            update_reason_stats=self.update_reason_stats,
        )
        return client

    def _timeline(self) -> list[pd.Timestamp]:
        """Sorted union of closed-bar timestamps (>= run start) across subscriptions.

        This is the cadence the per-bar loop iterates; the engine's per-subscription
        cadence gate then decides which subscriptions are due on each tick, so a
        manage-resolution tick between signal bars simply skips the signal work.
        """
        start = pd.Timestamp(self.start_date)
        stamps: set[pd.Timestamp] = set()
        for _sub, frame in self._sub_frames:
            if frame is None or frame.empty:
                continue
            for idx in frame.index:
                ts = to_utc_naive(pd.Timestamp(idx))
                if ts >= start:
                    stamps.add(ts)
        return sorted(stamps)

    def _simulate(self) -> None:
        """Per-bar loop calling ``engine.process_strategy`` — full live parity.

        The engine's orchestration runs identically here and in ``trader.py``;
        the backtest venue's execution client is the fill seam it submits to,
        and owns the end-of-run forced-close.
        """
        data_handler = BacktestDataHandler(
            df_signals=pd.DataFrame(),
            df_manage=pd.DataFrame(),
            signal_interval="",
            manage_interval="",
        )
        # Replay the pre-fetched subscription buffers (fed once in
        # ``_prefetch_data``) so the engine window reads them with no re-parse.
        for sub, frame in self._sub_frames:
            data_handler.seed(sub, frame)
        self.engine.data_handler = cast("DataHandlerProtocol", data_handler)
        self._venue.data_source = data_handler
        self._venue.execution_client = self._build_execution_client()

        for ts in self._timeline():
            self.engine.process_strategy(self.strategy, ts, self._venue)

        # Close positions still active at end of the backtest window.
        self._venue.execution_client.force_close_remaining()
        # Sync ledger results back to the backtester.
        self._running_equity = self._venue.execution_client.running_equity
