from datetime import datetime
from typing import Iterable, NamedTuple
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from src.core.enums import SubscriptionStatus
from src.core.utils.time import datetime_now
from src.infrastructure.database.models.subscription import Subscription
from src.infrastructure.database.models.user import User

from .exporter import ExportedUser


def is_expired(expire_at: datetime, now: datetime) -> bool:
    """The single cut-off: an expired user leaves the export and gets the "expired" reminder."""
    return expire_at <= now


class SubscriptionRow(NamedTuple):
    subscription_id: UUID
    status: SubscriptionStatus
    expire_at: datetime
    user_is_blocked: bool


def build_snapshot(rows: Iterable[SubscriptionRow], now: datetime) -> list[ExportedUser]:
    """Logins the config-delivery service should serve.

    Only current subscriptions that are active, not expired, and belong to users not blocked by
    an admin. Everyone else is left out, so the service removes them. The service does not
    check dates: the bot cuts expired users off by leaving them out of the next export, which
    the per-minute export job picks up even when nothing changed in the database.
    """
    users = {
        ExportedUser(id=row.subscription_id)
        for row in rows
        if row.status == SubscriptionStatus.ACTIVE
        and not row.user_is_blocked
        and not is_expired(row.expire_at, now)
    }
    return sorted(users)


async def load_snapshot(session: AsyncSession) -> list[ExportedUser]:
    stmt = select(
        Subscription.user_remna_id,
        Subscription.status,
        Subscription.expire_at,
        User.is_blocked,
    ).join(User, User.current_subscription_id == Subscription.id)
    result = await session.execute(stmt)
    return build_snapshot((SubscriptionRow(*row) for row in result.all()), now=datetime_now())
