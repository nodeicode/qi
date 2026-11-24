"""Fill simulator for OUTRIGHT futures (ES/MES/SR3/…).

The outright-futures backtest path is intentionally lean:

* **Bars are the signal data.**  The strategy computes indicators on futures
  OHLCV bars (continuous or dated symbol) and emits signals; ``parent`` bar
  data is the same series, so no option-style prefetch bag is needed — the
  generic ``BacktestDataHandler`` / manage-resolution path serves the fills.
* **Fills use bid/ask** (synthesized by ``FuturesDataHandler`` at every
  resolution) rounded to the product's tick; entry buys at ask-up-tick, exit
  sells at bid-down-tick — conservative, deterministic execution.
* **Contract math lives on ``ContractSpec``** (multiplier / tick), resolved
  by product root — P&L is ``(exit - entry) x multiplier x contracts x dir``
  with no per-product hardcoding here.

Entry timing reuses the shared equity fill clock (``calculate_fill_time``,
T+I+DATA_DELAY) and the session-agnostic intraday branch of the engine's bar
selection, so futures strategies trading 1m-1h bars fall out of the box.
Daily-interval futures strategies (which would hit the equity 09:30 ET
next-BDay rule) are a later enhancement behind :data:`SESSION_CLOCKS`.
"""

from __future__ import annotations

import math
from typing import cast

import pandas as pd

from quant.backtest.executors.base_executor import BaseBacktestExecutor
from quant.backtest.utils import calculate_fill_time
from quant.core._utils import PRODUCT_SPECS
from quant.core.sizing import size_for_signal


def resolve_futures_root(ticker: str, config) -> str:
    """Product root for *ticker* — explicit ``FUTURES_ROOT`` or the symbol stem.

    Continuous Databento symbols are bare roots (``MES``); dated outright
    symbols are ``ROOT.<exp>``-style (``MES.MAR25``) — the stem before the
    first ``.`` is the registered product root.
    """
    explicit = getattr(config, "FUTURES_ROOT", None)
    if explicit:
        return str(explicit).upper()
    return str(ticker).split(".")[0].upper()


class FuturesExecutor(BaseBacktestExecutor):
    """Fill simulator for FUTURES signals (long/short outright futures)."""

    #: Keep the asset class out of the router registry (str).
    ASSET_CLASS = "FUTURES"

    def enter(
        self,
        strategy,
        ticker: str,
        signal_row: dict,
        engine,
        *,
        now=None,
    ) -> dict | None:
        """Open an outright futures position. Returns the new position dict (or None)."""
        # Fail loud on an unregistered product root — a mistyped/missing
        # product table is a programming error, not a skip signal.
        root = resolve_futures_root(ticker, self.config)
        spec = PRODUCT_SPECS.get(root, strict=True)

        signal_time = now
        entry_time = calculate_fill_time(
            signal_time, self.config.INTERVAL, self.config.DATA_DELAY
        )
        try:
            row = self._df_signals.loc[now]
        except KeyError:
            row = pd.Series(signal_row)

        signal = str(signal_row.get("Signal", ""))
        confidence = float(signal_row.get("Confidence", 0.0))
        direction = self.engine.direction(signal)

        df_entry = self._slice_manage(start=entry_time)
        if df_entry.empty:
            return None

        entry_bar = df_entry.iloc[0]
        entry_price = self._fill_price(entry_bar, direction, spec, is_entry=True)

        pos_size = float(row.get("Position_Size", 1.0))
        sizing = size_for_signal(
            signal,
            config=self.config,
            equity=self._constant_equity(),
            entry_price=entry_price,
            pos_size=pos_size,
            root=root,
        )
        n_contracts = int(sizing.get("n_contracts", 0))
        if n_contracts < 1:
            self.logger.info(
                "FUTURES-SKIP [sizing_zero] %s %s equity=%.0f price=%.2f root=%s",
                entry_time,
                signal,
                self._constant_equity(),
                entry_price,
                root,
            )
            return None

        position = dict(
            self.engine.build_position(
                entry_time=entry_time,
                entry_price=entry_price,
                direction=direction,
                signal=signal,
                confidence=confidence,
                PositionSize=pos_size,
            )
        )
        position["Ticker"] = self.ticker
        position["_asset_class"] = "FUTURES"
        position["AssetType"] = "FUTURES"
        position["root"] = root
        position["_sizing"] = sizing
        position["_row"] = row
        self._active_positions.append(position)

        return self._active_positions[-1]

    def exit(
        self,
        strategy_name: str,
        position: dict,
        sig: dict,
        *,
        forced: bool = False,
        manage_df: pd.DataFrame | None = None,
    ) -> bool:
        """Close an outright futures *position*. Returns True on success."""
        pos = sig.get("_position", position)
        root = str(pos.get("root", resolve_futures_root(self.ticker, self.config)))
        spec = PRODUCT_SPECS.get(root, strict=True)

        if manage_df is None:
            manage_df = self._df_manage

        entry_time = pos.get("EntryTime")
        if entry_time is not None:
            _et = pd.Timestamp(entry_time)
            if pd.notna(_et):
                _trimmed = manage_df[manage_df.index >= _et]
                if not _trimmed.empty:
                    manage_df = _trimmed

        if manage_df.empty:
            self.logger.warning(
                "FuturesExecutor.exit: manage_df empty after EntryTime trim for %s — skipping",
                pos.get("Ticker"),
            )
            if pos in self._active_positions:
                self._active_positions.remove(pos)
            return True

        exit_idx = sig.get("exit_idx", len(manage_df) - 1)
        if exit_idx >= len(manage_df):
            exit_idx = len(manage_df) - 1
        exit_row = manage_df.iloc[exit_idx]
        exit_bar = cast("pd.Timestamp", exit_row.name)
        exit_bar = pd.Timestamp(exit_bar)

        exit_time = calculate_fill_time(
            exit_bar,
            self.config.MANAGE_INTERVAL or self.config.INTERVAL,
            self.config.DATA_DELAY,
        )

        direction = pos.get("Direction", 1)
        entry_price = pos["EntryPrice"]
        entry_time = pos["EntryTime"]
        exit_price = self._fill_price(exit_row, direction, spec, is_entry=False)

        sizing = pos.get("_sizing", {})
        n_contracts = int(sizing.get("n_contracts", 0))

        pnl = spec.pnl(entry_price, exit_price, n_contracts, int(direction))
        self._running_equity += pnl

        signal_row = pos.get("_row")
        reason_default = "Force_Close" if forced else "Signal"
        reason = (
            signal_row.get("Reason", reason_default)
            if signal_row is not None
            else reason_default
        )

        self.trade_log.append(
            {
                "EntryTime": entry_time,
                "ExitTime": exit_time,
                "Signal": pos.get("Signal", ""),
                "Direction": direction,
                "EntryPrice": entry_price,
                "ExitPrice": exit_price,
                "Contracts": n_contracts,
                "PnL": pnl,
                "DurationMinutes": (exit_time - entry_time).total_seconds() / 60.0,
                "Reason": reason,
                "Confidence": pos.get("Confidence", 0),
                "Root": root,
                "TickValue": spec.tick_value,
                "Multiplier": spec.multiplier,
            }
        )
        self._update_reason_stats(self.trade_log[-1])

        if pos in self._active_positions:
            self._active_positions.remove(pos)
        return True

    def force_close(self, position: dict) -> None:
        """Force-close outright futures position at last available manage bar."""
        if self._df_manage.empty:
            if position in self._active_positions:
                self._active_positions.remove(position)
            return
        sig = {
            "_position": position,
            "exit_idx": len(self._df_manage) - 1,
        }
        self.exit("", position, sig, forced=True, manage_df=self._df_manage)

    # ── Fill helpers ────────────────────────────────────────────────────
    def _fill_price(
        self,
        bar: pd.Series,
        direction: int,
        spec,
        *,
        is_entry: bool,
    ) -> float:
        """Fill price from a futures bar, rounded to the product tick.

        Entry: buyer pays ask / short pays bid.  Exit: long sells at bid /
        short covers at ask — least-favorable side on both legs.  Falls back
        to ``Raw_Close`` (tradable spot) then ``Close`` when bid/ask are
        absent (plain OHLCV handlers).
        """
        side = "ask" if (direction == 1) == is_entry else "bid"

        raw = None
        for col in (side, "Raw_Close", "Close"):
            if col in bar.index and pd.notna(bar[col]):
                raw = float(bar[col])
                break
        if raw is None or not math.isfinite(raw):
            return 0.0

        if getattr(self.config, "FUTURES_ROUND_TO_TICK", True):
            return spec.round_price(raw, side=side)
        return raw
