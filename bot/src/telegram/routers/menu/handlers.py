from adaptix import Retort
from aiogram import F, Router
from aiogram.filters import CommandStart
from aiogram.types import CallbackQuery, Message
from aiogram_dialog import DialogManager, ShowMode, StartMode
from aiogram_dialog.widgets.kbd import Button
from dishka import FromDishka
from dishka.integrations.aiogram_dialog import inject
from loguru import logger

from src.application.common import Notifier, Redirect
from src.application.common.dao import PlanDao, SettingsDao, SubscriptionDao
from src.application.dto import (
    MediaDescriptorDto,
    MessagePayloadDto,
    PlanSnapshotDto,
    TelegramUserDto,
)
from src.application.use_cases.subscription.commands.management import (
    AddSubscriptionDuration,
    AddSubscriptionDurationDto,
)
from src.application.use_cases.subscription.commands.purchase import (
    ActivateTrialSubscription,
    ActivateTrialSubscriptionDto,
)
from src.application.use_cases.subscription.commands.set_plan import (
    SetUserSubscription,
    SetUserSubscriptionDto,
)
from src.application.use_cases.user.commands.profile_edit import ResetOwnReferralCode
from src.application.use_cases.user.queries.plans import GetAvailableTrial
from src.core.constants import USER_KEY
from src.core.enums import PurchaseType
from src.core.exceptions import CooldownError
from src.core.utils.i18n_helpers import i18n_format_expire_time
from src.infrastructure.dobby.admin import ClientViewStore, gift_action
from src.infrastructure.dobby.config import DobbyConfig
from src.telegram.keyboards import CALLBACK_CHANNEL_CONFIRM, CALLBACK_RULES_ACCEPT
from src.telegram.purchase_widget import PurchaseWidget
from src.telegram.routers.subscription.handlers import PURCHASE_TYPE_KEY
from src.telegram.states import MainMenu, Subscription

router = Router(name=__name__)


async def on_start_dialog(user: TelegramUserDto, dialog_manager: DialogManager) -> None:
    logger.info(f"{user.log} Started dialog")
    await dialog_manager.start(
        state=MainMenu.MAIN,
        mode=StartMode.RESET_STACK,
        show_mode=ShowMode.DELETE_AND_SEND,
    )


@router.message(CommandStart(ignore_case=True))
async def on_start_command(
    message: Message, user: TelegramUserDto, dialog_manager: DialogManager
) -> None:
    await on_start_dialog(user, dialog_manager)


@router.callback_query(F.data == CALLBACK_RULES_ACCEPT)
async def on_rules_accept(
    callback: CallbackQuery,
    user: TelegramUserDto,
    dialog_manager: DialogManager,
) -> None:
    await on_start_dialog(user, dialog_manager)


@router.callback_query(F.data == CALLBACK_CHANNEL_CONFIRM)
async def on_channel_confirm(
    callback: CallbackQuery,
    user: TelegramUserDto,
    dialog_manager: DialogManager,
) -> None:
    await on_start_dialog(user, dialog_manager)


@inject
async def on_get_trial(
    callback: CallbackQuery,
    widget: Button,
    dialog_manager: DialogManager,
    notifier: FromDishka[Notifier],
    redirect: FromDishka[Redirect],
    retort: FromDishka[Retort],
    get_available_trial: FromDishka[GetAvailableTrial],
    activate_trial_subscription: FromDishka[ActivateTrialSubscription],
    settings_dao: FromDishka[SettingsDao],
) -> None:
    user: TelegramUserDto = dialog_manager.middleware_data[USER_KEY]
    plan = await get_available_trial.system(user)

    if not plan:
        await notifier.notify_user(user=user, i18n_key="ntf-common.trial-unavailable")
        raise ValueError("Trial plan not exist")

    settings = await settings_dao.get()
    currency = settings.default_currency
    raw_price = plan.durations[0].get_price(currency)

    if raw_price == 0:
        trial = PlanSnapshotDto.from_plan(plan, plan.durations[0].days)

        try:
            await activate_trial_subscription.system(ActivateTrialSubscriptionDto(user, trial))
        except Exception:
            logger.exception(f"{user.log} Trial activation failed")
            if user.telegram_id is not None:
                await redirect.to_failed_payment(user.telegram_id)
            return

        if user.telegram_id is not None:
            await redirect.to_success_trial(user.telegram_id)
        return

    await dialog_manager.start(
        state=Subscription.MAIN,
        data={
            "trial_plan": retort.dump(plan),
            "trial_duration": plan.durations[0].days,
        },
    )


@inject
async def on_text_button_click(
    callback: CallbackQuery,
    widget: Button,
    dialog_manager: DialogManager,
    settings_dao: FromDishka[SettingsDao],
    notifier: FromDishka[Notifier],
) -> None:
    user: TelegramUserDto = dialog_manager.middleware_data[USER_KEY]
    button_index = int(dialog_manager.item_id)  # type: ignore[attr-defined]
    settings = await settings_dao.get()
    button = next((b for b in settings.menu.buttons if b.index == button_index), None)

    if not button or not (button.payload or button.media_file_id):
        return

    await notifier.notify_user(
        user=user,
        payload=MessagePayloadDto(
            i18n_key="raw-message",
            i18n_kwargs={"content": button.payload},
            media=MediaDescriptorDto(kind="file_id", value=button.media_file_id)
            if button.media_file_id
            else None,
            media_type=button.media_type,
            disable_default_markup=False,
            delete_after=None,
        ),
    )


@inject
async def on_withdraw_points(
    callback: CallbackQuery,
    widget: Button,
    dialog_manager: DialogManager,
    notifier: FromDishka[Notifier],
) -> None:
    user: TelegramUserDto = dialog_manager.middleware_data[USER_KEY]
    await notifier.notify_user(user=user, i18n_key="ntf-common.withdraw-points")


@inject
async def on_invite(
    callback: CallbackQuery,
    widget: Button,
    dialog_manager: DialogManager,
    settings_dao: FromDishka[SettingsDao],
) -> None:
    settings = await settings_dao.get()
    if settings.referral.enable:
        await dialog_manager.switch_to(state=MainMenu.INVITE)
    return


@inject
async def on_reset_referral_code(
    callback: CallbackQuery,
    widget: Button,
    dialog_manager: DialogManager,
    reset_own_referral_code: FromDishka[ResetOwnReferralCode],
    notifier: FromDishka[Notifier],
) -> None:
    user: TelegramUserDto = dialog_manager.middleware_data[USER_KEY]
    try:
        await reset_own_referral_code(user)
    except CooldownError as e:
        await notifier.notify_user(
            user=user,
            payload=MessagePayloadDto(
                i18n_key="ntf-common.cooldown-active",
                i18n_kwargs={"available_at": i18n_format_expire_time(e.available_at)},
            ),
        )
        return
    await notifier.notify_user(user=user, i18n_key="ntf-invite.referral-reset")


@inject
async def on_buy(
    callback: CallbackQuery,
    widget: Button,
    dialog_manager: DialogManager,
    subscription_dao: FromDishka[SubscriptionDao],
    purchase_widget: FromDishka[PurchaseWidget],
    config: FromDishka[DobbyConfig],
) -> None:
    """ "Buy" and "Renew": the purchase widget, or the usual screens where it cannot be used."""
    user: TelegramUserDto = dialog_manager.middleware_data[USER_KEY]
    if config.purchase_widget:
        try:
            if await purchase_widget.send(user):
                return
        except Exception:
            logger.exception(f"{user.log} Failed to send the purchase widget")
    await start_text_purchase(dialog_manager, user, subscription_dao)


async def start_text_purchase(
    dialog_manager: DialogManager, user: TelegramUserDto, subscription_dao: SubscriptionDao
) -> None:
    """Straight to choosing a term. A trial cannot be renewed, so its "Renew" buys a plan."""
    subscription = await subscription_dao.get_current(user.id)
    renewable = subscription is not None and not subscription.is_trial
    purchase_type = PurchaseType.RENEW if renewable else PurchaseType.NEW
    logger.info(f"{user.log} Started purchase '{purchase_type}' from the main menu")
    await dialog_manager.start(
        state=Subscription.MAIN,
        data={PURCHASE_TYPE_KEY: purchase_type},
    )


@inject
async def on_client_view_on(
    callback: CallbackQuery,
    widget: Button,
    dialog_manager: DialogManager,
    client_view: FromDishka[ClientViewStore],
) -> None:
    user: TelegramUserDto = dialog_manager.middleware_data[USER_KEY]
    await client_view.set(user.id, True)
    logger.info(f"{user.log} Switched to client view")
    await dialog_manager.switch_to(MainMenu.MAIN)


@inject
async def on_client_view_off(
    callback: CallbackQuery,
    widget: Button,
    dialog_manager: DialogManager,
    client_view: FromDishka[ClientViewStore],
) -> None:
    user: TelegramUserDto = dialog_manager.middleware_data[USER_KEY]
    await client_view.set(user.id, False)
    logger.info(f"{user.log} Left client view")
    await dialog_manager.start(MainMenu.MAIN, mode=StartMode.RESET_STACK)


@inject
async def on_gift_self(
    callback: CallbackQuery,
    widget: Button,
    dialog_manager: DialogManager,
    subscription_dao: FromDishka[SubscriptionDao],
    plan_dao: FromDishka[PlanDao],
    add_duration: FromDishka[AddSubscriptionDuration],
    set_subscription: FromDishka[SetUserSubscription],
    notifier: FromDishka[Notifier],
) -> None:
    """A free subscription for the admin: no payment, so nothing counts as revenue."""
    user: TelegramUserDto = dialog_manager.middleware_data[USER_KEY]
    days = int((widget.widget_id or "gift_30").removeprefix("gift_"))
    current = await subscription_dao.get_current(user.id)
    active_until = current.expire_at if current and current.is_active else None
    action = gift_action(active_until, bool(current and current.is_unlimited), days)

    if action == "extend":
        await add_duration(user, AddSubscriptionDurationDto(user_id=user.id, days=days))
    else:
        plans = [p for p in await plan_dao.get_active_plans() if not p.is_trial]
        if not plans:
            await notifier.notify_user(user, i18n_key="ntf-admin.gift-no-plan")
            return
        await set_subscription(
            user, SetUserSubscriptionDto(user_id=user.id, plan_id=plans[0].id, duration=days)
        )

    logger.info(f"{user.log} Gave themselves a subscription: '{action}' {days} days")
    await notifier.notify_user(user, i18n_key="ntf-admin.gift-done")
    await dialog_manager.switch_to(MainMenu.MY_VPN)
