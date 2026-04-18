"""In-memory state store with SQLite persistence for crash recovery."""

from __future__ import annotations

import json
import time
from decimal import Decimal

import aiosqlite

from copybot.state.models import AccountState, OrderResult, OrderStatus
from copybot.utils.logging import get_logger

logger = get_logger(__name__)

# Custom JSON encoder for Decimal
class _DecimalEncoder(json.JSONEncoder):
    def default(self, o):
        if isinstance(o, Decimal):
            return str(o)
        return super().default(o)


def _serialize_state(state: AccountState) -> str:
    """Serialize AccountState to JSON string."""
    positions = {}
    for coin, pos in state.positions.items():
        positions[coin] = {
            "coin": pos.coin,
            "szi": str(pos.szi),
            "entry_px": str(pos.entry_px),
            "leverage_type": pos.leverage.type,
            "leverage_value": pos.leverage.value,
            "unrealized_pnl": str(pos.unrealized_pnl),
        }
    return json.dumps(positions, cls=_DecimalEncoder)


class StateStore:
    """In-memory account state with SQLite-backed persistence.

    Each leader-follower pair has its own state entries.
    On crash recovery, the latest snapshot is loaded from SQLite.
    """

    def __init__(self, db_path: str = "copybot_state.db"):
        self.db_path = db_path
        self._db: aiosqlite.Connection | None = None

        # In-memory state per pair
        self._leader_states: dict[str, AccountState] = {}  # pair_name → state
        self._follower_states: dict[str, AccountState] = {}

        # Follower session tracking
        self._starting_equity: dict[str, Decimal] = {}
        self._session_pnl: dict[str, Decimal] = {}

    async def initialize(self) -> None:
        """Create the database and tables if they don't exist."""
        self._db = await aiosqlite.connect(self.db_path)
        await self._db.execute("PRAGMA journal_mode=WAL")
        await self._db.execute("PRAGMA synchronous=NORMAL")

        await self._db.executescript("""
            CREATE TABLE IF NOT EXISTS state_snapshot (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                pair_name TEXT NOT NULL,
                role TEXT NOT NULL,
                positions_json TEXT NOT NULL,
                account_value TEXT NOT NULL,
                timestamp REAL NOT NULL
            );

            CREATE TABLE IF NOT EXISTS order_log (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                pair_name TEXT NOT NULL,
                timestamp REAL NOT NULL,
                coin TEXT NOT NULL,
                side TEXT NOT NULL,
                size TEXT NOT NULL,
                price TEXT,
                order_type TEXT NOT NULL,
                status TEXT NOT NULL,
                oid TEXT,
                cloid TEXT,
                error TEXT
            );

            CREATE INDEX IF NOT EXISTS idx_snapshot_pair_role
                ON state_snapshot(pair_name, role);
            CREATE INDEX IF NOT EXISTS idx_order_pair_time
                ON order_log(pair_name, timestamp);
        """)
        await self._db.commit()
        logger.info("State store initialized", db_path=self.db_path)

    async def close(self) -> None:
        """Close the database connection."""
        if self._db:
            await self._db.close()
            self._db = None

    # --- In-memory state ---

    def set_leader_state(self, pair_name: str, state: AccountState) -> None:
        self._leader_states[pair_name] = state

    def get_leader_state(self, pair_name: str) -> AccountState | None:
        return self._leader_states.get(pair_name)

    def set_follower_state(self, pair_name: str, state: AccountState) -> None:
        self._follower_states[pair_name] = state
        # Track session PnL
        if pair_name not in self._starting_equity:
            self._starting_equity[pair_name] = state.account_value
            self._session_pnl[pair_name] = Decimal("0")
            logger.info(
                "Follower starting equity set",
                pair=pair_name,
                equity=str(state.account_value),
            )

    def get_follower_state(self, pair_name: str) -> AccountState | None:
        return self._follower_states.get(pair_name)

    def get_starting_equity(self, pair_name: str) -> Decimal:
        return self._starting_equity.get(pair_name, Decimal("0"))

    def get_session_pnl(self, pair_name: str) -> Decimal:
        follower = self._follower_states.get(pair_name)
        start_eq = self._starting_equity.get(pair_name, Decimal("0"))
        if follower and start_eq > 0:
            return follower.account_value - start_eq
        return Decimal("0")

    # --- Persistence ---

    async def persist_snapshot(self, pair_name: str) -> None:
        """Save current leader + follower state to SQLite."""
        if not self._db:
            return

        now = time.time()

        for role, states in [("leader", self._leader_states), ("follower", self._follower_states)]:
            state = states.get(pair_name)
            if state:
                await self._db.execute(
                    """INSERT INTO state_snapshot
                       (pair_name, role, positions_json, account_value, timestamp)
                       VALUES (?, ?, ?, ?, ?)""",
                    (pair_name, role, _serialize_state(state), str(state.account_value), now),
                )

        await self._db.commit()

    async def log_order(self, pair_name: str, result: OrderResult) -> None:
        """Log an order result to SQLite."""
        if not self._db:
            return

        await self._db.execute(
            """INSERT INTO order_log
               (pair_name, timestamp, coin, side, size, price, order_type, status, oid, cloid, error)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                pair_name,
                result.timestamp or time.time(),
                result.intent.coin,
                "buy" if result.intent.is_buy else "sell",
                str(result.intent.abs_delta),
                str(result.filled_price) if result.filled_price else None,
                "ioc",
                result.status.value,
                result.oid,
                result.cloid,
                result.error,
            ),
        )
        await self._db.commit()

    async def get_recent_failures(self, pair_name: str, limit: int = 10) -> int:
        """Count the most recent consecutive failures."""
        if not self._db:
            return 0

        cursor = await self._db.execute(
            """SELECT status FROM order_log
               WHERE pair_name = ?
               ORDER BY timestamp DESC
               LIMIT ?""",
            (pair_name, limit),
        )
        rows = await cursor.fetchall()

        count = 0
        for row in rows:
            if row[0] in (OrderStatus.FAILED.value, OrderStatus.CANCELLED.value):
                count += 1
            else:
                break
        return count

    async def cleanup_old_snapshots(self, keep_latest: int = 100) -> None:
        """Remove old snapshots to prevent unbounded DB growth."""
        if not self._db:
            return

        await self._db.execute(
            """DELETE FROM state_snapshot
               WHERE id NOT IN (
                   SELECT id FROM state_snapshot ORDER BY timestamp DESC LIMIT ?
               )""",
            (keep_latest,),
        )
        await self._db.commit()
