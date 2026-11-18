import os
import tempfile
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Protocol, Sequence
from uuid import UUID

import orjson

EXPORT_FORMAT_VERSION = 1


@dataclass(frozen=True, order=True)
class ExportedUser:
    # Permanent id issued by the bot (the subscription UUID), never a Telegram username.
    id: UUID
    expires_at: datetime


def serialize_users(users: Sequence[ExportedUser], generated_at: datetime) -> bytes:
    """Placeholder format until the config-delivery service defines its own."""
    payload = {
        "version": EXPORT_FORMAT_VERSION,
        "generated_at": _iso(generated_at),
        "users": [
            {"id": str(user.id), "expires_at": _iso(user.expires_at)} for user in sorted(users)
        ],
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
