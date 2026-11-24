"""Monitor sidecar process — metrics JSONL, shadow signals, Alpaca trade reconcile.

Runs in a child process (own GIL). Never called from the trading hot path
except via the multiprocessing queue drained here.
"""

from __future__ import annotations

import contextlib
import datetime
import json
import logging
import os
import queue
import threading
import time
import zoneinfo
from collections import deque
from typing import Any

from quant.core.monitor_utils import (
    ShadowEntryResult,
    compare_manage_exits,
    compare_signals,
    is_actionable_signal,
    shadow_manage_exit,
    staleness_excess_ns,
)
from quant.core.monitor_utils import (
    shadow_entry_signal as shadow_backtest_signal,
)
from quant.observability import SLO_SPAN_KEYS

logger = logging.getLogger("monitor.sidecar")

_GRACE_SECS = int(os.environ.get("MONITOR_TRADE_GRACE_SECS", "600"))
_ROLLUP_SECS = 60
_ORDER_LOOKBACK_SECS = int(os.environ.get("MONITOR_ORDER_LOOKBACK_SECS", "7200"))
_MATCH_SLACK_SECS = 120
_RTH_OPEN_ET_MIN = 570  # 09:30 ET — matches data_handler._filter_market_hours
_ET = zoneinfo.ZoneInfo("America/New_York")
_DAILY_INTERVALS = frozenset({"1d", "1wk", "1mo"})
_SHADOW_CACHE_MAX = 64
# Freshness SLO: excess beyond DATA_DELAY (ms) that trips a breach flag.
# Deliberately hard-coded (D4) — no env override anywhere. This constant and
# the dashboard's staleness-excess SLO line MUST share one value (10 ms).
# At 10 ms the flag reflects minute-alignment / dispatch jitter, which the
# tick_dispatch_ns / main_loop_lag_ns series contextualise.
_STALENESS_SLO_MS = 10

_DEAD_ORDER = frozenset({"canceled", "cancelled", "expired", "rejected"})
_SHADOW_JOB_SHUTDOWN = object()

# Manage parity blind-spot guard: ``manage_evaluated`` and ``signal_evaluated``
# are emitted in the same engine tick, so an open position on a manage phase
# owes a ``manage_evaluated`` event every bar.  When none arrive, the whole
# manage parity plane (``shadow_manage_result`` / ``manage_exit_drift``) goes
# silently dead — flag it instead.  Grace window absorbs queue-order jitter;
# cooldown rate-limits the alert when the emitter flaps.
_MANAGE_EVAL_GRACE_SECS = 300
_MANAGE_MISSING_COOLDOWN_SECS = 1800


def _is_actionable(signal: str) -> bool:
    """Compatibility shim; shared entry semantics live in core.monitor_utils."""
    return is_actionable_signal(signal)


class MetricsWriter:
    """Persistent JSONL metrics writer backed by the stdlib logging writer.

    Uses ``logging.FileHandler`` (the canonical high-performance Python logging
    building block): it opens the destination once and keeps an internal stream
    open across writes (no per-event open/close syscalls — a queue-lag win),
    writes line-buffered via ``StreamHandler``/``Formatter``, and owns the fd
    lifecycle internally (``flush()``/``close()``). Because the persistent open
    lives inside the stdlib, no bare ``open()`` appears in our hot path — so it
    is SIM115-clean and has no manual fd-management to get wrong.
    """

    def __init__(self, path: str) -> None:
        self._handler = logging.FileHandler(path, mode="a", encoding="utf-8")
        self._formatter = logging.Formatter()
        self._handler.setFormatter(self._formatter)

    def write(self, line: str) -> None:
        """Append one record (JSONL), flushing so readers always see it."""
        rec = logging.LogRecord(
            name="monitor.sidecar.metrics",
            level=logging.INFO,
            pathname=__file__,
            lineno=0,
            msg=line.rstrip("\n"),
            args=(),
            exc_info=None,
        )
        self._handler.emit(rec)
        self._handler.flush()

    def flush(self) -> None:
        """Flush buffered output (e.g. on rollup)."""
        self._handler.flush()

    def close(self) -> None:
        """Flush and release the underlying stream (owner on shutdown)."""
        self._handler.close()


def _setup_logging() -> None:
    if logger.handlers:
        return
    handler = logging.StreamHandler()
    handler.setFormatter(
        logging.Formatter("%(asctime)s - %(levelname)s - %(name)s - %(message)s")
    )
    logger.addHandler(handler)
    logger.setLevel(logging.INFO)
    logger.propagate = False


def _percentile(values: list[int], p: float) -> int | None:
    if not values:
        return None
    s = sorted(values)
    idx = min(len(s) - 1, max(0, round((p / 100.0) * (len(s) - 1))))
    return s[idx]


def pd_timestamp_iso(bar_ts: Any) -> str:
    import pandas as pd

    return pd.Timestamp(bar_ts).isoformat()


def _underlying_from_symbol(symbol: str) -> str:
    """Map equity or OCC option symbol to underlying ticker root."""
    sym = (symbol or "").upper().strip()
    if not sym:
        return ""
    # OCC: ROOT + YYMMDD + C/P + strike — root is leading letters
    i = 0
    while i < len(sym) and sym[i].isalpha():
        i += 1
    root = sym[:i] if i else sym
    return root.rstrip()


def _order_underlying(order: Any) -> str:
    symbol = getattr(order, "symbol", None) or ""
    if symbol:
        return _underlying_from_symbol(str(symbol))
    legs = getattr(order, "legs", None) or []
    for leg in legs:
        leg_sym = getattr(leg, "symbol", None) or (
            leg.get("symbol") if isinstance(leg, dict) else None
        )
        if leg_sym:
            return _underlying_from_symbol(str(leg_sym))
    return ""


def _order_submitted_at(order: Any) -> datetime.datetime | None:
    ts = getattr(order, "submitted_at", None) or getattr(order, "created_at", None)
    if ts is None:
        return None
    if isinstance(ts, datetime.datetime):
        if ts.tzinfo is None:
            return ts.replace(tzinfo=datetime.UTC)
        return ts
    import pandas as pd

    p = pd.Timestamp(ts)
    if p.tzinfo is None:
        p = p.tz_localize("UTC")
    return p.to_pydatetime()


def _data_delay_minutes(event: dict[str, Any]) -> int:
    """Resolve ``DATA_DELAY`` from the live config snapshot (minutes)."""
    snap = event.get("config_snapshot")
    if isinstance(snap, dict) and "DATA_DELAY" in snap:
        with contextlib.suppress(TypeError, ValueError):
            return max(0, int(snap["DATA_DELAY"]))
    if "DATA_DELAY" in event:
        with contextlib.suppress(TypeError, ValueError):
            return max(0, int(event["DATA_DELAY"]))
    return 0


def is_freshness_sampleable(event: dict[str, Any]) -> bool:
    """True when bar availability — not a strategy clock — sets the action time.

    Only such bars belong in the freshness SLO, because only for them does
    "late" mean "we were slow". Excluded:

    * **Daily-like intervals** — an entry-time-scheduled strategy (``IC_ENTRY_*``)
      acts on yesterday's session bar hours after it closed by design, so its
      staleness measures the schedule, not our lag.
    * **``expected_delay_gap``** — pre-RTH stamps selected under ``DATA_DELAY``
      at the open are stale by construction and were never actionable.
    """
    if event.get("expected_delay_gap"):
        return False
    interval = str(event.get("interval") or "").strip().lower()
    return interval not in _DAILY_INTERVALS


def _bar_et_minutes(bar_ts: Any) -> int | None:
    """Minutes from midnight ET for *bar_ts*, or None if unparseable."""
    if bar_ts is None or bar_ts == "":
        return None
    try:
        import pandas as pd

        ts = pd.Timestamp(bar_ts)
        if ts.tzinfo is None:
            ts = ts.tz_localize("UTC")
        et = ts.tz_convert(_ET)
        return int(et.hour * 60 + et.minute)
    except Exception:
        return None


def is_expected_delay_gap(
    bar_ts: Any,
    *,
    interval: str,
    data_delay_min: int,
) -> bool:
    """True when *bar_ts* is a pre-RTH stamp caused by ``DATA_DELAY`` at open.

    With SIP delay, ``closed_bar_asof(now - DATA_DELAY)`` at session open can
    select 09:00 / 09:15 ET opens that RTH-filtered frames never contain.
    Only applies when ``DATA_DELAY > 0`` and *interval* is intraday.
    """
    if data_delay_min <= 0:
        return False
    if (interval or "").strip().lower() in _DAILY_INTERVALS:
        return False
    mins = _bar_et_minutes(bar_ts)
    if mins is None:
        return False
    return mins < _RTH_OPEN_ET_MIN


def _shadow_cache_key(
    strategy_name: str, ticker: str, bar_ts: str
) -> tuple[str, str, str]:
    base = strategy_name.rsplit("_", 1)[0] if strategy_name else ""
    return (base, ticker.upper(), str(bar_ts or ""))


def _run_manage_shadow_job(state: SidecarState, event: dict[str, Any]) -> list[str]:
    """Run the backtest-reference manage decision and persist exact parity."""
    try:
        shadow = shadow_manage_exit(
            strategy_name=str(event["strategy_name"]),
            ticker=str(event["ticker"]),
            position=event["position"],
            end_dt=str(event["end_dt"]),
            interval=str(event.get("interval") or "1h"),
            manage_interval=str(event.get("manage_interval") or "") or None,
            config_snapshot=event.get("config_snapshot")
            if isinstance(event.get("config_snapshot"), dict)
            else None,
        )
    except Exception as e:
        logger.exception(
            "[MONITOR] shadow_manage_exit failed strategy=%s ticker=%s: %s",
            event.get("strategy_name"),
            event.get("ticker"),
            e,
        )
        with contextlib.suppress(Exception):
            state.append_metrics(
                {
                    **event,
                    "kind": "shadow_manage_result",
                    "shadow_failed": True,
                    "shadow_error": str(e),
                }
            )
        return ["shadow_manage_failed"]

    out = {
        **event,
        "kind": "shadow_manage_result",
        "shadow_exit": shadow["exit_class"],
        "shadow_exit_bar": shadow["exit_bar"],
        "shadow_exit_idx": shadow["exit_idx"],
        "shadow_manage_bar_ts": shadow["manage_bar_ts"],
        "shadow_manage_ns": shadow["shadow_manage_ns"],
    }
    with contextlib.suppress(Exception):
        state.append_metrics(out)
    drift = compare_manage_exits(
        event.get("live_exit", "hold"),
        event.get("exit_bar"),
        shadow["exit_class"],
        shadow["exit_bar"],
    )
    if drift:
        logger.error(
            "[MONITOR] manage_exit_drift strategy=%s ticker=%s entry=%s live=%s/%s shadow=%s/%s",
            event.get("strategy_name"),
            event.get("ticker"),
            event.get("entry_time"),
            event.get("live_exit"),
            event.get("exit_bar"),
            shadow["exit_class"],
            shadow["exit_bar"],
        )
        return [drift]
    return []


def match_order_to_pending(
    order: Any,
    pending: dict[str, dict[str, Any]],
    *,
    now: datetime.datetime,
    grace_secs: int = _GRACE_SECS,
    slack_secs: int = _MATCH_SLACK_SECS,
) -> str | None:
    """Return pending key if *order* matches an expected entry, else None."""
    under = _order_underlying(order)
    submitted = _order_submitted_at(order)
    if not under or submitted is None:
        return None
    status = str(getattr(order, "status", "") or "").lower()
    if status in _DEAD_ORDER:
        return None

    for key, exp in pending.items():
        if str(exp.get("ticker", "")).upper() != under:
            continue
        entry = exp.get("entry_time")
        if not entry:
            continue
        import pandas as pd

        entry_dt = pd.Timestamp(entry).to_pydatetime()
        if entry_dt.tzinfo is None:
            entry_dt = entry_dt.replace(tzinfo=datetime.UTC)
        lo = entry_dt - datetime.timedelta(seconds=slack_secs)
        hi = entry_dt + datetime.timedelta(seconds=grace_secs)
        if lo <= submitted <= hi:
            return key
    return None


class SidecarState:
    # Durations — sampled from every event that carries them. Single source of
    # truth: the shared SpanName-derived sample registry (SPAN_KEYS minus the
    # non-SLO event-local spans), so producer and consumer can never drift.
    _SPAN_KEYS = SLO_SPAN_KEYS
    # Freshness — sampled only from SLO-eligible bars (is_freshness_sampleable).
    _FRESHNESS_KEYS = ("bar_staleness_excess_ns",)

    def __init__(self, metrics_path: str, state_path: str) -> None:
        self.metrics_path = metrics_path
        self.state_path = state_path
        self.pending: dict[str, dict[str, Any]] = {}
        self.matched_order_ids: set[str] = set()
        self.watched_tickers: set[str] = set()
        self.client_names: set[str] = set()
        self._span_samples: dict[str, deque[int]] = {
            key: deque(maxlen=2000)
            for key in (*self._SPAN_KEYS, *self._FRESHNESS_KEYS, "queue_lag_ns")
        }
        # Non-SLO warmup durations (startup / pre-open only). Kept separate from
        # ``_span_samples`` so a slow preload never pollutes the hot-path,
        # fresh-generation, or queue-lag latency SLOs.
        self._warmup_samples: deque[int] = deque(maxlen=64)
        self._last_rollup = time.monotonic()
        self._drops_since_rollup = 0
        # Persistent JSONL writer (open-once fd, flush-per-write). Removes the
        # per-event open/close syscalls on the drain loop — a queue-lag win —
        # while owning its lifecycle (see MetricsWriter) so no bare open() appears
        # in the hot path.
        self._metrics = MetricsWriter(metrics_path)
        # (base_strategy, ticker, bar_ts) → shadow result dict
        self._shadow_cache: dict[tuple[str, str, str], ShadowEntryResult] = {}
        self._shadow_cache_order: deque[tuple[str, str, str]] = deque()
        # (strategy_name, ticker) → wall ns of the last manage_evaluated arrival,
        # and of the last manage_telemetry_missing flag (rate-limit).
        self._last_manage_eval_ns: dict[tuple[str, str], int] = {}
        self._manage_missing_flagged_ns: dict[tuple[str, str], int] = {}
        self._lock = threading.Lock()
        self._load_state()

    def get_shadow_cached(
        self, strategy_name: str, ticker: str, bar_ts: str
    ) -> ShadowEntryResult | None:
        key = _shadow_cache_key(strategy_name, ticker, bar_ts)
        with self._lock:
            hit = self._shadow_cache.get(key)
            return hit if hit is not None else None

    def put_shadow_cached(
        self, strategy_name: str, ticker: str, bar_ts: str, shadow: ShadowEntryResult
    ) -> None:
        key = _shadow_cache_key(strategy_name, ticker, bar_ts)
        with self._lock:
            if key not in self._shadow_cache:
                self._shadow_cache_order.append(key)
                while len(self._shadow_cache_order) > _SHADOW_CACHE_MAX:
                    old = self._shadow_cache_order.popleft()
                    self._shadow_cache.pop(old, None)
            self._shadow_cache[key] = shadow

    def record_manage_eval(self, strategy_name: str, ticker: str) -> None:
        """Note a ``manage_evaluated`` arrival so the presence guard stays quiet."""
        s_name = str(strategy_name or "")
        sym = str(ticker or "").upper()
        if s_name and sym:
            self._last_manage_eval_ns[(s_name, sym)] = time.time_ns()

    def manage_telemetry_missing(self, strategy_name: str, ticker: str) -> bool:
        """True when a manage_evaluated event is owed but none arrived recently.

        Rate-limited to ``_MANAGE_MISSING_COOLDOWN_SECS`` per strategy/ticker so
        a dead manage plane alerts once, not on every bar.
        """
        key = (str(strategy_name or ""), str(ticker or "").upper())
        if not key[0] or not key[1]:
            return False
        now = time.time_ns()
        last = self._last_manage_eval_ns.get(key)
        if last is not None and (now - last) < _MANAGE_EVAL_GRACE_SECS * 1_000_000_000:
            return False
        last_flagged = self._manage_missing_flagged_ns.get(key, 0)
        if (now - last_flagged) < _MANAGE_MISSING_COOLDOWN_SECS * 1_000_000_000:
            return False
        self._manage_missing_flagged_ns[key] = now
        return True

    def _load_state(self) -> None:
        if not os.path.exists(self.state_path):
            return
        try:
            with open(self.state_path, encoding="utf-8") as f:
                raw = json.load(f)
            self.pending = dict(raw.get("pending") or {})
            self.matched_order_ids = set(raw.get("matched_order_ids") or [])
            self.watched_tickers = set(raw.get("watched_tickers") or [])
            self.client_names = set(raw.get("client_names") or [])
        except Exception as e:
            logger.warning("[MONITOR] failed to load state %s: %s", self.state_path, e)

    def save_state(self) -> None:
        payload = {
            "pending": self.pending,
            "matched_order_ids": sorted(self.matched_order_ids)[-500:],
            "watched_tickers": sorted(self.watched_tickers),
            "client_names": sorted(self.client_names),
        }
        tmp = self.state_path + ".tmp"
        try:
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(payload, f)
            os.replace(tmp, self.state_path)
        except Exception as e:
            logger.warning("[MONITOR] failed to save state: %s", e)

    def flush_metrics(self) -> None:
        """Flush the buffered metrics writer (on rollup / shutdown)."""
        with contextlib.suppress(Exception):
            self._metrics.flush()

    def close_metrics(self) -> None:
        """Close the buffered metrics writer (on shutdown)."""
        with contextlib.suppress(Exception):
            self._metrics.close()

    def append_metrics(self, event: dict[str, Any]) -> None:
        line = json.dumps(event, default=str, separators=(",", ":")) + "\n"
        with contextlib.suppress(Exception):
            self._metrics.write(line)

    def record_warmup(self, total_ns: int) -> None:
        """Bank one warmup duration (non-SLO telemetry)."""
        if isinstance(total_ns, int) and total_ns >= 0:
            self._warmup_samples.append(total_ns)

    def record_spans(self, event: dict[str, Any], queue_lag_ns: int | None) -> None:
        for key in self._SPAN_KEYS:
            v = event.get(key)
            if isinstance(v, int):
                self._span_samples[key].append(v)
        if is_freshness_sampleable(event):
            for key in self._FRESHNESS_KEYS:
                v = event.get(key)
                if isinstance(v, int):
                    self._span_samples[key].append(v)
        if isinstance(queue_lag_ns, int):
            self._span_samples["queue_lag_ns"].append(queue_lag_ns)

    def accumulate_drops(self, n: int) -> None:
        """Bank drops read from the shared counter until the next successful rollup."""
        if n > 0:
            self._drops_since_rollup += int(n)

    def maybe_rollup(self) -> list[str]:
        """Emit latency summary. Only clears banked drops when a rollup actually fires.

        Returns the flag names raised by this rollup (drops / freshness breach).
        """
        now = time.monotonic()
        if now - self._last_rollup < _ROLLUP_SECS:
            return []
        self._last_rollup = now
        self.flush_metrics()
        flags: list[str] = []
        drops = self._drops_since_rollup
        self._drops_since_rollup = 0
        parts = []
        for key, samples in self._span_samples.items():
            vals = list(samples)
            p50 = _percentile(vals, 50)
            p99 = _percentile(vals, 99)
            if p50 is not None:
                parts.append(f"{key}_p50={p50} p99={p99}")
        logger.info(
            "[MONITOR][LATENCY] %s drops=%s pending=%s",
            " ".join(parts),
            drops,
            len(self.pending),
        )
        if drops > 0:
            logger.error(
                "[MONITOR][LATENCY] queue drops=%s — monitor falling behind", drops
            )
            flags.append("queue_drops")

        stale_p99 = _percentile(list(self._span_samples["bar_staleness_excess_ns"]), 99)
        if stale_p99 is not None and stale_p99 > _STALENESS_SLO_MS * 1_000_000:
            logger.error(
                "[MONITOR] bar_staleness_breach excess_p99=%.0fms budget=%sms — "
                "bars are being acted on later than the feed delay explains",
                stale_p99 / 1e6,
                _STALENESS_SLO_MS,
            )
            flags.append("bar_staleness_breach")

        # Non-SLO series: warmup is a startup/pre-open cost, never a hot-path
        # SLO breach. Written to the JSONL as its own event so dashboards can
        # show preload cost (P50/P99) without touching the latency SLO lines.
        if self._warmup_samples:
            warm_vals = list(self._warmup_samples)
            self.append_metrics(
                {
                    "kind": "warmup_rollup",
                    "warmup_total_p50_ns": _percentile(warm_vals, 50),
                    "warmup_total_p99_ns": _percentile(warm_vals, 99),
                    "samples": len(warm_vals),
                    "emit_wall_ns": time.time_ns(),
                }
            )
            self._warmup_samples.clear()
        return flags


def _trading_clients() -> dict[str, Any]:
    try:
        from quant.data.data_handler import AlpacaDataHandler

        return AlpacaDataHandler().clients
    except Exception as e:
        logger.warning("[MONITOR] could not init Alpaca clients: %s", e)
        return {}


def _fetch_orders(client: Any, after: datetime.datetime) -> list[Any]:
    try:
        from alpaca.trading.enums import QueryOrderStatus
        from alpaca.trading.requests import GetOrdersRequest

        req = GetOrdersRequest(
            status=QueryOrderStatus.ALL,
            after=after,
            limit=500,
            nested=True,
        )
        orders = client.get_orders(filter=req)
        return list(orders or [])
    except Exception as e:
        logger.warning("[MONITOR] get_orders failed: %s", e)
        return []


def reconcile_orders(
    state: SidecarState, clients: dict[str, Any] | None = None
) -> list[str]:
    """Age pending expectations and flag the two critical trade drifts."""
    flags: list[str] = []
    now = datetime.datetime.now(datetime.UTC)
    after = now - datetime.timedelta(seconds=_ORDER_LOOKBACK_SECS)
    clients = clients if clients is not None else _trading_clients()

    # Collect live orders per client
    all_orders: list[tuple[str, Any]] = []
    for cname in sorted(state.client_names or clients.keys()):
        client = clients.get(cname)
        if client is None:
            continue
        for order in _fetch_orders(client, after):
            all_orders.append((cname, order))

    # Match pending → orders (lock: shadow worker may mutate pending concurrently)
    with state._lock:
        still_pending: dict[str, dict[str, Any]] = {}
        for key, exp in list(state.pending.items()):
            matched = False
            for cname, order in all_orders:
                if exp.get("client_name") and cname != exp.get("client_name"):
                    continue
                oid = str(getattr(order, "id", "") or "")
                if oid and oid in state.matched_order_ids:
                    continue
                if match_order_to_pending(order, {key: exp}, now=now) == key:
                    matched = True
                    if oid:
                        state.matched_order_ids.add(oid)
                    break
            if matched:
                continue

            created = exp.get("detect_wall_ns")
            age_ok = True
            if isinstance(created, int):
                age_secs = (time.time_ns() - created) / 1e9
                age_ok = age_secs >= _GRACE_SECS
            else:
                # Fallback: entry_time + grace
                import pandas as pd

                try:
                    entry_dt = pd.Timestamp(exp["entry_time"]).to_pydatetime()
                    if entry_dt.tzinfo is None:
                        entry_dt = entry_dt.replace(tzinfo=datetime.UTC)
                    age_ok = now >= entry_dt + datetime.timedelta(seconds=_GRACE_SECS)
                except Exception:
                    age_ok = False

            if age_ok:
                msg = (
                    f"[MONITOR] signal_without_trade strategy={exp.get('strategy_name')} "
                    f"ticker={exp.get('ticker')} bar_ts={exp.get('bar_ts')} "
                    f"shadow={exp.get('shadow_signal')} entry_time={exp.get('entry_time')}"
                )
                logger.error(msg)
                flags.append("signal_without_trade")
            else:
                still_pending[key] = exp

        state.pending = still_pending

        # Orders without expected signal (recent only — avoid historical flood)
        unexpected_horizon = now - datetime.timedelta(seconds=max(_GRACE_SECS, 1800))
        for cname, order in all_orders:
            oid = str(getattr(order, "id", "") or "")
            if oid and oid in state.matched_order_ids:
                continue
            status = str(getattr(order, "status", "") or "").lower()
            if status in _DEAD_ORDER:
                continue
            under = _order_underlying(order)
            if not under:
                continue
            if state.watched_tickers and under not in state.watched_tickers:
                continue
            submitted = _order_submitted_at(order)
            if submitted is None or submitted < unexpected_horizon:
                continue
            # Still waiting on a matching pending expectation — not unexpected yet
            if match_order_to_pending(order, state.pending, now=now) is not None:
                continue
            if oid:
                state.matched_order_ids.add(oid)
            msg = (
                f"[MONITOR] trade_without_signal client={cname} order_id={oid} "
                f"underlying={under} status={status} submitted_at={submitted}"
            )
            logger.error(msg)
            flags.append("trade_without_signal")

        state.save_state()
    return flags


def apply_shadow_result(
    state: SidecarState,
    event: dict[str, Any],
    shadow: ShadowEntryResult,
    *,
    live_signal: str,
    live_reason: str,
    live_frame_has_bar: bool,
) -> list[str]:
    """Persist shadow metrics, raise drift/missing flags, update pending."""
    flags: list[str] = []
    strategy_name = str(event.get("strategy_name") or "")
    ticker = str(event.get("ticker") or "")
    client_name = str(event.get("client_name") or "")

    event_out = {
        **event,
        "shadow_signal": shadow["shadow_signal"],
        "shadow_confidence": shadow["shadow_confidence"],
        "shadow_reason": shadow.get("shadow_reason", ""),
        "shadow_prepare_signals_ns": shadow["shadow_prepare_signals_ns"],
        "frame_has_bar": shadow.get("frame_has_bar", True),
    }
    with contextlib.suppress(Exception):
        state.append_metrics({**event_out, "kind": "shadow_result"})

    if not shadow.get("frame_has_bar", True):
        logger.error(
            "[MONITOR] shadow_bar_missing strategy=%s ticker=%s bar_ts=%s shadow=%s",
            strategy_name,
            ticker,
            shadow.get("bar_ts") or event.get("bar_ts"),
            shadow["shadow_signal"],
        )
        flags.append("shadow_bar_missing")
    elif live_frame_has_bar:
        drift = compare_signals(live_signal, shadow["shadow_signal"])
        if drift:
            logger.error(
                "[MONITOR] signal_drift strategy=%s ticker=%s bar_ts=%s "
                "live=%s shadow=%s live_reason=%s shadow_reason=%s",
                strategy_name,
                ticker,
                shadow.get("bar_ts") or event.get("bar_ts"),
                live_signal,
                shadow["shadow_signal"],
                live_reason,
                shadow.get("shadow_reason", ""),
            )
            flags.append("signal_drift")

    if shadow.get("actionable") and shadow.get("frame_has_bar", True):
        key = (
            f"{client_name}|{ticker.upper()}|"
            f"{shadow.get('bar_ts') or event.get('bar_ts')}"
        )
        with state._lock:
            state.pending[key] = {
                "strategy_name": strategy_name,
                "client_name": client_name,
                "ticker": ticker.upper(),
                "bar_ts": shadow.get("bar_ts") or event.get("bar_ts"),
                "entry_time": shadow.get("entry_time"),
                "shadow_signal": shadow["shadow_signal"],
                "detect_wall_ns": time.time_ns(),
            }
            state.save_state()
    return flags


def _run_shadow_job(state: SidecarState, event: dict[str, Any]) -> list[str]:
    """Execute prepare_signals (or cache hit) and apply the result."""
    flags: list[str] = []
    ticker = str(event.get("ticker") or "")
    strategy_name = str(event.get("strategy_name") or "")
    bar_ts = str(event.get("bar_ts") or "")
    live_signal = str(event.get("live_signal") or "HOLD").upper()
    live_reason = str(event.get("live_reason") or "")
    live_frame_has_bar = bool(event.get("live_frame_has_bar", True))

    shadow = state.get_shadow_cached(strategy_name, ticker, bar_ts)
    if shadow is None:
        try:
            shadow: ShadowEntryResult = shadow_backtest_signal(
                strategy_name=strategy_name,
                ticker=ticker,
                end_dt=str(event["end_dt"]),
                interval=str(event.get("interval") or "1h"),
                bar_ts=bar_ts,
                config_snapshot=event.get("config_snapshot")
                if isinstance(event.get("config_snapshot"), dict)
                else None,
            )
            state.put_shadow_cached(strategy_name, ticker, bar_ts, shadow)
        except Exception as e:
            logger.exception(
                "[MONITOR] shadow_backtest_signal failed strategy=%s ticker=%s: %s",
                strategy_name,
                ticker,
                e,
            )
            flags.append("shadow_failed")
            with contextlib.suppress(Exception):
                state.append_metrics(
                    {
                        **event,
                        "kind": "shadow_result",
                        "shadow_failed": True,
                        "shadow_error": str(e),
                    }
                )
            return flags

    flags.extend(
        apply_shadow_result(
            state,
            event,
            shadow,
            live_signal=live_signal,
            live_reason=live_reason,
            live_frame_has_bar=live_frame_has_bar,
        )
    )
    return flags


class ShadowWorker:
    """Background thread that runs ``prepare_signals`` off the drain loop."""

    def __init__(self, state: SidecarState) -> None:
        self._state = state
        self._q: queue.Queue[Any] = queue.Queue()
        self._thread = threading.Thread(
            target=self._loop,
            name="monitor-shadow-worker",
            daemon=True,
        )
        self._started = False

    def start(self) -> None:
        if self._started:
            return
        self._started = True
        self._thread.start()

    def stop(self, timeout: float = 2.0) -> None:
        if not self._started:
            return
        self._q.put(_SHADOW_JOB_SHUTDOWN)
        self._thread.join(timeout=timeout)

    def submit(self, event: dict[str, Any]) -> None:
        self._q.put(dict(event))

    def _loop(self) -> None:
        while True:
            job = self._q.get()
            if job is _SHADOW_JOB_SHUTDOWN:
                break
            if not isinstance(job, dict):
                continue
            try:
                if job.get("kind") == "manage_evaluated":
                    _run_manage_shadow_job(self._state, job)
                else:
                    _run_shadow_job(self._state, job)
            except Exception:
                logger.exception("[MONITOR] shadow worker job failed")


def process_event(
    state: SidecarState,
    event: dict[str, Any],
    *,
    clients: dict[str, Any] | None = None,
    run_shadow: bool = True,
    shadow_worker: ShadowWorker | None = None,
) -> list[str]:
    """Handle one queue event. Returns list of flag names raised.

    When *shadow_worker* is set, cache misses are enqueued and the drain path
    returns without waiting on ``prepare_signals`` (heartbeats stay low-lag).
    Cache hits and tests (no worker) still apply shadow synchronously.
    """
    flags: list[str] = []
    kind = event.get("kind", "signal_evaluated")

    emit_wall = event.get("emit_wall_ns")
    queue_lag = None
    if isinstance(emit_wall, int):
        queue_lag = time.time_ns() - emit_wall
        event = {**event, "queue_lag_ns": queue_lag}

    interval = str(event.get("interval") or "1h")
    data_delay = _data_delay_minutes(event)
    expected_gap = False
    if kind == "signal_evaluated":
        expected_gap = is_expected_delay_gap(
            event.get("bar_ts"),
            interval=interval,
            data_delay_min=data_delay,
        )
        # Always stamp the resolved delay and the SLO value so the JSONL is
        # self-describing — readers must not have to re-derive the excess from
        # a default DATA_DELAY that may not match this strategy.
        patch: dict[str, Any] = {"DATA_DELAY": data_delay}
        if event.get("bar_staleness_excess_ns") is None:
            excess = staleness_excess_ns(event.get("bar_staleness_ns"), data_delay)
            if excess is not None:
                patch["bar_staleness_excess_ns"] = excess
        if expected_gap:
            patch["expected_delay_gap"] = True
        event = {**event, **patch}
        event["freshness_slo_eligible"] = is_freshness_sampleable(event)

    if kind != "shutdown":
        try:
            state.append_metrics(event)
        except Exception as e:
            logger.warning("[MONITOR] metrics append failed: %s", e)
        state.record_spans(event, queue_lag)

    if kind == "warmup":
        state.record_warmup(int(event.get("warmup_total_ns") or 0))

    if kind == "heartbeat":
        flags.extend(reconcile_orders(state, clients=clients))
        flags.extend(state.maybe_rollup())
        return flags

    if kind == "manage_evaluated":
        # Note arrival first — even a degraded job keeps the presence guard quiet.
        state.record_manage_eval(
            str(event.get("strategy_name") or ""), str(event.get("ticker") or "")
        )
        required = ("strategy_name", "ticker", "position", "end_dt")
        if not run_shadow or not all(event.get(key) is not None for key in required):
            return flags
        if shadow_worker is not None:
            shadow_worker.submit(event)
            return flags
        return flags + _run_manage_shadow_job(state, event)

    if kind != "signal_evaluated":
        return flags

    ticker = str(event.get("ticker") or "")
    strategy_name = str(event.get("strategy_name") or "")
    client_name = str(event.get("client_name") or "")
    if ticker:
        state.watched_tickers.add(ticker.upper())
    if client_name:
        state.client_names.add(client_name)

    live_signal = str(event.get("live_signal") or "HOLD").upper()
    live_reason = str(event.get("live_reason") or "")
    # Absent key → True for backward compat with older emit payloads.
    live_frame_has_bar = bool(event.get("live_frame_has_bar", True))

    if expected_gap:
        # Pre-RTH stamp under DATA_DELAY — not a real frame miss; skip shadow
        # so bar coverage is not diluted.
        logger.info(
            "[MONITOR] expected_delay_gap strategy=%s ticker=%s bar_ts=%s "
            "DATA_DELAY=%s — skip missing/shadow",
            strategy_name,
            ticker,
            event.get("bar_ts"),
            data_delay,
        )
        return flags

    if not live_frame_has_bar:
        logger.error(
            "[MONITOR] live_bar_missing strategy=%s ticker=%s bar_ts=%s live=%s",
            strategy_name,
            ticker,
            event.get("bar_ts"),
            live_signal,
        )
        flags.append("live_bar_missing")

    # Manage parity blind-spot guard: every strategy implements ``manage_trade``
    # (mandatory StrategyProtocol method), so an open position on a manage phase
    # owes a ``manage_evaluated`` event every bar (emitted in the same tick as
    # this ``signal_evaluated``).  Zero arrivals means the emitter is silently
    # dropping them — which blanks shadow_manage_result and manage_exit_drift
    # parity checks for manage-exit trade closures entirely.
    if (
        int(event.get("active_positions") or 0) > 0
        and "manage" in str(event.get("phase") or "").lower()
        and state.manage_telemetry_missing(strategy_name, ticker)
    ):
        logger.error(
            "[MONITOR] manage_telemetry_missing strategy=%s ticker=%s — "
            "manage_evaluated events absent for manage phase with %s open "
            "position(s); manage-exit parity checks are blank",
            strategy_name,
            ticker,
            event.get("active_positions"),
        )
        flags.append("manage_telemetry_missing")

    if not (run_shadow and strategy_name and ticker and event.get("end_dt")):
        return flags

    bar_ts = str(event.get("bar_ts") or "")
    cached = state.get_shadow_cached(strategy_name, ticker, bar_ts)
    if cached is not None:
        flags.extend(
            apply_shadow_result(
                state,
                event,
                cached,
                live_signal=live_signal,
                live_reason=live_reason,
                live_frame_has_bar=live_frame_has_bar,
            )
        )
        return flags

    if shadow_worker is not None:
        shadow_worker.submit(event)
        return flags

    flags.extend(_run_shadow_job(state, event))
    return flags


def run_sidecar(
    queue: Any,
    drops: Any,
    metrics_path: str = "monitor_metrics.jsonl",
    state_path: str = "monitor_sidecar_state.json",
) -> None:
    """Child-process entrypoint — blocks draining *queue* until shutdown."""
    _setup_logging()
    logger.info(
        "[MONITOR] sidecar running metrics=%s state=%s", metrics_path, state_path
    )
    # Ensure metrics file exists for CloudWatch agent
    with contextlib.suppress(Exception):
        open(metrics_path, "a", encoding="utf-8").close()

    state = SidecarState(metrics_path, state_path)
    clients = _trading_clients()
    shadow_worker = ShadowWorker(state)
    shadow_worker.start()

    try:
        while True:
            try:
                event = queue.get()
            except Exception:
                break
            if not isinstance(event, dict):
                continue
            if event.get("kind") == "shutdown":
                logger.info("[MONITOR] sidecar shutdown")
                break
            if drops is not None:
                try:
                    with drops.get_lock():
                        n = int(drops.value)
                        drops.value = 0
                    state.accumulate_drops(n)
                except Exception:
                    pass
            try:
                process_event(
                    state, event, clients=clients, shadow_worker=shadow_worker
                )
            except Exception:
                logger.exception("[MONITOR] process_event failed")
            # Heartbeat already rolls up inside process_event; other events still need
            # a chance to flush banked drops once the 60s window elapses.
            if event.get("kind") != "heartbeat":
                state.maybe_rollup()
    finally:
        shadow_worker.stop()
        state.close_metrics()
