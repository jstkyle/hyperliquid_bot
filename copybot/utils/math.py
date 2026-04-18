"""Math utilities for Hyperliquid order sizing and price rounding."""

from __future__ import annotations

from decimal import ROUND_DOWN, Decimal


def floor_to_decimals(value: Decimal, decimals: int) -> Decimal:
    """Floor-truncate a decimal to the given number of decimal places.

    Always truncates toward zero — never rounds up, which would risk
    exceeding available balance.

    Args:
        value: The value to truncate.
        decimals: Number of decimal places to keep.

    Returns:
        Truncated value.

    Examples:
        >>> floor_to_decimals(Decimal("1.23456"), 3)
        Decimal('1.234')
        >>> floor_to_decimals(Decimal("-1.23456"), 3)
        Decimal('-1.234')
    """
    if decimals < 0:
        raise ValueError(f"decimals must be >= 0, got {decimals}")

    factor = Decimal(10) ** decimals
    if value >= 0:
        return (value * factor).to_integral_value(rounding=ROUND_DOWN) / factor
    else:
        return -((-value * factor).to_integral_value(rounding=ROUND_DOWN) / factor)


def round_price_to_sig_figs(price: Decimal, sig_figs: int = 5) -> Decimal:
    """Round a price to the given number of significant figures.

    Hyperliquid requires prices to have at most 5 significant figures.
    Integer prices are always allowed regardless of sig figs.

    Args:
        price: The price to round.
        sig_figs: Maximum significant figures (default 5 per HL spec).

    Returns:
        Price rounded to sig_figs significant figures.
    """
    if price == 0:
        return Decimal("0")

    abs_price = abs(price)
    sign = Decimal("1") if price > 0 else Decimal("-1")

    # Find the order of magnitude
    # e.g. 12345.678 → exponent = 4 (10^4 = 10000)
    import math

    exponent = math.floor(math.log10(float(abs_price)))
    factor = Decimal(10) ** (exponent - sig_figs + 1)

    rounded = (abs_price / factor).to_integral_value(rounding=ROUND_DOWN) * factor
    return sign * rounded


def compute_price_decimals(sz_decimals: int, is_perp: bool = True) -> int:
    """Compute max allowed price decimal places for an asset.

    Perps: MAX_DECIMALS(6) - szDecimals
    Spot:  MAX_DECIMALS(8) - szDecimals

    Args:
        sz_decimals: Size decimals from asset metadata.
        is_perp: True for perpetuals, False for spot.

    Returns:
        Maximum decimal places allowed for the price.
    """
    max_decimals = 6 if is_perp else 8
    return max(0, max_decimals - sz_decimals)


def round_price(price: Decimal, sz_decimals: int, is_perp: bool = True) -> Decimal:
    """Round price to valid Hyperliquid tick size.

    Applies both significant figure and decimal place constraints.

    Args:
        price: The raw price.
        sz_decimals: Size decimals from asset metadata.
        is_perp: True for perpetuals.

    Returns:
        Rounded price valid for Hyperliquid order submission.
    """
    # First: round to 5 significant figures
    price = round_price_to_sig_figs(price, 5)

    # Second: enforce max decimals
    max_dec = compute_price_decimals(sz_decimals, is_perp)
    return floor_to_decimals(price, max_dec)


def compute_target_size(
    leader_szi: Decimal,
    leader_equity: Decimal,
    follower_equity: Decimal,
    multiplier: Decimal,
    sz_decimals: int,
) -> Decimal:
    """Compute the target follower position size scaled by equity ratio.

    Formula: target = leader_szi × (follower_equity / leader_equity) × multiplier
    Result is floor-truncated to sz_decimals.

    Args:
        leader_szi: Leader's signed position size (+ long, - short).
        leader_equity: Leader's account value in USD.
        follower_equity: Follower's account value in USD.
        multiplier: Additional scaling factor.
        sz_decimals: Decimal precision for the asset.

    Returns:
        Target signed position size for the follower.
    """
    if leader_equity <= 0:
        return Decimal("0")

    if leader_szi == 0:
        return Decimal("0")

    raw = leader_szi * (follower_equity / leader_equity) * multiplier
    return floor_to_decimals(raw, sz_decimals)


def compute_delta(
    target_size: Decimal,
    current_size: Decimal,
) -> Decimal:
    """Compute the order delta needed to move from current to target position.

    Args:
        target_size: Desired signed position size.
        current_size: Current signed position size.

    Returns:
        Signed delta (positive = buy, negative = sell).
    """
    return target_size - current_size


def is_direction_flip(current: Decimal, target: Decimal) -> bool:
    """Check if moving from current to target requires a direction flip.

    A flip occurs when current is long and target is short (or vice versa).
    """
    if current == 0 or target == 0:
        return False
    return (current > 0) != (target > 0)


def notional_value(size: Decimal, price: Decimal) -> Decimal:
    """Compute notional value of a position."""
    return abs(size) * price
