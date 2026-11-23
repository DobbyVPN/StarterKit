from typing import Any

from src.infrastructure.dobby.config import DobbyConfig
from src.infrastructure.dobby.downloads import AndroidDownloadLink, pick_android_asset

RELEASE = {
    "assets": [
        {"name": "DobbyVPN-v1.5.15-debug.apk", "browser_download_url": "https://x/debug.apk"},
        {"name": "DobbyVPN-v1.5.15-sign.apk.idsig", "browser_download_url": "https://x/idsig"},
        {"name": "DobbyVPN-v1.5.15-sign.apk", "browser_download_url": "https://x/sign.apk"},
        {"name": "dobbyVPN-windows-amd64.msi", "browser_download_url": "https://x/win.msi"},
    ]
}


def test_picks_the_signed_release_apk() -> None:
    assert pick_android_asset(RELEASE) == "https://x/sign.apk"
    assert pick_android_asset({"assets": []}) is None


class Clock:
    def __init__(self) -> None:
        self.now = 0.0

    def __call__(self) -> float:
        return self.now


def link_with(releases: list[Any], clock: Clock, override: str = "") -> AndroidDownloadLink:
    link = AndroidDownloadLink("DobbyVPN/DobbyVPN", override=override, clock=clock)
    calls = iter(releases)

    async def latest() -> dict[str, Any]:
        item = next(calls)
        if isinstance(item, Exception):
            raise item
        return item  # type: ignore[no-any-return]

    link._latest_release = latest  # type: ignore[method-assign]
    return link


async def test_looks_up_once_an_hour() -> None:
    clock = Clock()
    link = link_with([RELEASE, {"assets": []}], clock)
    assert await link.get() == "https://x/sign.apk"
    clock.now += 1800
    assert await link.get() == "https://x/sign.apk"  # cached, no second lookup


async def test_falls_back_to_the_release_page() -> None:
    link = link_with([ConnectionError("offline")], Clock())
    assert await link.get() == "https://github.com/DobbyVPN/DobbyVPN/releases/latest"


async def test_keeps_the_last_known_link_when_github_fails() -> None:
    clock = Clock()
    link = link_with([RELEASE, ConnectionError("offline")], clock)
    await link.get()
    clock.now += 7200
    assert await link.get() == "https://x/sign.apk"


async def test_owner_link_wins() -> None:
    link = link_with([], Clock(), override="https://example.com/app.apk")
    assert await link.get() == "https://example.com/app.apk"


def test_desktop_defaults_are_direct_files() -> None:
    config = DobbyConfig()
    assert config.download_url_windows.endswith(
        "/releases/latest/download/dobbyVPN-windows-amd64.msi"
    )
    assert config.download_url_macos.endswith("/latest/download/dobbyVPN-macos-aarch64.pkg")
    assert config.download_url_macos_intel.endswith("/latest/download/dobbyVPN-macos-amd64.pkg")
    assert config.download_url_ios.startswith("https://apps.apple.com/")
