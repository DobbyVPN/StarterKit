import json
import re
from dataclasses import dataclass, field
from datetime import timedelta
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Optional
from uuid import uuid4

import pytest

from src.core.utils.time import datetime_now
from src.infrastructure.dobby.payment_check import NotYourPaymentError, UserCheckResult
from src.telegram.purchase_widget import model, service
from src.telegram.purchase_widget.model import (
    MAX_COMPRESSED_HTML_BYTES,
    MAX_DATA_BYTES,
    MAX_HTML_BYTES,
    TEXT_KEYS,
    Buy,
    Offer,
    WidgetState,
    compressed_size,
    fit_offers,
    format_money,
    parse_query,
    per_month,
    render_html,
)
from src.telegram.purchase_widget.service import PurchaseWidget, WidgetStore

FTL = Path(__file__).parents[2] / "assets/translations/ru/messages.ftl"


def _ru_texts() -> dict[str, str]:
    """The real Russian widget texts, read from the translation file."""
    block = FTL.read_text(encoding="utf-8").split("\nwidget-buy =\n", 1)[1]
    attrs = dict(re.findall(r"^    \.([\w-]+) = (.*)$", block, flags=re.M))
    return {key: attrs[value.split(".", 1)[1]] for key, value in TEXT_KEYS.items()}


# Size limits


def test_widget_html_fits_bot_api_limits() -> None:
    html = render_html(_ru_texts())
    assert "__TEXTS__" not in html
    assert len(html.encode()) <= MAX_HTML_BYTES
    # Keep a margin under the 4096 bytes Telegram counts after zlib level 9.
    assert compressed_size(html) <= MAX_COMPRESSED_HTML_BYTES - 512


def test_texts_cannot_close_the_script() -> None:
    html = render_html(dict.fromkeys(TEXT_KEYS, "</script><b>"))
    assert html.count("</script>") == 1


def test_html_size_check_rejects_too_large() -> None:
    with pytest.raises(ValueError):
        model.check_html_size("x" * (MAX_HTML_BYTES + 1))


def _offer(i: int, days: int = 30) -> Offer:
    return Offer(i, days, f"Тариф номер {i} · {days} дней", "1 999 ₽", "166 ₽ в месяц")


def test_chat_state_fits_the_limit_even_with_many_offers() -> None:
    state = WidgetState(offers=[_offer(i) for i in range(12)], trial="Попробовать 3 дня бесплатно")
    fit_offers(state)
    assert 0 < len(state.offers) <= model.MAX_OFFERS
    assert len(state.public().encode()) <= MAX_DATA_BYTES


def test_public_state_hides_payment_id_and_preselects_middle_offer() -> None:
    offers = [_offer(1, 30), _offer(1, 90), _offer(1, 365)]
    state = WidgetState(state="wait", offers=offers, offer=offers[0], payment_id="secret-id")
    state.payment_url = "https://pay.example/1"
    public = json.loads(state.public())
    assert "secret-id" not in state.public()
    assert public["sel"] == "1:90"
    assert public["w"]["u"] == "https://pay.example/1"
    restored = WidgetState.load(state.dump())
    assert restored.payment_id == "secret-id"
    assert restored.offer == offers[0]
    assert restored.state == "wait"


# Queries and formatting


@pytest.mark.parametrize(
    ("data", "expected"),
    [
        ("check", "check"),
        ("choose", "choose"),
        ("trial", "trial"),
        ("connect", "connect"),
        ("buy:2:90", Buy(2, 90)),
        ("buy:x:90", None),
        ("buy:2", None),
        ("drop table", None),
    ],
)
def test_parse_query(data: str, expected: object) -> None:
    assert parse_query(data) == expected


def test_money_and_per_month() -> None:
    assert format_money(Decimal("199.00"), "₽") == "199 ₽"
    assert format_money(Decimal("499.50"), "₽") == "499.5 ₽"
    assert per_month(Decimal("199"), 30) is None
    assert per_month(Decimal("499"), 90) == Decimal("166")
    assert per_month(Decimal("1990"), 365) == Decimal("164")


# The service, with fakes around it


@dataclass
class FakeDuration:
    days: int
    price: Decimal

    def get_price(self, currency: Any) -> Decimal:
        return self.price


@dataclass
class FakePlan:
    id: int
    name: str
    durations: list[FakeDuration]
    is_trial: bool = False

    def get_duration(self, days: int) -> Optional[FakeDuration]:
        return next((d for d in self.durations if d.days == days), None)


class FakeI18n:
    def get(self, key: str, **kw: Any) -> str:
        return f"{key}{kw}" if kw else key


class FakeRedis:
    def __init__(self) -> None:
        self.data: dict[str, str] = {}

    async def get(self, key: str) -> Optional[str]:
        return self.data.get(key)

    async def set(self, key: str, value: str, ex: Any = None) -> None:
        self.data[key] = value


@dataclass
class Calls:
    edits: list[str] = field(default_factory=list)
    payments: list[Any] = field(default_factory=list)
    trials: list[Any] = field(default_factory=list)
    connects: list[int] = field(default_factory=list)


def _interactor(fn: Any) -> Any:
    async def system(arg: Any) -> Any:
        return await fn(arg)

    return SimpleNamespace(system=system)


def make_widget(  # noqa: C901
    monkeypatch: pytest.MonkeyPatch,
    *,
    check: Any = None,
    subscription: Any = None,
    trial_plan: Optional[FakePlan] = None,
    blocked: bool = False,
) -> tuple[PurchaseWidget, Calls]:
    calls = Calls()
    plan = FakePlan(1, "1 месяц", [FakeDuration(30, Decimal(199)), FakeDuration(90, Decimal(499))])
    state: dict[str, Any] = {"subscription": subscription}

    monkeypatch.setattr(service, "PlanSnapshotDto", SimpleNamespace(from_plan=lambda p, d: (p, d)))

    async def edit(**kw: Any) -> bool:
        calls.edits.append(kw["chat_data"])
        return True

    async def plans(user: Any) -> list[FakePlan]:
        return [plan]

    async def trial(user: Any) -> Optional[FakePlan]:
        return trial_plan

    async def activate(dto: Any) -> None:
        calls.trials.append(dto)
        state["subscription"] = SimpleNamespace(
            is_trial=True, is_unlimited=False, expire_at=datetime_now() + timedelta(days=3)
        )

    async def create_payment(user: Any, dto: Any) -> Any:
        calls.payments.append(dto)
        return SimpleNamespace(id=uuid4(), url="https://yoomoney.ru/checkout/payments/v2/x")

    async def check_user(user_id: int, payment_id: Any) -> UserCheckResult:
        result = check(payment_id) if check else UserCheckResult(pending=[payment_id])
        if result.completed:
            state["subscription"] = SimpleNamespace(
                is_trial=False,
                is_unlimited=False,
                expire_at=datetime_now() + timedelta(days=30, hours=1),
                plan_snapshot=SimpleNamespace(id=1),
            )
        return result

    async def get_current(user_id: int) -> Any:
        return state["subscription"]

    async def is_on(user_id: int) -> bool:
        return blocked

    async def to_connect(telegram_id: int) -> None:
        calls.connects.append(telegram_id)

    async def match(dto: Any) -> Any:
        return plan

    gateway = SimpleNamespace(
        type="YOOKASSA",
        currency="RUB",
        settings=SimpleNamespace(api_key=SimpleNamespace(get_secret_value=lambda: "live_1")),
    )

    async def active() -> list[Any]:
        return [gateway]

    async def settings_get() -> Any:
        return SimpleNamespace(default_currency=SimpleNamespace(symbol="₽"))

    widget = PurchaseWidget(
        bot=SimpleNamespace(edit_message_widget_data=edit),  # type: ignore[arg-type]
        store=WidgetStore(FakeRedis()),  # type: ignore[arg-type]
        translator_hub=SimpleNamespace(get_translator_by_locale=lambda locale: FakeI18n()),  # type: ignore[arg-type]
        settings_dao=SimpleNamespace(get=settings_get),  # type: ignore[arg-type]
        subscription_dao=SimpleNamespace(get_current=get_current),  # type: ignore[arg-type]
        payment_gateway_dao=SimpleNamespace(get_active=active),  # type: ignore[arg-type]
        pricing_service=SimpleNamespace(  # type: ignore[arg-type]
            calculate_for_duration=lambda user, d, currency: SimpleNamespace(
                final_amount=d.price, is_free=d.price == 0
            )
        ),
        get_available_plans=_interactor(plans),  # type: ignore[arg-type]
        get_available_trial=_interactor(trial),  # type: ignore[arg-type]
        match_plan=_interactor(match),  # type: ignore[arg-type]
        create_payment=create_payment,  # type: ignore[arg-type]
        activate_trial=_interactor(activate),  # type: ignore[arg-type]
        checker=SimpleNamespace(check_user=check_user),  # type: ignore[arg-type]
        client_view=SimpleNamespace(is_on=is_on),  # type: ignore[arg-type]
        redirect=SimpleNamespace(to_connect=to_connect),  # type: ignore[arg-type]
    )
    return widget, calls


USER = SimpleNamespace(id=7, telegram_id=700, language="ru", log="[user 7]")


async def _ask(widget: PurchaseWidget, query: Any) -> dict[str, Any]:
    answer = await widget.handle(USER, 700, 42, query)  # type: ignore[arg-type]
    return json.loads(answer)


async def test_buy_creates_payment_and_waits(monkeypatch: pytest.MonkeyPatch) -> None:
    widget, calls = make_widget(monkeypatch)
    choose = await _ask(widget, "choose")
    assert [o["k"] for o in choose["o"]] == ["1:30", "1:90"]
    assert choose["o"][1]["m"].startswith("widget-buy.per-month")

    wait = await _ask(widget, Buy(1, 90))
    assert wait["s"] == "wait"
    assert wait["w"]["u"].startswith("https://")
    assert len(calls.payments) == 1
    assert json.loads(calls.edits[-1])["s"] == "wait"


async def test_payment_success_moves_to_paid(monkeypatch: pytest.MonkeyPatch) -> None:
    widget, calls = make_widget(monkeypatch, check=lambda pid: UserCheckResult(completed=[pid]))
    await _ask(widget, Buy(1, 30))
    paid = await _ask(widget, "check")
    assert paid["s"] == "paid"
    assert paid["pd"]["n"] == "unit-day{'value': 30}"
    assert json.loads(calls.edits[-1])["s"] == "paid"


async def test_payment_failure_moves_to_failed_and_retry_pays_again(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    widget, calls = make_widget(monkeypatch, check=lambda pid: UserCheckResult(canceled=[pid]))
    await _ask(widget, Buy(1, 30))
    failed = await _ask(widget, "check")
    assert failed["s"] == "fail"
    assert failed["w"]["k"] == "1:30"
    again = await _ask(widget, Buy(1, 30))
    assert again["s"] == "wait"
    assert len(calls.payments) == 2


async def test_pending_payment_stays_waiting(monkeypatch: pytest.MonkeyPatch) -> None:
    widget, _ = make_widget(monkeypatch)
    await _ask(widget, Buy(1, 30))
    assert (await _ask(widget, "check"))["s"] == "wait"


async def test_foreign_payment_goes_back_to_choosing(monkeypatch: pytest.MonkeyPatch) -> None:
    def foreign(pid: Any) -> UserCheckResult:
        raise NotYourPaymentError(str(pid))

    widget, _ = make_widget(monkeypatch, check=foreign)
    await _ask(widget, Buy(1, 30))
    assert (await _ask(widget, "check"))["s"] == "choose"


async def test_buy_of_an_offer_not_shown_is_refused(monkeypatch: pytest.MonkeyPatch) -> None:
    widget, calls = make_widget(monkeypatch)
    answer = await widget.handle(USER, 700, 42, Buy(1, 7))  # type: ignore[arg-type]
    assert answer == "widget-buy.error-unavailable"
    assert calls.payments == []


async def test_live_payment_blocked_in_client_view(monkeypatch: pytest.MonkeyPatch) -> None:
    widget, calls = make_widget(monkeypatch, blocked=True)
    answer = await widget.handle(USER, 700, 42, Buy(1, 30))  # type: ignore[arg-type]
    assert answer == "widget-buy.error-client-view"
    assert calls.payments == []


async def test_trial_activates_and_shows_paid(monkeypatch: pytest.MonkeyPatch) -> None:
    trial = FakePlan(9, "Пробный", [FakeDuration(3, Decimal(0))], is_trial=True)
    widget, calls = make_widget(monkeypatch, trial_plan=trial)
    choose = await _ask(widget, "choose")
    assert choose["tr"].startswith("widget-buy.trial")
    paid = await _ask(widget, "trial")
    assert paid["s"] == "paid"
    assert len(calls.trials) == 1


async def test_trial_unavailable_is_an_error_text(monkeypatch: pytest.MonkeyPatch) -> None:
    widget, calls = make_widget(monkeypatch)
    answer = await widget.handle(USER, 700, 42, "trial")  # type: ignore[arg-type]
    assert answer == "widget-buy.error-trial"
    assert calls.trials == []


async def test_connect_opens_the_connect_screen(monkeypatch: pytest.MonkeyPatch) -> None:
    widget, calls = make_widget(monkeypatch)
    await _ask(widget, "connect")
    assert calls.connects == [700]


async def test_renewal_offers_only_the_current_plan(monkeypatch: pytest.MonkeyPatch) -> None:
    current = SimpleNamespace(
        is_trial=False,
        is_unlimited=False,
        expire_at=datetime_now() + timedelta(days=5),
        plan_snapshot=SimpleNamespace(id=1),
    )
    widget, _ = make_widget(monkeypatch, subscription=current)
    choose = await _ask(widget, "choose")
    assert {o["k"].split(":")[0] for o in choose["o"]} == {"1"}
    assert "tr" not in choose
