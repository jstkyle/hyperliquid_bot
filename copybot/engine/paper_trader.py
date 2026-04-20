"""Paper trading execution engine — simulates fills without touching the exchange."""

from __future__ import annotations

import time
from copy import deepcopy
from decimal import Decimal

from copybot.state.models import (
    AccountState,
    LeverageInfo,
    OrderIntent,
    OrderResult,
    OrderStatus,
    PositionInfo,
)
from copybot.utils.logging import get_logger

logger = get_logger(__name__)


class PaperExecutionEngine:
    """Simulates order execution for paper trading mode.

    All intents are 'filled' at the aggressive price without actually
    submitting to the exchange. A virtual follower state is maintained
    to track paper positions and PnL.
    """

    def __init__(self, follower_address: str, pair_name: str):
        self.follower_address = follower_address
        self.pair_name = pair_name

        # Virtual state
        self._paper_positions: dict[str, PositionInfo] = {}
        self._paper_equity: Decimal = Decimal("0")
        self._total_orders: int = 0
        self._total_fills: int = 0

    def set_initial_equity(self, equity: Decimal) -> None:
        """Set the starting equity for paper trading."""
        self._paper_equity = equity
        logger.info(
            "Paper trader initial equity set",
            pair=self.pair_name,
            equity=str(equity),
        )

    async def initialize(self) -> None:
        """No-op for paper trading (no SDK initialization needed)."""
        logger.info("Paper execution engine initialized", pair=self.pair_name)

    async def execute(
        self,
        intent: OrderIntent,
        mid_prices: dict[str, Decimal],
    ) -> OrderResult:
        """Simulate order execution.

        The order is assumed to fill completely at the mid price
        plus the slippage offset (simulating aggressive IOC).

        Args:
            intent: The order to simulate.
            mid_prices: Current mid prices.

        Returns:
            OrderResult with simulated fill.
        """
        self._total_orders += 1
        coin = intent.coin
        mid_price = mid_prices.get(coin)

        if mid_price is None or mid_price <= 0:
            return OrderResult(
                intent=intent,
                status=OrderStatus.FAILED,
                error=f"No mid price for {coin} (paper)",
                timestamp=time.time(),
            )

        # Simulate fill at mid price (no slippage for paper)
        fill_price = mid_price
        fill_size = intent.abs_delta

        # Update virtual position
        self._apply_fill(coin, intent.delta, fill_price)
        self._total_fills += 1

        logger.info(
            "📝 PAPER TRADE executed",
            pair=self.pair_name,
            coin=coin,
            side="BUY" if intent.is_buy else "SELL",
            size=str(fill_size),
            price=str(fill_price),
            reduce_only=intent.is_reduce_only,
            new_position=str(self._paper_positions.get(coin)),
        )

        return OrderResult(
            intent=intent,
            status=OrderStatus.FILLED,
            filled_size=fill_size,
            filled_price=fill_price,
            timestamp=time.time(),
        )

    def _apply_fill(self, coin: str, delta: Decimal, price: Decimal) -> None:
        """Update virtual positions after a simulated fill."""
        current = self._paper_positions.get(coin)

        if current:
            new_szi = current.szi + delta
        else:
            new_szi = delta

        if new_szi == 0:
            # Position closed
            if coin in self._paper_positions:
                del self._paper_positions[coin]
        else:
            self._paper_positions[coin] = PositionInfo(
                coin=coin,
                szi=new_szi,
                entry_px=price,  # Simplified: use fill price as entry
                leverage=LeverageInfo("cross", 1),
                unrealized_pnl=Decimal("0"),
            )

    async def close_all_positions(
        self,
        follower_positions: dict[str, PositionInfo],
        mid_prices: dict[str, Decimal],
    ) -> list[OrderResult]:
        """Emergency close all paper positions."""
        results = []
        for coin, pos in list(self._paper_positions.items()):
            if pos.szi == 0:
                continue

            intent = OrderIntent(
                coin=coin,
                delta=-pos.szi,
                is_buy=(pos.szi < 0),
                is_reduce_only=True,
                target_size=Decimal("0"),
            )
            result = await self.execute(intent, mid_prices)
            results.append(result)

        return results

    @property
    def paper_positions(self) -> dict[str, PositionInfo]:
        return dict(self._paper_positions)

    @property
    def stats(self) -> dict:
        return {
            "total_orders": self._total_orders,
            "total_fills": self._total_fills,
            "open_positions": len(self._paper_positions),
            "positions": {
                coin: str(pos.szi) for coin, pos in self._paper_positions.items()
            },
        }

    def get_account_state(self) -> AccountState:
        """Return virtual positions as an AccountState for reconciliation.

        This prevents the reconciliation loop from fetching real (empty)
        state from the API and re-opening positions every cycle.
        """
        return AccountState(
            address=self.follower_address or "paper_wallet",
            account_value=self._paper_equity,
            positions=dict(self._paper_positions),
            timestamp=time.time(),
        )
