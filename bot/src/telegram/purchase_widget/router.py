from aiogram import F, Router
from aiogram.types import CallbackQuery, WidgetQuery
from aiogram_dialog import DialogManager
from dishka import FromDishka
from loguru import logger

from src.application.common.dao import SubscriptionDao
from src.application.dto import TelegramUserDto

from .model import parse_query
from .service import CALLBACK_TEXT_PURCHASE, PurchaseWidget

router = Router(name=__name__)


@router.widget_query()
async def on_widget_query(
    query: WidgetQuery, user: TelegramUserDto, widget: FromDishka[PurchaseWidget]
) -> None:
    parsed = parse_query(query.data)
    if parsed is None or query.chat is None or query.message_id is None:
        logger.warning(f"{user.log} Unknown widget query '{query.data}'")
        await query.answer()
        return
    if user.is_blocked:
        await query.answer()
        return
    answer = await widget.handle(user, query.chat.id, query.message_id, parsed)
    await query.answer(data=answer)


@router.callback_query(F.data == CALLBACK_TEXT_PURCHASE)
async def on_text_purchase(
    callback: CallbackQuery,
    user: TelegramUserDto,
    dialog_manager: DialogManager,
    subscription_dao: FromDishka[SubscriptionDao],
) -> None:
    """The button under the widget: the usual screens, for apps that cannot draw widgets."""
    # Imported here: the menu routers import this package for the "Buy" button.
    from src.telegram.routers.menu.handlers import start_text_purchase  # noqa: PLC0415

    await callback.answer()
    await start_text_purchase(dialog_manager, user, subscription_dao)
