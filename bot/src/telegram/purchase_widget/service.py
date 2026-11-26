from datetime import timedelta
from typing import Optional
from uuid import UUID

from aiogram import Bot
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup, Message
from loguru import logger
from redis.asyncio import Redis

from src.application.common import Redirect, TranslatorHub, TranslatorRunner
from src.application.common.dao import PaymentGatewayDao, SettingsDao, SubscriptionDao
from src.application.dto import PlanDto, PlanSnapshotDto, TelegramUserDto, UserDto
from src.application.services import PricingService
from src.application.use_cases.gateways.commands.payment import CreatePayment, CreatePaymentDto
from src.application.use_cases.plan.queries.match import MatchPlan, MatchPlanDto
from src.application.use_cases.subscription.commands.purchase import (
    ActivateTrialSubscription,
    ActivateTrialSubscriptionDto,
)
from src.application.use_cases.user.queries.plans import GetAvailablePlans, GetAvailableTrial
from src.core.enums import PurchaseType
from src.core.utils.i18n_helpers import i18n_format_days
from src.core.utils.i18n_keys import TimeUnitKey
from src.core.utils.time import datetime_now
from src.infrastructure.dobby.admin import ClientViewStore, live_payment_blocked
from src.infrastructure.dobby.payment_check import NotYourPaymentError, PaymentStatusChecker

from .model import (
    TEXT_KEYS,
    Buy,
    Offer,
    Query,
    WidgetState,
    check_html_size,
    fit_offers,
    format_money,
    per_month,
    render_html,
)

# Callback of the button under the widget: the usual screens, for apps that cannot draw it.
CALLBACK_TEXT_PURCHASE = "purchase_widget:text"
STATE_TTL = timedelta(days=30)


class WidgetStore:
    """The authoritative widget state, by message. Queries are checked against it."""

    def __init__(self, redis: Redis) -> None:
        self.redis = redis

    @staticmethod
    def _key(chat_id: int, message_id: int) -> str:
        return f"dobby:purchase_widget:{chat_id}:{message_id}"

    async def get(self, chat_id: int, message_id: int) -> Optional[WidgetState]:
        raw = await self.redis.get(self._key(chat_id, message_id))
        if raw is None:
            return None
        return WidgetState.load(raw.decode() if isinstance(raw, bytes) else raw)

    async def put(self, chat_id: int, message_id: int, state: WidgetState) -> None:
        await self.redis.set(self._key(chat_id, message_id), state.dump(), ex=STATE_TTL)


class PurchaseWidget:
    def __init__(
        self,
        bot: Bot,
        store: WidgetStore,
        translator_hub: TranslatorHub,
        settings_dao: SettingsDao,
        subscription_dao: SubscriptionDao,
        payment_gateway_dao: PaymentGatewayDao,
        pricing_service: PricingService,
        get_available_plans: GetAvailablePlans,
        get_available_trial: GetAvailableTrial,
        match_plan: MatchPlan,
        create_payment: CreatePayment,
        activate_trial: ActivateTrialSubscription,
        checker: PaymentStatusChecker,
        client_view: ClientViewStore,
        redirect: Redirect,
    ) -> None:
        self.bot = bot
        self.store = store
        self.translator_hub = translator_hub
        self.settings_dao = settings_dao
        self.subscription_dao = subscription_dao
        self.payment_gateway_dao = payment_gateway_dao
        self.pricing_service = pricing_service
        self.get_available_plans = get_available_plans
        self.get_available_trial = get_available_trial
        self.match_plan = match_plan
        self.create_payment = create_payment
        self.activate_trial = activate_trial
        self.checker = checker
        self.client_view = client_view
        self.redirect = redirect

    # Sending

    async def send(self, user: TelegramUserDto) -> Optional[Message]:
        """Sends the widget on its Choose screen. None when it cannot be used (several
        gateways, nothing to buy): the caller shows the usual screens instead."""
        if len(await self.payment_gateway_dao.get_active()) != 1:
            return None
        state = await self._choose_state(user)
        if not state.offers and not state.trial:
            return None

        i18n = self._i18n(user)
        html = render_html({key: i18n.get(value) for key, value in TEXT_KEYS.items()})
        check_html_size(html)
        message = await self.bot.send_widget(
            chat_id=user.telegram_id,
            html=html,
            chat_data=state.public(),
            caption=i18n.get("widget-buy.caption"),
            allow_queries=True,
            reply_markup=InlineKeyboardMarkup(
                inline_keyboard=[
                    [
                        InlineKeyboardButton(
                            text=i18n.get("widget-buy.text-path"),
                            callback_data=CALLBACK_TEXT_PURCHASE,
                        )
                    ]
                ]
            ),
        )
        await self.store.put(message.chat.id, message.message_id, state)
        logger.info(f"{user.log} Sent the purchase widget '{message.message_id}'")
        return message

    # Queries

    async def handle(
        self, user: TelegramUserDto, chat_id: int, message_id: int, query: Query
    ) -> str:
        """Answers one widget query with the new public state, or a short error text."""
        state = await self.store.get(chat_id, message_id)
        if state is None:
            # Unknown or expired message: start over from the current offers.
            state = await self._choose_state(user)

        i18n = self._i18n(user)
        if query == "choose":
            state = await self._choose_state(user)
        elif query == "check":
            state = await self._check(user, state)
        elif query == "trial":
            result = await self._trial(user, state)
            if isinstance(result, str):
                return i18n.get(result)
            state = result
        elif query == "connect":
            await self.redirect.to_connect(user.telegram_id)
            return state.public()
        elif isinstance(query, Buy):
            result = await self._buy(user, state, query)
            if isinstance(result, str):
                return i18n.get(result)
            state = result

        await self._save(chat_id, message_id, state)
        return state.public()

    async def _save(self, chat_id: int, message_id: int, state: WidgetState) -> None:
        await self.store.put(chat_id, message_id, state)
        try:
            await self.bot.edit_message_widget_data(
                chat_id=chat_id, message_id=message_id, chat_data=state.public()
            )
        except Exception as e:
            # The answer already carries the state; the stored copy only matters on redraw.
            logger.warning(f"Failed to edit widget data of message '{message_id}': {e}")

    async def _choose_state(self, user: TelegramUserDto) -> WidgetState:
        settings = await self.settings_dao.get()
        currency = settings.default_currency
        i18n = self._i18n(user)

        plans = [p for p in await self.get_available_plans.system(user) if not p.is_trial]
        current = await self.subscription_dao.get_current(user.id)
        if current is not None and not current.is_trial:
            # Renewal: the current plan only, as on the usual screens.
            matched = await self.match_plan.system(
                MatchPlanDto(plan_snapshot=current.plan_snapshot, plans=plans)
            )
            same_id = next((p for p in plans if p.id == current.plan_snapshot.id), None)
            renewable = matched or same_id
            plans = [renewable] if renewable else []

        offers: list[Offer] = []
        for plan in plans:
            for duration in plan.durations:
                price = self.pricing_service.calculate_for_duration(user, duration, currency)
                if price.is_free:
                    continue
                key, kw = i18n_format_days(duration.days)
                term = i18n.get(key, **kw)
                title = term if len(plans) == 1 else f"{i18n.get(plan.name)} · {term}"
                monthly = per_month(price.final_amount, duration.days)
                offers.append(
                    Offer(
                        plan_id=plan.id or 0,
                        days=duration.days,
                        title=title,
                        price=format_money(price.final_amount, currency.symbol),
                        per_month=i18n.get(
                            "widget-buy.per-month",
                            price=format_money(monthly, currency.symbol),
                        )
                        if monthly
                        else "",
                    )
                )

        trial = await self.get_available_trial.system(user)
        trial_label = ""
        if trial is not None and trial.durations[0].get_price(currency) == 0:
            days = trial.durations[0].days
            trial_label = i18n.get("widget-buy.trial", days=i18n.get(TimeUnitKey.DAY, value=days))
        return fit_offers(WidgetState(state="choose", offers=offers, trial=trial_label))

    async def _buy(
        self, user: TelegramUserDto, state: WidgetState, query: Buy
    ) -> WidgetState | str:
        fresh = await self._choose_state(user)
        offer = next(
            (o for o in fresh.offers if (o.plan_id, o.days) == (query.plan_id, query.days)),
            None,
        )
        plan = await self._plan(user, query.plan_id)
        if offer is None or plan is None:
            logger.warning(f"{user.log} Widget asked to buy an offer it was not shown")
            return "widget-buy.error-unavailable"

        gateway = (await self.payment_gateway_dao.get_active())[0]
        duration = plan.get_duration(query.days)
        if duration is None:
            return "widget-buy.error-unavailable"
        pricing = self.pricing_service.calculate_for_duration(user, duration, gateway.currency)
        if await live_payment_blocked(self.client_view, user.id, gateway.type, gateway.settings):
            logger.info(f"{user.log} Live payment blocked in client view")
            return "widget-buy.error-client-view"

        current = await self.subscription_dao.get_current(user.id)
        renewable = current is not None and not current.is_trial
        try:
            result = await self.create_payment(
                user,
                CreatePaymentDto(
                    plan_snapshot=PlanSnapshotDto.from_plan(plan, duration.days),
                    pricing=pricing,
                    purchase_type=PurchaseType.RENEW if renewable else PurchaseType.NEW,
                    gateway_type=gateway.type,
                ),
            )
        except Exception:
            logger.exception(f"{user.log} Failed to create a payment from the widget")
            return "widget-buy.error-payment"

        if not result.url:
            return "widget-buy.error-payment"
        return WidgetState(
            state="wait",
            offers=fresh.offers,
            trial=fresh.trial,
            offer=offer,
            payment_id=str(result.id),
            payment_url=result.url,
        )

    async def _check(self, user: TelegramUserDto, state: WidgetState) -> WidgetState:
        """Asks the gateway about the widget's payment, the same way "I paid" does."""
        if state.state != "wait" or not state.payment_id:
            return state
        try:
            result = await self.checker.check_user(user.id, UUID(state.payment_id))
        except NotYourPaymentError:
            logger.warning(f"{user.log} Widget checked a payment that is not theirs")
            return await self._choose_state(user)
        except Exception as e:
            logger.warning(f"{user.log} Widget payment check failed: {e}")
            return state

        if result.completed:
            return await self._paid_state(user, state)
        if result.canceled:
            state.state = "fail"
        return state

    async def _trial(self, user: TelegramUserDto, state: WidgetState) -> WidgetState | str:
        settings = await self.settings_dao.get()
        plan = await self.get_available_trial.system(user)
        if plan is None or plan.durations[0].get_price(settings.default_currency) != 0:
            return "widget-buy.error-trial"
        trial = PlanSnapshotDto.from_plan(plan, plan.durations[0].days)
        try:
            await self.activate_trial.system(ActivateTrialSubscriptionDto(user, trial))
        except Exception:
            logger.exception(f"{user.log} Trial activation from the widget failed")
            return "widget-buy.error"
        return await self._paid_state(user, state)

    async def _paid_state(self, user: TelegramUserDto, state: WidgetState) -> WidgetState:
        subscription = await self.subscription_dao.get_current(user.id)
        state.state = "paid"
        state.payment_id = None
        if subscription is None:
            return state
        i18n = self._i18n(user)
        if subscription.is_unlimited:
            state.paid_until = i18n.get("widget-buy.unlimited")
            state.paid_left = ""
            return state
        state.paid_until = subscription.expire_at.strftime("%d.%m.%Y")
        days_left = max(0, (subscription.expire_at - datetime_now()).days)
        state.paid_left = i18n.get(TimeUnitKey.DAY, value=days_left)
        return state

    async def _plan(self, user: UserDto, plan_id: int) -> Optional[PlanDto]:
        plans = await self.get_available_plans.system(user)
        return next((p for p in plans if p.id == plan_id), None)

    def _i18n(self, user: UserDto) -> TranslatorRunner:
        return self.translator_hub.get_translator_by_locale(user.language)
