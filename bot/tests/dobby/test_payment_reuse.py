from decimal import Decimal
from types import SimpleNamespace
from typing import Any, Optional
from uuid import UUID, uuid4

from src.application.dto import (
    PaymentResultDto,
    PlanSnapshotDto,
    PriceDetailsDto,
    TransactionDto,
    UserDto,
)
from src.application.use_cases.gateways.commands.payment import CreatePayment, CreatePaymentDto
from src.core.enums import Currency, PaymentGatewayType, PurchaseType, TransactionStatus

USER = UserDto(id=7, telegram_id=1001, name="Test")
PLAN = PlanSnapshotDto.test()


def price(amount: str) -> PriceDetailsDto:
    return PriceDetailsDto(
        original_amount=Decimal(amount), discount_percent=0, final_amount=Decimal(amount)
    )


class FakeGateway:
    supports_status_check = True
    data = SimpleNamespace(type=PaymentGatewayType.YOOKASSA, currency=Currency.RUB, settings=None)

    def __init__(self, pending_url: Optional[str]) -> None:
        self.pending_url = pending_url
        self.created = 0

    async def handle_create_payment(self, amount: Decimal, details: str) -> PaymentResultDto:
        self.created += 1
        return PaymentResultDto(id=uuid4(), url="https://pay.example.invalid/new")

    async def fetch_pending_payment_url(self, payment_id: UUID) -> Optional[str]:
        return self.pending_url


class FakeUow:
    async def __aenter__(self) -> "FakeUow":
        return self

    async def __aexit__(self, *args: Any) -> None:
        return None

    async def commit(self) -> None:
        return None


class FakeTransactionDao:
    def __init__(self, existing: Optional[TransactionDto]) -> None:
        self.existing = existing
        self.created: list[TransactionDto] = []

    async def get_recent_pending(self, **kwargs: Any) -> Optional[TransactionDto]:
        return self.existing

    async def create(self, transaction: TransactionDto) -> TransactionDto:
        self.created.append(transaction)
        return transaction


class Watcher:
    def __init__(self) -> None:
        self.watched: list[UUID] = []

    async def watch(self, payment_id: UUID) -> None:
        self.watched.append(payment_id)


def make(gateway: FakeGateway, existing: Optional[TransactionDto]) -> CreatePayment:
    async def get_gateway(gateway_type: Any) -> FakeGateway:
        return gateway

    translator = SimpleNamespace(get=lambda key, **kw: key)
    return CreatePayment(
        uow=FakeUow(),  # type: ignore[arg-type]
        payment_gateway_dao=None,  # type: ignore[arg-type]
        transaction_dao=FakeTransactionDao(existing),  # type: ignore[arg-type]
        get_payment_gateway_instance=SimpleNamespace(system=get_gateway),  # type: ignore[arg-type]
        translator_hub=SimpleNamespace(get_translator_by_locale=lambda locale: translator),  # type: ignore[arg-type]
        payment_watcher=Watcher(),
    )


def unpaid(amount: str = "199", purchase_type: PurchaseType = PurchaseType.RENEW) -> TransactionDto:
    return TransactionDto(
        payment_id=uuid4(),
        user_id=USER.id,
        status=TransactionStatus.PENDING,
        purchase_type=purchase_type,
        gateway_type=PaymentGatewayType.YOOKASSA,
        pricing=price(amount),
        currency=Currency.RUB,
        plan_snapshot=PLAN,
    )


def request(amount: str = "199") -> CreatePaymentDto:
    return CreatePaymentDto(
        plan_snapshot=PLAN,
        pricing=price(amount),
        purchase_type=PurchaseType.RENEW,
        gateway_type=PaymentGatewayType.YOOKASSA,
    )


async def test_reopening_payment_reuses_the_unpaid_one() -> None:
    existing = unpaid()
    gateway = FakeGateway(pending_url="https://pay.example.invalid/existing")
    result = await make(gateway, existing)._execute(USER, request())

    assert result.id == existing.payment_id
    assert result.url == "https://pay.example.invalid/existing"
    assert gateway.created == 0


async def test_new_payment_when_the_old_one_cannot_be_paid_anymore() -> None:
    gateway = FakeGateway(pending_url=None)  # canceled or already paid at the gateway
    result = await make(gateway, unpaid())._execute(USER, request())
    assert result.url == "https://pay.example.invalid/new"
    assert gateway.created == 1


async def test_new_payment_when_the_amount_changed() -> None:
    gateway = FakeGateway(pending_url="https://pay.example.invalid/existing")
    result = await make(gateway, unpaid(amount="199"))._execute(USER, request(amount="149"))
    assert result.url == "https://pay.example.invalid/new"
    assert gateway.created == 1


async def test_new_payment_when_the_purchase_type_differs() -> None:
    gateway = FakeGateway(pending_url="https://pay.example.invalid/existing")
    existing = unpaid(purchase_type=PurchaseType.CHANGE)
    result = await make(gateway, existing)._execute(USER, request())
    assert gateway.created == 1
    assert result.url == "https://pay.example.invalid/new"


async def test_new_payment_when_there_is_nothing_to_reuse() -> None:
    gateway = FakeGateway(pending_url=None)
    result = await make(gateway, None)._execute(USER, request())
    assert gateway.created == 1
    assert result.url == "https://pay.example.invalid/new"
