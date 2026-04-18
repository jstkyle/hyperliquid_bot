"""Unit tests for scaling logic and math utilities."""

from __future__ import annotations

from decimal import Decimal

import pytest

from copybot.utils.math import (
    compute_delta,
    compute_price_decimals,
    compute_target_size,
    floor_to_decimals,
    is_direction_flip,
    notional_value,
    round_price,
    round_price_to_sig_figs,
)


class TestFloorToDecimals:
    """Tests for floor truncation — the foundation of size rounding."""

    def test_basic_truncation(self):
        assert floor_to_decimals(Decimal("1.23456"), 3) == Decimal("1.234")

    def test_no_truncation_needed(self):
        assert floor_to_decimals(Decimal("1.23"), 3) == Decimal("1.230")

    def test_zero_decimals(self):
        assert floor_to_decimals(Decimal("1.999"), 0) == Decimal("1")

    def test_negative_value(self):
        """Negative values truncate toward zero."""
        assert floor_to_decimals(Decimal("-1.23456"), 3) == Decimal("-1.234")

    def test_negative_rounds_toward_zero(self):
        """Ensure -1.999 truncated to 0 decimals is -1, not -2."""
        assert floor_to_decimals(Decimal("-1.999"), 0) == Decimal("-1")

    def test_zero(self):
        assert floor_to_decimals(Decimal("0"), 5) == Decimal("0")

    def test_large_decimals(self):
        assert floor_to_decimals(Decimal("0.123456789"), 8) == Decimal("0.12345678")

    def test_exact_value(self):
        assert floor_to_decimals(Decimal("1.5"), 1) == Decimal("1.5")

    def test_invalid_decimals(self):
        with pytest.raises(ValueError):
            floor_to_decimals(Decimal("1.0"), -1)


class TestComputeTargetSize:
    """Tests for the core scaling formula."""

    def test_basic_scaling(self):
        """Leader: 1.0 BTC, equities 100k vs 10k → 0.10000."""
        result = compute_target_size(
            Decimal("1.0"), Decimal("100000"), Decimal("10000"), Decimal("1.0"), 5
        )
        assert result == Decimal("0.10000")

    def test_scaling_with_multiplier(self):
        """0.5x multiplier halves exposure."""
        result = compute_target_size(
            Decimal("1.0"), Decimal("100000"), Decimal("10000"), Decimal("0.5"), 5
        )
        assert result == Decimal("0.05000")

    def test_scaling_2x_multiplier(self):
        """2x multiplier doubles exposure."""
        result = compute_target_size(
            Decimal("1.0"), Decimal("100000"), Decimal("10000"), Decimal("2.0"), 5
        )
        assert result == Decimal("0.20000")

    def test_floor_truncation_applied(self):
        """Floor truncation: 0.123456789 → 0.1234 at szDecimals=4."""
        result = compute_target_size(
            Decimal("1.23456789"), Decimal("100000"), Decimal("10000"), Decimal("1.0"), 4
        )
        assert result == Decimal("0.1234")

    def test_short_position(self):
        """Short positions scale correctly (negative size)."""
        result = compute_target_size(
            Decimal("-2.5"), Decimal("50000"), Decimal("10000"), Decimal("1.0"), 3
        )
        assert result == Decimal("-0.500")

    def test_zero_leader_equity(self):
        """Leader equity = 0 → target should be 0."""
        result = compute_target_size(
            Decimal("1.0"), Decimal("0"), Decimal("10000"), Decimal("1.0"), 5
        )
        assert result == Decimal("0")

    def test_zero_leader_size(self):
        """No leader position → target = 0."""
        result = compute_target_size(
            Decimal("0"), Decimal("100000"), Decimal("10000"), Decimal("1.0"), 5
        )
        assert result == Decimal("0")

    def test_zero_follower_equity(self):
        """Zero follower equity → target = 0."""
        result = compute_target_size(
            Decimal("1.0"), Decimal("100000"), Decimal("0"), Decimal("1.0"), 5
        )
        assert result == Decimal("0")

    def test_equal_equities(self):
        """Equal equities → same size as leader (before truncation)."""
        result = compute_target_size(
            Decimal("1.0"), Decimal("50000"), Decimal("50000"), Decimal("1.0"), 5
        )
        assert result == Decimal("1.00000")

    def test_follower_larger_equity(self):
        """Follower has more equity → larger position."""
        result = compute_target_size(
            Decimal("1.0"), Decimal("10000"), Decimal("50000"), Decimal("1.0"), 5
        )
        assert result == Decimal("5.00000")

    def test_negative_leader_equity(self):
        """Negative leader equity → return 0."""
        result = compute_target_size(
            Decimal("1.0"), Decimal("-100"), Decimal("10000"), Decimal("1.0"), 5
        )
        assert result == Decimal("0")

    def test_very_small_result(self):
        """Very small follower → result truncates to zero."""
        result = compute_target_size(
            Decimal("0.001"), Decimal("1000000"), Decimal("100"), Decimal("1.0"), 5
        )
        assert result == Decimal("0.00000")

    def test_sz_decimals_zero(self):
        """szDecimals=0 means whole numbers only."""
        result = compute_target_size(
            Decimal("100"), Decimal("100000"), Decimal("10000"), Decimal("1.0"), 0
        )
        assert result == Decimal("10")


class TestComputeDelta:
    """Tests for delta computation."""

    def test_open_long(self):
        assert compute_delta(Decimal("1.0"), Decimal("0")) == Decimal("1.0")

    def test_open_short(self):
        assert compute_delta(Decimal("-1.0"), Decimal("0")) == Decimal("-1.0")

    def test_increase_long(self):
        assert compute_delta(Decimal("2.0"), Decimal("1.0")) == Decimal("1.0")

    def test_decrease_long(self):
        assert compute_delta(Decimal("0.5"), Decimal("1.0")) == Decimal("-0.5")

    def test_close_long(self):
        assert compute_delta(Decimal("0"), Decimal("1.0")) == Decimal("-1.0")

    def test_close_short(self):
        assert compute_delta(Decimal("0"), Decimal("-1.0")) == Decimal("1.0")

    def test_no_change(self):
        assert compute_delta(Decimal("1.0"), Decimal("1.0")) == Decimal("0")

    def test_flip_long_to_short(self):
        assert compute_delta(Decimal("-0.5"), Decimal("1.0")) == Decimal("-1.5")


class TestIsDirectionFlip:
    """Tests for direction flip detection."""

    def test_long_to_short(self):
        assert is_direction_flip(Decimal("1.0"), Decimal("-0.5")) is True

    def test_short_to_long(self):
        assert is_direction_flip(Decimal("-1.0"), Decimal("0.5")) is True

    def test_long_increase(self):
        assert is_direction_flip(Decimal("1.0"), Decimal("2.0")) is False

    def test_short_increase(self):
        assert is_direction_flip(Decimal("-1.0"), Decimal("-2.0")) is False

    def test_zero_to_long(self):
        assert is_direction_flip(Decimal("0"), Decimal("1.0")) is False

    def test_long_to_zero(self):
        assert is_direction_flip(Decimal("1.0"), Decimal("0")) is False

    def test_zero_to_zero(self):
        assert is_direction_flip(Decimal("0"), Decimal("0")) is False


class TestRoundPrice:
    """Tests for price rounding to Hyperliquid tick constraints."""

    def test_5_sig_figs(self):
        result = round_price_to_sig_figs(Decimal("12345.678"), 5)
        assert result == Decimal("12345")

    def test_small_price(self):
        result = round_price_to_sig_figs(Decimal("0.001234"), 5)
        assert result == Decimal("0.001234")

    def test_zero_price(self):
        assert round_price_to_sig_figs(Decimal("0"), 5) == Decimal("0")

    def test_price_decimals_perp(self):
        """BTC szDecimals=5 → max 1 price decimal."""
        assert compute_price_decimals(5, is_perp=True) == 1

    def test_price_decimals_low_sz(self):
        """szDecimals=0 → max 6 price decimals for perp."""
        assert compute_price_decimals(0, is_perp=True) == 6


class TestNotionalValue:
    def test_basic(self):
        assert notional_value(Decimal("0.5"), Decimal("60000")) == Decimal("30000")

    def test_negative_size(self):
        """Notional is always positive."""
        assert notional_value(Decimal("-0.5"), Decimal("60000")) == Decimal("30000")
