from typing import Optional, Protocol
from urllib.parse import urlparse
from uuid import UUID


class SubscriptionLinkProvider(Protocol):
    def url_for(self, subscription_id: UUID) -> str: ...


class TemplateLinkProvider(SubscriptionLinkProvider):
    """Builds the link from a template. Stub until the config-delivery service defines links."""

    def __init__(self, template: str) -> None:
        if "{id}" not in template:
            raise ValueError("Subscription URL template must contain '{id}'")
        self.template = template

    def url_for(self, subscription_id: UUID) -> str:
        return self.template.replace("{id}", str(subscription_id))


def is_key_issued(url: Optional[str]) -> bool:
    """False while the subscription link is still a placeholder (no config-delivery service yet).

    The reserved ".invalid" top-level domain marks placeholders, as in the default template.
    """
    if not url or not url.strip():
        return False
    host = urlparse(url.strip()).hostname or ""
    return bool(host) and not (host == "invalid" or host.endswith(".invalid"))
