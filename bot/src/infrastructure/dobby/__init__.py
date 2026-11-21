"""DobbyVPN backend: replaces the Remnawave panel.

The bot database is the source of truth for users and subscription terms. After every change
the full list of users is exported to the external config-delivery service, which owns server
keys and subscription links.
"""

from .backend import BackendUser, DobbyBackend, ExportTrigger
from .config import DobbyConfig
from .exporter import ExportedUser, FileUserExporter, UserExporter, serialize_users
from .links import SubscriptionLinkProvider, TemplateLinkProvider

__all__ = [
    "BackendUser",
    "DobbyBackend",
    "DobbyConfig",
    "ExportTrigger",
    "ExportedUser",
    "FileUserExporter",
    "SubscriptionLinkProvider",
    "TemplateLinkProvider",
    "UserExporter",
    "serialize_users",
]
