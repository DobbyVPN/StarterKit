from contextlib import asynccontextmanager
from typing import AsyncIterator, Optional
from uuid import UUID

import pytest

from src.infrastructure.dobby.export_job import UserExportJob
from src.infrastructure.dobby.exporter import ExportedUser


class MemoryState:
    def __init__(self) -> None:
        self.digest: Optional[str] = None
        self.at = 0.0

    async def get_last(self) -> tuple[Optional[str], float]:
        return self.digest, self.at

    async def set_last(self, digest: str, exported_at: float) -> None:
        self.digest, self.at = digest, exported_at

    @asynccontextmanager
    async def lock(self) -> AsyncIterator[object]:
        yield object()


class MemoryExporter:
    def __init__(self) -> None:
        self.files: list[bytes] = []
        self.fail = False

    async def export(self, content: bytes) -> None:
        if self.fail:
            raise ConnectionError("sftp down")
        self.files.append(content)


class Clock:
    def __init__(self) -> None:
        self.now = 1_000_000.0

    def __call__(self) -> float:
        return self.now


def make_job(users: list[ExportedUser]) -> tuple[UserExportJob, MemoryExporter, Clock]:
    async def load() -> list[ExportedUser]:
        return list(users)

    exporter, clock = MemoryExporter(), Clock()
    job = UserExportJob(load, exporter, MemoryState(), force_interval_seconds=3600, clock=clock)
    return job, exporter, clock


async def test_exports_only_on_change_or_when_stale() -> None:
    users = [ExportedUser(id=UUID(int=1))]
    job, exporter, clock = make_job(users)

    assert await job.run() is True
    clock.now += 60
    assert await job.run() is False

    users.append(ExportedUser(id=UUID(int=2)))
    assert await job.run() is True

    clock.now += 3600
    assert await job.run() is True
    assert len(exporter.files) == 3


async def test_force_exports_unchanged_list() -> None:
    job, exporter, _ = make_job([])
    await job.run()
    assert await job.run(force=True) is True
    assert len(exporter.files) == 2


async def test_failed_export_is_retried_on_next_run() -> None:
    job, exporter, clock = make_job([ExportedUser(id=UUID(int=1))])
    exporter.fail = True
    with pytest.raises(ConnectionError):
        await job.run()

    exporter.fail = False
    clock.now += 60
    assert await job.run() is True
    assert len(exporter.files) == 1


async def test_expiry_alone_triggers_an_export() -> None:
    # The list shrinks when a date passes, without any database change.
    users = [ExportedUser(id=UUID(int=1))]
    job, exporter, clock = make_job(users)
    await job.run()

    users.clear()
    clock.now += 60
    assert await job.run() is True
    assert b"logins" in exporter.files[-1]
    assert str(UUID(int=1)).encode() not in exporter.files[-1]
