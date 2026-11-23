from datetime import timedelta
from types import SimpleNamespace
from typing import Any, Optional
from uuid import uuid4

import pytest
from adaptix import Retort

from src.application.dto import PlanSnapshotDto, SubscriptionDto
from src.application.dto.settings import SettingsDto
from src.core.config import AppConfig
from src.core.constants import GOTO_PREFIX, PAYMENT_PREFIX
from src.core.enums import Currency, Locale, PurchaseType, SubscriptionStatus
from src.core.utils.time import datetime_now
from src.infrastructure.di.providers.i18n import I18nProvider
from src.infrastructure.dobby.links import is_key_issued
from src.telegram.routers.extra.goto import on_goto
from src.telegram.routers.menu.getters import SubState, subscription_screen_data
from src.telegram.routers.subscription import handlers
from src.telegram.routers.subscription.handlers import PURCHASE_TYPE_KEY
from src.telegram.states import Subscription

KEY = "https://sub.example.com/abc"
PLACEHOLDER = "https://sub.example.invalid/abc"


@pytest.fixture(scope="module")
def i18n() -> Any:
    hub = I18nProvider().get_translator_hub(AppConfig.get(), Retort())
    return hub.get_translator_by_locale(Locale.RU)


def sub(
    days: float = 10,
    status: SubscriptionStatus = SubscriptionStatus.ACTIVE,
    is_trial: bool = False,
    url: str = KEY,
) -> SubscriptionDto:
    return SubscriptionDto(
        user_remna_id=uuid4(),
        status=status,
        is_trial=is_trial,
        traffic_limit=0,
        device_limit=0,
        traffic_limit_strategy=PlanSnapshotDto.test().traffic_limit_strategy,
        expire_at=datetime_now() + timedelta(days=days),
        url=url,
        plan_snapshot=PlanSnapshotDto.test(),
    )


# Key placeholder


@pytest.mark.parametrize(
    ("url", "issued"),
    [
        (KEY, True),
        (PLACEHOLDER, False),
        ("https://invalid/abc", False),
        ("", False),
        (None, False),
        ("   ", False),
    ],
)
def test_key_issued(url: Optional[str], issued: bool) -> None:
    assert is_key_issued(url) is issued


# Main screen state


def test_active_subscription() -> None:
    data = subscription_screen_data(sub(days=9.5))
    assert data["sub_state"] == SubState.ACTIVE
    assert data["days_left"] == 10
    assert data["purchase_type"] == PurchaseType.RENEW
    assert data["key_issued"] is True


def test_expired_subscription() -> None:
    data = subscription_screen_data(sub(days=-1))
    assert data["sub_state"] == SubState.EXPIRED
    assert data["days_left"] == 0


def test_paused_subscription() -> None:
    data = subscription_screen_data(sub(status=SubscriptionStatus.DISABLED))
    assert data["sub_state"] == SubState.PAUSED


def test_trial_renew_buys_a_plan() -> None:
    assert subscription_screen_data(sub(is_trial=True))["purchase_type"] == PurchaseType.NEW


# Texts


def main_menu(i18n: Any, **kwargs: Any) -> str:
    base = {
        "sub_state": "NONE",
        "trial_available": 0,
        "is_unlimited": 0,
        "expire_date": "01.12.2026",
        "days_left": 5,
        "sub_key": KEY,
        "key_issued": 1,
    }
    return str(i18n.get("msg-main-menu", **{**base, **kwargs}))


def test_main_menu_without_subscription(i18n: Any) -> None:
    with_trial = main_menu(i18n, trial_available=1)
    without_trial = main_menu(i18n, trial_available=0)
    assert "VPN, который просто работает" in with_trial
    assert "бесплатно" in with_trial
    assert "бесплатно" not in without_trial


def test_main_menu_active_shows_date_days_and_key(i18n: Any) -> None:
    text = main_menu(i18n, sub_state="ACTIVE", days_left=5)
    assert "Активна до 01.12.2026" in text
    assert "осталось 5 дней" in text
    assert f"<code>{KEY}</code>" in text
    assert "осталось 1 день" in main_menu(i18n, sub_state="ACTIVE", days_left=1)
    assert "осталось 3 дня" in main_menu(i18n, sub_state="ACTIVE", days_left=3)


def test_main_menu_active_without_key(i18n: Any) -> None:
    text = main_menu(i18n, sub_state="ACTIVE", key_issued=0, sub_key=PLACEHOLDER)
    assert "Ключ появится" in text
    assert PLACEHOLDER not in text


def test_main_menu_expired_and_paused(i18n: Any) -> None:
    assert "закончилась 01.12.2026" in main_menu(i18n, sub_state="EXPIRED")
    assert "приостановлена" in main_menu(i18n, sub_state="PAUSED")


@pytest.mark.parametrize("state", ["NONE", "ACTIVE", "EXPIRED", "PAUSED"])
def test_no_traffic_or_devices_on_the_main_screen(i18n: Any, state: str) -> None:
    text = main_menu(i18n, sub_state=state).lower()
    assert "трафик" not in text
    assert "устройств" not in text.replace("на все устройства", "")


def test_connect_and_download_screens(i18n: Any) -> None:
    connect = str(i18n.get("msg-menu-connect"))
    assert "Скопировать ключ" in connect
    assert "Скачать DobbyVPN" in connect  # for those without the app
    assert "подписка нужна" in str(i18n.get("msg-menu-download"))


def test_waiting_for_the_key_suggests_downloading(i18n: Any) -> None:
    text = main_menu(i18n, sub_state="ACTIVE", key_issued=0, sub_key=PLACEHOLDER)
    assert "скачать приложение" in text


def test_copy_buttons_have_no_own_icon(i18n: Any) -> None:
    # Telegram draws its own icon on copy buttons.
    for key in ("btn-invite.copy", "btn-connect.copy-key"):
        assert str(i18n.get(key))[0].isalpha()


def test_payment_screen(i18n: Any) -> None:
    text = str(
        i18n.get(
            "msg-subscription-confirm",
            period="3 месяца",
            final_amount=499,
            currency="₽",
            discount_percent=0,
            original_amount=499,
            is_free=0,
        )
    )
    assert "3 месяца" in text and "499" in text
    assert "включится сама" in text
    assert "499 ₽" in str(i18n.get("btn-subscription.pay", final_amount=499, currency="₽"))


# Reminder -> choosing a term


class FakeBg:
    def __init__(self) -> None:
        self.started: list[dict[str, Any]] = []

    def bg(self, **kwargs: Any) -> "FakeBg":
        return self

    async def start(self, **kwargs: Any) -> None:
        self.started.append(kwargs)


class FakeCallback:
    def __init__(self, data: str) -> None:
        self.data = data

    async def answer(self) -> None:
        return None


@pytest.mark.parametrize("purchase_type", [PurchaseType.RENEW, PurchaseType.NEW])
async def test_reminder_button_opens_choosing_a_term(purchase_type: PurchaseType) -> None:
    manager = FakeBg()
    user = SimpleNamespace(telegram_id=1, log="[test]")
    callback = FakeCallback(f"{GOTO_PREFIX}{PAYMENT_PREFIX}{purchase_type}")

    await on_goto(callback, manager, user)  # type: ignore[arg-type]

    (started,) = manager.started
    assert started["state"] == Subscription.MAIN
    assert started["data"] == {PURCHASE_TYPE_KEY: purchase_type}


class FakeStartManager:
    def __init__(self, state: object) -> None:
        self.state = state
        self.middleware_data: dict[str, Any] = {}

    def current_context(self) -> Any:
        return SimpleNamespace(state=self.state)


async def test_success_screen_after_payment_does_not_restart_the_purchase(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    started: list[Any] = []

    async def fake_begin(manager: Any, purchase_type: PurchaseType) -> None:
        started.append(purchase_type)

    monkeypatch.setattr(handlers, "_begin_purchase_from_container", fake_begin)

    # What the payment redirect passes when it opens the success screen.
    success = FakeStartManager(Subscription.SUCCESS)
    await handlers.on_subscription_start({"purchase_type": PurchaseType.NEW}, success)  # type: ignore[arg-type]
    assert started == []

    # "Buy" / "Renew" / reminder buttons.
    main = FakeStartManager(Subscription.MAIN)
    await handlers.on_subscription_start({PURCHASE_TYPE_KEY: PurchaseType.RENEW}, main)  # type: ignore[arg-type]
    assert started == [PurchaseType.RENEW]


def test_defaults_sell_in_roubles() -> None:
    assert SettingsDto().default_currency == Currency.RUB


def test_expiry_notices_say_why_the_vpn_stopped(i18n: Any) -> None:
    expired = str(i18n.get("event-subscription.expired", is_trial=0))
    assert expired.startswith("<b>⏳ Подписка закончилась")
    assert "ключ останется тем же" in expired
    trial = str(i18n.get("event-subscription.expired", is_trial=1))
    assert "Пробный период закончился" in trial
    expiring = str(i18n.get("event-subscription.expiring", is_trial=0, value=3))
    assert "закончится через" in expiring


def test_choosing_a_term_does_not_repeat_the_buttons(i18n: Any) -> None:
    text = str(i18n.get("msg-subscription-duration", plan="1 месяц", description=0))
    assert "1 месяц" not in text
