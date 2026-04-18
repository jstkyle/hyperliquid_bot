"""Discord webhook alerting for critical events."""

from __future__ import annotations

import asyncio
from datetime import datetime, timezone

import aiohttp

from copybot.utils.logging import get_logger

logger = get_logger(__name__)


class DiscordAlerter:
    """Sends alert messages to a Discord channel via webhook."""

    def __init__(self, webhook_url: str, bot_name: str = "HL CopyBot"):
        self.webhook_url = webhook_url
        self.bot_name = bot_name
        self._session: aiohttp.ClientSession | None = None

    async def _get_session(self) -> aiohttp.ClientSession:
        if self._session is None or self._session.closed:
            self._session = aiohttp.ClientSession()
        return self._session

    async def close(self) -> None:
        if self._session and not self._session.closed:
            await self._session.close()

    async def send(
        self,
        title: str,
        message: str,
        color: int = 0xFF0000,
        fields: dict[str, str] | None = None,
    ) -> None:
        """Send an alert to Discord.

        Args:
            title: Embed title.
            message: Embed description.
            color: Embed color (hex). Red=0xFF0000, Yellow=0xFFAA00, Green=0x00FF00.
            fields: Optional dict of field name → value for the embed.
        """
        if not self.webhook_url:
            logger.warning("Discord webhook not configured, skipping alert", title=title)
            return

        embed = {
            "title": f"🤖 {title}",
            "description": message,
            "color": color,
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "footer": {"text": self.bot_name},
        }

        if fields:
            embed["fields"] = [
                {"name": k, "value": str(v), "inline": True} for k, v in fields.items()
            ]

        payload = {"embeds": [embed]}

        try:
            session = await self._get_session()
            async with session.post(self.webhook_url, json=payload) as resp:
                if resp.status != 204:
                    body = await resp.text()
                    logger.error(
                        "Discord webhook failed",
                        status=resp.status,
                        body=body[:200],
                    )
        except Exception as e:
            logger.error("Discord alert send failed", error=str(e))

    async def alert_kill_switch(self, reason: str, session_pnl: str, equity: str) -> None:
        """Send a critical kill switch alert."""
        await self.send(
            title="🚨 KILL SWITCH ACTIVATED",
            message=f"**Reason:** {reason}\n\nAll positions are being closed. Manual intervention required to re-enable.",
            color=0xFF0000,
            fields={"Session PnL": session_pnl, "Equity": equity},
        )

    async def alert_error(self, error_type: str, details: str) -> None:
        """Send an error alert."""
        await self.send(
            title=f"⚠️ Error: {error_type}",
            message=details,
            color=0xFFAA00,
        )

    async def alert_order(self, coin: str, side: str, size: str, price: str) -> None:
        """Send an order execution notification (optional, can be noisy)."""
        await self.send(
            title=f"📊 Order Executed: {coin}",
            message=f"**{side.upper()}** {size} @ {price}",
            color=0x00FF00,
        )

    async def alert_startup(self, mode: str, pairs: int) -> None:
        """Send a bot startup notification."""
        await self.send(
            title="✅ Bot Started",
            message=f"Mode: **{mode}**\nTracking **{pairs}** leader-follower pair(s)",
            color=0x00FF00,
        )
