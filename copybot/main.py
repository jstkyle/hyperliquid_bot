"""Main entry point — wires all components and runs the async event loop."""

from __future__ import annotations

import argparse
import asyncio
import signal
import sys
from pathlib import Path

from copybot.config.loader import BotConfig, PairConfig, load_config
from copybot.controller import BotController
from copybot.discord_bot import start_discord_bot
from copybot.engine.decision import DecisionEngine
from copybot.engine.execution import ExecutionEngine
from copybot.engine.paper_trader import PaperExecutionEngine
from copybot.engine.reconciliation import ReconciliationLoop
from copybot.engine.risk import RiskController
from copybot.ingestion.rest_poller import RestPoller
from copybot.ingestion.ws_listener import WebSocketListener
from copybot.state.metadata import MetadataCache
from copybot.state.store import StateStore
from copybot.utils.alerting import DiscordAlerter
from copybot.utils.logging import get_logger, setup_logging


async def run_pair(
    config: BotConfig,
    pair_config: PairConfig,
    metadata: MetadataCache,
    store: StateStore,
    alerter: DiscordAlerter,
    controller: BotController | None = None,
) -> None:
    """Run all components for a single leader-follower pair.

    Creates and manages:
    - REST poller (shared)
    - WebSocket listener for the leader
    - Decision engine
    - Risk controller
    - Execution engine (live or paper)
    - Reconciliation loop
    """
    logger = get_logger(f"pair.{pair_config.name}")
    pair_name = pair_config.name

    # --- Create components ---
    poller = RestPoller(config.api_url)

    # Leader event signal (WS → reconciliation)
    leader_event = asyncio.Event()

    # WebSocket listener
    ws_listener = WebSocketListener(
        ws_url=config.ws_url,
        leader_address=pair_config.leader_address,
        pair_name=pair_name,
        on_leader_event=leader_event,
        reconnect_delay=config.websocket.reconnect_delay_s,
        max_reconnect_delay=config.websocket.max_reconnect_delay_s,
        heartbeat_interval=config.websocket.heartbeat_interval_s,
    )

    # Decision engine
    decision = DecisionEngine(config, metadata)

    # Risk controller
    risk = RiskController(config, alerter)

    # Execution engine (paper or live)
    if config.is_paper:
        execution = PaperExecutionEngine(pair_config.follower_address, pair_name)
    else:
        execution = ExecutionEngine(config, pair_config, metadata)

    await execution.initialize()

    # Reconciliation loop
    recon = ReconciliationLoop(
        config=config,
        pair_config=pair_config,
        poller=poller,
        metadata=metadata,
        decision_engine=decision,
        risk_controller=risk,
        execution_engine=execution,
        store=store,
        leader_event=leader_event,
        alerter=alerter,
        controller=controller,
    )

    # Register with controller for Discord commands
    if controller:
        controller.register_pair(
            pair_name=pair_name,
            risk_controller=risk,
            execution_engine=execution,
            recon_loop=recon,
            ws_listener=ws_listener,
            rest_poller=poller,
        )

    # --- Fetch initial state ---
    logger.info(
        "Fetching initial state",
        pair=pair_name,
        leader=pair_config.leader_address[:10] + "...",
        follower=pair_config.follower_address[:10] + "..." if pair_config.follower_address else "not set",
    )

    try:
        leader_state = await poller.fetch_clearinghouse_state(pair_config.leader_address)
        store.set_leader_state(pair_name, leader_state)
        logger.info(
            "Leader state loaded",
            pair=pair_name,
            positions=len(leader_state.positions),
            equity=str(leader_state.account_value),
        )

        if pair_config.follower_address:
            follower_state = await poller.fetch_clearinghouse_state(pair_config.follower_address)
            store.set_follower_state(pair_name, follower_state)
            logger.info(
                "Follower state loaded",
                pair=pair_name,
                positions=len(follower_state.positions),
                equity=str(follower_state.account_value),
            )

            if config.is_paper and isinstance(execution, PaperExecutionEngine):
                # Use real equity if available, otherwise simulated paper equity
                equity = follower_state.account_value
                if equity <= 0:
                    equity = config.scaling.paper_equity
                    logger.info(
                        "Using simulated paper equity (follower has $0)",
                        pair=pair_name,
                        paper_equity=str(equity),
                    )
                execution.set_initial_equity(equity)
        elif config.is_paper and isinstance(execution, PaperExecutionEngine):
            # No follower address set — use simulated equity
            from copybot.state.models import AccountState
            from decimal import Decimal
            paper_equity = config.scaling.paper_equity
            follower_state = AccountState(
                address="paper_wallet",
                account_value=paper_equity,
                timestamp=0.0,
            )
            store.set_follower_state(pair_name, follower_state)
            execution.set_initial_equity(paper_equity)
            logger.info(
                "Paper mode: no follower address, using simulated equity",
                pair=pair_name,
                paper_equity=str(paper_equity),
            )

    except Exception as e:
        logger.error("Failed to fetch initial state", pair=pair_name, error=str(e))
        raise

    # --- Run tasks concurrently ---
    logger.info("Starting pair tasks", pair=pair_name)

    tasks = [
        asyncio.create_task(ws_listener.start(), name=f"{pair_name}_ws"),
        asyncio.create_task(recon.start(), name=f"{pair_name}_recon"),
    ]

    try:
        # Wait for any task to complete (they should run forever)
        done, pending = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)

        for task in done:
            if task.exception():
                logger.error(
                    "Pair task failed",
                    pair=pair_name,
                    task=task.get_name(),
                    error=str(task.exception()),
                )
    finally:
        # Cleanup
        await ws_listener.stop()
        await recon.stop()
        await poller.close()

        for task in tasks:
            if not task.done():
                task.cancel()
                try:
                    await task
                except asyncio.CancelledError:
                    pass


async def async_main(config: BotConfig) -> None:
    """Main async entry point — initializes shared resources and runs all pairs."""
    logger = get_logger("main")

    logger.info(
        "Starting Hyperliquid Copy Trading Bot",
        mode=config.mode,
        network=config.network,
        pairs=len(config.pairs),
    )

    # --- Shared resources ---
    metadata = MetadataCache(
        config.api_url,
        refresh_interval_s=config.polling.metadata_refresh_interval_s,
    )
    await metadata.refresh()
    logger.info("Metadata loaded", assets=len(metadata.all_coins))

    store = StateStore()
    await store.initialize()

    alerter = DiscordAlerter(
        webhook_url=config.alerting.discord_webhook_url,
        bot_name=f"HL CopyBot [{config.mode}]",
    )
    await alerter.alert_startup(config.mode, len(config.pairs))

    # --- Bot Controller (for Discord commands) ---
    controller = BotController()
    controller.set_config(config)
    controller.set_store(store)

    # --- Validate pair configs ---
    valid_pairs = []
    for pair in config.pairs:
        if not pair.leader_address:
            logger.error("Pair missing leader address", pair=pair.name)
            continue
        if not config.is_paper and not pair.agent_private_key:
            logger.error("Pair missing agent private key (required for live mode)", pair=pair.name)
            continue
        if not config.is_paper and not pair.follower_address:
            logger.error("Pair missing follower address (required for live mode)", pair=pair.name)
            continue
        valid_pairs.append(pair)

    if not valid_pairs:
        logger.error("No valid pairs configured. Exiting.")
        return

    # --- Run all pairs concurrently ---
    pair_tasks = [
        asyncio.create_task(
            run_pair(config, pair, metadata, store, alerter, controller),
            name=f"pair_{pair.name}",
        )
        for pair in valid_pairs
    ]

    # --- Start Discord bot (if token configured) ---
    discord_tasks = []
    if config.discord.bot_token:
        discord_task = asyncio.create_task(
            start_discord_bot(
                token=config.discord.bot_token,
                controller=controller,
                authorized_users=config.discord.authorized_user_ids,
                command_channel=config.discord.command_channel or None,
            ),
            name="discord_bot",
        )
        discord_tasks.append(discord_task)
        logger.info("Discord bot starting", authorized_users=config.discord.authorized_user_ids)
    else:
        logger.warning("Discord bot not started — DISCORD_BOT_TOKEN not set")

    # Graceful shutdown handler
    shutdown_event = asyncio.Event()

    def signal_handler():
        logger.info("Shutdown signal received")
        shutdown_event.set()

    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, signal_handler)

    # Wait for shutdown or task failure
    shutdown_task = asyncio.create_task(shutdown_event.wait())
    all_tasks = pair_tasks + discord_tasks + [shutdown_task]
    done, pending = await asyncio.wait(all_tasks, return_when=asyncio.FIRST_COMPLETED)

    # --- Cleanup ---
    logger.info("Shutting down...")

    for task in pending:
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass

    await alerter.close()
    await store.close()
    logger.info("Shutdown complete")


def main():
    """CLI entry point."""
    parser = argparse.ArgumentParser(description="Hyperliquid Copy Trading Bot")
    parser.add_argument(
        "--config",
        type=str,
        default=None,
        help="Path to settings.yaml (default: copybot/config/settings.yaml)",
    )
    parser.add_argument(
        "--mode",
        type=str,
        choices=["paper", "live"],
        default=None,
        help="Override operating mode",
    )
    args = parser.parse_args()

    # Load .env file (secrets, no manual export needed)
    from dotenv import load_dotenv
    load_dotenv()

    # Load config
    config = load_config(args.config)
    if args.mode:
        config.mode = args.mode

    # Setup logging
    setup_logging(config.log_level)

    # Run
    asyncio.run(async_main(config))


if __name__ == "__main__":
    main()
