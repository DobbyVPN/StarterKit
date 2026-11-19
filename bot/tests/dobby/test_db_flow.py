"""End-to-end: remnashop use cases on a real Postgres -> DobbyBackend -> exported file.

Runs only when DOBBY_DB_TESTS=1, inside the compose network with migrations applied:
    see docs/development.md in the repository root.
"""

# Imports are inside the tests: loading the app modules needs real-looking bot settings,
# which only the DB test run provides.
# ruff: noqa: PLC0415

import os
import random
from datetime import timedelta
from pathlib import Path
from typing import AsyncIterator

import orjson
import pytest
from dishka import AsyncContainer, Provider, Scope, make_async_container, provide

pytestmark = pytest.mark.skipif(os.getenv("DOBBY_DB_TESTS") != "1", reason="needs Postgres")


class NoopTrigger:
    async def request(self) -> None:
        return None


class RecordingPublisher:
    """Keeps events instead of sending Telegram notifications."""

    def __init__(self) -> None:
        self.events: list[object] = []

    async def publish(self, event: object) -> None:
        self.events.append(event)


@pytest.fixture
async def container(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> AsyncIterator[AsyncContainer]:
    monkeypatch.setenv("DOBBY_EXPORT_PATH", str(tmp_path / "users.json"))
    monkeypatch.setenv("DOBBY_SUBSCRIPTION_URL_TEMPLATE", "https://sub.example.invalid/{id}")

    from aiogram_dialog import BgManagerFactory

    from src.application.common import EventPublisher
    from src.core.config import AppConfig
    from src.infrastructure.di.providers import get_taskiq_providers
    from src.infrastructure.dobby.backend import ExportTrigger

    class TestOverrides(Provider):
        scope = Scope.APP

        @provide
        def trigger(self) -> ExportTrigger:
            return NoopTrigger()

        @provide
        def publisher(self) -> EventPublisher:
            return RecordingPublisher()  # type: ignore[return-value]

    container = make_async_container(
        *get_taskiq_providers(),
        TestOverrides(),
        context={AppConfig: AppConfig.get(), BgManagerFactory: object()},
        skip_validation=True,
    )
    yield container
    await container.close()


async def export(container: AsyncContainer) -> set[str]:
    from src.infrastructure.dobby.config import DobbyConfig
    from src.infrastructure.dobby.export_job import UserExportJob

    async with container(scope=Scope.REQUEST) as request:
        job = await request.get(UserExportJob)
        await job.run(force=True)
        path = (await request.get(DobbyConfig)).export_path
    return set(orjson.loads(path.read_bytes())["logins"])


async def take_trial(container: AsyncContainer, user_id: int) -> str:
    """Grants a 3-day trial through remnashop's own use case; returns the login."""
    from src.application.common.dao import SubscriptionDao, UserDao
    from src.application.common.uow import UnitOfWork
    from src.application.dto import PlanSnapshotDto
    from src.application.use_cases.subscription.commands.purchase import (
        ActivateTrialSubscription,
        ActivateTrialSubscriptionDto,
    )
    from src.core.enums import PlanType

    plan = PlanSnapshotDto(
        id=0,
        name="Trial",
        type=PlanType.UNLIMITED,
        traffic_limit=0,
        device_limit=0,
        duration=3,
        is_trial=True,
    )
    async with container(scope=Scope.REQUEST) as request:
        uow, user_dao = await request.get(UnitOfWork), await request.get(UserDao)
        async with uow:
            await user_dao.set_trial_available(user_id, True)
            await uow.commit()
        user = await user_dao.get_by_id(user_id)
        assert user is not None
        await (await request.get(ActivateTrialSubscription)).system(
            ActivateTrialSubscriptionDto(user=user, plan=plan)
        )
        sub = await (await request.get(SubscriptionDao)).get_current(user_id)
        assert sub is not None
        assert sub.url == f"https://sub.example.invalid/{sub.user_remna_id}"
        return str(sub.user_remna_id)


async def test_subscription_lifecycle_reaches_the_file(container: AsyncContainer) -> None:
    from src.application.common.dao import SubscriptionDao, UserDao
    from src.application.common.uow import UnitOfWork
    from src.application.dto import UserDto
    from src.application.use_cases.subscription.commands.management import (
        AddSubscriptionDuration,
        AddSubscriptionDurationDto,
        DeleteSubscription,
        ToggleSubscriptionStatus,
    )
    from src.core.utils.time import datetime_now

    telegram_id = random.randint(10**12, 10**13)

    async with container(scope=Scope.REQUEST) as request:
        uow, user_dao = await request.get(UnitOfWork), await request.get(UserDao)
        async with uow:
            user = await user_dao.create(
                UserDto(telegram_id=telegram_id, name="Test", referral_code=f"t{telegram_id}")
            )
            await uow.commit()

    async def toggle_block() -> None:
        async with container(scope=Scope.REQUEST) as request:
            uow, user_dao = await request.get(UnitOfWork), await request.get(UserDao)
            async with uow:
                await user_dao.toggle_blocked_status(user.id)
                await uow.commit()

    try:
        # Create: a new user takes the trial and gets a login.
        login = await take_trial(container, user.id)
        assert login in await export(container)

        # Update: an admin adds 10 days. Same login.
        async with container(scope=Scope.REQUEST) as request:
            add = await request.get(AddSubscriptionDuration)
            await add.system(AddSubscriptionDurationDto(user_id=user.id, days=10))
            sub = await (await request.get(SubscriptionDao)).get_current(user.id)
            assert sub is not None and str(sub.user_remna_id) == login
        assert login in await export(container)

        # Disable and enable again.
        async with container(scope=Scope.REQUEST) as request:
            await (await request.get(ToggleSubscriptionStatus)).system(user.id)
        assert login not in await export(container)
        async with container(scope=Scope.REQUEST) as request:
            await (await request.get(ToggleSubscriptionStatus)).system(user.id)
        assert login in await export(container)

        # Block the user in the bot, then unblock.
        await toggle_block()
        assert login not in await export(container)
        await toggle_block()
        assert login in await export(container)

        # Expire: the date passes, the login drops out of the file.
        async with container(scope=Scope.REQUEST) as request:
            uow = await request.get(UnitOfWork)
            subscription_dao = await request.get(SubscriptionDao)
            async with uow:
                sub = await subscription_dao.get_current(user.id)
                assert sub is not None
                sub.expire_at = datetime_now() - timedelta(minutes=1)
                await subscription_dao.update(sub)
                await uow.commit()
        assert login not in await export(container)

        # A new subscription after expiry keeps the login.
        assert await take_trial(container, user.id) == login
        assert login in await export(container)

        # Admin deletes the subscription: out of the file.
        async with container(scope=Scope.REQUEST) as request:
            await (await request.get(DeleteSubscription)).system(user.id)
        assert login not in await export(container)

        # A new subscription after delete keeps the login too.
        assert await take_trial(container, user.id) == login
        assert login in await export(container)
    finally:
        async with container(scope=Scope.REQUEST) as request:
            uow, user_dao = await request.get(UnitOfWork), await request.get(UserDao)
            async with uow:
                await user_dao.delete(user.id)
                await uow.commit()
