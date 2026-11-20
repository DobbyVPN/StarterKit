from datetime import datetime, timedelta
from enum import StrEnum
from typing import Awaitable, Callable, Iterable, NamedTuple, Optional, Protocol
from uuid import UUID

from loguru import logger
from redis.asyncio import Redis
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from src.application.common import EventPublisher
from src.application.common.dao import UserDao
from src.application.events import SubscriptionExpiredEvent, SubscriptionExpiresEvent
from src.application.events.base import UserEvent
from src.application.events.user import SubscriptionExpiredAgoEvent
from src.core.enums import SubscriptionStatus, UserNotificationType
from src.core.utils.time import datetime_now
from src.infrastructure.database.models.subscription import Subscription
from src.infrastructure.database.models.user import User

from .snapshot import is_expired

HOUR = timedelta(hours=1)


class Stage(StrEnum):
    IN_3_DAYS = "in_3_days"
    IN_2_DAYS = "in_2_days"
    IN_1_DAY = "in_1_day"
    EXPIRED = "expired"
    EXPIRED_1_DAY_AGO = "expired_1_day_ago"


# How far around expiry reminders are looked at; older expiries are not reminded about.
LOOK_AHEAD = 72 * HOUR
LOOK_BACK = 48 * HOUR


def stage_for(expire_at: datetime, now: datetime) -> Optional[Stage]:
    """The reminder due now. Only the latest stage counts: after downtime the bot does not
    send several reminders at once. "Expired" starts exactly when the user drops out of the
    export file."""
    if is_expired(expire_at, now):
        overdue = now - expire_at
        if overdue < 24 * HOUR:
            return Stage.EXPIRED
        if overdue < LOOK_BACK:
            return Stage.EXPIRED_1_DAY_AGO
        return None

    left = expire_at - now
    if left > LOOK_AHEAD:
        return None
    if left > 48 * HOUR:
        return Stage.IN_3_DAYS
    if left > 24 * HOUR:
        return Stage.IN_2_DAYS
    return Stage.IN_1_DAY


class ReminderRow(NamedTuple):
    user_id: int
    subscription_id: UUID
    expire_at: datetime
    is_trial: bool


class ReminderLog(Protocol):
    async def mark_once(self, key: str) -> bool:
        """Records the key; True only the first time."""
        ...


class RedisReminderLog(ReminderLog):
    # Longer than the whole reminder window, so a key outlives every stage it guards.
    TTL_SECONDS = int((LOOK_AHEAD + LOOK_BACK + 24 * HOUR).total_seconds())

    def __init__(self, redis: Redis) -> None:
        self.redis = redis

    async def mark_once(self, key: str) -> bool:
        return bool(
            await self.redis.set(f"dobby:reminder:{key}", "1", nx=True, ex=self.TTL_SECONDS)
        )


def reminder_key(row: ReminderRow, stage: Stage) -> str:
    # The expiry is part of the key: a renewal moves it, so the reminders start over.
    return f"{row.subscription_id}:{int(row.expire_at.timestamp())}:{stage}"


def build_event(stage: Stage, row: ReminderRow, user: object) -> UserEvent:
    match stage:
        case Stage.IN_3_DAYS | Stage.IN_2_DAYS | Stage.IN_1_DAY:
            day, notification_type = {
                Stage.IN_3_DAYS: (3, UserNotificationType.EXPIRES_IN_3_DAYS),
                Stage.IN_2_DAYS: (2, UserNotificationType.EXPIRES_IN_2_DAYS),
                Stage.IN_1_DAY: (1, UserNotificationType.EXPIRES_IN_1_DAY),
            }[stage]
            return SubscriptionExpiresEvent(
                user=user,  # type: ignore[arg-type]
                is_trial=row.is_trial,
                day=day,
                notification_type=notification_type,
            )
        case Stage.EXPIRED:
            return SubscriptionExpiredEvent(user=user, is_trial=row.is_trial)  # type: ignore[arg-type]
        case Stage.EXPIRED_1_DAY_AGO:
            return SubscriptionExpiredAgoEvent(user=user, is_trial=row.is_trial, day=1)  # type: ignore[arg-type]


async def load_reminder_rows(session: AsyncSession, now: datetime) -> list[ReminderRow]:
    """Current, active subscriptions of users who are not blocked, near their expiry."""
    stmt = (
        select(
            Subscription.user_id,
            Subscription.user_remna_id,
            Subscription.expire_at,
            Subscription.is_trial,
        )
        .join(User, User.current_subscription_id == Subscription.id)
        .where(
            Subscription.status == SubscriptionStatus.ACTIVE,
            User.is_blocked.is_(False),
            User.is_bot_blocked.is_(False),
            Subscription.expire_at > now - LOOK_BACK,
            Subscription.expire_at <= now + LOOK_AHEAD,
        )
    )
    result = await session.execute(stmt)
    return [ReminderRow(*row) for row in result.all()]


class RenewalReminders:
    """Sends the renewal reminders the Remnawave panel used to trigger by webhooks.

    Runs every minute. Each reminder goes out at most once per subscription period: what was
    sent is recorded in Redis (persistent), keyed by subscription and expiry. remnashop's
    notification settings and texts apply as before (they are handled by the event bus).
    """

    def __init__(
        self,
        load_rows: Callable[[datetime], Awaitable[Iterable[ReminderRow]]],
        user_dao: UserDao,
        event_publisher: EventPublisher,
        log: ReminderLog,
        now: Callable[[], datetime] = datetime_now,
    ) -> None:
        self.load_rows = load_rows
        self.user_dao = user_dao
        self.event_publisher = event_publisher
        self.log = log
        self.now = now

    async def run(self) -> int:
        now = self.now()
        sent = 0
        for row in await self.load_rows(now):
            stage = stage_for(row.expire_at, now)
            if stage is None or not await self.log.mark_once(reminder_key(row, stage)):
                continue
            user = await self.user_dao.get_by_id(row.user_id)
            if user is None:
                continue
            await self.event_publisher.publish(build_event(stage, row, user))
            logger.info(f"Sent '{stage}' reminder for subscription '{row.subscription_id}'")
            sent += 1
        return sent
