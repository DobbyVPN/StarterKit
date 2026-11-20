from typing import Protocol
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
