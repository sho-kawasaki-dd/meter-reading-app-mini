from __future__ import annotations

import sqlite3
from contextlib import closing
from importlib.resources import files
from pathlib import Path

import pytest

from meter_reading_mini.core.errors import MigrationError
from meter_reading_mini.db import legacy_migration
from meter_reading_mini.db.connection import connect, connect_readonly
from meter_reading_mini.db.legacy_migration import migrate_legacy_database
from meter_reading_mini.db.migrations import SCHEMA_VERSION, create_schema, open_database


class FakeLock:
    def __init__(self, acquired: bool = True) -> None:
        self.acquired = acquired
        self.release_count = 0

    def try_acquire(self, timeout_ms: int = 100) -> bool:
        return self.acquired

    def release(self) -> None:
        self.release_count += 1


def _create_database(path: Path, version: int = SCHEMA_VERSION) -> None:
    if version == SCHEMA_VERSION:
        with closing(open_database(path, path.parent / "backups", backup_timestamp=lambda: "test")):
            pass
        return
    with closing(connect(path)) as connection:
        schema = (
            files("meter_reading_mini.db").joinpath("schema_v5.sql").read_text(encoding="utf-8")
        )
        create_schema(connection, schema_sql=schema, version=version)


def test_migration_copies_wal_data_and_keeps_old_db_and_backups_unchanged(
    tmp_path: Path,
) -> None:
    old_dir = tmp_path / "old"
    new_dir = tmp_path / "new" / "meter_reading"
    old_dir.mkdir()
    new_dir.mkdir(parents=True)
    old_path = old_dir / "meter_reading.db"
    new_path = new_dir / "meter_reading.db"
    _create_database(old_path)
    old_backup = old_dir / "backups" / "existing.db"
    old_backup.parent.mkdir()
    old_backup.write_bytes(b"old backup")

    lock = FakeLock()
    with closing(connect(old_path)) as source:
        source.execute("PRAGMA wal_autocheckpoint = 0")
        source.execute("INSERT INTO staff (name) VALUES (?)", ("旧版担当者",))
        wal_path = Path(f"{old_path}-wal")
        assert wal_path.exists() and wal_path.stat().st_size > 0

        copied = migrate_legacy_database(
            new_path,
            old_path,
            lock_factory=lambda path: lock,
        )

        assert copied
        assert lock.release_count == 1
        assert old_backup.read_bytes() == b"old backup"
        assert source.execute("SELECT name FROM staff").fetchall() == [("旧版担当者",)]
        with closing(connect_readonly(new_path)) as migrated:
            assert migrated.execute("PRAGMA user_version").fetchone() == (SCHEMA_VERSION,)
            assert migrated.execute("SELECT name FROM staff").fetchall() == [("旧版担当者",)]
        assert not migrate_legacy_database(
            new_path,
            old_path,
            lock_factory=lambda path: pytest.fail("既存 DB のため旧ロックを再取得しました"),
        )
    assert not list(new_dir.glob("pre-migrate_*.db"))
    assert not list(new_dir.glob(".meter-reading-migration-*"))


def test_migration_refuses_old_database_when_old_lock_is_held(tmp_path: Path) -> None:
    old_path = tmp_path / "old" / "meter_reading.db"
    new_path = tmp_path / "new" / "meter_reading.db"
    old_path.parent.mkdir()
    new_path.parent.mkdir()
    _create_database(old_path)
    lock = FakeLock(acquired=False)

    with pytest.raises(MigrationError, match="旧版の検針アプリが使用中"):
        migrate_legacy_database(new_path, old_path, lock_factory=lambda path: lock)

    assert not new_path.exists()
    assert lock.release_count == 0


def test_older_supported_schema_is_migrated_without_pre_migration_backup(
    tmp_path: Path,
) -> None:
    old_path = tmp_path / "old" / "meter_reading.db"
    new_path = tmp_path / "new" / "meter_reading" / "meter_reading.db"
    backup_dir = tmp_path / "new" / "backups" / "meter_reading"
    old_path.parent.mkdir()
    new_path.parent.mkdir(parents=True)
    backup_dir.mkdir(parents=True)
    _create_database(old_path, version=5)

    copied = migrate_legacy_database(
        new_path,
        old_path,
        lock_factory=lambda path: FakeLock(),
    )

    assert copied
    with closing(connect_readonly(old_path)) as old:
        assert old.execute("PRAGMA user_version").fetchone() == (5,)
    with closing(connect_readonly(new_path)) as migrated:
        assert migrated.execute("PRAGMA user_version").fetchone() == (SCHEMA_VERSION,)
    assert list(backup_dir.iterdir()) == []


def test_failure_after_copy_keeps_old_database_and_allows_retry(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    old_path = tmp_path / "old" / "meter_reading.db"
    new_path = tmp_path / "new" / "meter_reading.db"
    old_path.parent.mkdir()
    new_path.parent.mkdir()
    _create_database(old_path, version=5)

    def fail_migration(*args: object, **kwargs: object) -> None:
        raise MigrationError("テスト用の移行失敗")

    original_migrate_schema = legacy_migration.migrate_schema
    monkeypatch.setattr(legacy_migration, "migrate_schema", fail_migration)

    with pytest.raises(MigrationError, match="テスト用の移行失敗"):
        migrate_legacy_database(
            new_path,
            old_path,
            lock_factory=lambda path: FakeLock(),
        )

    assert not new_path.exists()
    assert not list(new_path.parent.glob(".meter-reading-migration-*"))
    with closing(connect_readonly(old_path)) as old:
        assert old.execute("PRAGMA user_version").fetchone() == (5,)

    monkeypatch.setattr(legacy_migration, "migrate_schema", original_migrate_schema)
    assert migrate_legacy_database(
        new_path,
        old_path,
        lock_factory=lambda path: FakeLock(),
    )


def test_too_new_old_database_is_not_published(tmp_path: Path) -> None:
    old_path = tmp_path / "old" / "meter_reading.db"
    new_path = tmp_path / "new" / "meter_reading.db"
    old_path.parent.mkdir()
    new_path.parent.mkdir()
    _create_database(old_path)
    with closing(connect(old_path)) as connection:
        connection.execute("PRAGMA user_version = 999")

    with pytest.raises(MigrationError, match="新しい"):
        migrate_legacy_database(
            new_path,
            old_path,
            lock_factory=lambda path: FakeLock(),
        )

    assert not new_path.exists()


def test_zero_version_old_database_is_not_published(tmp_path: Path) -> None:
    old_path = tmp_path / "old" / "meter_reading.db"
    new_path = tmp_path / "new" / "meter_reading.db"
    old_path.parent.mkdir()
    new_path.parent.mkdir()
    _create_database(old_path)
    with closing(connect(old_path)) as connection:
        connection.execute("PRAGMA user_version = 0")

    with pytest.raises(MigrationError, match="サポートされていません"):
        migrate_legacy_database(
            new_path,
            old_path,
            lock_factory=lambda path: FakeLock(),
        )

    assert not new_path.exists()


def test_existing_new_database_is_never_replaced(tmp_path: Path) -> None:
    old_path = tmp_path / "old" / "meter_reading.db"
    new_path = tmp_path / "new" / "meter_reading.db"
    old_path.parent.mkdir()
    new_path.parent.mkdir()
    _create_database(old_path)
    new_path.write_bytes(b"invalid existing destination")

    copied = migrate_legacy_database(
        new_path,
        old_path,
        lock_factory=lambda path: FakeLock(),
    )

    assert not copied
    assert new_path.read_bytes() == b"invalid existing destination"


def test_destination_created_during_migration_is_not_overwritten(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    old_path = tmp_path / "old" / "meter_reading.db"
    new_path = tmp_path / "new" / "meter_reading.db"
    old_path.parent.mkdir()
    new_path.parent.mkdir()
    _create_database(old_path)
    racing_destination = b"created concurrently"

    def create_destination_then_link(source: Path, destination: Path) -> None:
        destination.write_bytes(racing_destination)
        raise FileExistsError(destination)

    monkeypatch.setattr(legacy_migration.os, "link", create_destination_then_link)

    copied = migrate_legacy_database(
        new_path,
        old_path,
        lock_factory=lambda path: FakeLock(),
    )

    assert not copied
    assert new_path.read_bytes() == racing_destination
    assert not list(new_path.parent.glob(".meter-reading-migration-*"))


@pytest.mark.parametrize("source_content", [b"not a database"])
def test_corrupt_old_database_is_not_published(tmp_path: Path, source_content: bytes) -> None:
    old_path = tmp_path / "old" / "meter_reading.db"
    new_path = tmp_path / "new" / "meter_reading.db"
    old_path.parent.mkdir()
    new_path.parent.mkdir()
    old_path.write_bytes(source_content)
    lock = FakeLock()

    with pytest.raises(MigrationError, match="旧 DB"):
        migrate_legacy_database(new_path, old_path, lock_factory=lambda path: lock)

    assert not new_path.exists()
    assert lock.release_count == 1
    assert old_path.read_bytes() == source_content
    assert not list(new_path.parent.glob(".meter-reading-migration-*"))


def test_unreadable_old_database_is_reported_without_creating_new_database(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    old_path = tmp_path / "old" / "meter_reading.db"
    new_path = tmp_path / "new" / "meter_reading.db"
    old_path.parent.mkdir()
    new_path.parent.mkdir()
    _create_database(old_path)
    lock = FakeLock()

    def deny_read(path: Path) -> sqlite3.Connection:
        raise PermissionError("access denied")

    monkeypatch.setattr(legacy_migration, "connect_readonly", deny_read)

    with pytest.raises(MigrationError, match="権限"):
        migrate_legacy_database(new_path, old_path, lock_factory=lambda path: lock)

    assert not new_path.exists()
    assert lock.release_count == 1


def test_leftover_staging_directory_does_not_block_retry(tmp_path: Path) -> None:
    old_path = tmp_path / "old" / "meter_reading.db"
    new_path = tmp_path / "new" / "meter_reading.db"
    old_path.parent.mkdir()
    new_path.parent.mkdir()
    _create_database(old_path)
    abandoned = new_path.parent / ".meter-reading-migration-abandoned"
    abandoned.mkdir()
    (abandoned / "meter_reading.db").write_bytes(b"partial candidate")

    copied = migrate_legacy_database(
        new_path,
        old_path,
        lock_factory=lambda path: FakeLock(),
    )

    assert copied
    assert new_path.is_file()
    assert (abandoned / "meter_reading.db").read_bytes() == b"partial candidate"


def test_missing_legacy_database_is_a_no_op(tmp_path: Path) -> None:
    new_path = tmp_path / "new" / "meter_reading.db"
    new_path.parent.mkdir()

    assert (
        migrate_legacy_database(
            new_path,
            tmp_path / "missing" / "meter_reading.db",
            lock_factory=lambda path: FakeLock(),
        )
        is False
    )
    assert not new_path.exists()
