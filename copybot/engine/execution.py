"""Execution engine — places orders on Hyperliquid via the Python SDK."""

from __future__ import annotations

import time
from decimal import Decimal

from hyperliquid.exchange import Exchange
from hyperliquid.info import Info
from hyperliquid.utils import constants

from copybot.config.loader import BotConfig, PairConfig
from copybot.state.metadata import MetadataCache
from copybot.state.models import OrderIntent, OrderResult, OrderStatus
from copybot.utils.logging import get_logger
from copybot.utils.math import round_price

logger = get_logger(__name__)


class ExecutionEngine:
    """Places and manages orders on the Hyperliquid exchange.

    All copy orders use IOC (Immediate-or-Cancel) with an aggressive price offset
    to simulate market orders, since HL doesn't have native market orders.
    """

    def __init__(
        self,
        config: BotConfig,
        pair_config: PairConfig,
        metadata: MetadataCache,
    ):
        self.config = config
        self.pair_config = pair_config
        self.metadata = metadata

        # Initialize SDK clients
        api_url = config.api_url
        self._info = Info(api_url, skip_ws=True)

        # The Exchange object handles signing with the agent private key
        self._exchange = Exchange(
            wallet=None,
            base_url=api_url,
            account_address=pair_config.follower_address,
        )
        # We'll initialize the exchange wallet in a setup method
        self._initialized = False

    async def initialize(self) -> None:
        """Initialize the exchange with the agent private key.

        Must be called after construction, once the private key is available.
        """
        if self._initialized:
            return

        try:
            base_url = self.config.api_url
            # The SDK Exchange takes the private key for signing
            # account_address should be the master/follower wallet address
            self._exchange = Exchange(
                wallet=self.pair_config.agent_private_key,
                base_url=base_url,
                account_address=self.pair_config.follower_address,
            )
            self._initialized = True
            logger.info(
                "Execution engine initialized",
                pair=self.pair_config.name,
                follower=self.pair_config.follower_address[:10] + "...",
            )
        except Exception as e:
            logger.error(
                "Failed to initialize execution engine",
                error=str(e),
                pair=self.pair_config.name,
            )
            raise

    async def execute(
        self,
        intent: OrderIntent,
        mid_prices: dict[str, Decimal],
    ) -> OrderResult:
        """Execute a single order intent.

        Args:
            intent: The order to execute.
            mid_prices: Current mid prices for aggressive pricing.

        Returns:
            OrderResult with fill status and details.
        """
        if not self._initialized:
            return OrderResult(
                intent=intent,
                status=OrderStatus.FAILED,
                error="Execution engine not initialized",
                timestamp=time.time(),
            )

        coin = intent.coin
        mid_price = mid_prices.get(coin)
        if mid_price is None or mid_price <= 0:
            return OrderResult(
                intent=intent,
                status=OrderStatus.FAILED,
                error=f"No mid price available for {coin}",
                timestamp=time.time(),
            )

        # Compute aggressive IOC price
        slippage_bps = self.config.risk.slippage_tolerance_bps
        slip = mid_price * Decimal(slippage_bps) / Decimal(10000)

        if intent.is_buy:
            limit_px = mid_price + slip
        else:
            limit_px = mid_price - slip

        # Round price to valid tick
        sz_decimals = self.metadata.get_sz_decimals(coin)
        limit_px = round_price(limit_px, sz_decimals)

        # Ensure positive price
        if limit_px <= 0:
            limit_px = Decimal("0.01")

        size = intent.abs_delta

        logger.info(
            "Placing order",
            pair=self.pair_config.name,
            coin=coin,
            side="buy" if intent.is_buy else "sell",
            size=str(size),
            limit_px=str(limit_px),
            reduce_only=intent.is_reduce_only,
        )

        try:
            # Sync leverage if needed (before opening)
            if not intent.is_reduce_only:
                await self._sync_leverage(coin)

            # Place IOC order via SDK
            result = self._exchange.order(
                coin,
                intent.is_buy,
                float(size),
                float(limit_px),
                {"limit": {"tif": "Ioc"}},
                reduce_only=intent.is_reduce_only,
            )

            return self._parse_order_result(intent, result)

        except Exception as e:
            logger.error(
                "Order execution failed",
                pair=self.pair_config.name,
                coin=coin,
                error=str(e),
            )
            return OrderResult(
                intent=intent,
                status=OrderStatus.FAILED,
                error=str(e),
                timestamp=time.time(),
            )

    def _parse_order_result(self, intent: OrderIntent, result: dict) -> OrderResult:
        """Parse the SDK order response into an OrderResult."""
        ts = time.time()

        if result.get("status") != "ok":
            error_msg = str(result.get("response", result))
            return OrderResult(
                intent=intent,
                status=OrderStatus.FAILED,
                error=error_msg,
                timestamp=ts,
            )

        response = result.get("response", {})
        data = response.get("data", {})
        statuses = data.get("statuses", [])

        if not statuses:
            return OrderResult(
                intent=intent,
                status=OrderStatus.FAILED,
                error="No status in response",
                timestamp=ts,
            )

        status_entry = statuses[0]

        if "filled" in status_entry:
            filled = status_entry["filled"]
            return OrderResult(
                intent=intent,
                status=OrderStatus.FILLED,
                filled_size=Decimal(str(filled.get("totalSz", "0"))),
                filled_price=Decimal(str(filled.get("avgPx", "0"))),
                oid=str(filled.get("oid", "")),
                timestamp=ts,
            )

        if "resting" in status_entry:
            resting = status_entry["resting"]
            logger.warning(
                "IOC order resting (unexpected)",
                oid=resting.get("oid"),
            )
            return OrderResult(
                intent=intent,
                status=OrderStatus.PARTIAL,
                oid=str(resting.get("oid", "")),
                timestamp=ts,
            )

        if "error" in status_entry:
            return OrderResult(
                intent=intent,
                status=OrderStatus.FAILED,
                error=status_entry["error"],
                timestamp=ts,
            )

        return OrderResult(
            intent=intent,
            status=OrderStatus.FAILED,
            error=f"Unknown status: {status_entry}",
            timestamp=ts,
        )

    async def _sync_leverage(self, coin: str) -> None:
        """Ensure the follower's leverage setting matches limits."""
        max_lev = min(
            self.config.risk.max_leverage,
            self.metadata.get_max_leverage(coin),
        )

        asset_index = self.metadata.get_asset_index(coin)
        if asset_index is None:
            logger.warning("Cannot sync leverage: unknown asset index", coin=coin)
            return

        try:
            self._exchange.update_leverage(
                max_lev,
                coin,
                is_cross=True,
            )
            logger.debug("Leverage synced", coin=coin, leverage=max_lev)
        except Exception as e:
            # Non-fatal: leverage might already be set correctly
            logger.debug("Leverage sync skipped", coin=coin, error=str(e))

    async def close_all_positions(
        self,
        follower_positions: dict[str, "PositionInfo"],
        mid_prices: dict[str, Decimal],
    ) -> list[OrderResult]:
        """Emergency close all follower positions (used by kill switch).

        Args:
            follower_positions: Current follower positions to close.
            mid_prices: Current mid prices.

        Returns:
            List of order results for each close attempt.
        """
        results = []
        for coin, pos in follower_positions.items():
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
            logger.info(
                "Kill switch close",
                coin=coin,
                status=result.status.value,
                error=result.error,
            )

        return results
