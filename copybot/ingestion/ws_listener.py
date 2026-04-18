"""WebSocket listener — subscribes to leader userEvents for real-time fill detection."""

from __future__ import annotations

import asyncio
import hashlib
import json
import random
import time

import websockets
from websockets.exceptions import ConnectionClosed

from copybot.utils.logging import get_logger

logger = get_logger(__name__)


class WebSocketListener:
    """Maintains a persistent WebSocket connection to Hyperliquid.

    Subscribes to userEvents for a leader address and notifies callbacks
    when fill events are detected. Handles reconnection with exponential
    backoff and event deduplication.
    """

    def __init__(
        self,
        ws_url: str,
        leader_address: str,
        pair_name: str,
        on_leader_event: asyncio.Event,
        reconnect_delay: float = 1.0,
        max_reconnect_delay: float = 60.0,
        heartbeat_interval: float = 15.0,
    ):
        self.ws_url = ws_url
        self.leader_address = leader_address
        self.pair_name = pair_name
        self.on_leader_event = on_leader_event  # Signaled when leader activity detected

        self._reconnect_delay = reconnect_delay
        self._max_reconnect_delay = max_reconnect_delay
        self._heartbeat_interval = heartbeat_interval

        self._ws: websockets.WebSocketClientProtocol | None = None
        self._connected = False
        self._running = False
        self._seen_events: set[str] = set()  # Hashes for dedup
        self._max_seen: int = 5000  # Max dedup cache size

    @property
    def is_connected(self) -> bool:
        return self._connected

    async def start(self) -> None:
        """Start the WebSocket listener (runs indefinitely with reconnection)."""
        self._running = True
        delay = self._reconnect_delay

        while self._running:
            try:
                await self._connect_and_listen()
            except ConnectionClosed as e:
                logger.warning(
                    "WebSocket connection closed",
                    pair=self.pair_name,
                    code=e.code,
                    reason=str(e.reason)[:100],
                )
            except Exception as e:
                logger.error(
                    "WebSocket error",
                    pair=self.pair_name,
                    error=str(e),
                )

            self._connected = False

            if not self._running:
                break

            # Exponential backoff with jitter
            jitter = random.uniform(0, delay * 0.3)
            wait = min(delay + jitter, self._max_reconnect_delay)
            logger.info(
                "WebSocket reconnecting",
                pair=self.pair_name,
                delay_s=round(wait, 2),
            )
            await asyncio.sleep(wait)
            delay = min(delay * 2, self._max_reconnect_delay)

            # Signal leader event so reconciliation runs after reconnect
            self.on_leader_event.set()

    async def stop(self) -> None:
        """Stop the listener gracefully."""
        self._running = False
        if self._ws:
            await self._ws.close()

    async def _connect_and_listen(self) -> None:
        """Establish connection, subscribe, and process messages."""
        logger.info("WebSocket connecting", pair=self.pair_name, url=self.ws_url)

        async with websockets.connect(
            self.ws_url,
            ping_interval=self._heartbeat_interval,
            ping_timeout=self._heartbeat_interval * 2,
            close_timeout=5,
        ) as ws:
            self._ws = ws
            self._connected = True

            # Subscribe to leader's userEvents
            subscribe_msg = {
                "method": "subscribe",
                "subscription": {
                    "type": "userEvents",
                    "user": self.leader_address,
                },
            }
            await ws.send(json.dumps(subscribe_msg))
            logger.info(
                "WebSocket subscribed to userEvents",
                pair=self.pair_name,
                leader=self.leader_address[:10] + "...",
            )

            # Reset reconnect delay on successful connection
            delay = self._reconnect_delay

            # Listen for messages
            async for raw_msg in ws:
                if not self._running:
                    break
                await self._handle_message(raw_msg)

    async def _handle_message(self, raw_msg: str | bytes) -> None:
        """Parse and handle a WebSocket message."""
        try:
            data = json.loads(raw_msg)
        except json.JSONDecodeError:
            logger.warning("Invalid JSON from WebSocket", raw=str(raw_msg)[:200])
            return

        channel = data.get("channel", "")

        # Subscription acknowledgment
        if channel == "subscriptionResponse":
            logger.debug("Subscription confirmed", data=data.get("data", {}))
            return

        # Pong (heartbeat response)
        if channel == "pong":
            return

        # User events (fills, orders, liquidations)
        if channel == "user":
            await self._handle_user_event(data)
            return

        logger.debug("Unknown WS channel", channel=channel)

    async def _handle_user_event(self, data: dict) -> None:
        """Process a userEvents message from the leader."""
        event_data = data.get("data", {})

        # Deduplication
        event_hash = hashlib.md5(
            json.dumps(event_data, sort_keys=True).encode()
        ).hexdigest()

        if event_hash in self._seen_events:
            logger.debug("Duplicate event skipped", hash=event_hash[:8])
            return

        self._seen_events.add(event_hash)
        if len(self._seen_events) > self._max_seen:
            # Evict oldest (approximate — set doesn't preserve order, but acceptable)
            self._seen_events = set(list(self._seen_events)[self._max_seen // 2 :])

        # Check for fills
        fills = event_data.get("fills", [])
        if fills:
            for fill in fills:
                logger.info(
                    "Leader fill detected",
                    pair=self.pair_name,
                    coin=fill.get("coin", "?"),
                    side=fill.get("side", "?"),
                    sz=fill.get("sz", "?"),
                    px=fill.get("px", "?"),
                )

            # Signal the decision engine to wake up
            self.on_leader_event.set()
            return

        # Check for liquidations
        liquidations = event_data.get("liquidation", None)
        if liquidations:
            logger.warning(
                "Leader liquidation detected",
                pair=self.pair_name,
                data=liquidations,
            )
            self.on_leader_event.set()
            return

        # Other events (order updates, etc.) — log but don't trigger action
        logger.debug(
            "Leader user event (non-fill)",
            pair=self.pair_name,
            keys=list(event_data.keys()),
        )
