"""Core data models for the copy trading bot."""

from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal
from enum import Enum
from typing import Any


class OrderStatus(Enum):
    PENDING = "pending"
    FILLED = "filled"
    PARTIAL = "partial"
    FAILED = "failed"
    CANCELLED = "cancelled"


class OrderSide(Enum):
    BUY = "buy"
    SELL = "sell"


@dataclass
class LeverageInfo:
    """Leverage configuration for a position."""

    type: str  # "cross" or "isolated"
    value: int

    @classmethod
    def from_api(cls, data: dict[str, Any]) -> LeverageInfo:
        return cls(
            type=data.get("type", "cross"),
            value=int(data.get("value", 1)),
        )


@dataclass
class PositionInfo:
    """A single perpetual position."""

    coin: str
    szi: Decimal  # Signed size: positive = long, negative = short
    entry_px: Decimal
    leverage: LeverageInfo
    unrealized_pnl: Decimal
    liquidation_px: Decimal | None = None

    @property
    def is_long(self) -> bool:
        return self.szi > 0

    @property
    def is_short(self) -> bool:
        return self.szi < 0

    @property
    def abs_size(self) -> Decimal:
        return abs(self.szi)

    @property
    def notional(self) -> Decimal:
        """Estimated notional value."""
        return self.abs_size * self.entry_px

    @classmethod
    def from_api(cls, data: dict[str, Any]) -> PositionInfo:
        """Parse from Hyperliquid clearinghouseState assetPositions entry."""
        pos = data.get("position", data)
        lev = data.get("leverage", pos.get("leverage", {}))
        return cls(
            coin=pos["coin"],
            szi=Decimal(str(pos["szi"])),
            entry_px=Decimal(str(pos.get("entryPx", "0"))),
            leverage=LeverageInfo.from_api(lev) if isinstance(lev, dict) else LeverageInfo("cross", 1),
            unrealized_pnl=Decimal(str(pos.get("unrealizedPnl", "0"))),
            liquidation_px=(
                Decimal(str(pos["liquidationPx"]))
                if pos.get("liquidationPx")
                else None
            ),
        )


@dataclass
class AccountState:
    """Complete account state from clearinghouseState."""

    address: str
    positions: dict[str, PositionInfo] = field(default_factory=dict)  # coin → PositionInfo
    account_value: Decimal = Decimal("0")
    total_margin_used: Decimal = Decimal("0")
    withdrawable: Decimal = Decimal("0")
    timestamp: float = 0.0

    @property
    def total_exposure(self) -> Decimal:
        """Sum of abs(notional) across all positions."""
        return sum(p.notional for p in self.positions.values())

    @classmethod
    def from_api(cls, address: str, data: dict[str, Any], timestamp: float) -> AccountState:
        """Parse from clearinghouseState API response."""
        margin = data.get("marginSummary", {})
        positions: dict[str, PositionInfo] = {}

        for item in data.get("assetPositions", []):
            pos = PositionInfo.from_api(item)
            if pos.szi != 0:  # Only track non-zero positions
                positions[pos.coin] = pos

        return cls(
            address=address,
            positions=positions,
            account_value=Decimal(str(margin.get("accountValue", "0"))),
            total_margin_used=Decimal(str(margin.get("totalMarginUsed", "0"))),
            withdrawable=Decimal(str(margin.get("withdrawable", "0"))),
            timestamp=timestamp,
        )


@dataclass
class LeaderFill:
    """A single fill event from the leader's WebSocket stream."""

    coin: str
    side: str  # "Buy" or "Sell" (as Hyperliquid sends it)
    size: Decimal  # Absolute size filled
    price: Decimal
    timestamp: float

    @property
    def is_buy(self) -> bool:
        return self.side in ("Buy", "B")

    @classmethod
    def from_ws(cls, data: dict[str, Any], ts: float) -> LeaderFill:
        return cls(
            coin=data.get("coin", ""),
            side=data.get("side", ""),
            size=Decimal(str(data.get("sz", "0"))),
            price=Decimal(str(data.get("px", "0"))),
            timestamp=ts,
        )


@dataclass
class OrderIntent:
    """A planned order to be executed."""

    coin: str
    delta: Decimal  # Signed quantity to change (positive = buy, negative = sell)
    is_buy: bool
    is_reduce_only: bool
    target_size: Decimal  # The desired final position size

    @property
    def abs_delta(self) -> Decimal:
        return abs(self.delta)


@dataclass
class OrderResult:
    """Result of an order execution attempt."""

    intent: OrderIntent
    status: OrderStatus
    filled_size: Decimal = Decimal("0")
    filled_price: Decimal = Decimal("0")
    oid: str | None = None
    cloid: str | None = None
    error: str | None = None
    timestamp: float = 0.0


@dataclass
class AssetMeta:
    """Metadata for a single asset from the meta endpoint."""

    name: str
    sz_decimals: int
    asset_index: int
    max_leverage: int = 50

    @property
    def min_size(self) -> Decimal:
        """Minimum tradeable size increment."""
        return Decimal(10) ** (-self.sz_decimals)
