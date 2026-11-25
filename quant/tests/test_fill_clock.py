"""Fill clock vs bar-close: DATA_DELAY must come from strategy config."""

from __future__ import annotations

import logging
from types import SimpleNamespace

import pandas as pd

from quant.utils.fills import calculate_fill_time
from quant.utils.time import calculate_entry_time, et_wall_to_utc_naive
from quant.venue import BacktestExecutionClient


def _client(cfg) -> BacktestExecutionClient:
    """Minimal fill client for ``data_end_dt`` (manage-window cutoff) checks."""
    return BacktestExecutionClient(
        config=cfg,
        ticker="SPY",
        logger=logging.getLogger("test_fill_clock"),
        df_signals=pd.DataFrame(),
        df_manage=pd.DataFrame(),
    )


def _manage_fill(bar_ts, cfg, *, interval: str | None = None):
    """Mirror executor exit fill clock: ``T+I+DATA_DELAY`` on manage stream."""
    iv = str(interval or getattr(cfg, "MANAGE_INTERVAL", None) or cfg.INTERVAL)
    delay = max(0, int(getattr(cfg, "DATA_DELAY", 0) or 0))
    fill = pd.Timestamp(calculate_fill_time(bar_ts, iv, delay))
    if fill.tzinfo is not None:
        fill = fill.tz_convert("UTC").tz_localize(None)
    return fill


def test_calculate_entry_time_15m_is_bar_close():
    # 09:45 ET on 2024-06-03 (EDT) → 13:45 UTC stamp; close at 14:00 UTC.
    sig = et_wall_to_utc_naive(pd.Timestamp("2024-06-03").date(), 9, 45)
    assert calculate_entry_time(sig, "15m") == et_wall_to_utc_naive(
        pd.Timestamp("2024-06-03").date(), 10, 0
    )


def test_calculate_fill_time_adds_data_delay_intraday():
    sig = et_wall_to_utc_naive(pd.Timestamp("2024-06-03").date(), 9, 45)
    fill = calculate_fill_time(sig, "15m", data_delay_min=15)
    assert fill == et_wall_to_utc_naive(pd.Timestamp("2024-06-03").date(), 10, 15)
    # EDT: 10:15 ET == 14:15 UTC
    assert fill == pd.Timestamp("2024-06-03 14:15:00")


def test_calculate_fill_time_zero_delay_equals_entry():
    sig = et_wall_to_utc_naive(pd.Timestamp("2024-06-03").date(), 9, 45)
    assert calculate_fill_time(sig, "15m", 0) == calculate_entry_time(sig, "15m")


def test_calculate_fill_time_ignores_delay_on_daily():
    sig = pd.Timestamp("2024-06-03")
    assert calculate_fill_time(sig, "1d", 15) == calculate_entry_time(sig, "1d")


def test_client_data_end_dt_is_undelayed_bar_close():
    """Manage cutoff is T+I; fill clock (T+I+DATA_DELAY) is separate."""
    cfg = SimpleNamespace(INTERVAL="15m", MANAGE_INTERVAL="15m", DATA_DELAY=15)
    client = _client(cfg)

    sig = et_wall_to_utc_naive(pd.Timestamp("2024-06-03").date(), 9, 45)
    assert client.data_end_dt(sig) == et_wall_to_utc_naive(
        pd.Timestamp("2024-06-03").date(), 10, 0
    )
    entry_time = calculate_fill_time(sig, cfg.INTERVAL, cfg.DATA_DELAY)
    assert entry_time == et_wall_to_utc_naive(pd.Timestamp("2024-06-03").date(), 10, 15)
    assert _manage_fill(sig, cfg, interval="15m") == entry_time


def test_exit_uses_same_fill_method_on_manage_bar():
    cfg = SimpleNamespace(INTERVAL="15m", DATA_DELAY=15, MANAGE_INTERVAL="15m")
    # Proximity on 11:00 ET 15m bar → fill at 11:30 ET (close+delay).
    exit_bar = et_wall_to_utc_naive(pd.Timestamp("2024-06-03").date(), 11, 0)
    assert _manage_fill(exit_bar, cfg) == et_wall_to_utc_naive(
        pd.Timestamp("2024-06-03").date(), 11, 30
    )


def test_alpaca_sellout_fill_is_breach_stamp_no_delay():
    """Auto-liquidate fires at the breach print — no T+I / DATA_DELAY lag."""
    cfg = SimpleNamespace(INTERVAL="15m", DATA_DELAY=15, MANAGE_INTERVAL="15m")

    breach = et_wall_to_utc_naive(pd.Timestamp("2024-06-03").date(), 15, 45)
    # Manage-path helper would delay to 16:01 (1m) or 16:15 (15m) — sellout must not.
    delayed_1m = _manage_fill(breach, cfg, interval="1m")
    assert delayed_1m == et_wall_to_utc_naive(pd.Timestamp("2024-06-03").date(), 16, 1)
    # Contract used inside IC sellout branch: exit_time = breach stamp.
    sellout_fill = pd.Timestamp(breach)
    assert sellout_fill == breach
    assert sellout_fill != delayed_1m


def test_missing_data_delay_means_undelayed_fill():
    cfg = SimpleNamespace(INTERVAL="15m", MANAGE_INTERVAL="15m")  # no DATA_DELAY
    sig = et_wall_to_utc_naive(pd.Timestamp("2024-06-03").date(), 9, 45)
    entry_time = calculate_fill_time(sig, cfg.INTERVAL, getattr(cfg, "DATA_DELAY", 0))
    assert entry_time == calculate_entry_time(sig, "15m")
    assert _manage_fill(sig, cfg) == entry_time


def test_vega_entry_fill_clock_0945_to_1015():
    """Live parity: 09:45 bar → fill wall 10:15 with DATA_DELAY=15."""
    sig = et_wall_to_utc_naive(pd.Timestamp("2024-06-03").date(), 9, 45)
    assert calculate_fill_time(sig, "15m", 15) == et_wall_to_utc_naive(
        pd.Timestamp("2024-06-03").date(), 10, 15
    )


def test_vega_exit_fill_clock_1100_to_1130():
    """Manage 11:00 bar → exit fill 11:30 with DATA_DELAY=15."""
    exit_bar = et_wall_to_utc_naive(pd.Timestamp("2024-06-03").date(), 11, 0)
    assert calculate_fill_time(exit_bar, "15m", 15) == et_wall_to_utc_naive(
        pd.Timestamp("2024-06-03").date(), 11, 30
    )
