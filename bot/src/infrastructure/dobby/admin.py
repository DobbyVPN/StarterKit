from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal
from typing import Iterable, NamedTuple, Optional, Protocol
from zoneinfo import ZoneInfo

from redis.asyncio import Redis
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from src.core.enums import Currency, PaymentGatewayType, SubscriptionStatus, TransactionStatus
from src.infrastructure.database.models.subscription import Subscription
from src.infrastructure.database.models.transaction import Transaction
from src.infrastructure.database.models.user import User


class SubscriptionPoint(NamedTuple):
    status: SubscriptionStatus
    expire_at: datetime


class Payment(NamedTuple):
    status: TransactionStatus
    is_test: bool
    currency: Currency
    final_amount: Decimal
    paid_at: datetime


@dataclass
class AdminSummary:
    active: int = 0
    revenue_today: dict[Currency, Decimal] = field(default_factory=dict)
    revenue_month: dict[Currency, Decimal] = field(default_factory=dict)


def build_summary(
    subscriptions: Iterable[SubscriptionPoint],
    payments: Iterable[Payment],
    now: datetime,
    timezone: str,
) -> AdminSummary:
    """Active subscriptions and revenue.

    Revenue counts only completed real payments: no free grants, no test payments, nothing
    refunded or canceled. "Today" and "this month" start at midnight in the owner's time zone.
    """
    summary = AdminSummary()
    for sub in subscriptions:
        if sub.status != SubscriptionStatus.ACTIVE or sub.expire_at <= now:
            continue
        summary.active += 1

    local_now = now.astimezone(ZoneInfo(timezone))
    day_start = local_now.replace(hour=0, minute=0, second=0, microsecond=0)
    month_start = day_start.replace(day=1)

    for pay in payments:
        if pay.status != TransactionStatus.COMPLETED or pay.is_test or pay.final_amount <= 0:
            continue
        if pay.paid_at >= month_start:
            summary.revenue_month[pay.currency] = (
                summary.revenue_month.get(pay.currency, Decimal(0)) + pay.final_amount
            )
        if pay.paid_at >= day_start:
            summary.revenue_today[pay.currency] = (
                summary.revenue_today.get(pay.currency, Decimal(0)) + pay.final_amount
            )
    return summary


def month_start_utc(now: datetime, timezone: str) -> datetime:
    local = now.astimezone(ZoneInfo(timezone))
    return local.replace(day=1, hour=0, minute=0, second=0, microsecond=0)


async def load_summary(session: AsyncSession, now: datetime, timezone: str) -> AdminSummary:
    subs = await session.execute(
        select(Subscription.status, Subscription.expire_at)
        .join(User, User.current_subscription_id == Subscription.id)
        .where(Subscription.expire_at > now)
    )
    pays = await session.execute(
        select(Transaction).where(
            Transaction.status == TransactionStatus.COMPLETED,
            Transaction.updated_at >= month_start_utc(now, timezone),
        )
    )
    payments = [
        Payment(
            status=t.status,
            is_test=t.is_test,
            currency=t.currency,
            final_amount=Decimal(str(t.pricing["final_amount"])),
            paid_at=t.updated_at,
        )
        for t in pays.scalars().all()
    ]
    return build_summary((SubscriptionPoint(*r) for r in subs.all()), payments, now, timezone)


def format_amounts(amounts: dict[Currency, Decimal]) -> str:
    if not amounts:
        return "0 ₽"
    return ", ".join(
        f"{value.normalize():f} {currency.symbol}" for currency, value in amounts.items()
    )


# "View as client": per admin, off by default, kept in Redis.


class ClientViewStore(Protocol):
    async def is_on(self, user_id: int) -> bool: ...

    async def set(self, user_id: int, on: bool) -> None: ...


class RedisClientViewStore(ClientViewStore):
    def __init__(self, redis: Redis) -> None:
        self.redis = redis

    @staticmethod
    def _key(user_id: int) -> str:
        return f"dobby:client_view:{user_id}"

    async def is_on(self, user_id: int) -> bool:
        return bool(await self.redis.exists(self._key(user_id)))

    async def set(self, user_id: int, on: bool) -> None:
        if on:
            await self.redis.set(self._key(user_id), "1")
        else:
            await self.redis.delete(self._key(user_id))


def payment_allowed_in_client_view(
    gateway_type: PaymentGatewayType, api_key: Optional[str]
) -> bool:
    """In client view only a YooKassa test shop may take a payment: its key starts with test_."""
    return (
        gateway_type == PaymentGatewayType.YOOKASSA
        and bool(api_key)
        and api_key.startswith(  # type: ignore[union-attr]
            "test_"
        )
    )


class AdminSummaryService:
    def __init__(self, session: AsyncSession, timezone: str) -> None:
        self.session = session
        self.timezone = timezone

    async def get(self, now: datetime) -> AdminSummary:
        return await load_summary(self.session, now, self.timezone)


def gift_action(current_active_until: Optional[datetime], is_unlimited: bool, days: int) -> str:
    """How "Give myself a subscription" applies: "extend" adds the days to an active
    subscription (the key stays), "set" issues a new one (none, expired, or "forever")."""
    if days > 0 and current_active_until is not None and not is_unlimited:
        return "extend"
    return "set"
