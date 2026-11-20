from datetime import datetime, timedelta, timezone
from typing import Optional
from uuid import UUID, uuid4

import pytest

from src.application.dto import PlanSnapshotDto, PriceDetailsDto, TransactionDto
from src.core.enums import Currency, PaymentGatewayType, PurchaseType, TransactionStatus
from src.infrastructure.dobby.payment_check import (
    GIVE_UP_AFTER,
    NotYourPaymentError,
    PaymentStatusChecker,
    next_fast_poll_delay,
)

NOW = datetime(2026, 11, 20, 12, 0, tzinfo=timezone.utc)


def make_tx(
    status: TransactionStatus = TransactionStatus.PENDING,
    age: timedelta = timedelta(minutes=1),
    user_id: int = 1,
) -> TransactionDto:
    return TransactionDto(
        payment_id=uuid4(),
        user_id=user_id,
        status=status,
        purchase_type=PurchaseType.NEW,
        gateway_type=PaymentGatewayType.YOOKASSA,
        pricing=PriceDetailsDto.test(),
        currency=Currency.RUB,
        plan_snapshot=PlanSnapshotDto.test(),
        created_at=NOW - age,
    )


class FakeGateway:
    supports_status_check = True

    def __init__(self, statuses: dict[UUID, Optional[TransactionStatus]]) -> None:
        self.statuses = statuses
        self.calls = 0

    async def fetch_transaction_status(self, payment_id: UUID) -> Optional[TransactionStatus]:
        self.calls += 1
        return self.statuses.get(payment_id)


class FakeGetGateway:
    def __init__(self, gateway: FakeGateway) -> None:
        self.gateway = gateway

    @property
    def system(self) -> "FakeGetGateway":
        return self

    async def __call__(self, gateway_type: PaymentGatewayType) -> FakeGateway:
        return self.gateway


class FakeProcessPayment:
    def __init__(self) -> None:
        self.applied: list[tuple[UUID, TransactionStatus]] = []

    @property
    def system(self) -> "FakeProcessPayment":
        return self

    async def __call__(self, data: object) -> None:
        self.applied.append((data.payment_id, data.new_transaction_status))  # type: ignore[attr-defined]


class FakeTransactionDao:
    def __init__(self, transactions: list[TransactionDto]) -> None:
        self.transactions = transactions

    async def get_by_payment_id(self, payment_id: UUID) -> Optional[TransactionDto]:
        return next((t for t in self.transactions if t.payment_id == payment_id), None)

    async def get_by_user(self, user_id: int) -> list[TransactionDto]:
        return [t for t in self.transactions if t.user_id == user_id]

    async def get_by_status(self, status: TransactionStatus) -> list[TransactionDto]:
        return [t for t in self.transactions if t.status == status]


def make_checker(
    transactions: list[TransactionDto], statuses: dict[UUID, Optional[TransactionStatus]]
) -> tuple[PaymentStatusChecker, FakeGateway, FakeProcessPayment]:
    gateway, process = FakeGateway(statuses), FakeProcessPayment()
    checker = PaymentStatusChecker(
        FakeTransactionDao(transactions),  # type: ignore[arg-type]
        FakeGetGateway(gateway),  # type: ignore[arg-type]
        process,  # type: ignore[arg-type]
        now=lambda: NOW,
    )
    return checker, gateway, process


def test_fast_poll_schedule_backs_off_then_stops() -> None:
    assert next_fast_poll_delay(0) == 4
    assert next_fast_poll_delay(179) == 4
    assert next_fast_poll_delay(180) == 15
    assert next_fast_poll_delay(599) == 15
    assert next_fast_poll_delay(600) is None


async def test_paid_payment_is_applied() -> None:
    tx = make_tx()
    checker, _, process = make_checker([tx], {tx.payment_id: TransactionStatus.COMPLETED})
    assert await checker.check(tx) == TransactionStatus.COMPLETED
    assert process.applied == [(tx.payment_id, TransactionStatus.COMPLETED)]


async def test_pending_payment_changes_nothing() -> None:
    tx = make_tx()
    checker, _, process = make_checker([tx], {tx.payment_id: None})
    assert await checker.check(tx) is None
    assert process.applied == []


async def test_canceled_payment_is_canceled() -> None:
    tx = make_tx()
    checker, _, process = make_checker([tx], {tx.payment_id: TransactionStatus.CANCELED})
    assert await checker.check(tx) == TransactionStatus.CANCELED
    assert process.applied == [(tx.payment_id, TransactionStatus.CANCELED)]


async def test_sweep_gives_up_only_after_30_minutes() -> None:
    young = make_tx(age=GIVE_UP_AFTER - timedelta(seconds=1))
    old = make_tx(age=GIVE_UP_AFTER)
    checker, gateway, process = make_checker([young, old], {})

    await checker.sweep()

    assert gateway.calls == 2  # both were still asked first
    assert process.applied == [(old.payment_id, TransactionStatus.CANCELED)]


async def test_sweep_applies_a_late_success_instead_of_giving_up() -> None:
    old = make_tx(age=GIVE_UP_AFTER + timedelta(minutes=5))
    checker, _, process = make_checker([old], {old.payment_id: TransactionStatus.COMPLETED})
    await checker.sweep()
    assert process.applied == [(old.payment_id, TransactionStatus.COMPLETED)]


async def test_sweep_ignores_final_payments() -> None:
    done = make_tx(status=TransactionStatus.COMPLETED)
    checker, gateway, _ = make_checker([done], {})
    await checker.sweep()
    assert gateway.calls == 0


async def test_user_check_finds_payment_made_after_giving_up() -> None:
    # Canceled by the give-up, then the user paid anyway and pressed "I paid".
    given_up = make_tx(status=TransactionStatus.CANCELED, age=timedelta(hours=2))
    checker, _, process = make_checker(
        [given_up], {given_up.payment_id: TransactionStatus.COMPLETED}
    )
    result = await checker.check_user(user_id=1)
    assert result.completed == [given_up.payment_id]
    assert process.applied == [(given_up.payment_id, TransactionStatus.COMPLETED)]


async def test_user_check_reports_pending_and_canceled() -> None:
    pending, canceled = make_tx(), make_tx()
    checker, _, _ = make_checker(
        [pending, canceled], {canceled.payment_id: TransactionStatus.CANCELED}
    )
    result = await checker.check_user(user_id=1)
    assert result.pending == [pending.payment_id]
    assert result.canceled == [canceled.payment_id]


async def test_user_check_skips_old_and_other_users_payments() -> None:
    old = make_tx(status=TransactionStatus.CANCELED, age=timedelta(days=2))
    other = make_tx(user_id=2)
    checker, gateway, _ = make_checker([old, other], {})
    result = await checker.check_user(user_id=1)
    assert result.pending == result.completed == result.canceled == []
    assert gateway.calls == 0


async def test_fast_check_stops_when_paid() -> None:
    tx = make_tx(age=timedelta(seconds=10))
    checker, _, _ = make_checker([tx], {tx.payment_id: TransactionStatus.COMPLETED})
    assert await checker.fast_check(tx.payment_id) is None


async def test_fast_check_continues_while_pending() -> None:
    tx = make_tx(age=timedelta(seconds=10))
    checker, _, _ = make_checker([tx], {})
    assert await checker.fast_check(tx.payment_id) == 4


class MemoryThrottle:
    def __init__(self) -> None:
        self.values: dict[UUID, str] = {}

    async def recent(self, payment_id: UUID) -> Optional[str]:
        return self.values.get(payment_id)

    async def remember(self, payment_id: UUID, status: str, ttl_seconds: int) -> None:
        self.values[payment_id] = status


def make_throttled(
    transactions: list[TransactionDto], statuses: dict[UUID, Optional[TransactionStatus]]
) -> tuple[PaymentStatusChecker, FakeGateway, FakeProcessPayment, MemoryThrottle]:
    gateway, process, throttle = FakeGateway(statuses), FakeProcessPayment(), MemoryThrottle()
    checker = PaymentStatusChecker(
        FakeTransactionDao(transactions),  # type: ignore[arg-type]
        FakeGetGateway(gateway),  # type: ignore[arg-type]
        process,  # type: ignore[arg-type]
        throttle=throttle,
        now=lambda: NOW,
    )
    return checker, gateway, process, throttle


async def test_i_paid_without_paying_activates_nothing() -> None:
    tx = make_tx()
    checker, gateway, process, _ = make_throttled([tx], {tx.payment_id: None})

    result = await checker.check_user(user_id=1, payment_id=tx.payment_id)

    assert result.pending == [tx.payment_id]
    assert result.completed == []
    assert process.applied == []
    assert gateway.calls == 1


async def test_i_paid_with_no_payments_at_all() -> None:
    checker, gateway, process, _ = make_throttled([], {})
    result = await checker.check_user(user_id=1)
    assert result.pending == result.completed == result.canceled == []
    assert gateway.calls == 0
    assert process.applied == []


async def test_rapid_presses_ask_the_gateway_once() -> None:
    tx = make_tx()
    checker, gateway, _, _ = make_throttled([tx], {tx.payment_id: None})

    for _ in range(10):
        result = await checker.check_user(user_id=1, payment_id=tx.payment_id)
        assert result.pending == [tx.payment_id]

    assert gateway.calls == 1


async def test_presses_after_the_window_ask_again() -> None:
    tx = make_tx()
    checker, gateway, process, throttle = make_throttled([tx], {tx.payment_id: None})
    await checker.check_user(user_id=1, payment_id=tx.payment_id)

    throttle.values.clear()  # the window expired
    gateway.statuses[tx.payment_id] = TransactionStatus.COMPLETED
    result = await checker.check_user(user_id=1, payment_id=tx.payment_id)

    assert result.completed == [tx.payment_id]
    assert gateway.calls == 2
    assert process.applied == [(tx.payment_id, TransactionStatus.COMPLETED)]


async def test_another_users_payment_is_refused() -> None:
    theirs = make_tx(user_id=2)
    checker, gateway, process, _ = make_throttled(
        [theirs], {theirs.payment_id: TransactionStatus.COMPLETED}
    )

    with pytest.raises(NotYourPaymentError):
        await checker.check_user(user_id=1, payment_id=theirs.payment_id)
    with pytest.raises(NotYourPaymentError):
        await checker.check_user(user_id=1, payment_id=uuid4())

    assert gateway.calls == 0
    assert process.applied == []


async def test_fast_check_survives_a_dropped_connection() -> None:
    tx = make_tx(age=timedelta(seconds=10))
    checker, gateway, _ = make_checker([tx], {})

    async def broken(payment_id: UUID) -> Optional[TransactionStatus]:
        raise ConnectionError("server disconnected")

    gateway.fetch_transaction_status = broken  # type: ignore[method-assign]
    assert await checker.fast_check(tx.payment_id) == 4
