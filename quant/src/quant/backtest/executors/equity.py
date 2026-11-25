from typing import cast

import pandas as pd

from quant.backtest.executors.base_executor import (
    BaseBacktestExecutor,
    tradable_close_at,
)
from quant.backtest.utils import apply_slippage, calculate_fill_time
from quant.portfolio import Fill, Side


class EquityExecutor(BaseBacktestExecutor):
    """Fill simulator for EQUITY signals (BUY / SELL / LONG / SHORT)."""

    def enter(
        self,
        strategy,
        ticker: str,
        signal_row: dict,
        engine,
        *,
        now=None,
    ) -> dict | None:
        """Open equity position. Returns the new position dict (or None)."""
        signal_time = now
        entry_time = calculate_fill_time(
            signal_time, self.config.INTERVAL, self.config.DATA_DELAY
        )
        try:
            row = self._df_signals.loc[now]
        except KeyError:
            row = pd.Series(signal_row)

        signal = str(signal_row.get("Signal", "BUY"))
        confidence = float(signal_row.get("Confidence", 0.0))
        direction = self.engine.direction(signal)

        df_entry = self._slice_manage(start=entry_time)
        if df_entry.empty:
            return None

        raw_entry = tradable_close_at(df_entry.iloc[0])
        entry_price = apply_slippage(self.config, raw_entry, direction, is_entry=True)

        # Fill quantity for the simulated entry: a fixed fraction of the
        # run's starting equity per trade, floored to whole shares. This is
        # the fill-simulation sizing leg (the registry dispatch it replaced
        # is deleted); strategy-owned sizing on instrument contract facts +
        # the portfolio lives on the author side in ``quant.strategies``.
        pos_size = float(row.get("Position_Size", 1.0))
        capital_frac = float(self.config.CAPITAL_PER_TRADE)
        effective_capital = self._constant_equity() * capital_frac * pos_size
        shares = int(effective_capital / entry_price) if entry_price > 0 else 0
        sizing = {
            "effective_capital": effective_capital,
            "shares": shares,
            "capital_fraction": capital_frac,
        }

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
        position["_asset_class"] = "EQUITY"
        position["_sizing"] = sizing
        position["_row"] = row
        position["_strategy_name"] = getattr(strategy, "name", None)
        self._active_positions.append(position)

        # Portfolio-collecting mode: stream the simulated entry fill through the
        # same Position machinery the live path uses (parity by construction).
        if self.portfolio is not None:
            direction = int(position.get("Direction", 1))
            shares = int(sizing.get("shares", 0) or 0)
            if shares > 0:
                self.portfolio.apply_fill(
                    Fill(
                        venue=self.venue,
                        symbol=self.ticker,
                        side=Side.BUY if direction >= 0 else Side.SELL,
                        qty=shares,
                        price=float(entry_price),
                        strategy=getattr(strategy, "name", None),
                        timestamp=entry_time,
                    )
                )

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
        """Close equity *position*. Returns True on success."""
        pos = sig.get("_position", position)

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
                "EquityExecutor.exit: manage_df empty after EntryTime trim for %s — skipping",
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

        exit_price = apply_slippage(
            self.config,
            tradable_close_at(exit_row),
            direction,
            is_entry=False,
        )

        sizing = pos.get("_sizing", {})
        eff_cap = sizing.get(
            "effective_capital",
            self._constant_equity() * float(self.config.CAPITAL_PER_TRADE),
        )
        pnl = (
            (
                (exit_price - entry_price)
                if direction == 1
                else (entry_price - exit_price)
            )
            * eff_cap
            / entry_price
            if entry_price
            else 0.0
        )

        self._running_equity += pnl

        # Portfolio-collecting mode: stream the simulated exit fill through the
        # same Position machinery the live path uses (parity by construction).
        if self.portfolio is not None:
            shares = int((sizing or {}).get("shares", 0) or 0)
            if shares > 0:
                self.portfolio.apply_fill(
                    Fill(
                        venue=self.venue,
                        symbol=str(pos.get("Ticker", self.ticker)),
                        side=Side.SELL if direction >= 0 else Side.BUY,
                        qty=shares,
                        price=float(exit_price),
                        strategy=strategy_name or pos.get("_strategy_name"),
                        timestamp=exit_time,
                    )
                )

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
                "Contracts": sizing.get("shares", 0),
                "PnL": pnl,
                "DurationMinutes": (exit_time - entry_time).total_seconds() / 60.0,
                "Reason": reason,
                "Confidence": pos.get("Confidence", 0),
            }
        )
        self._update_reason_stats(self.trade_log[-1])

        if pos in self._active_positions:
            self._active_positions.remove(pos)
        return True

    def force_close(self, position: dict) -> None:
        """Force-close equity position at last available manage bar."""
        if self._df_manage.empty:
            if position in self._active_positions:
                self._active_positions.remove(position)
            return
        sig = {
            "_position": position,
            "exit_idx": len(self._df_manage) - 1,
        }
        self.exit("", position, sig, forced=True, manage_df=self._df_manage)
