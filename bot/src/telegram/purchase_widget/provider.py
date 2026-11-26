from dishka import Provider, Scope, provide
from redis.asyncio import Redis

from .service import PurchaseWidget, WidgetStore


class PurchaseWidgetProvider(Provider):
    @provide(scope=Scope.APP)
    def get_store(self, redis: Redis) -> WidgetStore:
        return WidgetStore(redis)

    purchase_widget = provide(PurchaseWidget, scope=Scope.REQUEST)
