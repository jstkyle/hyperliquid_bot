"""Asset metadata cache — szDecimals, asset indices, max leverage per coin."""

from __future__ import annotations

import time

import aiohttp

from copybot.state.models import AssetMeta
from copybot.utils.logging import get_logger

logger = get_logger(__name__)


class MetadataCache:
    """Caches exchange metadata from the Hyperliquid info endpoint.

    Metadata is refreshed periodically (default every 5 minutes).
    Provides szDecimals, asset indices, and max leverage per coin.
    """

    def __init__(self, api_url: str, refresh_interval_s: int = 300):
        self.api_url = api_url
        self.refresh_interval_s = refresh_interval_s

        self._assets: dict[str, AssetMeta] = {}  # coin name → metadata
        self._last_refresh: float = 0.0

    async def refresh(self) -> None:
        """Fetch fresh metadata from the meta endpoint."""
        url = f"{self.api_url}/info"
        payload = {"type": "meta"}

        try:
            async with aiohttp.ClientSession() as session:
                async with session.post(url, json=payload) as resp:
                    resp.raise_for_status()
                    data = await resp.json()

            universe = data.get("universe", [])
            new_assets: dict[str, AssetMeta] = {}

            for idx, asset in enumerate(universe):
                name = asset["name"]
                new_assets[name] = AssetMeta(
                    name=name,
                    sz_decimals=int(asset.get("szDecimals", 0)),
                    asset_index=idx,
                    max_leverage=int(asset.get("maxLeverage", 50)),
                )

            self._assets = new_assets
            self._last_refresh = time.time()
            logger.info("Metadata cache refreshed", asset_count=len(new_assets))

        except Exception as e:
            logger.error("Failed to refresh metadata", error=str(e))
            if not self._assets:
                raise  # Fatal on first load

    async def ensure_fresh(self) -> None:
        """Refresh if stale."""
        if time.time() - self._last_refresh > self.refresh_interval_s:
            await self.refresh()

    def get(self, coin: str) -> AssetMeta | None:
        """Get metadata for a specific coin."""
        return self._assets.get(coin)

    def get_sz_decimals(self, coin: str) -> int:
        """Get szDecimals for a coin, defaulting to 0 if unknown."""
        meta = self._assets.get(coin)
        return meta.sz_decimals if meta else 0

    def get_asset_index(self, coin: str) -> int | None:
        """Get the asset index for use in exchange API calls."""
        meta = self._assets.get(coin)
        return meta.asset_index if meta else None

    def get_max_leverage(self, coin: str) -> int:
        """Get max allowed leverage for a coin."""
        meta = self._assets.get(coin)
        return meta.max_leverage if meta else 50

    @property
    def all_coins(self) -> list[str]:
        """List all known coin names."""
        return list(self._assets.keys())

    @property
    def is_loaded(self) -> bool:
        return len(self._assets) > 0
