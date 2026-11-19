import os
import tempfile
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Protocol, Sequence
from uuid import UUID

import orjson

EXPORT_FORMAT_VERSION = 2


@dataclass(frozen=True, order=True)
class ExportedUser:
    # Login: the permanent id the bot issues to a user, never a Telegram username.
    # The subscription link is built from it, so it must not change.
    id: UUID


def serialize_users(users: Sequence[ExportedUser], generated_at: datetime) -> bytes:
    """The full list of logins to serve. Logins missing from the list are removed.

    No dates: the bot decides who is served, including the cut-off at expiry.
    The JSON wrapper is a placeholder until the exact file syntax is agreed.
    """
    payload = {
        "version": EXPORT_FORMAT_VERSION,
        "generated_at": _iso(generated_at),
        "logins": [str(user.id) for user in sorted(users)],
    }
    return orjson.dumps(payload, option=orjson.OPT_INDENT_2)


def _iso(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


class UserExporter(Protocol):
    """Hands the full user list to the config-delivery service. Must replace, not append."""

    async def export(self, content: bytes) -> None: ...


class FileUserExporter(UserExporter):
    """Writes the file to a local path atomically: temp file in the same dir, fsync, rename."""

    def __init__(self, path: Path) -> None:
        self.path = path

    async def export(self, content: bytes) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp_name = tempfile.mkstemp(prefix=f".{self.path.name}.", dir=self.path.parent)
        try:
            with os.fdopen(fd, "wb") as tmp:
                tmp.write(content)
                tmp.flush()
                os.fsync(tmp.fileno())
            os.chmod(tmp_name, 0o644)
            os.replace(tmp_name, self.path)
        except BaseException:
            Path(tmp_name).unlink(missing_ok=True)
            raise
