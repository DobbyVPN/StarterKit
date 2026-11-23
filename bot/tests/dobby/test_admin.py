import inspect
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from typing import Any

import pytest
from adaptix import Retort

from src.application.dto import TelegramUserDto
from src.application.use_cases.subscription.commands.management import AddSubscriptionDuration
from src.application.use_cases.subscription.commands.set_plan import SetUserSubscription
from src.core.config import AppConfig
from src.core.enums import (
    Currency,
    Locale,
    PaymentGatewayType,
    Role,
    SubscriptionStatus,
    TransactionStatus,
)
from src.infrastructure.di.providers.i18n import I18nProvider
from src.infrastructure.dobby.admin import (
    Payment,
    SubscriptionPoint,
    build_summary,
    format_amounts,
    gift_action,
    payment_allowed_in_client_view,
)
from src.telegram.routers.menu.getters import admin_screen_data

NOW = datetime(2026, 11, 23, 10, 0, tzinfo=timezone.utc)  # 13:00 in Moscow
TZ = "Europe/Moscow"
H = timedelta(hours=1)


def pay(amount: str, when: datetime, **kwargs: Any) -> Payment:
    fields = {
        "status": TransactionStatus.COMPLETED,
        "is_test": False,
        "currency": Currency.RUB,
        "final_amount": Decimal(amount),
        "paid_at": when,
    }
    return Payment(**{**fields, **kwargs})


def test_summary_counts_active_and_expiring() -> None:
    subs = [
        SubscriptionPoint(SubscriptionStatus.ACTIVE, NOW + 30 * 24 * H),
        SubscriptionPoint(SubscriptionStatus.ACTIVE, NOW + 2 * 24 * H),
        SubscriptionPoint(SubscriptionStatus.ACTIVE, NOW + 72 * H),
        SubscriptionPoint(SubscriptionStatus.ACTIVE, NOW - H),  # expired
        SubscriptionPoint(SubscriptionStatus.DISABLED, NOW + 10 * 24 * H),
    ]
    summary = build_summary(subs, [], NOW, TZ)
    assert summary.active == 3
    assert summary.expiring_soon == 2


def test_revenue_only_counts_real_completed_payments() -> None:
    payments = [
        pay("199", NOW - H),
        pay("499", NOW - 5 * 24 * H),  # earlier this month
        pay("0", NOW - H),  # free grant
        pay("199", NOW - H, is_test=True),
        pay("199", NOW - H, status=TransactionStatus.REFUNDED),
        pay("199", NOW - H, status=TransactionStatus.CANCELED),
        pay("999", datetime(2026, 10, 31, 12, 0, tzinfo=timezone.utc)),  # last month
    ]
    summary = build_summary([], payments, NOW, TZ)
    assert summary.revenue_today == {Currency.RUB: Decimal("199")}
    assert summary.revenue_month == {Currency.RUB: Decimal("698")}


def test_today_starts_at_midnight_in_the_owner_time_zone() -> None:
    # 22:30 UTC on the 22nd is 01:30 on the 23rd in Moscow: today. 20:30 UTC is still the 22nd.
    late = datetime(2026, 11, 22, 22, 30, tzinfo=timezone.utc)
    earlier = datetime(2026, 11, 22, 20, 30, tzinfo=timezone.utc)
    summary = build_summary([], [pay("100", late), pay("50", earlier)], NOW, TZ)
    assert summary.revenue_today == {Currency.RUB: Decimal("100")}
    assert summary.revenue_month == {Currency.RUB: Decimal("150")}


def test_amounts_format() -> None:
    assert format_amounts({}) == "0 ₽"
    assert format_amounts({Currency.RUB: Decimal("698.00")}) == "698 ₽"


def test_gift_extends_an_active_subscription() -> None:
    assert gift_action(NOW + 10 * 24 * H, False, 30) == "extend"
    assert gift_action(None, False, 30) == "set"  # none or expired
    assert gift_action(NOW + 10 * 24 * H, False, 0) == "set"  # forever
    assert gift_action(NOW + 10 * 24 * H, True, 30) == "set"  # already unlimited


def test_client_view_allows_only_a_yookassa_test_shop() -> None:
    assert payment_allowed_in_client_view(PaymentGatewayType.YOOKASSA, "test_abc") is True
    assert payment_allowed_in_client_view(PaymentGatewayType.YOOKASSA, "live_abc") is False
    assert payment_allowed_in_client_view(PaymentGatewayType.YOOKASSA, None) is False
    assert payment_allowed_in_client_view(PaymentGatewayType.TELEGRAM_STARS, "test_x") is False


class Store:
    def __init__(self, on: bool) -> None:
        self.on = on

    async def is_on(self, user_id: int) -> bool:
        return self.on


class Summary:
    async def get(self, now: datetime) -> Any:
        return build_summary([], [], now, TZ)


def user(role: Role) -> TelegramUserDto:
    return TelegramUserDto(telegram_id=1, name="Test", role=role)


@pytest.mark.parametrize(
    ("role", "client_mode", "admin_view"),
    [
        (Role.OWNER, False, True),
        (Role.OWNER, True, False),
        (Role.USER, False, False),
        (Role.USER, True, False),  # the mode means nothing for clients
    ],
)
async def test_who_sees_the_summary(role: Role, client_mode: bool, admin_view: bool) -> None:
    data = await admin_screen_data(user(role), Store(client_mode), Summary(), None)  # type: ignore[arg-type]
    assert data["admin_view"] is admin_view
    assert data["client_mode"] is (client_mode and role == Role.OWNER)
    assert ("active_subscriptions" in data) is admin_view


@pytest.fixture(scope="module")
def i18n() -> Any:
    hub = I18nProvider().get_translator_hub(AppConfig.get(), Retort())
    return hub.get_translator_by_locale(Locale.RU)


def test_summary_text(i18n: Any) -> None:
    args = {"active_subscriptions": 5, "revenue_today": "199 ₽", "revenue_month": "698 ₽"}
    calm = str(i18n.get("msg-admin-home", expiring_soon=0, **args))
    alert = str(i18n.get("msg-admin-home", expiring_soon=2, **args))
    assert "Активных подписок: <b>5</b>" in calm
    assert "мониторинг пока не подключён" in calm
    assert "⚠️ <b>Заканчиваются за 3 дня: 2</b>" in alert


def test_blocked_payment_text(i18n: Any) -> None:
    text = str(
        i18n.get(
            "msg-subscription-confirm",
            period="1 месяц",
            final_amount=199,
            currency="₽",
            discount_percent=0,
            original_amount=199,
            is_free=0,
            client_view_blocked=1,
        )
    )
    assert "оплата отключена" in text


# Giving oneself a subscription creates no payment.


@pytest.mark.parametrize("use_case", [SetUserSubscription, AddSubscriptionDuration])
def test_free_grants_cannot_create_payments(use_case: type) -> None:
    # Revenue is read from transactions; the grant paths have no way to write one.
    params = inspect.signature(use_case.__init__).parameters.values()
    kinds = {getattr(p.annotation, "__name__", str(p.annotation)) for p in params}
    assert not kinds & {"TransactionDao", "CreatePayment", "ProcessPayment"}
