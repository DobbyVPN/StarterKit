from datetime import datetime, timedelta
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
from src.core.enums import SubscriptionStatus, UserNotificationType
from src.core.utils.time import datetime_now
from src.infrastructure.database.models.subscription import Subscription
from src.infrastructure.database.models.user import User

from .config import DEFAULT_REMINDER_HOURS_BEFORE
from .snapshot import is_expired

HOUR = timedelta(hours=1)

# The expiry reminder is still sent this long after expiry (for example after downtime).
EXPIRED_GRACE = 24 * HOUR

EXPIRED = "expired"


def before_stage(hours: int) -> str:
    return f"before_{hours}h"


def stage_for(
    expire_at: datetime, now: datetime, hours_before: Iterable[int] = DEFAULT_REMINDER_HOURS_BEFORE
) -> Optional[str]:
    """The reminder due now: "before_<N>h" or "expired".

    Only the latest due stage counts, so after downtime the bot does not send several
    reminders at once. "expired" starts exactly when the user drops out of the export file.
    """
    if is_expired(expire_at, now):
        return EXPIRED if now - expire_at < EXPIRED_GRACE else None

    left = expire_at - now
    due = [h for h in hours_before if left <= h * HOUR]
    return before_stage(min(due)) if due else None


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
    # Longer than any reminder window, so a key outlives every stage it guards.
    TTL_SECONDS = int(timedelta(days=30).total_seconds())

    def __init__(self, redis: Redis) -> None:
        self.redis = redis

    async def mark_once(self, key: str) -> bool:
        return bool(
            await self.redis.set(f"dobby:reminder:{key}", "1", nx=True, ex=self.TTL_SECONDS)
        )


def reminder_key(row: ReminderRow, stage: str) -> str:
    # The expiry is part of the key: a renewal moves it, so the reminders start over.
    return f"{row.subscription_id}:{int(row.expire_at.timestamp())}:{stage}"


_DAY_TYPES = {
    3: UserNotificationType.EXPIRES_IN_3_DAYS,
    2: UserNotificationType.EXPIRES_IN_2_DAYS,
    1: UserNotificationType.EXPIRES_IN_1_DAY,
}


def build_event(stage: str, row: ReminderRow, user: object) -> UserEvent:
    if stage == EXPIRED:
        return SubscriptionExpiredEvent(user=user, is_trial=row.is_trial)  # type: ignore[arg-type]

    hours = int(stage.removeprefix("before_").removesuffix("h"))
    # remnashop's text says "in N days"; its notification switches exist for 1, 2 and 3 days.
    day = max(1, round(hours / 24))
    return SubscriptionExpiresEvent(
        user=user,  # type: ignore[arg-type]
        is_trial=row.is_trial,
        day=day,
        notification_type=_DAY_TYPES.get(min(day, 3), UserNotificationType.EXPIRES_IN_3_DAYS),
    )


async def load_reminder_rows(
    session: AsyncSession, now: datetime, look_ahead: timedelta
) -> list[ReminderRow]:
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
            Subscription.expire_at > now - EXPIRED_GRACE,
            Subscription.expire_at <= now + look_ahead,
        )
    )
    result = await session.execute(stmt)
    return [ReminderRow(*row) for row in result.all()]


class RenewalReminders:
    """Sends renewal reminders: before expiry (by default 3 days and 1 day) and at expiry.

    The Remnawave panel used to trigger these by webhooks.

    Runs every minute. Each reminder goes out at most once per subscription period: what was
    sent is recorded in Redis (persistent), keyed by subscription and expiry. remnashop's
    notification settings and texts apply as before (they are handled by the event bus).
    """

    def __init__(
        self,
        load_rows: Callable[[datetime, timedelta], Awaitable[Iterable[ReminderRow]]],
        user_dao: UserDao,
        event_publisher: EventPublisher,
        log: ReminderLog,
        hours_before: Iterable[int] = DEFAULT_REMINDER_HOURS_BEFORE,
        now: Callable[[], datetime] = datetime_now,
    ) -> None:
        self.hours_before = tuple(sorted(set(hours_before), reverse=True))
        self.load_rows = load_rows
        self.user_dao = user_dao
        self.event_publisher = event_publisher
        self.log = log
        self.now = now

    async def run(self) -> int:
        now = self.now()
        sent = 0
        look_ahead = max(self.hours_before, default=0) * HOUR
        for row in await self.load_rows(now, look_ahead):
            stage = stage_for(row.expire_at, now, self.hours_before)
            if stage is None or not await self.log.mark_once(reminder_key(row, stage)):
                continue
            user = await self.user_dao.get_by_id(row.user_id)
            if user is None:
                continue
            await self.event_publisher.publish(build_event(stage, row, user))
            logger.info(f"Sent '{stage}' reminder for subscription '{row.subscription_id}'")
            sent += 1
        return sent
