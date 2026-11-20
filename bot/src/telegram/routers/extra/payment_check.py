from aiogram import F, Router
from aiogram.filters import CommandStart
from aiogram.types import CallbackQuery, Message
from aiogram_dialog import DialogManager
from aiogram_dialog.widgets.kbd import Button
from dishka import FromDishka
from dishka.integrations.aiogram_dialog import inject as inject_dialog
from loguru import logger

from src.application.common import Notifier
from src.application.dto import TelegramUserDto
from src.core.constants import USER_KEY
from src.core.enums import Deeplink
from src.infrastructure.dobby.payment_check import PaymentStatusChecker, UserCheckResult
from src.telegram.routers.menu.handlers import on_start_dialog

router = Router(name=__name__)


async def _report(user: TelegramUserDto, result: UserCheckResult, notifier: Notifier) -> bool:
    """Tells the user what the check found. Returns False when there was nothing to check."""
    if result.completed:
        # ProcessPayment already moved the user to the success screen.
        return True
    if result.pending:
        await notifier.notify_user(user, i18n_key="ntf-subscription.payment-pending")
        return True
    if result.canceled:
        await notifier.notify_user(user, i18n_key="ntf-subscription.payment-canceled")
        return True
    return False


@router.message(
    CommandStart(deep_link=True, ignore_case=True),
    F.text.lower().endswith(f" {Deeplink.PAYMENT_RETURN}"),
)
async def on_payment_return(
    message: Message,
    user: TelegramUserDto,
    dialog_manager: DialogManager,
    checker: FromDishka[PaymentStatusChecker],
    notifier: FromDishka[Notifier],
) -> None:
    """The payment page sends the user back here: check their payments right away."""
    logger.info(f"{user.log} Returned from the payment page")
    result = await checker.check_user(user.id)
    if not await _report(user, result, notifier):
        await on_start_dialog(user, dialog_manager)


@inject_dialog
async def on_paid_button(
    callback: CallbackQuery,
    widget: Button,
    dialog_manager: DialogManager,
    checker: FromDishka[PaymentStatusChecker],
    notifier: FromDishka[Notifier],
) -> None:
    """The "I paid" button on the payment message."""
    user: TelegramUserDto = dialog_manager.middleware_data[USER_KEY]
    logger.info(f"{user.log} Pressed 'I paid'")
    result = await checker.check_user(user.id)
    if not await _report(user, result, notifier):
        await notifier.notify_user(user, i18n_key="ntf-subscription.payment-not-found")
