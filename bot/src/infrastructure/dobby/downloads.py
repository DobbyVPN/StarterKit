import time
from typing import Any, Callable, Optional

import httpx
from loguru import logger

# The signed release APK, e.g. DobbyVPN-v1.5.15-sign.apk.
ANDROID_ASSET_SUFFIX = "-sign.apk"
CACHE_SECONDS = 3600


def pick_android_asset(release: dict[str, Any]) -> Optional[str]:
    for asset in release.get("assets", []):
        name = str(asset.get("name", ""))
        if name.endswith(ANDROID_ASSET_SUFFIX) and "debug" not in name:
            url = asset.get("browser_download_url")
            return str(url) if url else None
    return None


class AndroidDownloadLink:
    """Direct link to the latest Android APK, looked up on GitHub at most once an hour.

    Falls back to the latest release page when GitHub cannot be reached.
    """

    def __init__(
        self,
        repository: str,
        override: str = "",
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.repository = repository
        self.override = override
        self.clock = clock
        self._cached: Optional[str] = None
        self._fetched_at = float("-inf")

    @property
    def fallback(self) -> str:
        return f"https://github.com/{self.repository}/releases/latest"

    async def get(self) -> str:
        if self.override:
            return self.override
        if self._cached and self.clock() - self._fetched_at < CACHE_SECONDS:
            return self._cached
        try:
            release = await self._latest_release()
            url = pick_android_asset(release)
        except Exception as e:
            logger.warning(f"Could not look up the latest Android release: {e}")
            url = None
        if url:
            self._cached, self._fetched_at = url, self.clock()
            return url
        return self._cached or self.fallback

    async def _latest_release(self) -> dict[str, Any]:
        async with httpx.AsyncClient(timeout=10) as client:
            response = await client.get(
                f"https://api.github.com/repos/{self.repository}/releases/latest",
                headers={"Accept": "application/vnd.github+json"},
            )
            response.raise_for_status()
            data = response.json()
            return data if isinstance(data, dict) else {}
