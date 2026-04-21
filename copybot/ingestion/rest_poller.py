"""REST poller — fetches clearinghouseState and mid prices from the info endpoint."""

from __future__ import annotations

import time
from decimal import Decimal

import aiohttp

from copybot.state.models import AccountState
from copybot.utils.logging import get_logger

logger = get_logger(__name__)


class RestPoller:
    """Polls the Hyperliquid REST info endpoint for account state and prices.

    Used for:
    - Periodic equity refresh
    - Full state snapshot on WebSocket reconnect
    - Reconciliation loop state fetch
    """

    def __init__(self, api_url: str):
        self.api_url = api_url
        self._session: aiohttp.ClientSession | None = None

    async def _get_session(self) -> aiohttp.ClientSession:
        if self._session is None or self._session.closed:
            self._session = aiohttp.ClientSession()
        return self._session

    async def close(self) -> None:
        if self._session and not self._session.closed:
            await self._session.close()

    async def fetch_clearinghouse_state(self, address: str) -> AccountState:
        """Fetch the full clearinghouse state for an address.

        Args:
            address: The wallet address to query (must be master account, not agent).

        Returns:
            Parsed AccountState with positions and equity.

        Raises:
            aiohttp.ClientError: On network or API errors.
        """
        url = f"{self.api_url}/info"
        payload = {"type": "clearinghouseState", "user": address}

        session = await self._get_session()
        async with session.post(url, json=payload) as resp:
            resp.raise_for_status()
            data = await resp.json()

        state = AccountState.from_api(address, data, time.time())
        logger.debug(
            "Fetched clearinghouse state",
            address=address[:10] + "...",
            positions=len(state.positions),
            equity=str(state.account_value),
        )
        return state

    async def fetch_spot_balance(self, address: str) -> Decimal:
        """Fetch the USDC balance from spotClearinghouseState.

        This captures funds that are on the spot/unified side,
        which don't appear in the perp clearinghouseState.

        Returns:
            Total USDC balance on the spot side.
        """
        url = f"{self.api_url}/info"
        payload = {"type": "spotClearinghouseState", "user": address}

        try:
            session = await self._get_session()
            async with session.post(url, json=payload) as resp:
                resp.raise_for_status()
                data = await resp.json()

            balances = data.get("balances", [])
            for bal in balances:
                if bal.get("coin") == "USDC":
                    total = Decimal(str(bal.get("total", "0")))
                    logger.debug(
                        "Fetched spot USDC balance",
                        address=address[:10] + "...",
                        usdc=str(total),
                    )
                    return total
        except Exception as e:
            logger.warning(
                "Failed to fetch spot balance, using 0",
                address=address[:10] + "...",
                error=str(e),
            )

        return Decimal("0")

    async def fetch_full_account_state(self, address: str) -> AccountState:
        """Fetch combined perp + spot state for an address.

        Combines the perp accountValue with spot USDC balance so the
        leader's equity is accurate even if funds are on the spot side
        (common with Unified Accounts).

        Returns:
            AccountState with account_value = perp equity + spot USDC.
        """
        state = await self.fetch_clearinghouse_state(address)
        spot_usdc = await self.fetch_spot_balance(address)

        if spot_usdc > 0:
            state.account_value += spot_usdc
            logger.debug(
                "Combined perp + spot equity",
                address=address[:10] + "...",
                perp_equity=str(state.account_value - spot_usdc),
                spot_usdc=str(spot_usdc),
                total=str(state.account_value),
            )

        return state

    async def fetch_all_mids(self) -> dict[str, Decimal]:
        """Fetch mid prices for all assets.

        Returns:
            Dict of coin → mid price.
        """
        url = f"{self.api_url}/info"
        payload = {"type": "allMids"}

        session = await self._get_session()
        async with session.post(url, json=payload) as resp:
            resp.raise_for_status()
            data = await resp.json()

        mids: dict[str, Decimal] = {}
        for coin, price_str in data.items():
            try:
                mids[coin] = Decimal(str(price_str))
            except Exception:
                continue

        return mids

    async def fetch_user_fills(
        self, address: str, start_time: int | None = None
    ) -> list[dict]:
        """Fetch recent fills for a user.

        Args:
            address: The wallet address.
            start_time: Optional start time in milliseconds.

        Returns:
            List of fill dicts from the API.
        """
        url = f"{self.api_url}/info"
        payload: dict = {"type": "userFills", "user": address}
        if start_time is not None:
            payload["startTime"] = start_time

        session = await self._get_session()
        async with session.post(url, json=payload) as resp:
            resp.raise_for_status()
            data = await resp.json()

        return data if isinstance(data, list) else []
