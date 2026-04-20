"""Fill copier — directly copies leader fills with proportional scaling.

This is the PRIMARY execution path. When a leader fill is detected via
WebSocket, the fill copier immediately scales it and executes.

The reconciliation loop is the BACKUP that catches missed events.
"""

from __future__ import annotations

import asyncio
import time
from decimal import Decimal

from copybot.config.loader import BotConfig, PairConfig
from copybot.state.metadata import MetadataCache
from copybot.state.models import LeaderFill, OrderIntent, OrderResult, OrderStatus
from copybot.state.store import StateStore
from copybot.utils.alerting import DiscordAlerter
from copybot.utils.logging import get_logger
from copybot.utils.math import floor_to_decimals

from typing import TYPE_CHECKING
if TYPE_CHECKING:
    from copybot.controller import BotController

logger = get_logger(__name__)


class FillCopier:
    """Copies leader fills directly with proportional scaling.

    For each leader fill:
    1. Scale the fill size by (follower_equity / leader_equity) × multiplier
    2. Floor-truncate to szDecimals
    3. Execute immediately (same side)
    """

    def __init__(
        self,
        config: BotConfig,
        pair_config: PairConfig,
        metadata: MetadataCache,
        execution_engine,  # PaperExecutionEngine or ExecutionEngine
        store: StateStore,
        alerter: DiscordAlerter | None = None,
        controller: BotController | None = None,
    ):
        self.config = config
        self.pair_config = pair_config
        self.metadata = metadata
        self.execution = execution_engine
        self.store = store
        self.alerter = alerter
        self.controller = controller
        self.pair_name = pair_config.name

        # Fill history for tracking
        self._fill_history: list[dict] = []
        self._max_history: int = 500

        # Leader equity cache (refreshed periodically by recon loop)
        self._leader_equity: Decimal = Decimal("0")
        self._follower_equity: Decimal = Decimal("0")

    def update_equities(self, leader_equity: Decimal, follower_equity: Decimal) -> None:
        """Update cached equity values (called by reconciliation loop)."""
        self._leader_equity = leader_equity
        self._follower_equity = follower_equity

    async def copy_fill(self, fill: LeaderFill) -> OrderResult | None:
        """Scale and execute a leader fill immediately.

        Args:
            fill: The leader's fill event from WebSocket.

        Returns:
            OrderResult if executed, None if skipped.
        """
        # Check if paused
        if self.controller and self.controller.is_paused(self.pair_name):
            logger.debug("Fill copy skipped (paused)", coin=fill.coin, pair=self.pair_name)
            return None

        # Need equity values to scale
        if self._leader_equity <= 0 or self._follower_equity <= 0:
            logger.warning(
                "Cannot copy fill — equity not loaded yet",
                coin=fill.coin,
                leader_equity=str(self._leader_equity),
                follower_equity=str(self._follower_equity),
            )
            return None

        # Whitelist check
        if not self.config.risk.is_whitelisted(fill.coin):
            logger.debug("Fill skipped — not whitelisted", coin=fill.coin)
            return None

        # Get asset metadata
        sz_decimals = self.metadata.get_sz_decimals(fill.coin)
        if sz_decimals is None:
            logger.warning("Fill skipped — no metadata", coin=fill.coin)
            return None

        # Scale the fill size
        scale_factor = self._follower_equity / self._leader_equity * self.config.scaling.multiplier
        scaled_size = floor_to_decimals(fill.size * scale_factor, sz_decimals)

        if scaled_size <= 0:
            logger.debug(
                "Fill too small after scaling",
                coin=fill.coin,
                leader_size=str(fill.size),
                scaled=str(scaled_size),
            )
            return None

        # Check minimum notional
        notional = scaled_size * fill.price
        if notional < self.config.scaling.min_order_notional:
            logger.debug(
                "Fill below min notional",
                coin=fill.coin,
                notional=str(notional),
                min_notional=str(self.config.scaling.min_order_notional),
            )
            return None

        # Build the order intent (same side as leader)
        delta = scaled_size if fill.is_buy else -scaled_size
        intent = OrderIntent(
            coin=fill.coin,
            delta=delta,
            is_buy=fill.is_buy,
            is_reduce_only=False,  # We don't know if this reduces — execution engine handles it
            target_size=Decimal("0"),  # Not used in fill-based mode
        )

        # Execute
        mid_prices = {fill.coin: fill.price}  # Use the fill price as reference
        result = await self.execution.execute(intent, mid_prices)

        # Log
        if result.status == OrderStatus.FILLED:
            logger.info(
                "✅ Fill copied",
                pair=self.pair_name,
                coin=fill.coin,
                leader_side=fill.side,
                leader_size=str(fill.size),
                our_size=str(result.filled_size),
                price=str(result.filled_price),
                scale_factor=f"{scale_factor:.4f}",
            )

            # Discord notification
            if self.alerter:
                side = "BUY" if fill.is_buy else "SELL"
                prefix = "📝 PAPER" if self.config.is_paper else "💰 LIVE"
                await self.alerter.send(
                    title=f"{prefix} | {side} {fill.coin}",
                    message=(
                        f"**Leader filled:** {fill.size} @ ${fill.price}\n"
                        f"**We copied:** {result.filled_size} @ ${result.filled_price}\n"
                        f"**Scale:** {scale_factor:.2%}"
                    ),
                    color=0x00FF00 if fill.is_buy else 0xFF6600,
                )

            # Update controller
            if self.controller:
                self.controller.increment_trades(self.pair_name)

        elif result.status == OrderStatus.FAILED:
            logger.error(
                "❌ Fill copy failed",
                pair=self.pair_name,
                coin=fill.coin,
                error=result.error,
            )

        # Store in history
        self._fill_history.append({
            "time": fill.timestamp,
            "leader_coin": fill.coin,
            "leader_side": fill.side,
            "leader_size": str(fill.size),
            "leader_price": str(fill.price),
            "our_size": str(result.filled_size),
            "our_price": str(result.filled_price),
            "status": result.status.value,
        })
        if len(self._fill_history) > self._max_history:
            self._fill_history = self._fill_history[-self._max_history:]

        # Persist to database
        await self.store.log_order(self.pair_name, result)

        return result

    @property
    def fill_history(self) -> list[dict]:
        return list(self._fill_history)
