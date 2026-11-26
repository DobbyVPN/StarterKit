from dishka import Provider
from dishka.integrations.aiogram import AiogramProvider

from src.infrastructure.dobby.provider import DobbyProvider
from src.telegram.purchase_widget import PurchaseWidgetProvider

from .bot import BotProvider
from .config import ConfigProvider
from .dao import DaoProvider
from .database import DatabaseProvider
from .i18n import I18nAiogramProvider, I18nProvider, I18nTaskiqProvider
from .payment_gateways import PaymentGatewaysProvider
from .redis import RedisProvider
from .remnawave import RemnawaveProvider
from .retort import RetortProvider
from .services import ServicesProvider
from .use_cases import UseCasesProvider


def get_aiogram_providers() -> list[Provider]:
    return [
        AiogramProvider(),
        BotProvider(),
        ConfigProvider(),
        DaoProvider(),
        DatabaseProvider(),
        DobbyProvider(),
        I18nProvider(),
        I18nAiogramProvider(),
        PaymentGatewaysProvider(),
        PurchaseWidgetProvider(),
        RedisProvider(),
        RemnawaveProvider(),
        RetortProvider(),
        ServicesProvider(),
        UseCasesProvider(),
    ]


def get_taskiq_providers() -> list[Provider]:
    return [
        BotProvider(),
        ConfigProvider(),
        DaoProvider(),
        DatabaseProvider(),
        DobbyProvider(),
        I18nProvider(),
        I18nTaskiqProvider(),
        PaymentGatewaysProvider(),
        RedisProvider(),
        RemnawaveProvider(),
        RetortProvider(),
        ServicesProvider(),
        UseCasesProvider(),
    ]
