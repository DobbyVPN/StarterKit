from datetime import datetime, timedelta, timezone
from uuid import UUID

from src.core.enums import SubscriptionStatus
from src.infrastructure.dobby.exporter import ExportedUser
from src.infrastructure.dobby.snapshot import SubscriptionRow, build_snapshot

NOW = datetime(2026, 11, 20, 12, 0, tzinfo=timezone.utc)
LATER = NOW + timedelta(days=30)


def _id(n: int) -> UUID:
    return UUID(int=n)


def test_only_active_unexpired_unblocked_users_are_exported() -> None:
    rows = [
        SubscriptionRow(_id(1), SubscriptionStatus.ACTIVE, LATER, False),
        SubscriptionRow(_id(2), SubscriptionStatus.DISABLED, LATER, False),
        SubscriptionRow(_id(3), SubscriptionStatus.DELETED, LATER, False),
        SubscriptionRow(_id(4), SubscriptionStatus.ACTIVE, LATER, True),
        SubscriptionRow(_id(5), SubscriptionStatus.ACTIVE, NOW - timedelta(seconds=1), False),
        SubscriptionRow(_id(6), SubscriptionStatus.ACTIVE, NOW, False),
    ]
    assert build_snapshot(rows, now=NOW) == [ExportedUser(id=_id(1))]


def test_user_drops_out_once_the_date_passes() -> None:
    rows = [SubscriptionRow(_id(1), SubscriptionStatus.ACTIVE, LATER, False)]
    assert build_snapshot(rows, now=NOW) == [ExportedUser(id=_id(1))]
    assert build_snapshot(rows, now=LATER + timedelta(minutes=1)) == []
