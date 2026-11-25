"""Unit tests for standalone backtest utility functions."""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from quant.utils.fills import apply_slippage


def _slippage_config(*, per_share: float = 0.0, factor: float = 0.0):
    """Minimal config shape consumed by ``apply_slippage``."""
    return SimpleNamespace(
        SLIPPAGE_PER_SHARE=per_share,
        BASE_SLIPPAGE_FACTOR=factor,
    )


class TestApplySlippage:
    def test_no_slippage_returns_raw_price(self):
        assert apply_slippage(_slippage_config(), 100.0, 1, is_entry=True) == 100.0

    @pytest.mark.parametrize(
        ("direction", "is_entry", "expected"),
        [
            (1, True, 100.01),
            (-1, True, 99.99),
            (1, False, 99.99),
            (-1, False, 100.01),
        ],
    )
    def test_per_share_slippage_uses_order_side(self, direction, is_entry, expected):
        result = apply_slippage(
            _slippage_config(per_share=0.01), 100.0, direction, is_entry
        )
        assert result == pytest.approx(expected)

    def test_factor_slippage_applies_when_per_share_is_disabled(self):
        result = apply_slippage(_slippage_config(factor=0.001), 100.0, 1, is_entry=True)
        assert result == pytest.approx(100.1)

    def test_per_share_slippage_takes_priority_over_factor(self):
        result = apply_slippage(
            _slippage_config(per_share=0.02, factor=0.01), 100.0, 1, is_entry=True
        )
        assert result == pytest.approx(100.02)
