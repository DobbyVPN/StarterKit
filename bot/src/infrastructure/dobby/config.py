from pathlib import Path

from src.core.config.base import BaseConfig


class DobbyConfig(BaseConfig, env_prefix="DOBBY_"):
    # Where the file exporter writes the user list. Placeholder until the sftp target is known.
    export_path: Path = Path("/opt/remnashop/data/users.json")

    # Subscription link shown to the user. "{id}" is replaced with the subscription id.
    # Placeholder until the config-delivery service defines its link format.
    subscription_url_template: str = "https://sub.example.invalid/{id}"

    # A full export is forced at least this often even if nothing changed.
    force_export_interval_seconds: int = 3600

    # Local development only: poll Telegram instead of using a webhook (no public address).
    dev_polling: bool = False
