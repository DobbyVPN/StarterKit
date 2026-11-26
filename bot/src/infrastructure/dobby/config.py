from pathlib import Path
from typing import Literal

from src.core.config.base import BaseConfig

# Renewal reminders, hours before expiry; one more is sent at expiry. The single place for the
# defaults (3 days and 1 day). Owners override with DOBBY_REMINDER_HOURS_BEFORE, e.g. [72, 24].
DEFAULT_REMINDER_HOURS_BEFORE: tuple[int, ...] = (72, 24)


class DobbyConfig(BaseConfig, env_prefix="DOBBY_"):
    # Where the file exporter writes the user list. Placeholder until the sftp target is known.
    export_path: Path = Path("/opt/remnashop/data/users.json")

    # Subscription link shown to the user. "{id}" is replaced with the subscription id.
    # Placeholder until the config-delivery service defines its link format.
    subscription_url_template: str = "https://sub.example.invalid/{id}"

    # A full export is forced at least this often even if nothing changed.
    force_export_interval_seconds: int = 3600

    # How the bot receives Telegram updates. "polling" (default) needs no domain and no inbound
    # connections. "webhook" needs APP_DOMAIN with HTTPS in front of the bot.
    telegram_mode: Literal["polling", "webhook"] = "polling"

    reminder_hours_before: list[int] = list(DEFAULT_REMINDER_HOURS_BEFORE)

    # "Connect DobbyVPN" screen: where to download the app.
    # Direct file links: release files with a fixed name are taken from the latest release.
    download_url_ios: str = "https://apps.apple.com/us/app/dobbyvpn-do-better-by-vpn/id6741442515"
    download_url_windows: str = (
        "https://github.com/DobbyVPN/DobbyVPN/releases/latest/download/dobbyVPN-windows-amd64.msi"
    )
    download_url_macos: str = (
        "https://github.com/DobbyVPN/DobbyVPN/releases/latest/download/dobbyVPN-macos-aarch64.pkg"
    )
    download_url_macos_intel: str = (
        "https://github.com/DobbyVPN/DobbyVPN/releases/latest/download/dobbyVPN-macos-amd64.pkg"
    )
    # The Android file name carries the version, so by default the bot looks the current
    # file up in the latest release of this repository. Set a URL to use it instead.
    download_url_android: str = ""
    releases_repository: str = "DobbyVPN/DobbyVPN"

    # Optional video or page showing how to connect; adds a button to "Connect DobbyVPN".
    connect_guide_url: str = ""

    # Referral bonus for the invited friend on their first payment, in days (0 = none).
    # The inviter's reward is set in the admin menu (referral system).
    referral_friend_bonus_days: int = 30
