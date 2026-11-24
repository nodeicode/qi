"""
Tests for python/executors/ic_executor.py

All Alpaca API calls are mocked — no network calls.
time.sleep is patched to make the adaptive fill loop tests run instantly.

Covers:
  - __init__: all dependencies stored correctly
  - enter: delegates to _enter_ic_trade, returns its result
  - enter: exception in _enter_ic_trade → returns None and logs
  - enter: missing / invalid strikes → returns None
  - exit: expired position → calls _account_expiry, returns True without MLEG
  - exit: fills on first attempt → credits equity, returns True
  - exit: order never fills (max attempts exhausted) → returns False
  - exit: submit_order raises on first and only attempt → returns False
  - exit: position expired during fill loop → accounts and returns True
  - _account_expiry: calls equity_mgr.credit with correct net-return amount
  - _build_mleg_legs: SELL_IC (direction=1) → is_credit=True, SELL inner legs
  - _build_mleg_legs: BUY_IC (direction=-1) → is_credit=False, BUY inner legs
  - fetch_leg_quote: returns (bid, ask, mid) or (0, 0, 0) on failure
  - _fetch_combo_data: batch quotes + trades, trade fallback when no quote
  - _fetch_entry_quotes: quality gates (net credit, spread cap), trade-in-spread preference
  - _compute_combo_credit: trade-in-spread preference, net credit computation
  - slippage: SELL_IC and BUY_IC formulas produce correct sign and magnitude
  - ref_credit: EntryRefCredit and EntrySlippageFromRef tracked in position dict
  - enter fill loop: APIError code=40310000 → abort immediately, return None
  - enter fill loop: APIError 4xx status → abort immediately, return None
  - enter fill loop: APIError 5xx on attempt 0 → abort immediately, return None
  - enter fill loop: APIError 5xx on attempt > 0 → loop continues, exhausts → None
  - enter fill loop: 4xx/insufficient-BP abort uses warning, not exception log
  - enter fill loop: reprice uses replace_order_by_id (single call per loop)
  - enter fill loop: unchanged limit on reprice continues waiting (no abort)
  - enter fill loop: replace 4xx + order still working → continue, no abort
  - enter fill loop: replace 4xx + order filled on poll → book fill
  - enter fill loop: replace 4xx + poll failure → continue, no abort
  - exit fill loop: replace 4xx + poll failure → continue, no abort
"""

from __future__ import annotations

# Alpaca is stubbed per-test by the autouse fixture in quant/tests/live/conftest.py.
# ---------------------------------------------------------------------------
# Imports under test
# ---------------------------------------------------------------------------
import datetime
import logging
import sys
import zoneinfo
from typing import cast
from unittest.mock import MagicMock, patch

import quant.live.executors.ic_executor as _ic_executor_mod
from quant.live.executors.ic_executor import IronCondorExecutor
from quant.live.protocols import LivePositionDict

_logger = logging.getLogger("test_ic_executor")


class _StubAPIError(Exception):
    """Real exception class — mocked alpaca.common.exceptions.APIError is a MagicMock."""

    def __init__(self, code: int | None = None, status_code: int | None = None):
        super().__init__("broker error")
        self.code = code
        self.status_code = status_code


_ic_executor_mod.APIError = _StubAPIError


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _client_mock(ex: IronCondorExecutor) -> MagicMock:
    """Cast ex.client to MagicMock for type-safe mock setup in tests."""
    return cast("MagicMock", ex.client)


def _equity_mock(ex: IronCondorExecutor) -> MagicMock:
    """Cast ex.equity_mgr to MagicMock for type-safe mock assertions in tests."""
    return cast("MagicMock", ex.equity_mgr)


def _make_executor() -> IronCondorExecutor:
    """Return an IronCondorExecutor with all external deps mocked."""
    executor = IronCondorExecutor(
        client=MagicMock(),
        data_handler=MagicMock(),
        equity_mgr=MagicMock(),
        logger=_logger,
    )
    return executor


def _make_strategy(min_dte: int = 0) -> MagicMock:
    s = MagicMock()
    s.name = "TestIC_paper"
    s.env = "paper"
    s.client_name = "paper"
    s.config.MIN_OPTION_DTE = min_dte
    s.config.CAPITAL_PER_TRADE = 0.20
    s.config.MAX_CAPITAL_PER_TRADE = 1_000_000.0
    s.config.USE_CONFIDENCE_SIZING = False
    s.config.IC_MIN_NET_CREDIT = 0.08
    s.config.IC_MAX_SPREAD_PCT = 0.80
    # Explicit floats — MagicMock defaults float() to 1.0 and would clamp band to [1,1].
    s.config.IC_MIN_CREDIT = 0.0
    s.config.IC_MAX_CREDIT = float("inf")
    s.config.IC_FILL_STEP_PCT = 0.25
    s.config.IC_COMM_PER_LEG = 0.65
    s.config.IC_MAX_ATTEMPTS = 4
    s.config.IC_MAX_WAIT_SECS = 120
    s.config.IC_MIN_POLL_SECS = 15
    # Keep signal strikes unless a test explicitly enables recenter.
    s.config.IC_RECENTER_AT_FILL = False
    s.config.IC_OFFSET = 5
    s.config.IC_WING = 2
    return s


def _make_engine() -> MagicMock:
    """Minimal TradingEngine mock — engine is required for IC entry/sizing."""
    eng = MagicMock()
    _cfg = {
        "IC_COMM_PER_LEG": 0.65,
        "IC_MIN_NET_CREDIT": 0.08,
        "IC_MAX_SPREAD_PCT": 0.80,
    }
    eng.cfg.side_effect = lambda key, default=None: _cfg.get(
        key, default if default is not None else 0
    )
    eng.size_ic.return_value = {
        "n_contracts": 2,
        "capital": 2000.0,
        "collateral_per_contract": 1000.0,
    }

    return eng


def _fake_build_ic_position(*_args, **kwargs) -> LivePositionDict:
    """Return the complete live schema expected from the real builder."""
    strikes = kwargs["strikes"]
    return cast(
        "LivePositionDict",
        {
            "AssetType": "IC",
            "Ticker": kwargs["Ticker"],
            "Strategy": kwargs["Strategy"],
            "Env": kwargs["Env"],
            "OccSymbol": kwargs["OccSymbol"],
            "Expiration": kwargs["Expiration"],
            "NContracts": kwargs["NContracts"],
            "K_LP": strikes["K_LP"],
            "K_SP": strikes["K_SP"],
            "K_SC": strikes["K_SC"],
            "K_LC": strikes["K_LC"],
            "OrderIds": kwargs["OrderIds"],
            **kwargs,
        },
    )


def _make_ic_signal_row(
    k_lp: float = 430.0,
    k_sp: float = 440.0,
    k_sc: float = 460.0,
    k_lc: float = 470.0,
) -> dict:
    return {
        "Signal": "SELL_IC",
        "Close": 450.0,
        "K_LP": k_lp,
        "K_SP": k_sp,
        "K_SC": k_sc,
        "K_LC": k_lc,
        "IC_Direction": 1,
        "Confidence": 0.8,
        "ATR": 4.5,
    }


def _make_exit_pos(
    expiration: str = "2030-01-01",
    collateral: float = 1000.0,
    fill_credit: float = 1.5,
    n_contracts: int = 1,
) -> LivePositionDict:
    return cast(
        "LivePositionDict",
        {
            "Ticker": "SPY",
            "OccSymbol_LP": "SPY300101P00430000",
            "OccSymbol_SP": "SPY300101P00440000",
            "OccSymbol_SC": "SPY300101C00460000",
            "OccSymbol_LC": "SPY300101C00470000",
            "IC_Direction": 1,
            "Expiration": expiration,
            "NContracts": n_contracts,
            "Collateral": collateral,
            "FillCredit": fill_credit,
            # Set strikes to 0 so the parallel quote fetch is skipped in exit()
            "K_LP": 0.0,
            "K_SP": 0.0,
            "K_SC": 0.0,
            "K_LC": 0.0,
        },
    )


def _valid_live_ic_position() -> LivePositionDict:
    """A complete live IC position satisfying ``is_live_ic_position``."""
    return cast(
        "LivePositionDict",
        {
            "Strategy": "VegaRange",
            "Ticker": "SPY",
            "Env": "paper",
            "OccSymbol": "SPY300101C00470000",
            "Expiration": "2030-01-01",
            "NContracts": 1,
            "K_LP": 400.0,
            "K_SP": 410.0,
            "K_SC": 430.0,
            "K_LC": 440.0,
            "OrderIds": {"LP": "1", "SP": "2", "SC": "3", "LC": "4"},
        },
    )


# ===========================================================================
# __init__
# ===========================================================================


class TestIronCondorExecutorInit:
    def test_all_deps_stored(self) -> None:
        client = MagicMock()
        data_handler = MagicMock()
        equity_mgr = MagicMock()

        ex = IronCondorExecutor(
            client=client,
            data_handler=data_handler,
            equity_mgr=equity_mgr,
            logger=_logger,
        )

        assert ex.client is client
        assert ex.data_handler is data_handler
        assert ex.equity_mgr is equity_mgr
        assert ex.logger is _logger


# ===========================================================================
# enter
# ===========================================================================


class TestEnter:
    def test_enter_delegates_to_enter_ic_trade(self) -> None:
        ex = _make_executor()
        strategy = _make_strategy()
        fake_pos = {"Ticker": "SPY", "Signal": "SELL_IC"}

        with patch.object(ex, "_enter_ic_trade", return_value=fake_pos) as mock_inner:
            result = ex.enter(
                strategy, "SPY", _make_ic_signal_row(), engine=_make_engine()
            )

        mock_inner.assert_called_once()
        assert result is fake_pos

    def test_enter_returns_none_on_success_when_inner_returns_none(self) -> None:
        ex = _make_executor()
        strategy = _make_strategy()

        with patch.object(ex, "_enter_ic_trade", return_value=None):
            result = ex.enter(
                strategy, "SPY", _make_ic_signal_row(), engine=_make_engine()
            )

        assert result is None

    def test_enter_catches_exception_and_returns_none(self) -> None:
        ex = _make_executor()
        strategy = _make_strategy()

        with patch.object(ex, "_enter_ic_trade", side_effect=RuntimeError("boom")):
            result = ex.enter(
                strategy, "SPY", _make_ic_signal_row(), engine=_make_engine()
            )

        assert result is None

    def test_enter_invalid_strikes_returns_none(self) -> None:
        """Missing strikes (k=0) cause _enter_ic_trade to return None immediately."""
        ex = _make_executor()
        strategy = _make_strategy()
        signal_row = _make_ic_signal_row(k_lp=0, k_sp=0, k_sc=0, k_lc=0)

        result = ex.enter(strategy, "SPY", signal_row, engine=_make_engine())

        assert result is None


# ===========================================================================
# live boundary — partial positions never reach close logic
# ===========================================================================


class TestLivePositionBoundary:
    def test_complete_position_passes_guard(self) -> None:
        from quant.live.protocols import is_live_ic_position

        assert is_live_ic_position(_valid_live_ic_position())

    def test_partial_position_rejected(self) -> None:
        """A record missing a required IC/OccSymbol key is rejected at the
        boundary, so it cannot be treated as ``LivePositionDict`` and reach
        close logic."""
        from typing import cast

        from quant.live.protocols import PositionDict, is_live_ic_position

        def _partial(missing: str) -> PositionDict:
            base = dict(_valid_live_ic_position())
            base.pop(missing)
            return cast("PositionDict", base)

        assert not is_live_ic_position(_partial("K_LP"))
        assert not is_live_ic_position(_partial("OrderIds"))


# ===========================================================================
# exit — expiry guard
# ===========================================================================


class TestExitExpiry:
    def test_expired_position_accounts_and_returns_true(self) -> None:
        ex = _make_executor()
        pos = _make_exit_pos(
            expiration="2020-01-01", collateral=1000.0, fill_credit=0.0
        )

        result = ex.exit(pos, 1)

        assert result is True
        _equity_mock(ex).credit.assert_called()

    def test_expired_position_does_not_submit_order(self) -> None:
        ex = _make_executor()
        pos = _make_exit_pos(expiration="2020-01-01")

        ex.exit(pos, 1)

        _client_mock(ex).submit_order.assert_not_called()

    def test_future_expiration_proceeds_to_fill_loop(self) -> None:
        """Future expiration: fill loop runs (submit_order must be called)."""
        ex = _make_executor()
        mock_order = MagicMock()
        mock_order.id = "ord-001"
        _client_mock(ex).submit_order.return_value = mock_order

        filled = MagicMock()
        filled.status.value = "filled"
        _client_mock(ex).get_order_by_id.return_value = filled

        pos = _make_exit_pos(expiration="2030-01-01")

        with patch("quant.live.executors.ic_executor.time.sleep"):
            result = ex.exit(pos, 1)

        assert result is True
        _client_mock(ex).submit_order.assert_called()

    def test_expired_position_invokes_account_expiry_helper(self) -> None:
        """exit() must route through _account_expiry (not duplicate the math)
        so collateral release is consistent with the offline-recovery path."""
        ex = _make_executor()
        pos = _make_exit_pos(
            expiration="2020-01-01", collateral=800.0, fill_credit=2.0, n_contracts=1
        )

        with patch.object(ex, "_account_expiry") as mock_acc:
            result = ex.exit(pos, 1)

        assert result is True
        mock_acc.assert_called_once_with(pos, 1)
        # MLEG submission must be skipped entirely.
        _client_mock(ex).submit_order.assert_not_called()
        _client_mock(ex).get_order_by_id.assert_not_called()

    def test_malformed_expiration_does_not_crash(self) -> None:
        """Bad ISO date in Expiration must not raise — exit proceeds normally."""
        ex = _make_executor()
        mock_order = MagicMock()
        mock_order.id = "ord-bad-exp"
        _client_mock(ex).submit_order.return_value = mock_order
        filled = MagicMock()
        filled.status.value = "filled"
        _client_mock(ex).get_order_by_id.return_value = filled

        pos = _make_exit_pos(expiration="not-a-date")

        with patch("quant.live.executors.ic_executor.time.sleep"):
            result = ex.exit(pos, 1)

        # Should not raise; falls through to normal fill loop.
        assert result is True


# ===========================================================================
# exit — fill loop
# ===========================================================================


class TestExitFillLoop:
    def test_fills_on_first_attempt_returns_true(self) -> None:
        ex = _make_executor()
        mock_order = MagicMock()
        mock_order.id = "ord-abc"
        _client_mock(ex).submit_order.return_value = mock_order

        filled = MagicMock()
        filled.status.value = "filled"
        _client_mock(ex).get_order_by_id.return_value = filled

        pos = _make_exit_pos(
            expiration="2030-01-01",
            collateral=1000.0,
            fill_credit=1.5,
        )

        with patch("quant.live.executors.ic_executor.time.sleep"):
            result = ex.exit(pos, 1)

        assert result is True

    def test_fills_and_credits_equity(self) -> None:
        ex = _make_executor()
        mock_order = MagicMock()
        mock_order.id = "ord-credit"
        _client_mock(ex).submit_order.return_value = mock_order

        filled = MagicMock()
        filled.status.value = "filled"
        _client_mock(ex).get_order_by_id.return_value = filled

        # collateral=500, fill_credit=1.0, n_contracts=1
        # net_return = 500 - 1.0*100*1 = 400 → credit(400)
        pos = _make_exit_pos(
            expiration="2030-01-01",
            collateral=500.0,
            fill_credit=1.0,
            n_contracts=1,
        )

        with patch("quant.live.executors.ic_executor.time.sleep"):
            ex.exit(pos, 1)

        _equity_mock(ex).credit.assert_called_once_with(400.0)

    def test_all_attempts_fail_returns_false(self) -> None:
        ex = _make_executor()
        mock_order = MagicMock()
        mock_order.id = "ord-pending"
        _client_mock(ex).submit_order.return_value = mock_order

        # Order never fills
        pending = MagicMock()
        pending.status.value = "pending_new"
        _client_mock(ex).get_order_by_id.return_value = pending

        pos = _make_exit_pos(expiration="2030-01-01")

        with patch("quant.live.executors.ic_executor.time.sleep"):
            result = ex.exit(pos, 1)

        assert result is False

    def test_submit_order_raises_returns_false(self) -> None:
        ex = _make_executor()
        _client_mock(ex).submit_order.side_effect = Exception("broker rejection")

        pos = _make_exit_pos(expiration="2030-01-01")

        with patch("quant.live.executors.ic_executor.time.sleep"):
            result = ex.exit(pos, 1)

        # max_attempts=3; on first submit failure attempts continue;
        # after all attempts exhausted → False
        assert result is False

    def test_expired_during_fill_loop_returns_true(self) -> None:
        """Position expiry date in the past → Alpaca auto-settled; return True."""
        ex = _make_executor()
        mock_order = MagicMock()
        mock_order.id = "ord-exp"
        _client_mock(ex).submit_order.return_value = mock_order

        # Order never fills
        pending = MagicMock()
        pending.status.value = "pending_new"
        _client_mock(ex).get_order_by_id.return_value = pending

        # Set expiration to yesterday → the post-loop expiry check will catch it.
        # Same-day (today == exp_date) is NOT treated as expired because options
        # are still tradeable until market close on expiration day.
        yesterday_str = (
            (
                datetime.datetime.now(zoneinfo.ZoneInfo("America/New_York"))
                - datetime.timedelta(days=1)
            )
            .date()
            .isoformat()
        )
        pos = _make_exit_pos(
            expiration=yesterday_str, collateral=300.0, fill_credit=0.0
        )

        with patch("quant.live.executors.ic_executor.time.sleep"):
            result = ex.exit(pos, 1)

        assert result is True
        _equity_mock(ex).credit.assert_called()

    def test_fills_on_second_attempt_after_reprice(self) -> None:
        """First attempt open, replace on second loop, then fill."""
        ex = _make_executor()

        mock_order = MagicMock()
        mock_order.id = "ord-reprice"
        _client_mock(ex).submit_order.return_value = mock_order
        _client_mock(ex).replace_order_by_id.return_value = mock_order

        pending = MagicMock()
        pending.status.value = "accepted"
        filled = MagicMock()
        filled.status.value = "filled"
        _client_mock(ex).get_order_by_id.side_effect = [pending, filled]

        # Provide realistic combo quotes so the repricing math runs.
        combo_data = {
            "SPY300101P00430000": {"bid": 0.50, "ask": 0.60, "mid": 0.55},
            "SPY300101P00440000": {"bid": 1.00, "ask": 1.20, "mid": 1.10},
            "SPY300101C00460000": {"bid": 1.10, "ask": 1.30, "mid": 1.20},
            "SPY300101C00470000": {"bid": 0.40, "ask": 0.50, "mid": 0.45},
        }

        pos = _make_exit_pos(
            expiration="2030-01-01", collateral=1000.0, fill_credit=1.5
        )

        with (
            patch.object(ex, "_fetch_combo_data", return_value=combo_data),
            patch("quant.live.executors.ic_executor.time.sleep"),
        ):
            result = ex.exit(pos, 1)

        assert result is True
        assert _client_mock(ex).submit_order.call_count == 1
        _client_mock(ex).replace_order_by_id.assert_called_once()

    def test_exit_replace_4xx_filled_on_poll(self) -> None:
        """Exit: replace 4xx but poll shows filled — success."""
        ex = _make_executor()
        mock_order = MagicMock()
        mock_order.id = "ord-exit-filled"
        _client_mock(ex).submit_order.return_value = mock_order
        _client_mock(ex).replace_order_by_id.side_effect = _StubAPIError(
            status_code=422
        )

        pending = MagicMock()
        pending.status.value = "accepted"
        filled = MagicMock()
        filled.status.value = "filled"
        _client_mock(ex).get_order_by_id.side_effect = [pending, filled]

        combo_data = {
            "SPY300101P00430000": {"bid": 0.50, "ask": 0.60, "mid": 0.55},
            "SPY300101P00440000": {"bid": 1.00, "ask": 1.20, "mid": 1.10},
            "SPY300101C00460000": {"bid": 1.10, "ask": 1.30, "mid": 1.20},
            "SPY300101C00470000": {"bid": 0.40, "ask": 0.50, "mid": 0.45},
        }
        pos = _make_exit_pos(expiration="2030-01-01")

        with (
            patch.object(ex, "_fetch_combo_data", return_value=combo_data),
            patch("quant.live.executors.ic_executor.time.sleep"),
        ):
            result = ex.exit(pos, 1)

        assert result is True

    def test_exit_replace_4xx_order_still_working_continues(self) -> None:
        """Exit: replace 4xx but order pending — keep looping until fill."""
        ex = _make_executor()
        mock_order = MagicMock()
        mock_order.id = "ord-exit-pending"
        _client_mock(ex).submit_order.return_value = mock_order
        _client_mock(ex).replace_order_by_id.side_effect = _StubAPIError(
            status_code=422
        )

        pending = MagicMock()
        pending.status.value = "accepted"
        pending_replace = MagicMock()
        pending_replace.status.value = "pending_replace"
        filled = MagicMock()
        filled.status.value = "filled"
        _client_mock(ex).get_order_by_id.side_effect = [
            pending,
            pending_replace,
            filled,
        ]

        combo_data = {
            "SPY300101P00430000": {"bid": 0.50, "ask": 0.60, "mid": 0.55},
            "SPY300101P00440000": {"bid": 1.00, "ask": 1.20, "mid": 1.10},
            "SPY300101C00460000": {"bid": 1.10, "ask": 1.30, "mid": 1.20},
            "SPY300101C00470000": {"bid": 0.40, "ask": 0.50, "mid": 0.45},
        }
        pos = _make_exit_pos(expiration="2030-01-01")

        with (
            patch.object(ex, "_fetch_combo_data", return_value=combo_data),
            patch("quant.live.executors.ic_executor.time.sleep"),
        ):
            result = ex.exit(pos, 1)

        assert result is True

    def test_exit_replace_4xx_poll_failure_continues(self) -> None:
        """Exit: replace 4xx + status poll error — keep loop until fill."""
        ex = _make_executor()
        mock_order = MagicMock()
        mock_order.id = "ord-exit-poll-fail"
        _client_mock(ex).submit_order.return_value = mock_order
        _client_mock(ex).replace_order_by_id.side_effect = _StubAPIError(
            status_code=422
        )

        pending = MagicMock()
        pending.status.value = "accepted"
        filled = MagicMock()
        filled.status.value = "filled"
        _client_mock(ex).get_order_by_id.side_effect = [
            pending,
            RuntimeError("transient poll error"),
            filled,
        ]

        combo_data = {
            "SPY300101P00430000": {"bid": 0.50, "ask": 0.60, "mid": 0.55},
            "SPY300101P00440000": {"bid": 1.00, "ask": 1.20, "mid": 1.10},
            "SPY300101C00460000": {"bid": 1.10, "ask": 1.30, "mid": 1.20},
            "SPY300101C00470000": {"bid": 0.40, "ask": 0.50, "mid": 0.45},
        }
        pos = _make_exit_pos(expiration="2030-01-01")

        with (
            patch.object(ex, "_fetch_combo_data", return_value=combo_data),
            patch("quant.live.executors.ic_executor.time.sleep"),
        ):
            result = ex.exit(pos, 1)

        assert result is True
        _client_mock(ex).cancel_order_by_id.assert_not_called()

    def test_spread_edge_escalation_fills(self) -> None:
        """Normal loop exhausted → aggressive spread-edge attempt succeeds."""
        ex = _make_executor()

        mock_order = MagicMock()
        mock_order.id = "ord-edge"
        _client_mock(ex).submit_order.return_value = mock_order
        _client_mock(ex).replace_order_by_id.return_value = mock_order

        # All normal-loop polls return "pending_new" so loop exhausts.
        pending = MagicMock()
        pending.status.value = "pending_new"

        # Last call (aggressive escalation poll) returns filled.
        filled = MagicMock()
        filled.status.value = "filled"

        # pos defaults: IC_EXIT_MAX_ATTEMPTS = 4 (from _make_exit_pos defaults)
        # Normal loop: 4 attempts x (submit + get_order_by_id) = 4 get calls.
        # Escalation: 1 more get_order_by_id call.
        _client_mock(ex).get_order_by_id.side_effect = [
            pending,
            pending,
            pending,
            pending,  # normal loop
            filled,  # escalation
        ]

        combo_data = {
            "SPY300101P00430000": {"bid": 0.50, "ask": 0.60, "mid": 0.55},
            "SPY300101P00440000": {"bid": 1.00, "ask": 1.20, "mid": 1.10},
            "SPY300101C00460000": {"bid": 1.10, "ask": 1.30, "mid": 1.20},
            "SPY300101C00470000": {"bid": 0.40, "ask": 0.50, "mid": 0.45},
        }

        pos = _make_exit_pos(
            expiration="2030-01-01", collateral=1000.0, fill_credit=1.5
        )

        with (
            patch.object(ex, "_fetch_combo_data", return_value=combo_data),
            patch("quant.live.executors.ic_executor.time.sleep"),
        ):
            result = ex.exit(pos, 1)

        assert result is True
        # One initial submit, reprices via replace, aggressive step replaces again.
        assert _client_mock(ex).submit_order.call_count == 1
        assert _client_mock(ex).replace_order_by_id.call_count == 4

    def test_aggressive_abort_skips_poll_and_cancel(self) -> None:
        """Aggressive replace 4xx abort returns False without canceling order."""
        ex = _make_executor()
        mock_order = MagicMock()
        mock_order.id = "ord-agg-abort"
        _client_mock(ex).submit_order.return_value = mock_order
        _client_mock(ex).replace_order_by_id.side_effect = [
            mock_order,
            mock_order,
            mock_order,
            _StubAPIError(code=40310000),
        ]

        pending = MagicMock()
        pending.status.value = "pending_new"
        _client_mock(ex).get_order_by_id.return_value = pending

        combo_data = {
            "SPY300101P00430000": {"bid": 0.50, "ask": 0.60, "mid": 0.55},
            "SPY300101P00440000": {"bid": 1.00, "ask": 1.20, "mid": 1.10},
            "SPY300101C00460000": {"bid": 1.10, "ask": 1.30, "mid": 1.20},
            "SPY300101C00470000": {"bid": 0.40, "ask": 0.50, "mid": 0.45},
        }
        pos = _make_exit_pos(
            expiration="2030-01-01",
            collateral=1000.0,
            fill_credit=1.5,
        )
        pos["IC_EXIT_MAX_ATTEMPTS"] = 4

        with (
            patch.object(ex, "_fetch_combo_data", return_value=combo_data),
            patch("quant.live.executors.ic_executor.time.sleep"),
            patch.object(ex.logger, "error") as mock_error,
        ):
            result = ex.exit(pos, 1)

        assert result is False
        _client_mock(ex).cancel_order_by_id.assert_not_called()
        mock_error.assert_called_once()
        assert "Manual close may be required" in mock_error.call_args[0][0]

    def test_aggressive_continue_polls_working_order(self) -> None:
        """Aggressive replace 4xx + order still working — poll before fail/cancel."""
        ex = _make_executor()
        mock_order = MagicMock()
        mock_order.id = "ord-agg-continue"
        _client_mock(ex).submit_order.return_value = mock_order
        _client_mock(ex).replace_order_by_id.side_effect = [
            mock_order,
            mock_order,
            mock_order,
            _StubAPIError(status_code=422),
        ]

        pending = MagicMock()
        pending.status.value = "pending_new"
        pending_replace = MagicMock()
        pending_replace.status.value = "pending_replace"
        _client_mock(ex).get_order_by_id.side_effect = [
            pending,
            pending,
            pending,
            pending,
            pending_replace,
            pending_replace,
        ]

        combo_data = {
            "SPY300101P00430000": {"bid": 0.50, "ask": 0.60, "mid": 0.55},
            "SPY300101P00440000": {"bid": 1.00, "ask": 1.20, "mid": 1.10},
            "SPY300101C00460000": {"bid": 1.10, "ask": 1.30, "mid": 1.20},
            "SPY300101C00470000": {"bid": 0.40, "ask": 0.50, "mid": 0.45},
        }
        pos = _make_exit_pos(
            expiration="2030-01-01",
            collateral=1000.0,
            fill_credit=1.5,
        )
        pos["IC_EXIT_MAX_ATTEMPTS"] = 4

        with (
            patch.object(ex, "_fetch_combo_data", return_value=combo_data),
            patch("quant.live.executors.ic_executor.time.sleep"),
            patch.object(ex.logger, "error") as mock_error,
        ):
            result = ex.exit(pos, 1)

        assert result is False
        _client_mock(ex).cancel_order_by_id.assert_called_once()
        mock_error.assert_called_once()
        assert "Manual close may be required" in mock_error.call_args[0][0]

    def test_aggressive_continue_fills_on_poll(self) -> None:
        """Aggressive replace 4xx + order still working — poll shows filled."""
        ex = _make_executor()
        mock_order = MagicMock()
        mock_order.id = "ord-agg-continue-fill"
        _client_mock(ex).submit_order.return_value = mock_order
        _client_mock(ex).replace_order_by_id.side_effect = [
            mock_order,
            mock_order,
            mock_order,
            _StubAPIError(status_code=422),
        ]

        pending = MagicMock()
        pending.status.value = "pending_new"
        pending_replace = MagicMock()
        pending_replace.status.value = "pending_replace"
        filled = MagicMock()
        filled.status.value = "filled"
        _client_mock(ex).get_order_by_id.side_effect = [
            pending,
            pending,
            pending,
            pending,
            pending_replace,
            filled,
        ]

        combo_data = {
            "SPY300101P00430000": {"bid": 0.50, "ask": 0.60, "mid": 0.55},
            "SPY300101P00440000": {"bid": 1.00, "ask": 1.20, "mid": 1.10},
            "SPY300101C00460000": {"bid": 1.10, "ask": 1.30, "mid": 1.20},
            "SPY300101C00470000": {"bid": 0.40, "ask": 0.50, "mid": 0.45},
        }
        pos = _make_exit_pos(
            expiration="2030-01-01",
            collateral=1000.0,
            fill_credit=1.5,
        )
        pos["IC_EXIT_MAX_ATTEMPTS"] = 4

        with (
            patch.object(ex, "_fetch_combo_data", return_value=combo_data),
            patch("quant.live.executors.ic_executor.time.sleep"),
        ):
            result = ex.exit(pos, 1)

        assert result is True
        _client_mock(ex).cancel_order_by_id.assert_not_called()

    def test_exit_transient_503_on_first_attempt_retries(self) -> None:
        """Exit: 503 on initial submit retries instead of immediate abort."""
        ex = _make_executor()
        mock_order = MagicMock()
        mock_order.id = "ord-retry-503"
        _client_mock(ex).submit_order.side_effect = [
            _StubAPIError(status_code=503),
            mock_order,
        ]

        pending = MagicMock()
        pending.status.value = "accepted"
        filled = MagicMock()
        filled.status.value = "filled"
        _client_mock(ex).get_order_by_id.side_effect = [pending, filled]

        combo_data = {
            "SPY300101P00430000": {"bid": 0.50, "ask": 0.60, "mid": 0.55},
            "SPY300101P00440000": {"bid": 1.00, "ask": 1.20, "mid": 1.10},
            "SPY300101C00460000": {"bid": 1.10, "ask": 1.30, "mid": 1.20},
            "SPY300101C00470000": {"bid": 0.40, "ask": 0.50, "mid": 0.45},
        }
        pos = _make_exit_pos(
            expiration="2030-01-01", collateral=1000.0, fill_credit=1.5
        )

        with (
            patch.object(ex, "_fetch_combo_data", return_value=combo_data),
            patch("quant.live.executors.ic_executor.time.sleep"),
        ):
            result = ex.exit(pos, 1)

        assert result is True
        assert _client_mock(ex).submit_order.call_count == 2

    def test_incomplete_quotes_uses_fallback_price(self) -> None:
        """When _fetch_combo_data returns incomplete data, fallback FillCredit-derived limit is used."""
        ex = _make_executor()

        mock_order = MagicMock()
        mock_order.id = "ord-skip"
        _client_mock(ex).submit_order.return_value = mock_order

        filled = MagicMock()
        filled.status.value = "filled"
        _client_mock(ex).get_order_by_id.return_value = filled

        # First call: incomplete (empty dict).  Second call: valid quotes.
        combo_data = {
            "SPY300101P00430000": {"bid": 0.50, "ask": 0.60, "mid": 0.55},
            "SPY300101P00440000": {"bid": 1.00, "ask": 1.20, "mid": 1.10},
            "SPY300101C00460000": {"bid": 1.10, "ask": 1.30, "mid": 1.20},
            "SPY300101C00470000": {"bid": 0.40, "ask": 0.50, "mid": 0.45},
        }

        pos = _make_exit_pos(
            expiration="2030-01-01", collateral=1000.0, fill_credit=1.5
        )

        with (
            patch.object(
                ex,
                "_fetch_combo_data",
                side_effect=[{}, combo_data, combo_data, combo_data],
            ),
            patch("quant.live.executors.ic_executor.time.sleep"),
        ):
            result = ex.exit(pos, 1)

        assert result is True
        # First attempt uses FillCredit fallback and fills immediately.
        assert _client_mock(ex).submit_order.call_count == 1

    def test_quote_fetch_exception_uses_fallback_price(self) -> None:
        """When _fetch_combo_data raises, fallback FillCredit-derived limit is used."""
        ex = _make_executor()

        mock_order = MagicMock()
        mock_order.id = "ord-err"
        _client_mock(ex).submit_order.return_value = mock_order

        filled = MagicMock()
        filled.status.value = "filled"
        _client_mock(ex).get_order_by_id.return_value = filled

        combo_data = {
            "SPY300101P00430000": {"bid": 0.50, "ask": 0.60, "mid": 0.55},
            "SPY300101P00440000": {"bid": 1.00, "ask": 1.20, "mid": 1.10},
            "SPY300101C00460000": {"bid": 1.10, "ask": 1.30, "mid": 1.20},
            "SPY300101C00470000": {"bid": 0.40, "ask": 0.50, "mid": 0.45},
        }

        pos = _make_exit_pos(
            expiration="2030-01-01", collateral=1000.0, fill_credit=1.5
        )

        with (
            patch.object(
                ex,
                "_fetch_combo_data",
                side_effect=[
                    RuntimeError("API timeout"),
                    combo_data,
                    combo_data,
                    combo_data,
                ],
            ),
            patch("quant.live.executors.ic_executor.time.sleep"),
        ):
            result = ex.exit(pos, 1)

        assert result is True
        assert _client_mock(ex).submit_order.call_count == 1


# ===========================================================================
# _account_expiry
# ===========================================================================


class TestAccountExpiry:
    def test_credits_net_return(self) -> None:
        """net_return = collateral - fill_credit * 100 * contracts"""
        ex = _make_executor()
        pos = cast(
            "LivePositionDict",
            {
                "Collateral": 1000.0,
                "FillCredit": 1.5,
                "NContracts": 2,
            },
        )

        ex._account_expiry(pos, qty=2.0)

        # net_return = 1000 - 1.5 * 100 * 2 = 1000 - 300 = 700
        _equity_mock(ex).credit.assert_called_once_with(700.0)

    def test_zero_fill_credit_credits_full_collateral(self) -> None:
        ex = _make_executor()
        pos = cast(
            "LivePositionDict",
            {"Collateral": 500.0, "FillCredit": 0.0, "NContracts": 1},
        )

        ex._account_expiry(pos, qty=1.0)

        _equity_mock(ex).credit.assert_called_once_with(500.0)

    def test_zero_net_return_no_credit(self) -> None:
        """collateral = fill_credit * 100 * n → net_return = 0 → no credit call."""
        ex = _make_executor()
        pos = cast(
            "LivePositionDict",
            {"Collateral": 100.0, "FillCredit": 1.0, "NContracts": 1},
        )

        ex._account_expiry(pos, qty=1.0)

        _equity_mock(ex).credit.assert_not_called()


# ===========================================================================
# exit — quote-flip handling (combo edge sign-flip)
# ===========================================================================


class TestExitQuoteFlip:
    """When the live combo edge inverts versus the original entry side,
    exit() must submit the close on the opposite side (credit↔debit) instead
    of pricing through zero or rejecting a non-positive limit."""

    @staticmethod
    def _flip_combo_data() -> dict[str, dict[str, float]]:
        """Wings (LP/LC) priced richer than inner legs (SP/SC) — typical of a
        deep-ITM-shorts SELL_IC needing to be closed at a debit, but the
        market has moved so far the natural close is now a credit."""
        return {
            "SPY300101P00430000": {  # LP — long put wing, very rich
                "bid": 2.00,
                "ask": 2.20,
                "mid": 2.10,
                "last_trade": 2.10,
            },
            "SPY300101P00440000": {  # SP — short put inner, cheap
                "bid": 0.10,
                "ask": 0.30,
                "mid": 0.20,
                "last_trade": 0.20,
            },
            "SPY300101C00460000": {  # SC — short call inner, cheap
                "bid": 0.10,
                "ask": 0.30,
                "mid": 0.20,
                "last_trade": 0.20,
            },
            "SPY300101C00470000": {  # LC — long call wing, very rich
                "bid": 2.00,
                "ask": 2.20,
                "mid": 2.10,
                "last_trade": 2.10,
            },
        }

    def test_sell_ic_debit_close_flips_to_credit_in_fill_loop(self) -> None:
        """SELL_IC close (is_debit_close=True) must submit as credit
        (negative limit_price) when the combo ask edge has gone negative."""
        ex = _make_executor()

        mock_order = MagicMock()
        mock_order.id = "ord-flip"
        _client_mock(ex).submit_order.return_value = mock_order

        filled = MagicMock()
        filled.status.value = "filled"
        _client_mock(ex).get_order_by_id.return_value = filled

        pos = _make_exit_pos(
            expiration="2030-01-01",
            collateral=1000.0,
            fill_credit=1.5,
            n_contracts=1,
        )
        # ic_direction=1 → SELL_IC → is_debit_close=True

        with (
            patch.object(ex, "_fetch_combo_data", return_value=self._flip_combo_data()),
            patch("quant.live.executors.ic_executor.time.sleep"),
            patch(
                "quant.live.executors.ic_executor.LimitOrderRequest"
            ) as mock_limit_req,
        ):
            mock_limit_req.return_value = MagicMock()
            result = ex.exit(pos, 1)

        assert result is True
        _client_mock(ex).submit_order.assert_called()
        # combo_ask < 0 → credit close (negative limit on first submit).
        first_limit = mock_limit_req.call_args_list[0].kwargs["limit_price"]
        assert first_limit < 0, (
            f"Expected credit submission (negative limit) on flip, got {first_limit}"
        )

    def test_sell_ic_aggressive_escalation_flips_to_credit(self) -> None:
        """Aggressive spread-edge fallback must also handle the sign-flip
        case: SELL_IC close with negative ask edge → credit submission."""
        ex = _make_executor()

        mock_order = MagicMock()
        mock_order.id = "ord-agg"
        _client_mock(ex).submit_order.return_value = mock_order
        _client_mock(ex).replace_order_by_id.return_value = mock_order

        # All normal-loop polls pending → loop exhausts.  Escalation poll fills.
        pending = MagicMock()
        pending.status.value = "pending_new"
        filled = MagicMock()
        filled.status.value = "filled"
        # 4 normal attempts (default IC_EXIT_MAX_ATTEMPTS) + 1 escalation poll.
        _client_mock(ex).get_order_by_id.side_effect = [
            pending,
            pending,
            pending,
            pending,
            filled,
        ]

        pos = _make_exit_pos(
            expiration="2030-01-01",
            collateral=1000.0,
            fill_credit=1.5,
            n_contracts=1,
        )

        with (
            patch.object(ex, "_fetch_combo_data", return_value=self._flip_combo_data()),
            patch("quant.live.executors.ic_executor.time.sleep"),
            patch(
                "quant.live.executors.ic_executor.ReplaceOrderRequest"
            ) as mock_replace_req,
        ):
            mock_replace_req.return_value = MagicMock()
            result = ex.exit(pos, 1)

        assert result is True
        # Aggressive escalation replaces in-place with credit pricing on flip.
        assert _client_mock(ex).replace_order_by_id.call_count >= 1
        last_limit = mock_replace_req.call_args_list[-1].kwargs["limit_price"]
        assert last_limit < 0, (
            f"Aggressive escalation expected credit on flip, got {last_limit}"
        )


# ===========================================================================
# _build_mleg_legs
# ===========================================================================


class TestBuildMlegLegs:
    def test_sell_ic_direction_1_is_credit(self) -> None:
        ex = _make_executor()
        legs, is_credit = ex._build_mleg_legs(
            "LP_OCC", "SP_OCC", "SC_OCC", "LC_OCC", ic_direction=1
        )

        assert is_credit is True
        assert len(legs) == 4

    def test_buy_ic_direction_minus1_is_not_credit(self) -> None:
        ex = _make_executor()
        legs, is_credit = ex._build_mleg_legs(
            "LP_OCC", "SP_OCC", "SC_OCC", "LC_OCC", ic_direction=-1
        )

        assert is_credit is False
        assert len(legs) == 4

    def test_legs_use_correct_occ_symbols(self) -> None:
        import quant.live.executors.ic_executor as _ic_mod

        ex = _make_executor()
        with patch.object(_ic_mod, "OptionLegRequest") as mock_olr:
            ex._build_mleg_legs("LP_OCC", "SP_OCC", "SC_OCC", "LC_OCC", ic_direction=1)

        call_symbols = {c.kwargs["symbol"] for c in mock_olr.call_args_list}
        assert call_symbols == {"LP_OCC", "SP_OCC", "SC_OCC", "LC_OCC"}


# ===========================================================================
# Slippage computation
# ===========================================================================


class TestSlippageComputation:
    """Verify the slippage formula for both SELL_IC (credit) and BUY_IC (debit)."""

    @staticmethod
    def _compute_slippage(
        is_credit: bool, actual_credit: float, last_combo_mid: float
    ) -> float:
        """Mirror the production slippage formula from _enter_ic_trade."""
        if is_credit:
            return last_combo_mid - actual_credit
        return abs(actual_credit) - last_combo_mid

    def test_sell_ic_no_slippage(self) -> None:
        # Filled at exactly mid → 0 slippage
        assert (
            self._compute_slippage(True, actual_credit=2.00, last_combo_mid=2.00) == 0.0
        )

    def test_sell_ic_positive_slippage(self) -> None:
        # Received 1.90 when mid was 2.00 → gave away 0.10
        slip = self._compute_slippage(True, actual_credit=1.90, last_combo_mid=2.00)
        assert round(slip, 4) == 0.10

    def test_sell_ic_negative_slippage(self) -> None:
        # Received 2.10 when mid was 2.00 → gained 0.10 over mid (favourable)
        slip = self._compute_slippage(True, actual_credit=2.10, last_combo_mid=2.00)
        assert round(slip, 4) == -0.10

    def test_buy_ic_no_slippage(self) -> None:
        # Paid exactly mid → 0 slippage
        assert (
            self._compute_slippage(False, actual_credit=-2.00, last_combo_mid=2.00)
            == 0.0
        )

    def test_buy_ic_positive_slippage(self) -> None:
        # Paid 2.10 (actual_credit=-2.10) when mid was 2.00 → overpaid 0.10
        slip = self._compute_slippage(False, actual_credit=-2.10, last_combo_mid=2.00)
        assert round(slip, 4) == 0.10

    def test_buy_ic_negative_slippage(self) -> None:
        # Paid 1.90 (actual_credit=-1.90) when mid was 2.00 → saved 0.10
        slip = self._compute_slippage(False, actual_credit=-1.90, last_combo_mid=2.00)
        assert round(slip, 4) == -0.10


# ===========================================================================
# _fetch_combo_data
# ===========================================================================


def _make_quote_obj(bid: float, ask: float) -> MagicMock:
    """Create a mock quote object with bid_price and ask_price."""
    q = MagicMock()
    q.bid_price = bid
    q.ask_price = ask
    return q


def _make_trade_obj(price: float) -> MagicMock:
    """Create a mock trade object with price attribute."""
    t = MagicMock()
    t.price = price
    return t


class TestFetchComboData:
    def test_returns_quotes_and_trades_for_all_symbols(self) -> None:
        ex = _make_executor()
        syms = ["LP", "SP", "SC", "LC"]
        opt_client = cast("MagicMock", ex.data_handler.option_historical_client)

        opt_client.get_option_latest_quote.return_value = {
            "LP": _make_quote_obj(0.50, 0.60),
            "SP": _make_quote_obj(1.00, 1.20),
            "SC": _make_quote_obj(1.10, 1.30),
            "LC": _make_quote_obj(0.40, 0.50),
        }
        opt_client.get_option_latest_trade.return_value = {
            "LP": _make_trade_obj(0.55),
            "SP": _make_trade_obj(1.10),
            "SC": _make_trade_obj(1.20),
            "LC": _make_trade_obj(0.45),
        }

        result = ex._fetch_combo_data(syms)

        assert set(result.keys()) == {"LP", "SP", "SC", "LC"}
        assert result["SP"]["bid"] == 1.00
        assert result["SP"]["ask"] == 1.20
        assert result["SP"]["mid"] == 1.10
        assert result["SP"]["last_trade"] == 1.10

    def test_trade_fills_in_when_quote_missing(self) -> None:
        """If no quote exists but a trade does, trade is used as mid estimate."""
        ex = _make_executor()
        syms = ["SYM"]
        opt_client = cast("MagicMock", ex.data_handler.option_historical_client)

        # Quote returns empty
        opt_client.get_option_latest_quote.return_value = {}
        opt_client.get_option_latest_trade.return_value = {
            "SYM": _make_trade_obj(2.00),
        }

        result = ex._fetch_combo_data(syms)

        assert result["SYM"]["mid"] == 2.00
        assert result["SYM"]["last_trade"] == 2.00
        # bid/ask estimated from trade
        assert result["SYM"]["bid"] == round(2.00 * 0.97, 3)
        assert result["SYM"]["ask"] == round(2.00 * 1.03, 3)

    def test_quote_api_failure_still_returns_trades(self) -> None:
        """If get_option_latest_quote raises, trades are still fetched."""
        ex = _make_executor()
        syms = ["SYM"]
        opt_client = cast("MagicMock", ex.data_handler.option_historical_client)

        opt_client.get_option_latest_quote.side_effect = Exception("API error")
        opt_client.get_option_latest_trade.return_value = {
            "SYM": _make_trade_obj(3.00),
        }

        result = ex._fetch_combo_data(syms)

        assert "SYM" in result
        assert result["SYM"]["last_trade"] == 3.00
        assert result["SYM"]["mid"] == 3.00

    def test_trade_api_failure_still_returns_quotes(self) -> None:
        """If get_option_latest_trade raises, quotes are still returned."""
        ex = _make_executor()
        syms = ["SYM"]
        opt_client = cast("MagicMock", ex.data_handler.option_historical_client)

        opt_client.get_option_latest_quote.return_value = {
            "SYM": _make_quote_obj(1.00, 1.20),
        }
        opt_client.get_option_latest_trade.side_effect = Exception("API error")

        result = ex._fetch_combo_data(syms)

        assert result["SYM"]["bid"] == 1.00
        assert result["SYM"]["ask"] == 1.20
        assert result["SYM"]["mid"] == 1.10
        assert result["SYM"]["last_trade"] == 0.0

    def test_both_apis_fail_returns_empty(self) -> None:
        ex = _make_executor()
        opt_client = cast("MagicMock", ex.data_handler.option_historical_client)
        opt_client.get_option_latest_quote.side_effect = Exception("fail")
        opt_client.get_option_latest_trade.side_effect = Exception("fail")

        result = ex._fetch_combo_data(["SYM"])

        assert result == {}


# ===========================================================================
# _fetch_entry_quotes
# ===========================================================================


class TestFetchEntryQuotes:
    @staticmethod
    def _make_data(
        lp_mid: float = 0.55,
        sp_mid: float = 1.10,
        sc_mid: float = 1.20,
        lc_mid: float = 0.45,
        trade_offset: float = 0.0,
    ) -> dict[str, dict[str, float]]:
        """Build a _fetch_combo_data return value with configurable prices."""

        def entry(mid: float) -> dict[str, float]:
            return {
                "bid": round(mid - 0.05, 3),
                "ask": round(mid + 0.05, 3),
                "mid": mid,
                "last_trade": round(mid + trade_offset, 3),
            }

        return {
            "LP": entry(lp_mid),
            "SP": entry(sp_mid),
            "SC": entry(sc_mid),
            "LC": entry(lc_mid),
        }

    def test_returns_credit(self) -> None:
        """Valid quotes produce a positive credit."""
        ex = _make_executor()
        data = self._make_data()

        with patch.object(ex, "_fetch_combo_data", return_value=data):
            result = ex._fetch_entry_quotes(
                "SPY",
                ["LP", "SP", "SC", "LC"],
                wing_width=10.0,
                min_net_credit=0.0,
                max_spread_pct=1.0,
                comm_per_leg=0.65,
            )

        assert result is not None
        ret_data, credit = result
        # credit = 1.10 + 1.20 - 0.55 - 0.45 = 1.30
        assert round(credit, 2) == 1.30
        assert ret_data is data

    def test_returns_none_when_incomplete_quotes(self) -> None:
        """Incomplete quotes (missing symbols) returns None after retries."""
        ex = _make_executor()

        with (
            patch.object(ex, "_fetch_combo_data", return_value={}),
            patch("quant.live.executors.ic_executor.time.sleep"),
        ):
            result = ex._fetch_entry_quotes(
                "SPY",
                ["LP", "SP", "SC", "LC"],
                wing_width=10.0,
                min_net_credit=0.0,
                max_spread_pct=1.0,
                comm_per_leg=0.65,
            )

        assert result is None

    def test_incomplete_quotes_retries_then_succeeds(self) -> None:
        """Quotes missing on first attempt but available on retry → succeeds."""
        ex = _make_executor()
        good_data = self._make_data()

        with (
            patch.object(ex, "_fetch_combo_data", side_effect=[{}, good_data]),
            patch("quant.live.executors.ic_executor.time.sleep"),
        ):
            result = ex._fetch_entry_quotes(
                "SPY",
                ["LP", "SP", "SC", "LC"],
                wing_width=10.0,
                min_net_credit=0.0,
                max_spread_pct=1.0,
                comm_per_leg=0.65,
            )

        assert result is not None
        _, credit = result
        assert round(credit, 2) == 1.30

    def test_negative_credit_returns_none(self) -> None:
        """Negative credit (wings more expensive) returns None."""
        ex = _make_executor()
        data = self._make_data(lp_mid=5.0, sp_mid=0.10, sc_mid=0.10, lc_mid=5.0)

        with patch.object(ex, "_fetch_combo_data", return_value=data):
            result = ex._fetch_entry_quotes(
                "SPY",
                ["LP", "SP", "SC", "LC"],
                wing_width=10.0,
                min_net_credit=0.0,
                max_spread_pct=1.0,
                comm_per_leg=0.65,
            )

        assert result is None

    def test_prefers_last_trade_within_spread(self) -> None:
        """When last_trade falls within bid/ask, it's used instead of mid."""
        ex = _make_executor()
        data = self._make_data(sp_mid=1.10)
        data["SP"]["last_trade"] = 1.12
        data["SP"]["bid"] = 1.05
        data["SP"]["ask"] = 1.15

        with patch.object(ex, "_fetch_combo_data", return_value=data):
            result = ex._fetch_entry_quotes(
                "SPY",
                ["LP", "SP", "SC", "LC"],
                wing_width=10.0,
                min_net_credit=0.0,
                max_spread_pct=1.0,
                comm_per_leg=0.65,
            )

        assert result is not None
        _, credit = result
        # credit uses trade 1.12 for SP instead of mid 1.10
        # Expected: 1.12 + 1.20 - 0.55 - 0.45 = 1.32
        assert round(credit, 2) == 1.32

    def test_skips_trade_outside_spread(self) -> None:
        """Trade outside bid/ask is ignored, mid is used."""
        ex = _make_executor()
        data = self._make_data(sp_mid=1.10)
        data["SP"]["last_trade"] = 2.00
        data["SP"]["bid"] = 1.05
        data["SP"]["ask"] = 1.15

        with patch.object(ex, "_fetch_combo_data", return_value=data):
            result = ex._fetch_entry_quotes(
                "SPY",
                ["LP", "SP", "SC", "LC"],
                wing_width=10.0,
                min_net_credit=0.0,
                max_spread_pct=1.0,
                comm_per_leg=0.65,
            )

        assert result is not None
        _, credit = result
        assert round(credit, 2) == 1.30

    def test_net_credit_below_floor_returns_none(self) -> None:
        """Credit below min_net_credit floor is rejected."""
        ex = _make_executor()
        # Tiny credit: 0.10 + 0.10 - 0.05 - 0.05 = 0.10
        data = self._make_data(lp_mid=0.05, sp_mid=0.10, sc_mid=0.10, lc_mid=0.05)

        with patch.object(ex, "_fetch_combo_data", return_value=data):
            result = ex._fetch_entry_quotes(
                "SPY",
                ["LP", "SP", "SC", "LC"],
                wing_width=10.0,
                min_net_credit=0.50,  # require 0.50 net
                max_spread_pct=1.0,
                comm_per_leg=0.65,
            )

        assert result is None

    def test_gross_credit_passes_but_net_credit_after_commission_fails(self) -> None:
        """Gross credit exceeds min_net_credit but net (after commissions) does not."""
        ex = _make_executor()
        # credit = 0.12 + 0.12 - 0.05 - 0.05 = 0.14
        # commission deduction = 4 * 0.65 / 100 = 0.026
        # net_credit = 0.14 - 0.026 = 0.114
        # min_net_credit = 0.12 → gross (0.14) > 0.12 but net (0.114) < 0.12
        data = self._make_data(lp_mid=0.05, sp_mid=0.12, sc_mid=0.12, lc_mid=0.05)

        with patch.object(ex, "_fetch_combo_data", return_value=data):
            result = ex._fetch_entry_quotes(
                "SPY",
                ["LP", "SP", "SC", "LC"],
                wing_width=10.0,
                min_net_credit=0.12,
                max_spread_pct=5.0,
                comm_per_leg=0.65,
            )

        assert result is None

    def test_spread_too_wide_returns_none(self) -> None:
        """Combo spread wider than max_spread_pct * credit → rejected."""
        ex = _make_executor()
        # Each leg has spread of 0.10, so combo spread = 0.40
        # Credit = 1.30, spread_pct = 0.40/1.30 ≈ 0.31
        data = self._make_data()

        with patch.object(ex, "_fetch_combo_data", return_value=data):
            result = ex._fetch_entry_quotes(
                "SPY",
                ["LP", "SP", "SC", "LC"],
                wing_width=10.0,
                min_net_credit=0.0,
                max_spread_pct=0.10,  # very tight — should reject
                comm_per_leg=0.65,
            )

        assert result is None

    def test_cwr_is_logged_but_does_not_gate(self) -> None:
        """CWR is just a log metric — even low CWR passes if credit is good."""
        ex = _make_executor()
        # credit ≈ 1.30, wing = 100 → CWR = 0.013 — very low
        data = self._make_data()

        with patch.object(ex, "_fetch_combo_data", return_value=data):
            result = ex._fetch_entry_quotes(
                "SPY",
                ["LP", "SP", "SC", "LC"],
                wing_width=100.0,  # huge wing → tiny CWR
                min_net_credit=0.0,
                max_spread_pct=1.0,
                comm_per_leg=0.65,
            )

        # Should still pass — CWR doesn't gate
        assert result is not None


# ===========================================================================
# ref_credit tracking in position dict
# ===========================================================================


class TestRefCreditTracking:
    """Verify EntryRefCredit and EntrySlippageFromRef appear in position."""

    @staticmethod
    def _compute_ref_slippage(
        is_credit: bool, actual_credit: float, ref_credit: float
    ) -> float:
        """Mirror the production ref-slippage formula."""
        if is_credit:
            return ref_credit - actual_credit
        return abs(actual_credit) - ref_credit

    def test_sell_ic_ref_slippage_positive(self) -> None:
        # Ref was 2.00, filled at 1.80 → lost 0.20 from ref
        slip = self._compute_ref_slippage(True, actual_credit=1.80, ref_credit=2.00)
        assert round(slip, 4) == 0.20

    def test_sell_ic_ref_slippage_negative(self) -> None:
        # Ref was 2.00, filled at 2.10 → gained 0.10 over ref
        slip = self._compute_ref_slippage(True, actual_credit=2.10, ref_credit=2.00)
        assert round(slip, 4) == -0.10

    def test_buy_ic_ref_slippage(self) -> None:
        # Ref was 2.00, paid 2.20 → overpaid 0.20
        slip = self._compute_ref_slippage(False, actual_credit=-2.20, ref_credit=2.00)
        assert round(slip, 4) == 0.20


# ===========================================================================
# enter — MLEG reprice via replace_order_by_id
# ===========================================================================


class TestEnterFillLoopReprice:
    """Entry fill loop reprices with replace_order_by_id (not cancel+resubmit)."""

    _EXP = "2030-01-16"
    _OCC_LP = "SPY300116P00430000"
    _OCC_SP = "SPY300116P00440000"
    _OCC_SC = "SPY300116C00460000"
    _OCC_LC = "SPY300116C00470000"

    def _entry_data(self) -> dict[str, dict[str, float]]:
        return {
            self._OCC_LP: {"bid": 0.50, "ask": 0.60, "mid": 0.55, "last_trade": 0.55},
            self._OCC_SP: {"bid": 1.00, "ask": 1.20, "mid": 1.10, "last_trade": 1.10},
            self._OCC_SC: {"bid": 1.10, "ask": 1.30, "mid": 1.20, "last_trade": 1.20},
            self._OCC_LC: {"bid": 0.40, "ask": 0.50, "mid": 0.45, "last_trade": 0.45},
        }

    def _setup_enter(self, ex: IronCondorExecutor) -> None:
        cast("MagicMock", ex.data_handler).get_option_expirations.return_value = [
            self._EXP
        ]
        cast("MagicMock", ex.equity_mgr).get.return_value = 50_000.0

    def test_reprice_uses_replace_order_by_id(self) -> None:
        """Open MLEG is repriced in-place; only one submit for the whole loop."""
        ex = _make_executor()
        strategy = _make_strategy()
        self._setup_enter(ex)
        client = _client_mock(ex)

        order = MagicMock()
        order.id = "ord-first"
        client.submit_order.return_value = order
        client.replace_order_by_id.return_value = order

        accepted = MagicMock()
        accepted.status.value = "accepted"
        filled = MagicMock()
        filled.status.value = "filled"
        filled.filled_avg_price = 0.22
        client.get_order_by_id.side_effect = [accepted, filled]

        with (
            patch.object(
                ex, "_fetch_entry_quotes", return_value=(self._entry_data(), 1.30)
            ),
            patch.object(ex, "_fetch_combo_data", return_value=self._entry_data()),
            patch.object(ex, "_size_contracts", return_value=(1, 400.0, 325.0)),
            patch("quant.live.executors.ic_executor.time.sleep"),
        ):
            result = ex.enter(
                strategy, "SPY", _make_ic_signal_row(), engine=_make_engine()
            )

        assert result is not None
        client.submit_order.assert_called_once()
        client.cancel_order_by_id.assert_not_called()
        client.replace_order_by_id.assert_called_once()
        assert client.replace_order_by_id.call_args[0][0] == "ord-first"

        call_names = [c[0] for c in client.mock_calls]
        submit_idx = call_names.index("submit_order")
        replace_idx = call_names.index("replace_order_by_id")
        assert submit_idx < replace_idx


# ===========================================================================
# enter — APIError classification in the MLEG fill loop
# ===========================================================================


class TestEnterFillLoopAPIError:
    """APIError classification in the _enter_ic_trade MLEG fill loop.

    APIError branches in the except-APIError clause:
      1. code == 40310000  (insufficient buying power) → abort, return None
      2. 4xx on initial submit                          → abort, return None
      3. 4xx on replace → poll: filled / working / dead
      4. 5xx / unknown transient                      → log error + continue;
         but if attempt == 0, still abort immediately.

    Business-error aborts (cases 1 and 2) emit logger.warning, not
    logger.exception, so no multi-line traceback appears per attempt.
    Case 3 aborts on attempt 0 after logger.error.
    """

    class _FakeAPIError(Exception):
        """Minimal stand-in for alpaca.common.exceptions.APIError.

        Patching ``quant.live.executors.ic_executor.APIError`` with this class
        makes the ``except APIError`` clause in _enter_ic_trade catch it.
        """

        def __init__(self, code: int | None = None, status_code: int | None = None):
            super().__init__("broker error")
            self.code = code
            self.status_code = status_code

    # OCC symbols produced by get_occ_symbol for _make_ic_signal_row()
    # (K_LP=430, K_SP=440, K_SC=460, K_LC=470) with expiration "2030-01-16".
    _EXP = "2030-01-16"
    _OCC_LP = "SPY300116P00430000"
    _OCC_SP = "SPY300116P00440000"
    _OCC_SC = "SPY300116C00460000"
    _OCC_LC = "SPY300116C00470000"

    def _entry_data(self) -> dict[str, dict[str, float]]:
        """Realistic per-leg quote data keyed by the expected OCC symbols."""
        return {
            self._OCC_LP: {"bid": 0.50, "ask": 0.60, "mid": 0.55, "last_trade": 0.55},
            self._OCC_SP: {"bid": 1.00, "ask": 1.20, "mid": 1.10, "last_trade": 1.10},
            self._OCC_SC: {"bid": 1.10, "ask": 1.30, "mid": 1.20, "last_trade": 1.20},
            self._OCC_LC: {"bid": 0.40, "ask": 0.50, "mid": 0.45, "last_trade": 0.45},
        }

    def _setup_enter(self, ex: IronCondorExecutor) -> None:
        """Configure the executor so _enter_ic_trade reaches the MLEG submit."""
        cast("MagicMock", ex.data_handler).get_option_expirations.return_value = [
            self._EXP
        ]
        cast("MagicMock", ex.equity_mgr).get.return_value = 50_000.0

    def test_insufficient_buying_power_aborts_immediately(self) -> None:
        """code=40310000 → return None on the first attempt with no retry."""
        ex = _make_executor()
        strategy = _make_strategy()
        self._setup_enter(ex)

        with (
            patch("quant.live.executors.ic_executor.APIError", self._FakeAPIError),
            patch.object(
                ex, "_fetch_entry_quotes", return_value=(self._entry_data(), 1.30)
            ),
            patch.object(ex, "_fetch_combo_data", return_value=self._entry_data()),
            patch.object(ex, "_size_contracts", return_value=(2, 2000.0, 1000.0)),
            patch("quant.live.executors.ic_executor.time.sleep"),
        ):
            _client_mock(ex).submit_order.side_effect = self._FakeAPIError(
                code=40310000
            )
            result = ex.enter(
                strategy, "SPY", _make_ic_signal_row(), engine=_make_engine()
            )

        assert result is None
        _client_mock(ex).submit_order.assert_called_once()

    def test_4xx_rejection_aborts_immediately(self) -> None:
        """4xx status (excluding 40310000) → abort on the first attempt, no retry."""
        ex = _make_executor()
        strategy = _make_strategy()
        self._setup_enter(ex)

        with (
            patch("quant.live.executors.ic_executor.APIError", self._FakeAPIError),
            patch.object(
                ex, "_fetch_entry_quotes", return_value=(self._entry_data(), 1.30)
            ),
            patch.object(ex, "_fetch_combo_data", return_value=self._entry_data()),
            patch.object(ex, "_size_contracts", return_value=(2, 2000.0, 1000.0)),
            patch("quant.live.executors.ic_executor.time.sleep"),
        ):
            _client_mock(ex).submit_order.side_effect = self._FakeAPIError(
                status_code=422
            )
            result = ex.enter(
                strategy, "SPY", _make_ic_signal_row(), engine=_make_engine()
            )

        assert result is None
        _client_mock(ex).submit_order.assert_called_once()

    def test_4xx_boundary_status_400_aborts(self) -> None:
        """status_code=400 is the lower boundary — must still abort."""
        ex = _make_executor()
        strategy = _make_strategy()
        self._setup_enter(ex)

        with (
            patch("quant.live.executors.ic_executor.APIError", self._FakeAPIError),
            patch.object(
                ex, "_fetch_entry_quotes", return_value=(self._entry_data(), 1.30)
            ),
            patch.object(ex, "_fetch_combo_data", return_value=self._entry_data()),
            patch.object(ex, "_size_contracts", return_value=(2, 2000.0, 1000.0)),
            patch("quant.live.executors.ic_executor.time.sleep"),
        ):
            _client_mock(ex).submit_order.side_effect = self._FakeAPIError(
                status_code=400
            )
            result = ex.enter(
                strategy, "SPY", _make_ic_signal_row(), engine=_make_engine()
            )

        assert result is None
        _client_mock(ex).submit_order.assert_called_once()

    def test_5xx_on_first_attempt_aborts(self) -> None:
        """5xx on attempt=0 → abort immediately (guard: if attempt == 0: return None)."""
        ex = _make_executor()
        strategy = _make_strategy()
        self._setup_enter(ex)

        with (
            patch("quant.live.executors.ic_executor.APIError", self._FakeAPIError),
            patch.object(
                ex, "_fetch_entry_quotes", return_value=(self._entry_data(), 1.30)
            ),
            patch.object(ex, "_fetch_combo_data", return_value=self._entry_data()),
            patch.object(ex, "_size_contracts", return_value=(2, 2000.0, 1000.0)),
            patch("quant.live.executors.ic_executor.time.sleep"),
        ):
            _client_mock(ex).submit_order.side_effect = self._FakeAPIError(
                status_code=503
            )
            result = ex.enter(
                strategy, "SPY", _make_ic_signal_row(), engine=_make_engine()
            )

        assert result is None
        _client_mock(ex).submit_order.assert_called_once()

    def test_5xx_after_first_submit_retries_then_exhausts(self) -> None:
        """5xx on attempt > 0 → loop continues and retries; exhausts max_attempts → None."""
        ex = _make_executor()
        strategy = _make_strategy()
        self._setup_enter(ex)

        mock_order = MagicMock()
        mock_order.id = "ord-001"
        canceled = MagicMock()
        canceled.status.value = "canceled"

        with (
            patch("quant.live.executors.ic_executor.APIError", self._FakeAPIError),
            patch.object(
                ex, "_fetch_entry_quotes", return_value=(self._entry_data(), 1.30)
            ),
            patch.object(ex, "_fetch_combo_data", return_value=self._entry_data()),
            patch.object(ex, "_size_contracts", return_value=(2, 2000.0, 1000.0)),
            patch("quant.live.executors.ic_executor.time.sleep"),
        ):
            # Attempt 0: submit succeeds → order placed.
            # Attempts 1+: submit raises 5xx → loop continues until max_attempts.
            _client_mock(ex).submit_order.side_effect = [
                mock_order,
                self._FakeAPIError(status_code=500),
                self._FakeAPIError(status_code=500),
                self._FakeAPIError(status_code=500),
            ]
            # Poll returns canceled so mleg_order is cleared before each reprice.
            _client_mock(ex).get_order_by_id.return_value = canceled
            result = ex.enter(
                strategy, "SPY", _make_ic_signal_row(), engine=_make_engine()
            )

        assert result is None
        # Must have retried: initial successful submit + at least one 5xx attempt.
        assert _client_mock(ex).submit_order.call_count > 1

    def test_4xx_uses_warning_not_exception_log(self) -> None:
        """4xx abort emits logger.warning only — no traceback-producing logger.exception."""
        ex = _make_executor()
        strategy = _make_strategy()
        self._setup_enter(ex)

        with (
            patch("quant.live.executors.ic_executor.APIError", self._FakeAPIError),
            patch.object(
                ex, "_fetch_entry_quotes", return_value=(self._entry_data(), 1.30)
            ),
            patch.object(ex, "_fetch_combo_data", return_value=self._entry_data()),
            patch.object(ex, "_size_contracts", return_value=(2, 2000.0, 1000.0)),
            patch("quant.live.executors.ic_executor.time.sleep"),
            patch.object(ex.logger, "warning") as mock_warn,
            patch.object(ex.logger, "exception") as mock_exc,
        ):
            _client_mock(ex).submit_order.side_effect = self._FakeAPIError(
                status_code=422
            )
            ex.enter(strategy, "SPY", _make_ic_signal_row(), engine=_make_engine())

        mock_warn.assert_called()
        mock_exc.assert_not_called()

    def test_insufficient_bp_uses_warning_not_exception_log(self) -> None:
        """Insufficient-BP abort emits logger.warning only — no traceback spam."""
        ex = _make_executor()
        strategy = _make_strategy()
        self._setup_enter(ex)

        with (
            patch("quant.live.executors.ic_executor.APIError", self._FakeAPIError),
            patch.object(
                ex, "_fetch_entry_quotes", return_value=(self._entry_data(), 1.30)
            ),
            patch.object(ex, "_fetch_combo_data", return_value=self._entry_data()),
            patch.object(ex, "_size_contracts", return_value=(2, 2000.0, 1000.0)),
            patch("quant.live.executors.ic_executor.time.sleep"),
            patch.object(ex.logger, "warning") as mock_warn,
            patch.object(ex.logger, "exception") as mock_exc,
        ):
            _client_mock(ex).submit_order.side_effect = self._FakeAPIError(
                code=40310000
            )
            ex.enter(strategy, "SPY", _make_ic_signal_row(), engine=_make_engine())

        mock_warn.assert_called()
        mock_exc.assert_not_called()

    def test_unchanged_limit_skips_replace_call(self) -> None:
        """When computed limit matches last submit, skip replace_order_by_id."""
        ex = _make_executor()
        strategy = _make_strategy()
        strategy.config.IC_FILL_STEP_PCT = 0
        self._setup_enter(ex)
        client = _client_mock(ex)

        order = MagicMock()
        order.id = "ord-skip"
        accepted = MagicMock()
        accepted.status.value = "accepted"
        filled = MagicMock()
        filled.status.value = "filled"
        filled.filled_avg_price = 1.30

        client.submit_order.return_value = order
        client.get_order_by_id.side_effect = [accepted, accepted, filled]

        stable_data = self._entry_data()

        with (
            patch.object(ex, "_fetch_entry_quotes", return_value=(stable_data, 1.30)),
            patch.object(ex, "_fetch_combo_data", return_value=stable_data),
            patch.object(ex, "_size_contracts", return_value=(66, 20000.0, 1000.0)),
            patch("quant.live.executors.ic_executor.time.sleep"),
        ):
            result = ex.enter(
                strategy, "SPY", _make_ic_signal_row(), engine=_make_engine()
            )

        assert result is not None
        client.submit_order.assert_called_once()
        client.replace_order_by_id.assert_not_called()

    def test_replace_4xx_order_still_working_continues(self) -> None:
        """Replace 4xx but poll shows pending — keep loop, fill later."""
        ex = _make_executor()
        strategy = _make_strategy()
        strategy.config.IC_FILL_STEP_PCT = 0.25
        self._setup_enter(ex)
        client = _client_mock(ex)

        order = MagicMock()
        order.id = "ord-pending-replace"
        accepted = MagicMock()
        accepted.status.value = "accepted"
        pending_replace = MagicMock()
        pending_replace.status.value = "pending_replace"
        filled = MagicMock()
        filled.status.value = "filled"
        filled.filled_avg_price = 1.28

        client.submit_order.return_value = order
        client.replace_order_by_id.side_effect = self._FakeAPIError(status_code=422)
        client.get_order_by_id.side_effect = [
            accepted,
            pending_replace,
            filled,
        ]

        with (
            patch("quant.live.executors.ic_executor.APIError", self._FakeAPIError),
            patch.object(
                ex, "_fetch_entry_quotes", return_value=(self._entry_data(), 1.30)
            ),
            patch.object(ex, "_fetch_combo_data", return_value=self._entry_data()),
            patch.object(ex, "_size_contracts", return_value=(2, 2000.0, 1000.0)),
            patch("quant.live.executors.ic_executor.time.sleep"),
        ):
            result = ex.enter(
                strategy, "SPY", _make_ic_signal_row(), engine=_make_engine()
            )

        assert result is not None
        client.cancel_order_by_id.assert_not_called()

    def test_replace_4xx_order_filled_on_poll(self) -> None:
        """Replace 4xx but poll shows filled — book at filled_avg_price."""
        ex = _make_executor()
        strategy = _make_strategy()
        strategy.config.IC_FILL_STEP_PCT = 0.25
        self._setup_enter(ex)
        client = _client_mock(ex)

        order = MagicMock()
        order.id = "ord-filled-on-replace"
        accepted = MagicMock()
        accepted.status.value = "accepted"
        filled = MagicMock()
        filled.status.value = "filled"
        filled.filled_avg_price = 0.47

        client.submit_order.return_value = order
        client.replace_order_by_id.side_effect = self._FakeAPIError(status_code=422)
        client.get_order_by_id.side_effect = [accepted, filled]

        with (
            patch("quant.live.executors.ic_executor.APIError", self._FakeAPIError),
            patch.object(
                ex, "_fetch_entry_quotes", return_value=(self._entry_data(), 1.30)
            ),
            patch.object(ex, "_fetch_combo_data", return_value=self._entry_data()),
            patch.object(ex, "_size_contracts", return_value=(2, 2000.0, 1000.0)),
            patch("quant.live.executors.ic_executor.time.sleep"),
        ):
            result = ex.enter(
                strategy, "SPY", _make_ic_signal_row(), engine=_make_engine()
            )

        assert result is not None
        assert result["FillCredit"] == 0.47
        client.cancel_order_by_id.assert_not_called()

    def test_replace_4xx_poll_failure_continues(self) -> None:
        """Replace 4xx + status poll error — keep loop, do not abort."""
        ex = _make_executor()
        strategy = _make_strategy()
        strategy.config.IC_FILL_STEP_PCT = 0.25
        self._setup_enter(ex)
        client = _client_mock(ex)

        order = MagicMock()
        order.id = "ord-poll-fail"
        accepted = MagicMock()
        accepted.status.value = "accepted"
        filled = MagicMock()
        filled.status.value = "filled"
        filled.filled_avg_price = 1.28

        client.submit_order.return_value = order
        client.replace_order_by_id.side_effect = self._FakeAPIError(status_code=422)
        client.get_order_by_id.side_effect = [
            accepted,
            RuntimeError("transient poll error"),
            filled,
        ]

        with (
            patch("quant.live.executors.ic_executor.APIError", self._FakeAPIError),
            patch.object(
                ex, "_fetch_entry_quotes", return_value=(self._entry_data(), 1.30)
            ),
            patch.object(ex, "_fetch_combo_data", return_value=self._entry_data()),
            patch.object(ex, "_size_contracts", return_value=(2, 2000.0, 1000.0)),
            patch("quant.live.executors.ic_executor.time.sleep"),
        ):
            result = ex.enter(
                strategy, "SPY", _make_ic_signal_row(), engine=_make_engine()
            )

        assert result is not None
        client.cancel_order_by_id.assert_not_called()


# ===========================================================================
# enter — non-moving min-credit floor (backtest parity)
# ===========================================================================


class TestEnterFillLoopMinCreditFloor:
    """The MLEG fill loop must never fill below IC_MIN_CREDIT.

    Backtest applies its credit gate (check_ic_credit_gates → credit_too_low)
    to the post-slip *fill* credit and drops the trade when the fill lands
    below. Live repricing concedes credit on every attempt; without the floor
    it can fill below min_credit (e.g. mid 0.16 → fill 0.08) — a trade whose
    loss we cannot afford and that backtest would never have validated.

    The fix is a simple bottom snap: when the computed limit would fill below
    min_credit, snap it UP to min_credit (sign-preserving for credit vs debit)
    and keep trying — no separate net-credit/commission math is needed because
    IC_MIN_CREDIT dominates min_net_credit + comm at all current configs.

    Covered:
      - SELL_IC: fresh quotes move below min_credit during the loop → limit
        snapped up to min_credit, order placed, fill at floor books normally.
      - SELL_IC: fresh quotes stay above min_credit → fills normally (control).
      - BUY_IC: same snap applies to the debit magnitude (symmetric).
      - min_credit = 0 (unconfigured) → no snap, legacy behavior.
    """

    _EXP = "2030-01-16"
    _OCC_LP = "SPY300116P00430000"
    _OCC_SP = "SPY300116P00440000"
    _OCC_SC = "SPY300116C00460000"
    _OCC_LC = "SPY300116C00470000"

    def _setup_enter(self, ex: IronCondorExecutor) -> None:
        cast("MagicMock", ex.data_handler).get_option_expirations.return_value = [
            self._EXP
        ]
        cast("MagicMock", ex.equity_mgr).get.return_value = 50_000.0

    def _entry_data(self) -> dict[str, dict[str, float]]:
        """Gate-time quotes: mid credit 1.30 — passes IC_MIN_CREDIT=0.15."""
        return {
            self._OCC_LP: {"bid": 0.50, "ask": 0.60, "mid": 0.55, "last_trade": 0.55},
            self._OCC_SP: {"bid": 1.00, "ask": 1.20, "mid": 1.10, "last_trade": 1.10},
            self._OCC_SC: {"bid": 1.10, "ask": 1.30, "mid": 1.20, "last_trade": 1.20},
            self._OCC_LC: {"bid": 0.40, "ask": 0.50, "mid": 0.45, "last_trade": 0.45},
        }

    def _subfloor_data(self) -> dict[str, dict[str, float]]:
        """Fresh quotes moved through the floor: mid credit 0.12, bid edge 0.0.

        combo_mid = 0.38 + 0.36 - 0.32 - 0.30 = 0.12
        combo_bid = 0.32 + 0.30 - 0.36 - 0.26 = 0.00
        → limit credit 0.12 < floor 0.15 → abort.
        """
        return {
            self._OCC_LP: {"bid": 0.28, "ask": 0.36, "mid": 0.32, "last_trade": 0.32},
            self._OCC_SP: {"bid": 0.32, "ask": 0.44, "mid": 0.38, "last_trade": 0.38},
            self._OCC_SC: {"bid": 0.30, "ask": 0.42, "mid": 0.36, "last_trade": 0.36},
            self._OCC_LC: {"bid": 0.26, "ask": 0.34, "mid": 0.30, "last_trade": 0.30},
        }

    def _atfloor_data(self) -> dict[str, dict[str, float]]:
        """Fresh quotes still pay the floor: mid credit 0.20, bid edge 0.16.

        combo_mid = 0.36 + 0.34 - 0.26 - 0.24 = 0.20
        combo_bid = 0.34 + 0.32 - 0.28 - 0.22 = 0.16
        → limit credit 0.20 >= floor 0.15 → normal fill.
        """
        return {
            self._OCC_LP: {"bid": 0.24, "ask": 0.28, "mid": 0.26, "last_trade": 0.26},
            self._OCC_SP: {"bid": 0.34, "ask": 0.38, "mid": 0.36, "last_trade": 0.36},
            self._OCC_SC: {"bid": 0.32, "ask": 0.36, "mid": 0.34, "last_trade": 0.34},
            self._OCC_LC: {"bid": 0.22, "ask": 0.26, "mid": 0.24, "last_trade": 0.24},
        }

    def _sell_ic_strategy(self) -> MagicMock:
        strategy = _make_strategy()
        strategy.config.IC_MIN_CREDIT = 0.15
        strategy.config.IC_MIN_NET_CREDIT = 0.08
        strategy.config.IC_COMM_PER_LEG = 0.65
        return strategy

    def test_sell_ic_clamps_limit_at_floor_and_fills(self) -> None:
        """Fresh quotes dip below the floor → limit is clamped AT the floor
        (credit orders fill at limit-or-better, so never below), and a fill
        at the floor books normally instead of aborting the entry."""
        ex = _make_executor()
        strategy = self._sell_ic_strategy()
        self._setup_enter(ex)
        client = _client_mock(ex)

        order = MagicMock()
        order.id = "ord-floor-clamp"
        client.submit_order.return_value = order
        filled = MagicMock()
        filled.status.value = "filled"
        filled.filled_avg_price = 0.15
        client.get_order_by_id.return_value = filled

        with (
            patch.object(
                ex, "_fetch_entry_quotes", return_value=(self._entry_data(), 1.30)
            ),
            patch.object(ex, "_fetch_combo_data", return_value=self._subfloor_data()),
            patch.object(ex, "_size_contracts", return_value=(2, 2000.0, 1000.0)),
            patch("quant.live.executors.ic_executor.time.sleep"),
            patch.object(ex.logger, "warning") as mock_warn,
            patch(
                "quant.live.executors.ic_executor.LimitOrderRequest"
            ) as mock_limit_req,
        ):
            mock_limit_req.return_value = MagicMock()
            result = ex.enter(
                strategy, "SPY", _make_ic_signal_row(), engine=_make_engine()
            )

        assert result is not None
        assert result["FillCredit"] == 0.15
        client.submit_order.assert_called_once()
        client.cancel_order_by_id.assert_not_called()
        # Clamped limit must be exactly the floor, submitted as credit.
        limit = mock_limit_req.call_args_list[0].kwargs["limit_price"]
        assert limit == -0.15
        assert any("floor" in str(call.args[0]) for call in mock_warn.call_args_list)

    def test_sell_ic_fills_normally_when_market_above_floor(self) -> None:
        """Control: quotes above the floor are unaffected — fill books."""
        ex = _make_executor()
        strategy = self._sell_ic_strategy()
        self._setup_enter(ex)
        client = _client_mock(ex)

        order = MagicMock()
        order.id = "ord-floor-ok"
        client.submit_order.return_value = order
        filled = MagicMock()
        filled.status.value = "filled"
        filled.filled_avg_price = 0.20
        client.get_order_by_id.return_value = filled

        with (
            patch.object(
                ex, "_fetch_entry_quotes", return_value=(self._entry_data(), 1.30)
            ),
            patch.object(ex, "_fetch_combo_data", return_value=self._atfloor_data()),
            patch.object(ex, "_size_contracts", return_value=(2, 2000.0, 1000.0)),
            patch("quant.live.executors.ic_executor.time.sleep"),
            patch(
                "quant.live.executors.ic_executor.LimitOrderRequest"
            ) as mock_limit_req,
        ):
            mock_limit_req.return_value = MagicMock()
            result = ex.enter(
                strategy, "SPY", _make_ic_signal_row(), engine=_make_engine()
            )

        assert result is not None
        client.submit_order.assert_called_once()
        # Credit limit must be at or above the floor and submitted as credit.
        limit = mock_limit_req.call_args_list[0].kwargs["limit_price"]
        assert limit <= -0.15

    def test_buy_ic_snaps_debit_limit_to_floor(self) -> None:
        """BUY_IC is symmetric: a sub-floor debit magnitude snaps up to
        min_credit on the buy limit too."""
        ex = _make_executor()
        strategy = self._sell_ic_strategy()
        self._setup_enter(ex)
        client = _client_mock(ex)

        order = MagicMock()
        order.id = "ord-buy-snap"
        client.submit_order.return_value = order
        filled = MagicMock()
        filled.status.value = "filled"
        filled.filled_avg_price = 0.15
        client.get_order_by_id.return_value = filled

        signal = _make_ic_signal_row()
        signal["Signal"] = "BUY_IC"
        signal["IC_Direction"] = -1

        with (
            patch.object(
                ex, "_fetch_entry_quotes", return_value=(self._entry_data(), 1.30)
            ),
            patch.object(ex, "_fetch_combo_data", return_value=self._subfloor_data()),
            patch.object(ex, "_size_contracts", return_value=(2, 2000.0, 1000.0)),
            patch("quant.live.executors.ic_executor.time.sleep"),
            patch(
                "quant.live.executors.ic_executor.LimitOrderRequest"
            ) as mock_limit_req,
        ):
            mock_limit_req.return_value = MagicMock()
            result = ex.enter(strategy, "SPY", signal, engine=_make_engine())

        assert result is not None
        client.submit_order.assert_called_once()
        client.cancel_order_by_id.assert_not_called()
        # Debit limit snapped up to the floor, sign preserved (positive debit).
        limit = mock_limit_req.call_args_list[0].kwargs["limit_price"]
        assert limit == 0.15

    def test_no_snap_when_min_credit_off(self) -> None:
        """min_credit=0 (unconfigured) → no snap is applied (legacy behavior)."""
        ex = _make_executor()
        strategy = _make_strategy()  # IC_MIN_CREDIT = 0.0
        strategy.config.IC_MIN_NET_CREDIT = 0.0
        self._setup_enter(ex)
        client = _client_mock(ex)

        order = MagicMock()
        order.id = "ord-nofloor"
        client.submit_order.return_value = order
        filled = MagicMock()
        filled.status.value = "filled"
        filled.filled_avg_price = 0.12
        client.get_order_by_id.return_value = filled

        with (
            patch.object(
                ex, "_fetch_entry_quotes", return_value=(self._entry_data(), 1.30)
            ),
            patch.object(ex, "_fetch_combo_data", return_value=self._subfloor_data()),
            patch.object(ex, "_size_contracts", return_value=(2, 2000.0, 1000.0)),
            patch("quant.live.executors.ic_executor.time.sleep"),
        ):
            result = ex.enter(
                strategy, "SPY", _make_ic_signal_row(), engine=_make_engine()
            )

        assert result is not None
        client.submit_order.assert_called_once()


# ===========================================================================
# _size_contracts — engine required (no inline fallback)
# ===========================================================================


class TestSizeContractsFallback:
    """``_size_contracts`` delegates to the core ``size_for_signal`` dispatch."""

    def test_zero_collateral_returns_zero(self) -> None:
        ex = _make_executor()
        strategy = _make_strategy()

        n, capital, collateral_per = ex._size_contracts(
            strategy, "SPY", 440, 440, 440, 440, confidence=0.8
        )

        assert n == 0
        assert capital == 0.0
        assert collateral_per == 0.0

    def test_size_contracts_delegates_to_dispatch(self) -> None:
        """``_size_contracts`` calls ``size_for_signal``."""
        ex = _make_executor()
        strategy = _make_strategy()
        _equity_mock(ex).get.return_value = 50_000.0

        with patch(
            "quant.live.executors.ic_executor.size_for_signal",
            return_value={
                "n_contracts": 5,
                "capital": 5_000.0,
                "collateral_per_contract": 1000.0,
            },
        ) as mock_size:
            n, capital, collateral_per = ex._size_contracts(
                strategy, "SPY", 430, 440, 460, 470, confidence=0.8
            )

        assert n == 5
        assert capital == 5_000.0
        assert collateral_per == 1000.0
        mock_size.assert_called_once()

    def test_basic_sizing_all_fields_passed_through(self) -> None:
        """``size_for_signal`` return values are passed through."""
        ex = _make_executor()
        strategy = _make_strategy()
        _equity_mock(ex).get.return_value = 50_000.0

        with patch(
            "quant.live.executors.ic_executor.size_for_signal",
            return_value={
                "n_contracts": 10,
                "capital": 10_000.0,
                "collateral_per_contract": 1000.0,
            },
        ):
            n, capital, collateral_per = ex._size_contracts(
                strategy, "SPY", 430, 440, 460, 470, confidence=0.8
            )

        assert collateral_per == 1000.0
        assert n == 10
        assert capital == 10_000.0


# ===========================================================================
# _enter_ic_trade — engine required
# ===========================================================================


class TestEnterICTrade:
    """Entry guards: no expiration / no entry quotes / zero sizing abort the entry."""

    def test_no_expiration_returns_none(self) -> None:
        ex = _make_executor()
        strategy = _make_strategy()

        with patch.object(ex, "_resolve_expiration", return_value=""):
            result = ex._enter_ic_trade(
                strategy, "SPY", _make_ic_signal_row(), engine=_make_engine()
            )

        assert result is None

    def test_no_entry_result_returns_none(self) -> None:
        ex = _make_executor()
        strategy = _make_strategy()
        _equity_mock(ex).get.return_value = 50_000.0

        with (
            patch.object(ex, "_resolve_expiration", return_value="2030-01-01"),
            patch.object(ex, "_fetch_entry_quotes", return_value=None),
        ):
            result = ex._enter_ic_trade(
                strategy, "SPY", _make_ic_signal_row(), engine=_make_engine()
            )

        assert result is None

    def test_zero_contracts_returns_none(self) -> None:
        ex = _make_executor()
        strategy = _make_strategy()
        _equity_mock(ex).get.return_value = 50_000.0

        entry_data = {
            "SPY300101P00430000": {
                "bid": 0.50,
                "ask": 0.60,
                "mid": 0.55,
                "last_trade": 0.55,
            },
            "SPY300101P00440000": {
                "bid": 1.00,
                "ask": 1.20,
                "mid": 1.10,
                "last_trade": 1.10,
            },
            "SPY300101C00460000": {
                "bid": 1.00,
                "ask": 1.20,
                "mid": 1.10,
                "last_trade": 1.10,
            },
            "SPY300101C00470000": {
                "bid": 0.50,
                "ask": 0.60,
                "mid": 0.55,
                "last_trade": 0.55,
            },
        }
        with (
            patch.object(ex, "_resolve_expiration", return_value="2030-01-01"),
            patch.object(ex, "_fetch_entry_quotes", return_value=(entry_data, 1.30)),
            patch.object(ex, "_size_contracts", return_value=(0, 0.0, 0.0)),
        ):
            result = ex._enter_ic_trade(
                strategy, "SPY", _make_ic_signal_row(), engine=_make_engine()
            )

        assert result is None

    @staticmethod
    def _good_entry_data() -> dict[str, dict[str, float]]:
        return {
            "SPY300101P00430000": {
                "bid": 0.50,
                "ask": 0.60,
                "mid": 0.55,
                "last_trade": 0.55,
            },
            "SPY300101P00440000": {
                "bid": 1.00,
                "ask": 1.20,
                "mid": 1.10,
                "last_trade": 1.10,
            },
            "SPY300101C00460000": {
                "bid": 1.10,
                "ask": 1.30,
                "mid": 1.20,
                "last_trade": 1.20,
            },
            "SPY300101C00470000": {
                "bid": 0.40,
                "ask": 0.50,
                "mid": 0.45,
                "last_trade": 0.45,
            },
        }

    def test_returns_position_dict_with_engine(self) -> None:
        ex = _make_executor()
        _equity_mock(ex).get.return_value = 50_000.0
        strategy = _make_strategy()
        eng = _make_engine()

        entry_data = self._good_entry_data()
        mock_order = MagicMock()
        mock_order.id = "mleg-001"
        filled = MagicMock()
        filled.status.value = "filled"
        filled.filled_avg_price = 1.25

        with (
            patch.object(ex, "_resolve_expiration", return_value="2030-01-01"),
            patch.object(ex, "_fetch_entry_quotes", return_value=(entry_data, 1.30)),
            patch.object(ex, "_size_contracts", return_value=(2, 2000.0, 1000.0)),
            patch.object(ex, "_fetch_combo_data", return_value=entry_data),
            patch("quant.live.executors.ic_executor.time.sleep"),
            patch(
                "quant.live.executors.ic_executor.build_ic_position",
                side_effect=_fake_build_ic_position,
            ) as mock_bic,
        ):
            client = _client_mock(ex)
            client.submit_order.return_value = mock_order
            client.get_order_by_id.return_value = filled
            result = ex._enter_ic_trade(
                strategy, "SPY", _make_ic_signal_row(), engine=eng
            )

        assert result is not None
        mock_bic.assert_called_once()

    def test_position_dict_contains_occ_symbols(self) -> None:
        ex = _make_executor()
        _equity_mock(ex).get.return_value = 50_000.0
        strategy = _make_strategy()
        eng = _make_engine()

        entry_data = self._good_entry_data()
        mock_order = MagicMock()
        mock_order.id = "mleg-002"
        filled = MagicMock()
        filled.status.value = "filled"
        filled.filled_avg_price = 1.25

        with (
            patch.object(ex, "_resolve_expiration", return_value="2030-01-01"),
            patch.object(ex, "_fetch_entry_quotes", return_value=(entry_data, 1.30)),
            patch.object(ex, "_size_contracts", return_value=(2, 2000.0, 1000.0)),
            patch.object(ex, "_fetch_combo_data", return_value=entry_data),
            patch("quant.live.executors.ic_executor.time.sleep"),
            patch(
                "quant.live.executors.ic_executor.build_ic_position",
                side_effect=_fake_build_ic_position,
            ) as mock_bic,
        ):
            client = _client_mock(ex)
            client.submit_order.return_value = mock_order
            client.get_order_by_id.return_value = filled
            result = ex._enter_ic_trade(
                strategy, "SPY", _make_ic_signal_row(), engine=eng
            )

        assert result is not None
        kwargs = mock_bic.call_args.kwargs
        assert "OccSymbol_LP" in kwargs
        assert "OccSymbol_SP" in kwargs
        assert "OccSymbol_SC" in kwargs
        assert "OccSymbol_LC" in kwargs


# ===========================================================================
# _order_id / _order_status / _order_fill_price — dict-branch (lines 92, 98-99, 106)
# ===========================================================================


class TestOrderHelperDictBranch:
    def test_order_id_from_dict_id_key(self) -> None:
        assert IronCondorExecutor._order_id({"id": "abc-123"}) == "abc-123"

    def test_order_id_from_dict_order_id_key(self) -> None:
        assert IronCondorExecutor._order_id({"order_id": "xyz-456"}) == "xyz-456"

    def test_order_status_from_dict_string(self) -> None:
        assert IronCondorExecutor._order_status({"status": "filled"}) == "filled"

    def test_order_status_from_dict_enum_value(self) -> None:
        status_enum = MagicMock()
        status_enum.value = "pending_new"
        assert (
            IronCondorExecutor._order_status({"status": status_enum}) == "pending_new"
        )

    def test_order_fill_price_from_dict(self) -> None:
        assert (
            IronCondorExecutor._order_fill_price({"filled_avg_price": "1.25"}) == 1.25
        )

    def test_order_fill_price_from_dict_none_uses_default(self) -> None:
        assert (
            IronCondorExecutor._order_fill_price(
                {"filled_avg_price": None}, default=0.5
            )
            == 0.5
        )


# ===========================================================================
# _resolve_expiration — success and failure paths (lines 774-777, 810-811)
# ===========================================================================


class TestResolveExpiration:
    def test_no_expirations_returns_none(self) -> None:
        """Lines 774-777: get_option_expirations returns empty → return None."""
        ex = _make_executor()
        ex.data_handler.get_option_expirations.return_value = []
        result = ex._resolve_expiration("SPY", 1)
        assert result is None

    def test_returns_first_expiration(self) -> None:
        """Line 810-811: expirations found → returns first."""
        ex = _make_executor()
        ex.data_handler.get_option_expirations.return_value = [
            "2030-01-17",
            "2030-01-24",
        ]
        result = ex._resolve_expiration("SPY", 1)
        assert result == "2030-01-17"


# ===========================================================================
# exit() — BUY_IC direction (lines 162->183, 192-198)
# ===========================================================================


class TestExitBuyICDirection:
    def test_buy_ic_fills_on_first_attempt_returns_true(self) -> None:
        """BUY_IC (direction=-1) close → is_debit_close=False (credit close)."""
        ex = _make_executor()
        mock_order = MagicMock()
        mock_order.id = "ord-buy-ic"
        _client_mock(ex).submit_order.return_value = mock_order

        filled = MagicMock()
        filled.status.value = "filled"
        _client_mock(ex).get_order_by_id.return_value = filled

        pos = cast(
            "LivePositionDict",
            {**_make_exit_pos(expiration="2030-01-01"), "IC_Direction": -1},
        )

        with patch("quant.live.executors.ic_executor.time.sleep"):
            result = ex.exit(pos, 1)

        assert result is True

    def test_exit_without_expiration_skips_expiry_guard(self) -> None:
        """Line 162->183: when expiration is empty, skip the expiry guard block."""
        ex = _make_executor()
        mock_order = MagicMock()
        mock_order.id = "ord-no-exp"
        _client_mock(ex).submit_order.return_value = mock_order

        filled = MagicMock()
        filled.status.value = "filled"
        _client_mock(ex).get_order_by_id.return_value = filled

        # Position with no Expiration field
        pos = cast(
            "LivePositionDict",
            {**_make_exit_pos(expiration=""), "IC_Direction": 1},
        )

        with patch("quant.live.executors.ic_executor.time.sleep"):
            result = ex.exit(pos, 1)

        assert result is True


# ===========================================================================
# IC_RECENTER_AT_FILL — live quote spot
# ===========================================================================


class TestICRecenterAtFill:
    def test_latest_underlying_spot_prefers_quote_mid(self) -> None:
        import pandas as pd
        import pytest

        ex = _make_executor()
        cast(
            "MagicMock", ex.data_handler
        ).get_latest_stock_quotes.return_value = pd.DataFrame(
            [{"mid": 452.3, "price": 452.1, "bid": 452.2, "ask": 452.4}],
            index=pd.Index(["SPY"]),
        )
        assert ex._latest_underlying_spot("SPY", 450.0) == pytest.approx(452.3)

    def test_latest_underlying_spot_falls_back_to_bar(self) -> None:
        import pandas as pd
        import pytest

        ex = _make_executor()
        cast(
            "MagicMock", ex.data_handler
        ).get_latest_stock_quotes.return_value = pd.DataFrame()
        assert ex._latest_underlying_spot("SPY", 450.0) == pytest.approx(450.0)

    def test_enter_recenters_strikes_from_live_quote(self) -> None:
        """Live mid hops DATA_DELAY: strikes re-snapped before OCC / quotes."""
        import pandas as pd

        from quant.utils.options import snap_ic_strikes

        ex = _make_executor()
        strategy = _make_strategy()
        strategy.config.IC_RECENTER_AT_FILL = True
        strategy.config.IC_OFFSET = 5
        strategy.config.IC_WING = 2
        _equity_mock(ex).get.return_value = 50_000.0

        live_mid = 452.0
        cast(
            "MagicMock", ex.data_handler
        ).get_latest_stock_quotes.return_value = pd.DataFrame(
            [{"mid": live_mid, "price": live_mid, "bid": 451.9, "ask": 452.1}],
            index=pd.Index(["SPY"]),
        )
        exp_lp, exp_sp, exp_sc, exp_lc = snap_ic_strikes(live_mid, 5.0, 2.0, 1.0)

        signal = _make_ic_signal_row(k_lp=430.0, k_sp=440.0, k_sc=460.0, k_lc=470.0)
        signal["Raw_Close"] = 450.0

        captured: dict[str, float] = {}

        def _capture_occ(ticker, strike, exp, side):
            captured[f"{side}:{strike}"] = float(strike)
            return f"OCC{int(strike)}{side[0].upper()}"

        with (
            patch.object(ex, "_resolve_expiration", return_value="2030-01-01"),
            patch(
                "quant.live.executors.ic_executor.get_occ_symbol",
                side_effect=_capture_occ,
            ),
            patch.object(ex, "_fetch_entry_quotes", return_value=None),
        ):
            result = ex._enter_ic_trade(strategy, "SPY", signal, engine=_make_engine())

        assert result is None  # aborted at quotes — snap already applied
        assert captured.get(f"put:{exp_sp}") == exp_sp
        assert captured.get(f"call:{exp_sc}") == exp_sc
        assert captured.get(f"put:{exp_lp}") == exp_lp
        assert captured.get(f"call:{exp_lc}") == exp_lc
