"""Decision engine — computes scaled targets and order intents from position deltas."""

from __future__ import annotations

from decimal import Decimal

from copybot.config.loader import BotConfig
from copybot.state.metadata import MetadataCache
from copybot.state.models import AccountState, OrderIntent
from copybot.utils.logging import get_logger
from copybot.utils.math import compute_delta, compute_target_size, is_direction_flip

logger = get_logger(__name__)


class DecisionEngine:
    """Core decision logic: compares leader vs follower state and produces order intents.

    Principle: We copy POSITIONS, not orders. The engine computes the target
    follower position for each coin, then determines the minimum set of orders
    to converge the follower to that target.
    """

    def __init__(self, config: BotConfig, metadata: MetadataCache):
        self.config = config
        self.metadata = metadata

    def compute_intents(
        self,
        leader_state: AccountState,
        follower_state: AccountState,
        force: bool = False,
    ) -> list[OrderIntent]:
        """Compare leader and follower states, return order intents to converge.

        Args:
            leader_state: Current leader account state.
            follower_state: Current follower account state.
            force: If True, skip drift threshold check (used on event-triggered runs).

        Returns:
            List of OrderIntents to be passed through risk controller and executed.
        """
        if leader_state.account_value <= 0:
            # Leader has no equity — they likely closed everything.
            # If follower still has open positions, close them all.
            if follower_state.positions:
                logger.warning(
                    "Leader equity is zero — closing all follower positions to mirror",
                    leader_equity=str(leader_state.account_value),
                    follower_positions=list(follower_state.positions.keys()),
                )
                close_intents: list[OrderIntent] = []
                for coin, pos in follower_state.positions.items():
                    close_intents.append(
                        OrderIntent(
                            coin=coin,
                            delta=-pos.szi,
                            is_buy=(pos.szi < 0),  # buy to close short, sell to close long
                            is_reduce_only=True,
                            target_size=Decimal("0"),
                        )
                    )
                    logger.info(
                        "Queued close for orphaned position",
                        coin=coin,
                        size=str(pos.szi),
                    )
                return close_intents
            else:
                logger.info(
                    "Leader equity is zero and follower has no positions — nothing to do",
                    leader_equity=str(leader_state.account_value),
                )
                return []

        if follower_state.account_value <= 0:
            logger.warning(
                "Follower equity is zero or negative — skipping",
                follower_equity=str(follower_state.account_value),
            )
            return []

        intents: list[OrderIntent] = []

        # Consider all coins in either leader or follower positions
        all_coins = set(leader_state.positions.keys()) | set(follower_state.positions.keys())

        for coin in all_coins:
            # Whitelist check
            if not self.config.risk.is_whitelisted(coin):
                leader_pos = leader_state.positions.get(coin)
                if leader_pos:
                    logger.debug("Skipping non-whitelisted coin", coin=coin)
                continue

            coin_intents = self._compute_coin_intents(
                coin, leader_state, follower_state, force
            )
            intents.extend(coin_intents)

        if intents:
            logger.info(
                "Decision engine produced intents",
                count=len(intents),
                coins=[i.coin for i in intents],
            )

        return intents

    def _compute_coin_intents(
        self,
        coin: str,
        leader_state: AccountState,
        follower_state: AccountState,
        force: bool,
    ) -> list[OrderIntent]:
        """Compute order intents for a single coin."""
        leader_pos = leader_state.positions.get(coin)
        follower_pos = follower_state.positions.get(coin)

        leader_szi = leader_pos.szi if leader_pos else Decimal("0")
        follower_szi = follower_pos.szi if follower_pos else Decimal("0")

        sz_decimals = self.metadata.get_sz_decimals(coin)

        # Compute target position
        target = compute_target_size(
            leader_szi,
            leader_state.account_value,
            follower_state.account_value,
            self.config.scaling.multiplier,
            sz_decimals,
        )

        # Compute delta
        delta = compute_delta(target, follower_szi)

        if delta == 0:
            return []

        # Check minimum order size (notional)
        abs_delta = abs(delta)
        meta = self.metadata.get(coin)
        min_size = meta.min_size if meta else Decimal("0.0001")

        if abs_delta < min_size:
            logger.debug(
                "Delta below minimum size, skipping",
                coin=coin,
                delta=str(delta),
                min_size=str(min_size),
            )
            return []

        # Check drift threshold (skip tiny adjustments)
        # force=True uses a lower threshold but NEVER skips entirely
        if target != 0:
            drift_pct = abs_delta / abs(target)
            threshold = self.config.scaling.drift_threshold_pct
            if force:
                # Even forced reconciliation ignores sub-0.5% drifts
                threshold = min(threshold, Decimal("0.005"))
            if drift_pct < threshold:
                logger.debug(
                    "Drift below threshold, skipping",
                    coin=coin,
                    drift_pct=f"{drift_pct:.4f}",
                    threshold=str(threshold),
                    forced=force,
                )
                return []

        # Plan the orders
        return self._plan_orders(coin, follower_szi, target)

    def _plan_orders(
        self, coin: str, current: Decimal, target: Decimal
    ) -> list[OrderIntent]:
        """Plan orders to move from current position to target.

        Handles the special case of direction flips (long → short or vice versa)
        by splitting into two orders: a reduce-only close + a new open.
        """
        orders: list[OrderIntent] = []

        if current == target:
            return orders

        if is_direction_flip(current, target):
            # Step 1: Close existing position (reduce-only)
            orders.append(
                OrderIntent(
                    coin=coin,
                    delta=-current,
                    is_buy=(current < 0),
                    is_reduce_only=True,
                    target_size=Decimal("0"),
                )
            )
            # Step 2: Open in opposite direction
            orders.append(
                OrderIntent(
                    coin=coin,
                    delta=target,
                    is_buy=(target > 0),
                    is_reduce_only=False,
                    target_size=target,
                )
            )
            logger.info(
                "Direction flip planned",
                coin=coin,
                current=str(current),
                target=str(target),
            )
        else:
            # Simple increase/decrease — single order
            delta = target - current
            is_reducing = abs(target) < abs(current)
            orders.append(
                OrderIntent(
                    coin=coin,
                    delta=delta,
                    is_buy=(delta > 0),
                    is_reduce_only=is_reducing,
                    target_size=target,
                )
            )

        return orders
