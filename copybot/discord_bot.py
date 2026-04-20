"""Discord bot — command interface for controlling the copy trading bot."""

from __future__ import annotations

import asyncio
import time
from datetime import datetime, timezone
from decimal import Decimal

import discord
from discord.ext import commands

from copybot.controller import BotController
from copybot.utils.logging import get_logger

logger = get_logger(__name__)


class CopyBotDiscord(commands.Bot):
    """Discord bot for controlling the Hyperliquid copy trading bot."""

    def __init__(
        self,
        controller: BotController,
        authorized_users: list[int],
        command_channel: str | None = None,
    ):
        intents = discord.Intents.default()
        intents.message_content = True

        super().__init__(command_prefix="!", intents=intents, help_command=None)

        self.controller = controller
        self.authorized_users = set(authorized_users)
        self.command_channel = command_channel

        # Register commands
        self._register_commands()

    def _is_authorized(self, ctx: commands.Context) -> bool:
        """Check if the user is authorized to run commands."""
        return ctx.author.id in self.authorized_users

    def _check_channel(self, ctx: commands.Context) -> bool:
        """Check if the command is in the correct channel."""
        if not self.command_channel:
            return True
        return ctx.channel.name == self.command_channel

    async def on_ready(self):
        logger.info("Discord bot connected", user=str(self.user), guilds=len(self.guilds))

    def _register_commands(self):
        """Register all bot commands."""

        @self.command(name="status")
        async def cmd_status(ctx: commands.Context):
            """Show bot status and uptime."""
            if not self._is_authorized(ctx) or not self._check_channel(ctx):
                return await ctx.send("⛔ Not authorized or wrong channel.")

            mode_emoji = "📝" if self.controller.mode == "paper" else "💰"
            embed = discord.Embed(
                title=f"{mode_emoji} HL CopyBot — Status",
                color=0x00FF00,
                timestamp=datetime.now(timezone.utc),
            )
            embed.add_field(name="⏱ Uptime", value=self.controller.uptime_str, inline=True)
            embed.add_field(name="⚡ Mode", value=self.controller.mode.upper(), inline=True)
            embed.add_field(name="📊 Pairs", value=str(len(self.controller.pair_names)), inline=True)

            for pair_name in self.controller.pair_names:
                status = self.controller.get_pair_status(pair_name)
                if not status:
                    continue

                ws_icon = "🟢" if status.ws_connected else "🔴"
                paused_icon = "⏸️ PAUSED" if status.paused else "▶️ Active"

                last_recon = "Never"
                if status.last_reconciliation > 0:
                    ago = int(time.time() - status.last_reconciliation)
                    last_recon = f"{ago}s ago"

                embed.add_field(
                    name=f"📊 {pair_name}",
                    value=(
                        f"{ws_icon} WebSocket | {paused_icon}\n"
                        f"🔄 Last Recon: {last_recon}\n"
                        f"📈 Trades: {status.total_trades}"
                    ),
                    inline=False,
                )

            await ctx.send(embed=embed)

        @self.command(name="balance")
        async def cmd_balance(ctx: commands.Context):
            """Show leader and follower equity."""
            if not self._is_authorized(ctx) or not self._check_channel(ctx):
                return await ctx.send("⛔ Not authorized or wrong channel.")

            embed = discord.Embed(
                title="💰 Balance",
                color=0x3498DB,
                timestamp=datetime.now(timezone.utc),
            )

            for pair_name in self.controller.pair_names:
                leader = self.controller.get_leader_state(pair_name)
                follower = self.controller.get_follower_state(pair_name)

                leader_eq = f"${leader.account_value:,.2f}" if leader else "N/A"
                follower_eq = f"${follower.account_value:,.2f}" if follower else "N/A"
                leader_pos = len(leader.positions) if leader else 0
                follower_pos = len(follower.positions) if follower else 0

                embed.add_field(
                    name=f"📊 {pair_name}",
                    value=(
                        f"**Leader:** {leader_eq} ({leader_pos} positions)\n"
                        f"**Follower:** {follower_eq} ({follower_pos} positions)"
                    ),
                    inline=False,
                )

            await ctx.send(embed=embed)

        @self.command(name="positions")
        async def cmd_positions(ctx: commands.Context):
            """Show all open positions side by side."""
            if not self._is_authorized(ctx) or not self._check_channel(ctx):
                return await ctx.send("⛔ Not authorized or wrong channel.")

            for pair_name in self.controller.pair_names:
                leader = self.controller.get_leader_state(pair_name)
                follower = self.controller.get_follower_state(pair_name)

                if not leader and not follower:
                    await ctx.send(f"📊 **{pair_name}**: No state available")
                    continue

                all_coins = set()
                if leader:
                    all_coins |= set(leader.positions.keys())
                if follower:
                    all_coins |= set(follower.positions.keys())

                if not all_coins:
                    await ctx.send(f"📊 **{pair_name}**: No open positions")
                    continue

                embed = discord.Embed(
                    title=f"📈 Positions — {pair_name}",
                    color=0x9B59B6,
                    timestamp=datetime.now(timezone.utc),
                )

                for coin in sorted(all_coins):
                    l_pos = leader.positions.get(coin) if leader else None
                    f_pos = follower.positions.get(coin) if follower else None

                    l_szi = f"{l_pos.szi:+}" if l_pos else "—"
                    f_szi = f"{f_pos.szi:+}" if f_pos else "—"
                    l_side = "🟢 LONG" if l_pos and l_pos.is_long else "🔴 SHORT" if l_pos else ""

                    embed.add_field(
                        name=f"{l_side} {coin}",
                        value=f"Leader: `{l_szi}`\nFollower: `{f_szi}`",
                        inline=True,
                    )

                await ctx.send(embed=embed)

        @self.command(name="pnl")
        async def cmd_pnl(ctx: commands.Context):
            """Show session PnL."""
            if not self._is_authorized(ctx) or not self._check_channel(ctx):
                return await ctx.send("⛔ Not authorized or wrong channel.")

            embed = discord.Embed(
                title="📊 Session PnL",
                color=0x2ECC71,
                timestamp=datetime.now(timezone.utc),
            )

            for pair_name in self.controller.pair_names:
                pnl = self.controller.get_session_pnl(pair_name)
                start_eq = self.controller.get_starting_equity(pair_name)
                follower = self.controller.get_follower_state(pair_name)
                current_eq = follower.account_value if follower else Decimal("0")

                pnl_pct = (pnl / start_eq * 100) if start_eq > 0 else Decimal("0")
                pnl_emoji = "📈" if pnl >= 0 else "📉"
                pnl_color = "+" if pnl >= 0 else ""

                embed.add_field(
                    name=f"{pnl_emoji} {pair_name}",
                    value=(
                        f"**PnL:** {pnl_color}${pnl:,.2f} ({pnl_color}{pnl_pct:.2f}%)\n"
                        f"**Starting Equity:** ${start_eq:,.2f}\n"
                        f"**Current Equity:** ${current_eq:,.2f}"
                    ),
                    inline=False,
                )

            await ctx.send(embed=embed)

        @self.command(name="trades")
        async def cmd_trades(ctx: commands.Context, count: int = 10):
            """Show recent trades. Usage: !trades [count]"""
            if not self._is_authorized(ctx) or not self._check_channel(ctx):
                return await ctx.send("⛔ Not authorized or wrong channel.")

            count = min(count, 25)  # Cap at 25
            trades = await self.controller.get_recent_trades(limit=count)

            if not trades:
                return await ctx.send("📊 No trades recorded yet.")

            embed = discord.Embed(
                title=f"📋 Last {len(trades)} Trades",
                color=0xE67E22,
                timestamp=datetime.now(timezone.utc),
            )

            for t in trades:
                ts = datetime.fromtimestamp(t["time"], tz=timezone.utc).strftime("%m/%d %H:%M")
                side_emoji = "🟢" if t["side"] == "buy" else "🔴"
                status_emoji = "✅" if t["status"] == "filled" else "❌"

                embed.add_field(
                    name=f"{side_emoji} {t['coin']} — {ts}",
                    value=f"`{t['side'].upper()}` {t['size']} @ ${t['price']} {status_emoji}",
                    inline=False,
                )

            await ctx.send(embed=embed)

        @self.command(name="pause")
        async def cmd_pause(ctx: commands.Context):
            """Pause all trading."""
            if not self._is_authorized(ctx) or not self._check_channel(ctx):
                return await ctx.send("⛔ Not authorized or wrong channel.")

            result = self.controller.pause()
            await ctx.send(f"⏸️ {result}")

        @self.command(name="resume")
        async def cmd_resume(ctx: commands.Context):
            """Resume all trading."""
            if not self._is_authorized(ctx) or not self._check_channel(ctx):
                return await ctx.send("⛔ Not authorized or wrong channel.")

            result = self.controller.resume()
            await ctx.send(f"▶️ {result}")

        @self.command(name="kill")
        async def cmd_kill(ctx: commands.Context):
            """Activate kill switch — closes all positions."""
            if not self._is_authorized(ctx) or not self._check_channel(ctx):
                return await ctx.send("⛔ Not authorized or wrong channel.")

            # Confirmation
            await ctx.send("⚠️ **Are you sure?** This will close ALL positions. Type `!confirm_kill` within 30 seconds.")

            def check(m):
                return m.author == ctx.author and m.content == "!confirm_kill"

            try:
                await self.wait_for("message", check=check, timeout=30)
            except asyncio.TimeoutError:
                return await ctx.send("⏰ Kill switch cancelled (timed out).")

            result = await self.controller.kill()
            await ctx.send(result)

        @self.command(name="confirm_kill")
        async def cmd_confirm_kill(ctx: commands.Context):
            """Hidden — handled by kill command."""
            pass

        @self.command(name="reset")
        async def cmd_reset(ctx: commands.Context):
            """Reset kill switch after manual review."""
            if not self._is_authorized(ctx) or not self._check_channel(ctx):
                return await ctx.send("⛔ Not authorized or wrong channel.")

            result = self.controller.reset_kill()
            await ctx.send(result)

        @self.command(name="config")
        async def cmd_config(ctx: commands.Context):
            """Show current configuration."""
            if not self._is_authorized(ctx) or not self._check_channel(ctx):
                return await ctx.send("⛔ Not authorized or wrong channel.")

            summary = self.controller.get_config_summary()

            embed = discord.Embed(
                title="⚙️ Configuration",
                color=0x95A5A6,
                timestamp=datetime.now(timezone.utc),
            )

            for key, value in summary.items():
                embed.add_field(name=key, value=value, inline=True)

            await ctx.send(embed=embed)

        @self.command(name="set")
        async def cmd_set(ctx: commands.Context, setting: str = "", value: str = ""):
            """Change a config setting. Usage: !set multiplier 0.5"""
            if not self._is_authorized(ctx) or not self._check_channel(ctx):
                return await ctx.send("⛔ Not authorized or wrong channel.")

            if not setting or not value:
                return await ctx.send(
                    "Usage: `!set <setting> <value>`\n"
                    "Available: `multiplier`, `max_position`"
                )

            try:
                float_val = float(value)
            except ValueError:
                return await ctx.send(f"❌ Invalid number: {value}")

            if setting == "multiplier":
                result = self.controller.set_multiplier(float_val)
            elif setting == "max_position":
                result = self.controller.set_max_position(float_val)
            else:
                return await ctx.send(f"❌ Unknown setting: `{setting}`")

            await ctx.send(f"✅ {result}")

        @self.command(name="help")
        async def cmd_help(ctx: commands.Context):
            """Show all available commands."""
            if not self._check_channel(ctx):
                return

            embed = discord.Embed(
                title="🤖 HL CopyBot — Commands",
                color=0x3498DB,
            )

            commands_list = {
                "📊 Monitoring": (
                    "`!status` — Bot status, uptime, WS connection\n"
                    "`!balance` — Leader & follower equity\n"
                    "`!positions` — Open positions side by side\n"
                    "`!pnl` — Session profit/loss\n"
                    "`!trades [N]` — Last N trades"
                ),
                "🎮 Control": (
                    "`!pause` — Pause all trading\n"
                    "`!resume` — Resume trading\n"
                    "`!kill` — Emergency: close all positions\n"
                    "`!reset` — Reset kill switch"
                ),
                "⚙️ Configuration": (
                    "`!config` — View current settings\n"
                    "`!set multiplier 0.5` — Change scaling\n"
                    "`!set max_position 25000` — Position cap"
                ),
            }

            for section, cmds in commands_list.items():
                embed.add_field(name=section, value=cmds, inline=False)

            embed.set_footer(text="Only authorized users can run control commands.")
            await ctx.send(embed=embed)


async def start_discord_bot(
    token: str,
    controller: BotController,
    authorized_users: list[int],
    command_channel: str | None = None,
) -> None:
    """Start the Discord bot as an async task."""
    bot = CopyBotDiscord(
        controller=controller,
        authorized_users=authorized_users,
        command_channel=command_channel,
    )

    try:
        await bot.start(token)
    except discord.LoginFailure:
        logger.error("Discord bot login failed — check DISCORD_BOT_TOKEN")
    except Exception as e:
        logger.error("Discord bot error", error=str(e))
    finally:
        if not bot.is_closed():
            await bot.close()
