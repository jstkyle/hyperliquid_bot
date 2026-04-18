"""Risk controller — validates and gates all order intents before execution."""

from __future__ import annotations

import time
from decimal import Decimal

from copybot.config.loader import BotConfig
from copybot.state.models import AccountState, OrderIntent
from copybot.utils.alerting import DiscordAlerter
from copybot.utils.logging import get_logger

logger = get_logger(__name__)


class KillSwitch:
    """Emergency stop mechanism that halts all trading."""

    def __init__(self, loss_usd: Decimal, loss_pct: Decimal):
        self.loss_usd = loss_usd
        self.loss_pct = loss_pct
        self.active = False
        self.reason: str | None = None
        self.activated_at: float | None = None

    def check(self, session_pnl: Decimal, starting_equity: Decimal) -> bool:
        """Check if kill switch should activate.

        Args:
            session_pnl: Current session PnL (negative = loss).
            starting_equity: Equity at bot start.

        Returns:
            True if kill switch is active (trading should stop).
        """
        if self.active:
            return True

        # Absolute loss check
        if session_pnl < self.loss_usd:
            self.activate(
                f"Session loss ${session_pnl} exceeded limit ${self.loss_usd}"
            )
            return True

        # Percentage loss check
        if starting_equity > 0:
            pct = session_pnl / starting_equity
            if pct < self.loss_pct:
                self.activate(
                    f"Session loss {pct:.2%} exceeded limit {self.loss_pct:.2%}"
                )
                return True

        return False

    def activate(self, reason: str) -> None:
        """Activate the kill switch."""
        self.active = True
        self.reason = reason
        self.activated_at = time.time()
        logger.critical("KILL SWITCH ACTIVATED", reason=reason)

    def reset(self) -> None:
        """Manual reset (requires human intervention)."""
        self.active = False
        self.reason = None
        self.activated_at = None
        logger.warning("Kill switch manually reset")


class RiskController:
    """Validates order intents against risk limits before execution.

    Risk checks are applied in order of severity. Reduce-only orders
    (position closes) bypass most limits to ensure we can always exit.
    """

    def __init__(
        self,
        config: BotConfig,
        alerter: DiscordAlerter | None = None,
    ):
        self.config = config
        self.alerter = alerter
        self.kill_switch = KillSwitch(
            loss_usd=config.risk.kill_switch.loss_usd,
            loss_pct=config.risk.kill_switch.loss_pct,
        )
        self._consecutive_failures: int = 0

    def record_failure(self) -> None:
        """Record an execution failure."""
        self._consecutive_failures += 1

    def record_success(self) -> None:
        """Record a successful execution."""
        self._consecutive_failures = 0

    async def check(
        self,
        intent: OrderIntent,
        follower_state: AccountState,
        session_pnl: Decimal,
        starting_equity: Decimal,
        mid_prices: dict[str, Decimal] | None = None,
    ) -> RiskDecision:
        """Run all risk checks on an order intent.

        Args:
            intent: The proposed order.
            follower_state: Current follower account state.
            session_pnl: Cumulative session PnL.
            starting_equity: Equity at bot start.
            mid_prices: Current mid prices for notional calculations.

        Returns:
            RiskDecision indicating whether to proceed, modify, or reject.
        """
        reason: str | None = None

        # 1. Kill switch check
        if self.kill_switch.check(session_pnl, starting_equity):
            reason = f"Kill switch active: {self.kill_switch.reason}"
            logger.warning("Order rejected by kill switch", coin=intent.coin)
            return RiskDecision(approved=False, reason=reason)

        # Reduce-only orders bypass most limits (we should always be able to close)
        if intent.is_reduce_only:
            return RiskDecision(approved=True)

        # 2. Symbol whitelist
        if not self.config.risk.is_whitelisted(intent.coin):
            reason = f"Coin {intent.coin} not in whitelist"
            logger.warning("Order rejected by whitelist", coin=intent.coin)
            return RiskDecision(approved=False, reason=reason)

        # 3. Max position size (notional)
        if mid_prices and intent.coin in mid_prices:
            price = mid_prices[intent.coin]
            target_notional = abs(intent.target_size) * price
            if target_notional > self.config.risk.max_position_usd:
                # Cap the order to stay within limit
                max_size = self.config.risk.max_position_usd / price
                current_pos = follower_state.positions.get(intent.coin)
                current_size = current_pos.szi if current_pos else Decimal("0")

                if abs(current_size) >= max_size:
                    reason = f"Position cap {self.config.risk.max_position_usd} USD already reached"
                    return RiskDecision(approved=False, reason=reason)

                capped_target = max_size if intent.target_size > 0 else -max_size
                capped_delta = capped_target - current_size

                logger.warning(
                    "Position capped by max_position_usd",
                    coin=intent.coin,
                    original_target=str(intent.target_size),
                    capped_target=str(capped_target),
                )
                return RiskDecision(
                    approved=True,
                    modified_intent=OrderIntent(
                        coin=intent.coin,
                        delta=capped_delta,
                        is_buy=(capped_delta > 0),
                        is_reduce_only=False,
                        target_size=capped_target,
                    ),
                )

        # 4. Max total exposure
        if mid_prices:
            current_exposure = Decimal("0")
            for coin, pos in follower_state.positions.items():
                if coin in mid_prices:
                    current_exposure += abs(pos.szi) * mid_prices[coin]

            # Add the proposed order's notional
            proposed_notional = Decimal("0")
            if intent.coin in mid_prices:
                proposed_notional = abs(intent.delta) * mid_prices[intent.coin]

            if current_exposure + proposed_notional > self.config.risk.max_total_exposure_usd:
                reason = (
                    f"Total exposure would exceed {self.config.risk.max_total_exposure_usd} USD "
                    f"(current: {current_exposure:.0f}, proposed: +{proposed_notional:.0f})"
                )
                logger.warning("Order rejected by total exposure limit", coin=intent.coin)
                return RiskDecision(approved=False, reason=reason)

        # 5. Consecutive failure check
        if self._consecutive_failures >= self.config.risk.max_consecutive_failures:
            reason = (
                f"Too many consecutive failures ({self._consecutive_failures})"
            )
            logger.error("Order rejected: consecutive failures", count=self._consecutive_failures)
            if self.alerter:
                asyncio.ensure_future(
                    self.alerter.alert_error("Consecutive Failures", reason)
                )
            return RiskDecision(approved=False, reason=reason)

        return RiskDecision(approved=True)


class RiskDecision:
    """Result of a risk check on an order intent."""

    def __init__(
        self,
        approved: bool,
        reason: str | None = None,
        modified_intent: OrderIntent | None = None,
    ):
        self.approved = approved
        self.reason = reason
        self.modified_intent = modified_intent  # Set if intent was capped/adjusted


# Need asyncio at module level for ensure_future in consecutive failure check
import asyncio  # noqa: E402
