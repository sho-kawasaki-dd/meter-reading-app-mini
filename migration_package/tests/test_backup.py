from __future__ import annotations

import hashlib
import sqlite3
from collections.abc import Callable
from pathlib import Path

import pytest

from meter_reading_mini.db import backup
from meter_reading_mini.db.backup import check_sqlite_integrity, copy_database
from meter_reading_mini.db.connection import connect


class FailingBackupConnection(sqlite3.Connection):
    def backup(
        self,
        target: sqlite3.Connection,
        *,
        pages: int = -1,
        progress: Callable[[int, int, int], object] | None = None,
        name: str = "main",
        sleep: float = 0.250,
    ) -> None:
        target_path = Path(target.execute("PRAGMA database_list").fetchone()[2])
        Path(f"{target_path}-wal").write_bytes(b"incomplete wal")
        Path(f"{target_path}-shm").write_bytes(b"incomplete shm")
        raise sqlite3.OperationalError("injected backup failure")


class FakeResult:
    def __init__(self, rows: list[tuple[str]]) -> None:
        self._rows = rows

    def fetchall(self) -> list[tuple[str]]:
        return self._rows


class FailingIntegrityConnection(sqlite3.Connection):
    def execute(self, sql: str, parameters: object = (), /) -> sqlite3.Cursor:
        if sql == "PRAGMA integrity_check":
            return FakeResult([("page 1: malformed",)])  # type: ignore[return-value]
        return super().execute(sql, parameters)  # type: ignore[arg-type]


def test_check_sqlite_integrity_accepts_valid_database() -> None:
    conn = sqlite3.connect(":memory:", autocommit=True)
    try:
        conn.execute("PRAGMA foreign_keys = ON")
        conn.execute("CREATE TABLE parent (id INTEGER PRIMARY KEY)")
        conn.execute("CREATE TABLE child (parent_id INTEGER REFERENCES parent(id))")
        assert check_sqlite_integrity(conn) == []
    finally:
        conn.close()


def test_check_sqlite_integrity_reports_integrity_and_foreign_key_failures() -> None:
    conn = sqlite3.connect(":memory:", autocommit=True, factory=FailingIntegrityConnection)
    try:
        conn.execute("PRAGMA foreign_keys = OFF")
        conn.execute("CREATE TABLE parent (id INTEGER PRIMARY KEY)")
        conn.execute("CREATE TABLE child (parent_id INTEGER REFERENCES parent(id))")
        conn.execute("INSERT INTO child VALUES (42)")

        assert check_sqlite_integrity(conn) == [
            "SQLite 整合性検査に失敗しました: page 1: malformed",
            "外部キー制約に違反しています: テーブル child、行 1、参照先 parent、制約 0",
        ]
    finally:
        conn.close()


def test_copy_database_includes_committed_data_in_wal(tmp_path: Path) -> None:
    source_path = tmp_path / "source.db"
    source = connect(source_path)
    source.execute("PRAGMA wal_autocheckpoint = 0")
    source.execute("CREATE TABLE sample (value TEXT)")
    source.execute("INSERT INTO sample VALUES ('committed')")
    assert Path(f"{source_path}-wal").stat().st_size > 0

    destination_path = tmp_path / "backup.db"
    try:
        copy_database(source, destination_path)
    finally:
        source.close()

    destination = sqlite3.connect(destination_path)
    try:
        assert destination.execute("SELECT value FROM sample").fetchall() == [("committed",)]
    finally:
        destination.close()


def test_copy_database_refuses_existing_destination(tmp_path: Path) -> None:
    source = sqlite3.connect(":memory:", autocommit=True)
    destination_path = tmp_path / "existing.db"
    destination_path.write_bytes(b"keep this file")
    original_hash = hashlib.sha256(destination_path.read_bytes()).digest()
    try:
        with pytest.raises(FileExistsError):
            copy_database(source, destination_path)
    finally:
        source.close()

    assert hashlib.sha256(destination_path.read_bytes()).digest() == original_hash


def test_copy_database_reports_destination_allocation_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source = sqlite3.connect(":memory:", autocommit=True)
    destination_path = tmp_path / "unwritable" / "backup.db"

    def reject_destination(*args: object, **kwargs: object) -> int:
        raise PermissionError("injected destination permission failure")

    monkeypatch.setattr(backup.os, "open", reject_destination)
    try:
        with pytest.raises(PermissionError, match="injected destination permission failure"):
            copy_database(source, destination_path)
    finally:
        source.close()

    assert not destination_path.exists()


def test_copy_database_rejects_source_transaction_before_creating_destination(
    tmp_path: Path,
) -> None:
    source = sqlite3.connect(":memory:", autocommit=True)
    destination_path = tmp_path / "backup.db"
    source.execute("BEGIN IMMEDIATE")
    try:
        with pytest.raises(RuntimeError):
            copy_database(source, destination_path)
        assert not destination_path.exists()
    finally:
        source.execute("ROLLBACK")
        source.close()


def test_copy_database_cleans_created_files_after_copy_failure(tmp_path: Path) -> None:
    source_path = tmp_path / "source.db"
    source = sqlite3.connect(source_path, autocommit=True, factory=FailingBackupConnection)
    destination_path = tmp_path / "failed.db"
    try:
        with pytest.raises(sqlite3.OperationalError, match="injected backup failure"):
            copy_database(source, destination_path)
    finally:
        source.close()

    assert not Path(f"{destination_path}").exists()
    assert not Path(f"{destination_path}-wal").exists()
    assert not Path(f"{destination_path}-shm").exists()
