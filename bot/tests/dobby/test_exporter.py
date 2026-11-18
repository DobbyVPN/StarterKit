import os
from datetime import datetime, timezone
from pathlib import Path
from uuid import UUID

import orjson
import pytest

from src.infrastructure.dobby.exporter import ExportedUser, FileUserExporter, serialize_users

A = UUID("00000000-0000-0000-0000-00000000000a")
B = UUID("00000000-0000-0000-0000-00000000000b")
NOW = datetime(2026, 11, 19, 12, 0, tzinfo=timezone.utc)


def test_serialize_sorts_users_and_uses_utc() -> None:
    users = [
        ExportedUser(id=B, expires_at=datetime(2026, 12, 1, tzinfo=timezone.utc)),
        ExportedUser(id=A, expires_at=datetime(2027, 1, 1, tzinfo=timezone.utc)),
    ]
    data = orjson.loads(serialize_users(users, generated_at=NOW))
    assert data["version"] == 1
    assert data["generated_at"] == "2026-11-19T12:00:00Z"
    assert data["users"] == [
        {"id": str(A), "expires_at": "2027-01-01T00:00:00Z"},
        {"id": str(B), "expires_at": "2026-12-01T00:00:00Z"},
    ]


async def test_file_exporter_writes_and_replaces(tmp_path: Path) -> None:
    target = tmp_path / "out" / "users.json"
    exporter = FileUserExporter(target)

    await exporter.export(b"first")
    await exporter.export(b"second")

    assert target.read_bytes() == b"second"
    assert sorted(p.name for p in target.parent.iterdir()) == ["users.json"]


async def test_file_exporter_keeps_old_file_on_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    target = tmp_path / "users.json"
    exporter = FileUserExporter(target)
    await exporter.export(b"old")

    def broken_replace(src: str, dst: str) -> None:
        raise OSError("disk full")

    monkeypatch.setattr(os, "replace", broken_replace)
    with pytest.raises(OSError):
        await exporter.export(b"new")

    assert target.read_bytes() == b"old"
    assert [p.name for p in tmp_path.iterdir()] == ["users.json"]
