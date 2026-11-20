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
