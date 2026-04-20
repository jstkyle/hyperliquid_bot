"""BotController — shared state bridge between Discord commands and trading components."""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any

from copybot.state.models import AccountState, PositionInfo
from copybot.state.store import StateStore
from copybot.utils.logging import get_logger

logger = get_logger(__name__)


@dataclass
class PairStatus:
    """Runtime status for a single leader-follower pair."""

    name: str
    paused: bool = False
    ws_connected: bool = False
    last_reconciliation: float = 0.0
    total_trades: int = 0
    consecutive_failures: int = 0


class BotController:
    """Central controller that bridges Discord commands to trading components.

    Holds references to all runtime components and provides a clean API
    for querying state and issuing control commands.
    """

    def __init__(self):
        self._start_time: float = time.time()
        self._mode: str = "paper"
        self._network: str = "mainnet"

        # Per-pair runtime state
        self._pair_statuses: dict[str, PairStatus] = {}

        # Component references (set during startup)
        self._store: StateStore | None = None
        self._risk_controllers: dict[str, Any] = {}  # pair_name → RiskController
        self._execution_engines: dict[str, Any] = {}  # pair_name → ExecutionEngine
        self._recon_loops: dict[str, Any] = {}  # pair_name → ReconciliationLoop
        self._ws_listeners: dict[str, Any] = {}  # pair_name → WebSocketListener
        self._rest_pollers: dict[str, Any] = {}  # pair_name → RestPoller
        self._config: Any = None

        # Kill switch active flag
        self._killed: bool = False

    def set_config(self, config: Any) -> None:
        self._config = config
        self._mode = config.mode
        self._network = config.network

    def set_store(self, store: StateStore) -> None:
        self._store = store

    def register_pair(
        self,
        pair_name: str,
        risk_controller: Any = None,
        execution_engine: Any = None,
        recon_loop: Any = None,
        ws_listener: Any = None,
        rest_poller: Any = None,
    ) -> None:
        """Register a pair's components for control."""
        self._pair_statuses[pair_name] = PairStatus(name=pair_name)
        if risk_controller:
            self._risk_controllers[pair_name] = risk_controller
        if execution_engine:
            self._execution_engines[pair_name] = execution_engine
        if recon_loop:
            self._recon_loops[pair_name] = recon_loop
        if ws_listener:
            self._ws_listeners[pair_name] = ws_listener
        if rest_poller:
            self._rest_pollers[pair_name] = rest_poller

    # --- Status queries ---

    @property
    def uptime_seconds(self) -> float:
        return time.time() - self._start_time

    @property
    def uptime_str(self) -> str:
        s = int(self.uptime_seconds)
        days, s = divmod(s, 86400)
        hours, s = divmod(s, 3600)
        minutes, s = divmod(s, 60)
        parts = []
        if days:
            parts.append(f"{days}d")
        if hours:
            parts.append(f"{hours}h")
        parts.append(f"{minutes}m")
        return " ".join(parts)

    @property
    def mode(self) -> str:
        return self._mode

    @property
    def pair_names(self) -> list[str]:
        return list(self._pair_statuses.keys())

    def get_pair_status(self, pair_name: str) -> PairStatus | None:
        return self._pair_statuses.get(pair_name)

    def update_ws_status(self, pair_name: str, connected: bool) -> None:
        status = self._pair_statuses.get(pair_name)
        if status:
            status.ws_connected = connected

    def update_recon_time(self, pair_name: str) -> None:
        status = self._pair_statuses.get(pair_name)
        if status:
            status.last_reconciliation = time.time()

    def increment_trades(self, pair_name: str) -> None:
        status = self._pair_statuses.get(pair_name)
        if status:
            status.total_trades += 1

    # --- State queries ---

    def get_leader_state(self, pair_name: str) -> AccountState | None:
        if self._store:
            return self._store.get_leader_state(pair_name)
        return None

    def get_follower_state(self, pair_name: str) -> AccountState | None:
        if self._store:
            return self._store.get_follower_state(pair_name)
        return None

    def get_session_pnl(self, pair_name: str) -> Decimal:
        if self._store:
            return self._store.get_session_pnl(pair_name)
        return Decimal("0")

    def get_starting_equity(self, pair_name: str) -> Decimal:
        if self._store:
            return self._store.get_starting_equity(pair_name)
        return Decimal("0")

    # --- Control commands ---

    def pause(self, pair_name: str | None = None) -> str:
        """Pause trading for a pair or all pairs."""
        targets = [pair_name] if pair_name else list(self._pair_statuses.keys())
        paused = []
        for name in targets:
            status = self._pair_statuses.get(name)
            if status:
                status.paused = True
                paused.append(name)
        logger.warning("Trading paused", pairs=paused)
        return f"Paused: {', '.join(paused)}"

    def resume(self, pair_name: str | None = None) -> str:
        """Resume trading for a pair or all pairs."""
        targets = [pair_name] if pair_name else list(self._pair_statuses.keys())
        resumed = []
        for name in targets:
            status = self._pair_statuses.get(name)
            if status:
                status.paused = False
                resumed.append(name)
        logger.info("Trading resumed", pairs=resumed)
        return f"Resumed: {', '.join(resumed)}"

    def is_paused(self, pair_name: str) -> bool:
        status = self._pair_statuses.get(pair_name)
        return status.paused if status else False

    async def kill(self) -> str:
        """Activate kill switch — close all positions."""
        self._killed = True
        results = []
        for pair_name, risk in self._risk_controllers.items():
            risk.kill_switch.activate("Manual kill via Discord")

            # Close positions
            follower = self.get_follower_state(pair_name)
            if follower and pair_name in self._execution_engines:
                poller = self._rest_pollers.get(pair_name)
                if poller:
                    mid_prices = await poller.fetch_all_mids()
                    close_results = await self._execution_engines[pair_name].close_all_positions(
                        follower.positions, mid_prices
                    )
                    results.append(f"{pair_name}: closed {len(close_results)} positions")

        logger.critical("KILL SWITCH activated via Discord")
        return "🚨 Kill switch activated. " + "; ".join(results) if results else "🚨 Kill switch activated."

    def reset_kill(self) -> str:
        """Reset kill switch after manual review."""
        self._killed = False
        for risk in self._risk_controllers.values():
            risk.kill_switch.reset()
        logger.warning("Kill switch reset via Discord")
        return "✅ Kill switch reset. Trading will resume on next reconciliation cycle."

    # --- Config commands ---

    def set_multiplier(self, value: float) -> str:
        if self._config:
            self._config.scaling.multiplier = Decimal(str(value))
            return f"Multiplier set to {value}"
        return "Config not available"

    def set_max_position(self, value: float) -> str:
        if self._config:
            self._config.risk.max_position_usd = Decimal(str(value))
            return f"Max position set to ${value:,.0f}"
        return "Config not available"

    def get_config_summary(self) -> dict[str, str]:
        if not self._config:
            return {"error": "Config not loaded"}
        return {
            "Mode": self._config.mode,
            "Multiplier": str(self._config.scaling.multiplier),
            "Max Position": f"${self._config.risk.max_position_usd:,.0f}",
            "Max Exposure": f"${self._config.risk.max_total_exposure_usd:,.0f}",
            "Max Leverage": str(self._config.risk.max_leverage),
            "Slippage BPS": str(self._config.risk.slippage_tolerance_bps),
            "Recon Interval": f"{self._config.polling.reconciliation_interval_s}s",
            "Paper Equity": f"${self._config.scaling.paper_equity:,.0f}",
            "Whitelist": str(self._config.risk.symbol_whitelist),
        }

    async def get_recent_trades(self, pair_name: str | None = None, limit: int = 10) -> list[dict]:
        """Fetch recent trades from the database."""
        if not self._store or not self._store._db:
            return []

        query = """SELECT timestamp, coin, side, size, price, status
                   FROM order_log"""
        params: list = []

        if pair_name:
            query += " WHERE pair_name = ?"
            params.append(pair_name)

        query += " ORDER BY timestamp DESC LIMIT ?"
        params.append(limit)

        cursor = await self._store._db.execute(query, params)
        rows = await cursor.fetchall()

        trades = []
        for row in rows:
            trades.append({
                "time": row[0],
                "coin": row[1],
                "side": row[2],
                "size": row[3],
                "price": row[4],
                "status": row[5],
            })
        return trades
