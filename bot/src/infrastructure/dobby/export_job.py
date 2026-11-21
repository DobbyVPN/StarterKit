import hashlib
import time
from contextlib import AbstractAsyncContextManager
from datetime import datetime, timezone
from typing import Awaitable, Callable, Optional, Protocol, Sequence

from loguru import logger
from redis.asyncio import Redis

from .exporter import ExportedUser, UserExporter, serialize_users


class ExportState(Protocol):
    async def get_last(self) -> tuple[Optional[str], float]: ...

    async def set_last(self, digest: str, exported_at: float) -> None: ...

    def lock(self) -> AbstractAsyncContextManager[object]: ...


class RedisExportState(ExportState):
    HASH_KEY = "dobby:export:hash"
    AT_KEY = "dobby:export:at"
    LOCK_KEY = "dobby:export:lock"

    def __init__(self, redis: Redis) -> None:
        self.redis = redis

    async def get_last(self) -> tuple[Optional[str], float]:
        digest, exported_at = await self.redis.mget(self.HASH_KEY, self.AT_KEY)
        return (
            digest.decode() if isinstance(digest, bytes) else digest,
            float(exported_at) if exported_at else 0.0,
        )

    async def set_last(self, digest: str, exported_at: float) -> None:
        await self.redis.mset({self.HASH_KEY: digest, self.AT_KEY: str(exported_at)})

    def lock(self) -> AbstractAsyncContextManager[object]:
        return self.redis.lock(self.LOCK_KEY, timeout=120, blocking_timeout=60)


def users_digest(users: Sequence[ExportedUser]) -> str:
    fixed = datetime(1970, 1, 1, tzinfo=timezone.utc)
    return hashlib.sha256(serialize_users(users, generated_at=fixed)).hexdigest()


class UserExportJob:
    """Exports the full user list when it changed, or when the last export is too old.

    It runs right after every change and every minute. A failed export is not recorded, so the
    next run retries it; the minute run also catches changes made outside the backend adapter
    (for example an admin blocking a user).
    """

    def __init__(
        self,
        load_users: Callable[[], Awaitable[list[ExportedUser]]],
        exporter: UserExporter,
        state: ExportState,
        force_interval_seconds: int,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self.load_users = load_users
        self.exporter = exporter
        self.state = state
        self.force_interval_seconds = force_interval_seconds
        self.clock = clock

    async def run(self, force: bool = False) -> bool:
        async with self.state.lock():
            users = await self.load_users()
            digest = users_digest(users)
            last_digest, last_at = await self.state.get_last()
            now = self.clock()
            stale = now - last_at >= self.force_interval_seconds

            if not force and not stale and digest == last_digest:
                return False

            content = serialize_users(users, generated_at=datetime.fromtimestamp(now, timezone.utc))
            await self.exporter.export(content)
            await self.state.set_last(digest, now)
            logger.info(f"Exported {len(users)} users to the config-delivery service")
            return True
