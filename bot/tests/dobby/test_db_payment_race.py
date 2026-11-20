"""Racing confirmations of one payment on a real Postgres: it must activate once.

Return to the bot, the "I paid" button, the background check and a notification can all
check the same payment at the same moment. Runs only with DOBBY_DB_TESTS=1 (see
docs/development.md).
"""

# ruff: noqa: PLC0415

import asyncio
import os
import random
from typing import AsyncIterator, Optional
from uuid import UUID, uuid4

import pytest
from dishka import AsyncContainer, Provider, Scope, make_async_container, provide

pytestmark = pytest.mark.skipif(os.getenv("DOBBY_DB_TESTS") != "1", reason="needs Postgres")


class StubGateway:
    """Reports a fixed status, like the YooKassa API would."""

    supports_status_check = True
    status: Optional[object] = None

    async def fetch_transaction_status(self, payment_id: UUID) -> Optional[object]:
        await asyncio.sleep(0.01)  # let the racing checks overlap
        return StubGateway.status


class StubGetGateway:
    @property
    def system(self) -> "StubGetGateway":
        return self

    async def __call__(self, gateway_type: object) -> StubGateway:
        return StubGateway()


class Silent:
    """Stands in for Telegram-facing services (notifications, redirects, events)."""

    def __getattr__(self, name: str) -> object:
        async def noop(*args: object, **kwargs: object) -> None:
            return None

        return noop


@pytest.fixture
async def container(tmp_path, monkeypatch) -> AsyncIterator[AsyncContainer]:  # type: ignore[no-untyped-def]
    monkeypatch.setenv("DOBBY_EXPORT_PATH", str(tmp_path / "users.json"))
    from aiogram_dialog import BgManagerFactory

    from src.application.common import EventPublisher, Notifier, Redirect
    from src.application.use_cases.gateways.queries.providers import GetPaymentGatewayInstance
    from src.core.config import AppConfig
    from src.infrastructure.di.providers import get_taskiq_providers
    from src.infrastructure.dobby.backend import ExportTrigger

    class Overrides(Provider):
        scope = Scope.APP

        @provide
        def trigger(self) -> ExportTrigger:
            return Silent()  # type: ignore[return-value]

        @provide
        def publisher(self) -> EventPublisher:
            return Silent()  # type: ignore[return-value]

        @provide(scope=Scope.REQUEST)
        def notifier(self) -> Notifier:
            return Silent()  # type: ignore[return-value]

        @provide
        def redirect(self) -> Redirect:
            return Silent()  # type: ignore[return-value]

        @provide(scope=Scope.REQUEST)
        def gateways(self) -> GetPaymentGatewayInstance:
            return StubGetGateway()  # type: ignore[return-value]

    container = make_async_container(
        *get_taskiq_providers(),
        Overrides(),
        context={AppConfig: AppConfig.get(), BgManagerFactory: object()},
        skip_validation=True,
    )
    yield container
    await container.close()


async def make_user_and_payment(container: AsyncContainer) -> tuple[int, UUID]:
    from src.application.common.dao import TransactionDao, UserDao
    from src.application.common.uow import UnitOfWork
    from src.application.dto import PlanSnapshotDto, PriceDetailsDto, TransactionDto, UserDto
    from src.core.enums import Currency, PaymentGatewayType, PurchaseType, TransactionStatus

    telegram_id = random.randint(10**12, 10**13)
    payment_id = uuid4()
    async with container(scope=Scope.REQUEST) as request:
        uow = await request.get(UnitOfWork)
        async with uow:
            user = await (await request.get(UserDao)).create(
                UserDto(telegram_id=telegram_id, name="Race", referral_code=f"r{telegram_id}")
            )
            plan = PlanSnapshotDto.test()
            plan.duration = 30
            await (await request.get(TransactionDao)).create(
                TransactionDto(
                    payment_id=payment_id,
                    user_id=user.id,
                    status=TransactionStatus.PENDING,
                    purchase_type=PurchaseType.NEW,
                    gateway_type=PaymentGatewayType.YOOKASSA,
                    pricing=PriceDetailsDto.test(),
                    currency=Currency.RUB,
                    plan_snapshot=plan,
                )
            )
            await uow.commit()
    return user.id, payment_id


async def confirm(container: AsyncContainer, payment_id: UUID, how: str) -> None:
    from src.application.common.dao import TransactionDao
    from src.infrastructure.dobby.payment_check import PaymentStatusChecker

    async with container(scope=Scope.REQUEST) as request:
        checker = await request.get(PaymentStatusChecker)
        if how == "button":
            transaction = await (await request.get(TransactionDao)).get_by_payment_id(payment_id)
            assert transaction is not None
            await checker.check_user(transaction.user_id)
        elif how == "poller":
            await checker.fast_check(payment_id)
        else:
            await checker.sweep()


async def state(container: AsyncContainer, user_id: int, payment_id: UUID) -> tuple[str, int]:
    from src.application.common.dao import SubscriptionDao, TransactionDao

    async with container(scope=Scope.REQUEST) as request:
        transaction = await (await request.get(TransactionDao)).get_by_payment_id(payment_id)
        subscriptions = await (await request.get(SubscriptionDao)).get_all_by_user(user_id)
    assert transaction is not None
    return transaction.status.value, len(subscriptions)


async def cleanup(container: AsyncContainer, user_id: int) -> None:
    from src.application.common.dao import UserDao
    from src.application.common.uow import UnitOfWork

    async with container(scope=Scope.REQUEST) as request:
        uow = await request.get(UnitOfWork)
        async with uow:
            await (await request.get(UserDao)).delete(user_id)
            await uow.commit()


async def test_racing_confirmations_activate_once(container: AsyncContainer) -> None:
    from src.core.enums import TransactionStatus

    StubGateway.status = TransactionStatus.COMPLETED
    user_id, payment_id = await make_user_and_payment(container)
    try:
        await asyncio.gather(
            *(confirm(container, payment_id, how) for how in ("button", "poller", "sweep") * 3)
        )
        assert await state(container, user_id, payment_id) == ("COMPLETED", 1)
    finally:
        await cleanup(container, user_id)


async def test_given_up_payment_paid_later_activates_once(container: AsyncContainer) -> None:
    from src.core.enums import TransactionStatus

    user_id, payment_id = await make_user_and_payment(container)
    try:
        # Still unpaid at the give-up: the sweep cancels it.
        from src.application.common.dao import TransactionDao
        from src.infrastructure.dobby.payment_check import PaymentStatusChecker

        StubGateway.status = None
        async with container(scope=Scope.REQUEST) as request:
            checker = await request.get(PaymentStatusChecker)
            transaction = await (await request.get(TransactionDao)).get_by_payment_id(payment_id)
            assert transaction is not None
            await checker.check(transaction, give_up=True)
        assert await state(container, user_id, payment_id) == ("CANCELED", 0)

        # The user pays anyway and presses "I paid" twice at once.
        StubGateway.status = TransactionStatus.COMPLETED
        await asyncio.gather(*(confirm(container, payment_id, "button") for _ in range(2)))
        assert await state(container, user_id, payment_id) == ("COMPLETED", 1)
    finally:
        await cleanup(container, user_id)


async def test_canceled_payment_activates_nothing(container: AsyncContainer) -> None:
    from src.core.enums import TransactionStatus

    StubGateway.status = TransactionStatus.CANCELED
    user_id, payment_id = await make_user_and_payment(container)
    try:
        await asyncio.gather(*(confirm(container, payment_id, how) for how in ("button", "sweep")))
        assert await state(container, user_id, payment_id) == ("CANCELED", 0)
    finally:
        await cleanup(container, user_id)
