"""Discord bot — slash command interface for controlling the copy trading bot."""

from __future__ import annotations

import asyncio
import time
from datetime import datetime, timezone
from decimal import Decimal

import discord
from discord import app_commands

from copybot.controller import BotController
from copybot.utils.logging import get_logger

logger = get_logger(__name__)


class CopyBotDiscord(discord.Client):
    """Discord bot for controlling the Hyperliquid copy trading bot."""

    def __init__(
        self,
        controller: BotController,
        authorized_users: list[int],
        command_channel: str | None = None,
    ):
        intents = discord.Intents.default()
        intents.message_content = True

        super().__init__(intents=intents)

        self.controller = controller
        self.authorized_users = set(authorized_users)
        self.command_channel = command_channel
        self.tree = app_commands.CommandTree(self)

        # Pending kill confirmations: user_id → timestamp
        self._pending_kills: dict[int, float] = {}

        # Register commands
        self._register_commands()

    def _check_auth(self, interaction: discord.Interaction) -> bool:
        """Check if the user is authorized."""
        return interaction.user.id in self.authorized_users

    def _check_channel(self, interaction: discord.Interaction) -> bool:
        """Check if the command is in the correct channel."""
        if not self.command_channel:
            return True
        return interaction.channel.name == self.command_channel

    async def on_ready(self):
        logger.info("Discord bot connected", user=str(self.user), guilds=len(self.guilds))
        # Sync slash commands with Discord
        await self.tree.sync()
        logger.info("Slash commands synced")

    def _register_commands(self):
        """Register all slash commands."""

        # --- STATUS ---
        @self.tree.command(name="status", description="Show bot status, uptime, and connection info")
        async def cmd_status(interaction: discord.Interaction):
            if not self._check_auth(interaction) or not self._check_channel(interaction):
                return await interaction.response.send_message("⛔ Not authorized.", ephemeral=True)

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

            await interaction.response.send_message(embed=embed)

        # --- BALANCE ---
        @self.tree.command(name="balance", description="Show leader and follower equity")
        async def cmd_balance(interaction: discord.Interaction):
            if not self._check_auth(interaction) or not self._check_channel(interaction):
                return await interaction.response.send_message("⛔ Not authorized.", ephemeral=True)

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

            await interaction.response.send_message(embed=embed)

        # --- POSITIONS ---
        @self.tree.command(name="positions", description="Show all open positions side by side")
        async def cmd_positions(interaction: discord.Interaction):
            if not self._check_auth(interaction) or not self._check_channel(interaction):
                return await interaction.response.send_message("⛔ Not authorized.", ephemeral=True)

            embeds = []
            for pair_name in self.controller.pair_names:
                leader = self.controller.get_leader_state(pair_name)
                follower = self.controller.get_follower_state(pair_name)

                all_coins = set()
                if leader:
                    all_coins |= set(leader.positions.keys())
                if follower:
                    all_coins |= set(follower.positions.keys())

                if not all_coins:
                    embeds.append(discord.Embed(
                        title=f"📊 {pair_name}",
                        description="No open positions",
                        color=0x95A5A6,
                    ))
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

                embeds.append(embed)

            await interaction.response.send_message(embeds=embeds[:10])  # Discord max 10 embeds

        # --- PNL ---
        @self.tree.command(name="pnl", description="Show session profit/loss")
        async def cmd_pnl(interaction: discord.Interaction):
            if not self._check_auth(interaction) or not self._check_channel(interaction):
                return await interaction.response.send_message("⛔ Not authorized.", ephemeral=True)

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
                pnl_sign = "+" if pnl >= 0 else ""

                embed.add_field(
                    name=f"{pnl_emoji} {pair_name}",
                    value=(
                        f"**PnL:** {pnl_sign}${pnl:,.2f} ({pnl_sign}{pnl_pct:.2f}%)\n"
                        f"**Starting Equity:** ${start_eq:,.2f}\n"
                        f"**Current Equity:** ${current_eq:,.2f}"
                    ),
                    inline=False,
                )

            await interaction.response.send_message(embed=embed)

        # --- TRADES ---
        @self.tree.command(name="trades", description="Show recent trade history")
        @app_commands.describe(count="Number of trades to show (max 25)")
        async def cmd_trades(interaction: discord.Interaction, count: int = 10):
            if not self._check_auth(interaction) or not self._check_channel(interaction):
                return await interaction.response.send_message("⛔ Not authorized.", ephemeral=True)

            count = min(count, 25)
            trades = await self.controller.get_recent_trades(limit=count)

            if not trades:
                return await interaction.response.send_message("📊 No trades recorded yet.")

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

            await interaction.response.send_message(embed=embed)

        # --- HISTORY ---
        @self.tree.command(name="history", description="Show copy history: leader vs follower side-by-side")
        @app_commands.describe(count="Number of entries to show (max 15)")
        async def cmd_history(interaction: discord.Interaction, count: int = 10):
            if not self._check_auth(interaction) or not self._check_channel(interaction):
                return await interaction.response.send_message("⛔ Not authorized.", ephemeral=True)

            count = min(count, 15)
            history = await self.controller.get_copy_history(limit=count)

            if not history:
                return await interaction.response.send_message("📊 No copy history recorded yet.")

            embeds = []
            for entry in history:
                # Format timestamps to human-readable with milliseconds
                leader_dt = datetime.fromtimestamp(entry["leader_timestamp"], tz=timezone.utc)
                follower_dt = datetime.fromtimestamp(entry["follower_timestamp"], tz=timezone.utc)
                leader_ts_str = leader_dt.strftime("%m/%d %H:%M:%S.") + f"{leader_dt.microsecond // 1000:03d}"
                follower_ts_str = follower_dt.strftime("%m/%d %H:%M:%S.") + f"{follower_dt.microsecond // 1000:03d}"

                latency = entry["latency_ms"]
                latency_str = f"{latency:,.0f}ms" if latency is not None else "N/A"
                if latency is not None and latency < 1000:
                    latency_color = "🟢"  # fast
                elif latency is not None and latency < 5000:
                    latency_color = "🟡"  # moderate
                else:
                    latency_color = "🔴"  # slow

                source_icon = "⚡" if entry["source"] == "fill" else "🔄"
                status_icon = "✅" if entry["follower_status"] == "filled" else "❌"
                side_emoji = "🟢" if entry["leader_side"] == "buy" else "🔴"
                l_side = entry["leader_side"].upper()
                f_side = entry["follower_side"].upper()

                embed_entry = discord.Embed(
                    title=f"{side_emoji} {entry['coin']} — {source_icon} {entry['source'].upper()}",
                    color=0x3498DB if entry["follower_status"] == "filled" else 0xE74C3C,
                )
                embed_entry.add_field(
                    name="👤 Leader",
                    value=(
                        f"**{l_side}** {entry['leader_size']}\n"
                        f"@ ${entry['leader_price']}\n"
                        f"🕐 `{leader_ts_str}`"
                    ),
                    inline=True,
                )
                embed_entry.add_field(
                    name=f"{status_icon} Follower",
                    value=(
                        f"**{f_side}** {entry['follower_size']}\n"
                        f"@ ${entry['follower_price']}\n"
                        f"🕐 `{follower_ts_str}`"
                    ),
                    inline=True,
                )
                embed_entry.add_field(
                    name=f"{latency_color} Latency",
                    value=latency_str,
                    inline=True,
                )

                if entry.get("error"):
                    embed_entry.set_footer(text=f"❌ {entry['error'][:80]}")

                embeds.append(embed_entry)

            # Discord allows max 10 embeds per message
            for i in range(0, len(embeds), 10):
                batch = embeds[i:i + 10]
                if i == 0:
                    await interaction.response.send_message(embeds=batch)
                else:
                    await interaction.followup.send(embeds=batch)

        # --- PAUSE ---
        @self.tree.command(name="pause", description="Pause all trading (keeps monitoring)")
        async def cmd_pause(interaction: discord.Interaction):
            if not self._check_auth(interaction) or not self._check_channel(interaction):
                return await interaction.response.send_message("⛔ Not authorized.", ephemeral=True)

            result = self.controller.pause()
            await interaction.response.send_message(f"⏸️ {result}")

        # --- RESUME ---
        @self.tree.command(name="resume", description="Resume trading after pause")
        async def cmd_resume(interaction: discord.Interaction):
            if not self._check_auth(interaction) or not self._check_channel(interaction):
                return await interaction.response.send_message("⛔ Not authorized.", ephemeral=True)

            result = self.controller.resume()
            await interaction.response.send_message(f"▶️ {result}")

        # --- KILL ---
        @self.tree.command(name="kill", description="⚠️ Emergency: close ALL positions immediately")
        async def cmd_kill(interaction: discord.Interaction):
            if not self._check_auth(interaction) or not self._check_channel(interaction):
                return await interaction.response.send_message("⛔ Not authorized.", ephemeral=True)

            # Store pending confirmation
            self._pending_kills[interaction.user.id] = time.time()

            await interaction.response.send_message(
                "⚠️ **Are you sure?** This will close ALL positions.\n"
                "Use `/confirm_kill` within 30 seconds to confirm."
            )

        # --- CONFIRM KILL ---
        @self.tree.command(name="confirm_kill", description="Confirm kill switch activation")
        async def cmd_confirm_kill(interaction: discord.Interaction):
            if not self._check_auth(interaction) or not self._check_channel(interaction):
                return await interaction.response.send_message("⛔ Not authorized.", ephemeral=True)

            pending_time = self._pending_kills.pop(interaction.user.id, None)
            if pending_time is None or (time.time() - pending_time) > 30:
                return await interaction.response.send_message("⏰ No pending kill or timed out. Use `/kill` first.")

            await interaction.response.defer()
            result = await self.controller.kill()
            await interaction.followup.send(result)

        # --- RESET ---
        @self.tree.command(name="reset", description="Reset kill switch after manual review")
        async def cmd_reset(interaction: discord.Interaction):
            if not self._check_auth(interaction) or not self._check_channel(interaction):
                return await interaction.response.send_message("⛔ Not authorized.", ephemeral=True)

            result = self.controller.reset_kill()
            await interaction.response.send_message(result)

        # --- CONFIG ---
        @self.tree.command(name="config", description="Show current bot configuration")
        async def cmd_config(interaction: discord.Interaction):
            if not self._check_auth(interaction) or not self._check_channel(interaction):
                return await interaction.response.send_message("⛔ Not authorized.", ephemeral=True)

            summary = self.controller.get_config_summary()

            embed = discord.Embed(
                title="⚙️ Configuration",
                color=0x95A5A6,
                timestamp=datetime.now(timezone.utc),
            )

            for key, value in summary.items():
                embed.add_field(name=key, value=value, inline=True)

            await interaction.response.send_message(embed=embed)

        # --- SET ---
        @self.tree.command(name="set", description="Change a config setting")
        @app_commands.describe(
            setting="Setting to change",
            value="New value"
        )
        @app_commands.choices(setting=[
            app_commands.Choice(name="multiplier", value="multiplier"),
            app_commands.Choice(name="max_position", value="max_position"),
        ])
        async def cmd_set(interaction: discord.Interaction, setting: str, value: float):
            if not self._check_auth(interaction) or not self._check_channel(interaction):
                return await interaction.response.send_message("⛔ Not authorized.", ephemeral=True)

            if setting == "multiplier":
                result = self.controller.set_multiplier(value)
            elif setting == "max_position":
                result = self.controller.set_max_position(value)
            else:
                return await interaction.response.send_message(f"❌ Unknown setting: `{setting}`")

            await interaction.response.send_message(f"✅ {result}")

        # --- HELP ---
        @self.tree.command(name="help", description="Show all available commands")
        async def cmd_help(interaction: discord.Interaction):
            embed = discord.Embed(
                title="🤖 HL CopyBot — Commands",
                color=0x3498DB,
            )

            commands_list = {
                "📊 Monitoring": (
                    "`/status` — Bot status, uptime, WS connection\n"
                    "`/balance` — Leader & follower equity\n"
                    "`/positions` — Open positions side by side\n"
                    "`/pnl` — Session profit/loss\n"
                    "`/trades` — Recent trade history\n"
                    "`/history` — Copy history: leader vs follower timestamps"
                ),
                "🎮 Control": (
                    "`/pause` — Pause all trading\n"
                    "`/resume` — Resume trading\n"
                    "`/kill` — Emergency: close all positions\n"
                    "`/reset` — Reset kill switch"
                ),
                "⚙️ Configuration": (
                    "`/config` — View current settings\n"
                    "`/set` — Change multiplier or position cap"
                ),
            }

            for section, cmds in commands_list.items():
                embed.add_field(name=section, value=cmds, inline=False)

            embed.set_footer(text="Only authorized users can run control commands.")
            await interaction.response.send_message(embed=embed)


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
