from datetime import datetime, timezone
from uuid import UUID

from src.core.enums import SubscriptionStatus
from src.infrastructure.dobby.exporter import ExportedUser
from src.infrastructure.dobby.snapshot import SubscriptionRow, build_snapshot

EXP = datetime(2026, 12, 1, tzinfo=timezone.utc)
PAST = datetime(2026, 1, 1, tzinfo=timezone.utc)


def _id(n: int) -> UUID:
    return UUID(int=n)


def test_only_active_unblocked_users_are_exported() -> None:
    rows = [
        SubscriptionRow(_id(1), SubscriptionStatus.ACTIVE, EXP, False),
        SubscriptionRow(_id(2), SubscriptionStatus.DISABLED, EXP, False),
        SubscriptionRow(_id(3), SubscriptionStatus.DELETED, EXP, False),
        SubscriptionRow(_id(4), SubscriptionStatus.ACTIVE, EXP, True),
        SubscriptionRow(_id(5), SubscriptionStatus.ACTIVE, PAST, False),
    ]
    assert build_snapshot(rows) == [
        ExportedUser(id=_id(1), expires_at=EXP),
        ExportedUser(id=_id(5), expires_at=PAST),
    ]
