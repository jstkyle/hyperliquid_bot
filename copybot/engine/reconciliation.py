"""Reconciliation loop — periodically corrects drift between leader and follower."""

from __future__ import annotations

import asyncio
from decimal import Decimal

from copybot.config.loader import BotConfig, PairConfig
from copybot.engine.decision import DecisionEngine
from copybot.engine.risk import RiskController
from copybot.ingestion.rest_poller import RestPoller
from copybot.state.metadata import MetadataCache
from copybot.state.models import OrderStatus
from copybot.state.store import StateStore
from copybot.utils.alerting import DiscordAlerter
from copybot.utils.logging import get_logger

from typing import TYPE_CHECKING
if TYPE_CHECKING:
    from copybot.controller import BotController

logger = get_logger(__name__)


class ReconciliationLoop:
    """Periodically fetches fresh state and corrects any position drift.

    This loop is the safety net that catches:
    - Missed WebSocket events
    - Partial fills that didn't fully converge
    - Rounding drift over time
    - Orphaned follower positions (leader closed but follower didn't)

    It also handles event-triggered reconciliation when the WebSocket listener
    detects leader activity (via the leader_event asyncio.Event).
    """

    def __init__(
        self,
        config: BotConfig,
        pair_config: PairConfig,
        poller: RestPoller,
        metadata: MetadataCache,
        decision_engine: DecisionEngine,
        risk_controller: RiskController,
        execution_engine,  # ExecutionEngine or PaperExecutionEngine
        store: StateStore,
        leader_event: asyncio.Event,
        alerter: DiscordAlerter | None = None,
        controller: BotController | None = None,
        fill_copier=None,
    ):
        self.config = config
        self.pair_config = pair_config
        self.poller = poller
        self.metadata = metadata
        self.decision = decision_engine
        self.risk = risk_controller
        self.execution = execution_engine
        self.store = store
        self.leader_event = leader_event
        self.alerter = alerter
        self.controller = controller
        self.fill_copier = fill_copier
        self._running = False

    async def start(self) -> None:
        """Start the reconciliation loop. Runs until stopped."""
        self._running = True
        pair_name = self.pair_config.name

        logger.info(
            "Reconciliation loop started",
            pair=pair_name,
            interval_s=self.config.polling.reconciliation_interval_s,
        )

        while self._running:
            try:
                # Wait for either:
                # 1. A leader event (WebSocket fill detected)
                # 2. The reconciliation interval timer
                try:
                    await asyncio.wait_for(
                        self.leader_event.wait(),
                        timeout=self.config.polling.reconciliation_interval_s,
                    )
                    # Leader event triggered — run with force=True
                    self.leader_event.clear()
                    triggered_by = "leader_event"
                    force = True
                except asyncio.TimeoutError:
                    # Timer expired — periodic reconciliation
                    triggered_by = "timer"
                    force = False

                await self._run_cycle(triggered_by, force)

                # Update controller state
                if self.controller:
                    self.controller.update_recon_time(self.pair_config.name)

            except Exception as e:
                logger.error(
                    "Reconciliation cycle error",
                    pair=pair_name,
                    error=str(e),
                    exc_info=True,
                )
                await asyncio.sleep(5)  # Brief pause before retry

    async def stop(self) -> None:
        """Stop the reconciliation loop."""
        self._running = False

    async def _run_cycle(self, triggered_by: str, force: bool) -> None:
        """Execute a single reconciliation cycle."""
        pair_name = self.pair_config.name

        # Check if paused via controller
        if self.controller and self.controller.is_paused(pair_name):
            logger.debug("Reconciliation skipped (paused)", pair=pair_name)
            return

        # 1. Refresh metadata if stale
        await self.metadata.ensure_fresh()

        # 2. Fetch fresh state
        leader_state = await self.poller.fetch_clearinghouse_state(
            self.pair_config.leader_address
        )

        # In paper mode, use the paper trader's virtual positions
        # (NOT the real API, which would always show 0 positions)
        from copybot.engine.paper_trader import PaperExecutionEngine
        if self.config.is_paper and isinstance(self.execution, PaperExecutionEngine):
            follower_state = self.execution.get_account_state()
        elif self.pair_config.follower_address:
            follower_state = await self.poller.fetch_clearinghouse_state(
                self.pair_config.follower_address
            )
        else:
            from copybot.state.models import AccountState
            follower_state = AccountState(
                address="paper_wallet",
                account_value=self.config.scaling.paper_equity,
            )

        # In paper mode, ensure follower equity is never $0
        if self.config.is_paper and follower_state.account_value <= 0:
            follower_state.account_value = self.config.scaling.paper_equity

        # 3. Update state store
        self.store.set_leader_state(pair_name, leader_state)
        self.store.set_follower_state(pair_name, follower_state)

        # 3b. Update fill copier equities (so real-time fills use fresh values)
        if self.fill_copier:
            self.fill_copier.update_equities(
                leader_state.account_value, follower_state.account_value
            )

        # 4. Get session PnL for risk checks
        session_pnl = self.store.get_session_pnl(pair_name)
        starting_equity = self.store.get_starting_equity(pair_name)

        # 5. Check kill switch BEFORE computing intents
        if self.risk.kill_switch.check(session_pnl, starting_equity):
            logger.critical(
                "Kill switch active — closing all positions",
                pair=pair_name,
                session_pnl=str(session_pnl),
            )
            mid_prices = await self.poller.fetch_all_mids()
            await self.execution.close_all_positions(
                follower_state.positions, mid_prices
            )
            # Alert
            if self.risk.alerter:
                await self.risk.alerter.alert_kill_switch(
                    self.risk.kill_switch.reason or "Unknown",
                    str(session_pnl),
                    str(follower_state.account_value),
                )
            self._running = False
            return

        # 6. Compute order intents
        intents = self.decision.compute_intents(leader_state, follower_state, force=force)

        if not intents:
            logger.debug(
                "Reconciliation: no action needed",
                pair=pair_name,
                triggered_by=triggered_by,
                leader_positions=len(leader_state.positions),
                follower_positions=len(follower_state.positions),
            )
            # Persist snapshot periodically
            await self.store.persist_snapshot(pair_name)
            return

        # 7. Fetch mid prices for execution
        mid_prices = await self.poller.fetch_all_mids()

        # 8. Execute each intent through risk controller
        executed = 0
        for intent in intents:
            decision = await self.risk.check(
                intent, follower_state, session_pnl, starting_equity, mid_prices
            )

            if not decision.approved:
                logger.warning(
                    "Order rejected by risk controller",
                    pair=pair_name,
                    coin=intent.coin,
                    reason=decision.reason,
                )
                continue

            # Use modified intent if risk controller capped it
            final_intent = decision.modified_intent or intent

            # Execute
            result = await self.execution.execute(final_intent, mid_prices)

            if result.status == OrderStatus.FILLED:
                self.risk.record_success()
                executed += 1
                logger.info(
                    "Order filled",
                    pair=pair_name,
                    coin=result.intent.coin,
                    side="buy" if result.intent.is_buy else "sell",
                    filled_size=str(result.filled_size),
                    filled_price=str(result.filled_price),
                )
                # Discord notification
                if self.alerter:
                    side = "BUY" if result.intent.is_buy else "SELL"
                    prefix = "📝 PAPER" if self.config.is_paper else "💰 LIVE"
                    await self.alerter.send(
                        title=f"{prefix} | {side} {result.intent.coin}",
                        message=(
                            f"**Size:** {result.filled_size}\n"
                            f"**Price:** ${result.filled_price}\n"
                            f"**Target Position:** {result.intent.target_size}"
                        ),
                        color=0x00FF00 if result.intent.is_buy else 0xFF6600,
                    )
            elif result.status == OrderStatus.PARTIAL:
                self.risk.record_success()  # Partial is not a failure
                executed += 1
                logger.warning(
                    "Order partially filled",
                    pair=pair_name,
                    coin=result.intent.coin,
                    filled_size=str(result.filled_size),
                )
            else:
                self.risk.record_failure()
                logger.error(
                    "Order failed",
                    pair=pair_name,
                    coin=result.intent.coin,
                    error=result.error,
                )

            # Log to database
            await self.store.log_order(pair_name, result)

        logger.info(
            "Reconciliation cycle complete",
            pair=pair_name,
            triggered_by=triggered_by,
            intents=len(intents),
            executed=executed,
            session_pnl=str(session_pnl),
        )

        # 9. Persist state snapshot
        await self.store.persist_snapshot(pair_name)
