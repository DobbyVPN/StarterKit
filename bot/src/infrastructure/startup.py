from dishka import AsyncContainer, Scope

from src.application.use_cases.gateways.commands.payment import CreateDefaultPaymentGateway
from src.application.use_cases.settings.commands.defaults import CreateDefaultSettings


async def ensure_defaults(container: AsyncContainer) -> None:
    """Creates the default settings row and payment gateways if they are missing.

    Runs first on startup of the bot and of the task worker, before anything can publish an
    event: notification handlers read the settings, and on a fresh database the row does not
    exist yet ("Settings row is missing").
    """
    async with container(scope=Scope.REQUEST) as request:
        await (await request.get(CreateDefaultSettings)).system()
        await (await request.get(CreateDefaultPaymentGateway)).system()
