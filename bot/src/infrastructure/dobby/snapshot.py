from datetime import datetime
from typing import Iterable, NamedTuple
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from src.core.enums import SubscriptionStatus
from src.infrastructure.database.models.subscription import Subscription
from src.infrastructure.database.models.user import User

from .exporter import ExportedUser


class SubscriptionRow(NamedTuple):
    subscription_id: UUID
    status: SubscriptionStatus
    expire_at: datetime
    user_is_blocked: bool


def build_snapshot(rows: Iterable[SubscriptionRow]) -> list[ExportedUser]:
    """Users the config-delivery service should serve.

    Only current subscriptions that are active and belong to users not blocked by an admin.
    Disabled, deleted and blocked users are left out, so the service stops serving them.
    Expired subscriptions stay in the file with their past date: the service checks the date
    itself, which keeps working while the bot is down.
    """
    users = {
        row.subscription_id: ExportedUser(id=row.subscription_id, expires_at=row.expire_at)
        for row in rows
        if row.status == SubscriptionStatus.ACTIVE and not row.user_is_blocked
    }
    return sorted(users.values())


async def load_snapshot(session: AsyncSession) -> list[ExportedUser]:
    stmt = select(
        Subscription.user_remna_id,
        Subscription.status,
        Subscription.expire_at,
        User.is_blocked,
    ).join(User, User.current_subscription_id == Subscription.id)
    result = await session.execute(stmt)
    return build_snapshot(SubscriptionRow(*row) for row in result.all())
