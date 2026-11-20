from datetime import timedelta
from typing import Optional
from uuid import UUID, uuid4

from src.application.dto import PlanSnapshotDto, SubscriptionDto, UserDto
from src.core.enums import PlanType, SubscriptionStatus
from src.core.utils.time import datetime_now
from src.infrastructure.dobby.backend import DobbyBackend
from src.infrastructure.dobby.links import TemplateLinkProvider


class CountingTrigger:
    def __init__(self, fail: bool = False) -> None:
        self.calls = 0
        self.fail = fail

    async def request(self) -> None:
        self.calls += 1
        if self.fail:
            raise ConnectionError("redis down")


class FakeUserDao:
    def __init__(self, users: list[UserDto]) -> None:
        self.users = users

    async def get_by_id(self, user_id: int) -> Optional[UserDto]:
        return next((u for u in self.users if u.id == user_id), None)

    async def get_by_telegram_id(self, telegram_id: int) -> Optional[UserDto]:
        return next((u for u in self.users if u.telegram_id == telegram_id), None)

    async def get_by_email(self, email: str) -> Optional[UserDto]:
        return None


class FakeSubscriptionDao:
    def __init__(self, subscriptions: list[SubscriptionDto]) -> None:
        self.subscriptions = subscriptions

    async def get_by_remna_id(self, remna_id: UUID) -> Optional[SubscriptionDto]:
        return next((s for s in self.subscriptions if s.user_remna_id == remna_id), None)

    async def get_current(self, user_id: int) -> Optional[SubscriptionDto]:
        return next((s for s in self.subscriptions if s.user_id == user_id and s.id == -1), None)

    async def get_all_by_user(self, user_id: int) -> list[SubscriptionDto]:
        return [s for s in self.subscriptions if s.user_id == user_id]


USER = UserDto(id=7, telegram_id=1001, name="Test")
PLAN = PlanSnapshotDto(
    id=1, name="1 month", type=PlanType.UNLIMITED, traffic_limit=0, device_limit=0, duration=30
)
LINKS = TemplateLinkProvider("https://sub.example.invalid/{id}")


def make_backend(
    subscriptions: Optional[list[SubscriptionDto]] = None, trigger: Optional[CountingTrigger] = None
) -> tuple[DobbyBackend, CountingTrigger]:
    trigger = trigger or CountingTrigger()
    backend = DobbyBackend(
        FakeUserDao([USER]),  # type: ignore[arg-type]
        FakeSubscriptionDao(subscriptions or []),  # type: ignore[arg-type]
        LINKS,
        trigger,
    )
    return backend, trigger


def make_subscription(
    uuid: UUID, status: SubscriptionStatus, current: bool = True, sub_id: int = 1
) -> SubscriptionDto:
    return SubscriptionDto(
        id=-1 if current else sub_id,
        user_id=USER.id,
        user_remna_id=uuid,
        status=status,
        traffic_limit=0,
        device_limit=0,
        traffic_limit_strategy=PLAN.traffic_limit_strategy,
        expire_at=datetime_now() + timedelta(days=5),
        url="",
        plan_snapshot=PLAN,
    )


async def test_create_from_plan_issues_id_link_and_expiry() -> None:
    backend, trigger = make_backend()
    created = await backend.create_user(USER, plan=PLAN)

    assert created.status == SubscriptionStatus.ACTIVE
    assert created.subscription_url == f"https://sub.example.invalid/{created.uuid}"
    assert abs(created.expire_at - (datetime_now() + timedelta(days=30))) < timedelta(minutes=1)
    assert created.telegram_id == USER.telegram_id
    assert trigger.calls == 1


async def test_create_from_existing_subscription_keeps_its_id() -> None:
    uuid = uuid4()
    backend, _ = make_backend()
    created = await backend.create_user(
        USER, subscription=make_subscription(uuid, SubscriptionStatus.ACTIVE)
    )
    assert created.uuid == uuid


async def test_update_keeps_id_and_reflects_disabled_status() -> None:
    uuid = uuid4()
    backend, trigger = make_backend()
    sub = make_subscription(uuid, SubscriptionStatus.DISABLED)

    updated = await backend.update_user(USER, uuid, subscription=sub)

    assert updated.uuid == uuid
    assert updated.status == SubscriptionStatus.DISABLED
    assert updated.expire_at == sub.expire_at
    assert trigger.calls == 1


async def test_every_change_requests_export() -> None:
    backend, trigger = make_backend()
    uuid = uuid4()
    await backend.enable_user(uuid)
    await backend.disable_user(uuid)
    assert await backend.delete_user(uuid) is True
    assert trigger.calls == 3


async def test_failed_trigger_does_not_break_the_change() -> None:
    backend, trigger = make_backend(trigger=CountingTrigger(fail=True))
    created = await backend.create_user(USER, plan=PLAN)
    assert created.uuid
    assert trigger.calls == 1


async def test_reads_come_from_bot_database() -> None:
    uuid = uuid4()
    sub = make_subscription(uuid, SubscriptionStatus.ACTIVE)
    backend, _ = make_backend([sub])

    found = await backend.get_user_by_uuid(uuid)
    assert found is not None
    assert found.expire_at == sub.expire_at
    assert found.subscription_url == f"https://sub.example.invalid/{uuid}"
    assert await backend.get_user_by_uuid(uuid4()) is None

    by_telegram = await backend.get_users_by_telegram_id(USER.telegram_id or 0)
    assert [u.uuid for u in by_telegram] == [uuid]
    assert await backend.get_users_by_telegram_id(999) == []


async def test_panel_only_features_are_empty() -> None:
    backend, _ = make_backend()
    assert await backend.get_devices(uuid4()) == []
    assert await backend.get_squads_available() is False
    assert await backend.get_internal_squads() == []
    assert await backend.get_all_users(limit=10, offset=0) == []


async def test_first_subscription_gets_a_new_login() -> None:
    backend, _ = make_backend()
    first = await backend.create_user(USER, plan=PLAN)
    second_backend, _ = make_backend()
    second = await second_backend.create_user(USER, plan=PLAN)
    assert first.uuid != second.uuid


async def test_new_subscription_reuses_the_users_login() -> None:
    # After expiry or an admin delete the user has no current subscription, only history.
    login = uuid4()
    old = make_subscription(login, SubscriptionStatus.DELETED, current=False)
    backend, _ = make_backend([old])

    created = await backend.create_user(USER, plan=PLAN)

    assert created.uuid == login
    assert created.subscription_url == f"https://sub.example.invalid/{login}"


async def test_lookup_by_login_prefers_the_current_subscription() -> None:
    login = uuid4()
    old = make_subscription(login, SubscriptionStatus.DELETED, current=False)
    current = make_subscription(login, SubscriptionStatus.ACTIVE)
    backend, _ = make_backend([old, current])

    found = await backend.get_user_by_uuid(login)

    assert found is not None
    assert found.status == SubscriptionStatus.ACTIVE


async def test_backend_user_has_the_fields_admin_screens_read() -> None:
    # Attributes remnashop reads from a panel user (admin subscription screen, sync screen).
    login = uuid4()
    backend, _ = make_backend([make_subscription(login, SubscriptionStatus.ACTIVE)])
    found = await backend.get_user_by_uuid(login)
    assert found is not None
    assert found.user_traffic.online_at is None
    assert found.first_connected_at is None
    assert found.used_traffic_bytes == 0
    assert found.updated_at is None
    assert found.active_internal_squads == []
