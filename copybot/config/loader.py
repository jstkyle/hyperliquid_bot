"""Configuration loader — merges YAML + environment variable overrides."""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from decimal import Decimal
from pathlib import Path
from typing import Any

import yaml


@dataclass
class PairConfig:
    """Configuration for a single leader-follower copy trading pair."""

    name: str
    leader_address: str
    follower_address: str
    agent_private_key: str  # Loaded from env at runtime, never persisted


@dataclass
class ScalingConfig:
    multiplier: Decimal = Decimal("1.0")
    min_order_notional: Decimal = Decimal("11.0")
    drift_threshold_pct: Decimal = Decimal("0.02")
    paper_equity: Decimal = Decimal("10000.0")  # Simulated starting equity for paper mode


@dataclass
class KillSwitchConfig:
    loss_usd: Decimal = Decimal("-5000.0")
    loss_pct: Decimal = Decimal("-0.10")


@dataclass
class RiskConfig:
    symbol_whitelist: list[str] | str = "ALL"
    max_position_usd: Decimal = Decimal("50000.0")
    max_total_exposure_usd: Decimal = Decimal("200000.0")
    max_leverage: int = 20
    slippage_tolerance_bps: int = 50
    max_consecutive_failures: int = 5
    max_open_orders: int = 20
    kill_switch: KillSwitchConfig = field(default_factory=KillSwitchConfig)

    def is_whitelisted(self, coin: str) -> bool:
        if self.symbol_whitelist == "ALL":
            return True
        return coin in self.symbol_whitelist


@dataclass
class PollingConfig:
    reconciliation_interval_s: int = 30
    equity_refresh_interval_s: int = 10
    metadata_refresh_interval_s: int = 300


@dataclass
class WebSocketConfig:
    reconnect_delay_s: float = 1.0
    max_reconnect_delay_s: float = 60.0
    heartbeat_interval_s: float = 15.0


@dataclass
class AlertingConfig:
    discord_webhook_url: str = ""


@dataclass
class DiscordBotConfig:
    """Configuration for the Discord command bot."""

    bot_token: str = ""
    command_channel: str = ""
    authorized_user_ids: list[int] = field(default_factory=list)


@dataclass
class BotConfig:
    """Top-level configuration for the copy trading bot."""

    pairs: list[PairConfig] = field(default_factory=list)
    scaling: ScalingConfig = field(default_factory=ScalingConfig)
    risk: RiskConfig = field(default_factory=RiskConfig)
    polling: PollingConfig = field(default_factory=PollingConfig)
    websocket: WebSocketConfig = field(default_factory=WebSocketConfig)
    alerting: AlertingConfig = field(default_factory=AlertingConfig)
    discord: DiscordBotConfig = field(default_factory=DiscordBotConfig)
    mode: str = "paper"
    network: str = "mainnet"
    log_level: str = "INFO"

    @property
    def is_paper(self) -> bool:
        return self.mode == "paper"

    @property
    def is_mainnet(self) -> bool:
        return self.network == "mainnet"

    @property
    def api_url(self) -> str:
        if self.is_mainnet:
            return "https://api.hyperliquid.xyz"
        return "https://api.hyperliquid-testnet.xyz"

    @property
    def ws_url(self) -> str:
        if self.is_mainnet:
            return "wss://api.hyperliquid.xyz/ws"
        return "wss://api.hyperliquid-testnet.xyz/ws"


def load_config(config_path: str | Path | None = None) -> BotConfig:
    """Load configuration from YAML file, with env var overrides for secrets.

    Args:
        config_path: Path to settings.yaml. Defaults to copybot/config/settings.yaml.

    Returns:
        Fully resolved BotConfig with secrets loaded from environment.
    """
    if config_path is None:
        config_path = Path(__file__).parent / "settings.yaml"
    else:
        config_path = Path(config_path)

    with open(config_path) as f:
        raw: dict[str, Any] = yaml.safe_load(f)

    # --- Build pair configs ---
    pairs: list[PairConfig] = []
    for i, pair_raw in enumerate(raw.get("pairs", [])):
        # Resolve follower address from env if not in YAML
        follower_addr = pair_raw.get("follower_address", "")
        if not follower_addr:
            env_key = f"HL_PAIR_{i}_FOLLOWER_ADDRESS"
            follower_addr = os.environ.get(env_key, "")

        # Resolve agent private key from env
        key_env_name = pair_raw.get("agent_private_key_env", f"HL_PAIR_{i}_AGENT_PRIVATE_KEY")
        agent_key = os.environ.get(key_env_name, "")

        pairs.append(
            PairConfig(
                name=pair_raw.get("name", f"pair_{i}"),
                leader_address=pair_raw["leader_address"],
                follower_address=follower_addr,
                agent_private_key=agent_key,
            )
        )

    # --- Scaling ---
    scaling_raw = raw.get("scaling", {})
    scaling = ScalingConfig(
        multiplier=Decimal(str(scaling_raw.get("multiplier", "1.0"))),
        min_order_notional=Decimal(str(scaling_raw.get("min_order_notional", "11.0"))),
        drift_threshold_pct=Decimal(str(scaling_raw.get("drift_threshold_pct", "0.02"))),
        paper_equity=Decimal(str(scaling_raw.get("paper_equity", "10000.0"))),
    )

    # --- Risk ---
    risk_raw = raw.get("risk", {})
    ks_raw = risk_raw.get("kill_switch", {})
    kill_switch = KillSwitchConfig(
        loss_usd=Decimal(str(ks_raw.get("loss_usd", "-5000.0"))),
        loss_pct=Decimal(str(ks_raw.get("loss_pct", "-0.10"))),
    )

    whitelist = risk_raw.get("symbol_whitelist", "ALL")

    risk = RiskConfig(
        symbol_whitelist=whitelist,
        max_position_usd=Decimal(str(risk_raw.get("max_position_usd", "50000.0"))),
        max_total_exposure_usd=Decimal(
            str(risk_raw.get("max_total_exposure_usd", "200000.0"))
        ),
        max_leverage=int(risk_raw.get("max_leverage", 20)),
        slippage_tolerance_bps=int(risk_raw.get("slippage_tolerance_bps", 50)),
        max_consecutive_failures=int(risk_raw.get("max_consecutive_failures", 5)),
        max_open_orders=int(risk_raw.get("max_open_orders", 20)),
        kill_switch=kill_switch,
    )

    # --- Polling ---
    poll_raw = raw.get("polling", {})
    polling = PollingConfig(
        reconciliation_interval_s=int(poll_raw.get("reconciliation_interval_s", 30)),
        equity_refresh_interval_s=int(poll_raw.get("equity_refresh_interval_s", 10)),
        metadata_refresh_interval_s=int(poll_raw.get("metadata_refresh_interval_s", 300)),
    )

    # --- WebSocket ---
    ws_raw = raw.get("websocket", {})
    websocket = WebSocketConfig(
        reconnect_delay_s=float(ws_raw.get("reconnect_delay_s", 1.0)),
        max_reconnect_delay_s=float(ws_raw.get("max_reconnect_delay_s", 60.0)),
        heartbeat_interval_s=float(ws_raw.get("heartbeat_interval_s", 15.0)),
    )

    # --- Alerting ---
    alert_raw = raw.get("alerting", {})
    discord_env = alert_raw.get("discord_webhook_url_env", "DISCORD_WEBHOOK_URL")
    discord_url = os.environ.get(discord_env, "")
    alerting = AlertingConfig(discord_webhook_url=discord_url)

    # --- Discord Bot ---
    discord_raw = raw.get("discord", {})
    bot_token_env = discord_raw.get("bot_token_env", "DISCORD_BOT_TOKEN")
    bot_token = os.environ.get(bot_token_env, "")
    discord_bot = DiscordBotConfig(
        bot_token=bot_token,
        command_channel=discord_raw.get("command_channel", ""),
        authorized_user_ids=[int(uid) for uid in discord_raw.get("authorized_user_ids", [])],
    )

    return BotConfig(
        pairs=pairs,
        scaling=scaling,
        risk=risk,
        polling=polling,
        websocket=websocket,
        alerting=alerting,
        discord=discord_bot,
        mode=raw.get("mode", "paper"),
        network=raw.get("network", "mainnet"),
        log_level=raw.get("log_level", "INFO"),
    )
