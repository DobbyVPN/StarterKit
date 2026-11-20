import os
from datetime import datetime, timedelta, timezone
from typing import Iterable, Optional
from uuid import UUID, uuid4

import pytest
from redis.asyncio import Redis

from src.application.dto import UserDto
from src.application.events import SubscriptionExpiredEvent, SubscriptionExpiresEvent
from src.application.events.user import SubscriptionExpiredAgoEvent
from src.core.config import AppConfig
from src.core.enums import SubscriptionStatus, UserNotificationType
from src.infrastructure.dobby.reminders import (
    RedisReminderLog,
    ReminderRow,
    RenewalReminders,
    Stage,
    reminder_key,
    stage_for,
)
from src.infrastructure.dobby.snapshot import SubscriptionRow, build_snapshot

NOW = datetime(2026, 11, 20, 12, 0, tzinfo=timezone.utc)
H = timedelta(hours=1)
USER = UserDto(id=7, telegram_id=1001, name="Test")


class MemoryLog:
    def __init__(self) -> None:
        self.keys: set[str] = set()

    async def mark_once(self, key: str) -> bool:
        if key in self.keys:
            return False
        self.keys.add(key)
        return True


class Publisher:
    def __init__(self) -> None:
        self.events: list[object] = []

    async def publish(self, event: object) -> None:
        self.events.append(event)


class Users:
    async def get_by_id(self, user_id: int) -> Optional[UserDto]:
        return USER if user_id == USER.id else None


def job(
    rows: list[ReminderRow], log: MemoryLog, publisher: Publisher, now: datetime = NOW
) -> RenewalReminders:
    async def load(at: datetime) -> Iterable[ReminderRow]:
        return list(rows)

    return RenewalReminders(load, Users(), publisher, log, now=lambda: now)  # type: ignore[arg-type]


def row(expire_at: datetime, is_trial: bool = False, sub: Optional[UUID] = None) -> ReminderRow:
    return ReminderRow(USER.id, sub or uuid4(), expire_at, is_trial)


@pytest.mark.parametrize(
    ("left", "stage"),
    [
        (80 * H, None),
        (72 * H, Stage.IN_3_DAYS),
        (49 * H, Stage.IN_3_DAYS),
        (48 * H, Stage.IN_2_DAYS),
        (25 * H, Stage.IN_2_DAYS),
        (24 * H, Stage.IN_1_DAY),
        (timedelta(minutes=1), Stage.IN_1_DAY),
        (timedelta(0), Stage.EXPIRED),
        (-23 * H, Stage.EXPIRED),
        (-24 * H, Stage.EXPIRED_1_DAY_AGO),
        (-47 * H, Stage.EXPIRED_1_DAY_AGO),
        (-48 * H, None),
    ],
)
def test_each_threshold(left: timedelta, stage: Optional[Stage]) -> None:
    assert stage_for(NOW + left, NOW) == stage


@pytest.mark.parametrize("left", [timedelta(seconds=1), timedelta(0), -timedelta(seconds=1)])
def test_expired_reminder_matches_the_export_cut_off(left: timedelta) -> None:
    sub = uuid4()
    exported = build_snapshot(
        [SubscriptionRow(sub, SubscriptionStatus.ACTIVE, NOW + left, False)], now=NOW
    )
    expired_reminder = stage_for(NOW + left, NOW) == Stage.EXPIRED
    assert expired_reminder == (exported == [])


async def test_events_carry_remnashop_texts_and_settings() -> None:
    publisher = Publisher()
    rows = [
        row(NOW + 60 * H),
        row(NOW + 30 * H),
        row(NOW + 2 * H, is_trial=True),
        row(NOW - H),
        row(NOW - 30 * H),
    ]
    assert await job(rows, MemoryLog(), publisher).run() == 5

    in3, in2, in1, expired, ago = publisher.events
    assert isinstance(in3, SubscriptionExpiresEvent) and in3.day == 3
    assert in3.notification_type == UserNotificationType.EXPIRES_IN_3_DAYS
    assert isinstance(in2, SubscriptionExpiresEvent) and in2.day == 2
    assert in2.notification_type == UserNotificationType.EXPIRES_IN_2_DAYS
    assert isinstance(in1, SubscriptionExpiresEvent) and in1.day == 1 and in1.is_trial
    assert isinstance(expired, SubscriptionExpiredEvent)
    assert isinstance(ago, SubscriptionExpiredAgoEvent)


async def test_no_duplicates_across_runs_and_restarts() -> None:
    log, publisher = MemoryLog(), Publisher()  # the log is Redis: it survives restarts
    rows = [row(NOW + 30 * H)]

    await job(rows, log, publisher).run()
    await job(rows, log, publisher, now=NOW + timedelta(minutes=1)).run()
    await job(rows, log, Publisher()).run()  # a fresh process after a restart

    assert len(publisher.events) == 1


async def test_each_stage_once_as_time_passes() -> None:
    log, publisher = MemoryLog(), Publisher()
    expire_at = NOW + 72 * H
    rows = [row(expire_at)]
    for minutes in range(0, 5 * 24 * 60, 30):
        await job(rows, log, publisher, now=NOW + timedelta(minutes=minutes)).run()

    kinds = [(type(e).__name__, getattr(e, "day", None)) for e in publisher.events]
    assert kinds == [
        ("SubscriptionExpiresEvent", 3),
        ("SubscriptionExpiresEvent", 2),
        ("SubscriptionExpiresEvent", 1),
        ("SubscriptionExpiredEvent", None),
        ("SubscriptionExpiredAgoEvent", 1),
    ]


async def test_renewal_starts_the_reminders_over() -> None:
    log, publisher = MemoryLog(), Publisher()
    sub = uuid4()
    await job([row(NOW + 20 * H, sub=sub)], log, publisher).run()

    # Renewed by 30 days, later the new expiry is 20 h away again.
    later = NOW + 30 * 24 * H
    await job([row(later + 20 * H, sub=sub)], log, publisher, now=later).run()

    assert len(publisher.events) == 2


async def test_only_latest_stage_after_downtime() -> None:
    publisher = Publisher()
    await job([row(NOW + 5 * H)], MemoryLog(), publisher).run()
    assert len(publisher.events) == 1
    assert getattr(publisher.events[0], "day") == 1


def test_key_depends_on_subscription_expiry_and_stage() -> None:
    sub = uuid4()
    a = reminder_key(row(NOW, sub=sub), Stage.IN_1_DAY)
    assert a != reminder_key(row(NOW + H, sub=sub), Stage.IN_1_DAY)
    assert a != reminder_key(row(NOW, sub=sub), Stage.EXPIRED)
    assert a == reminder_key(row(NOW, sub=sub), Stage.IN_1_DAY)


@pytest.mark.skipif(os.getenv("DOBBY_DB_TESTS") != "1", reason="needs Redis")
async def test_redis_log_records_once() -> None:
    redis = Redis.from_url(AppConfig.get().redis.dsn)
    try:
        log = RedisReminderLog(redis)
        key = f"test:{uuid4()}"
        assert await log.mark_once(key) is True
        assert await RedisReminderLog(redis).mark_once(key) is False  # another process
        assert await redis.ttl(f"dobby:reminder:{key}") > 0
    finally:
        await redis.aclose()
