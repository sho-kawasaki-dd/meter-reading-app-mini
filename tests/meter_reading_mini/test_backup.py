from __future__ import annotations

import hashlib
import os
import sqlite3
from collections.abc import Callable
from contextlib import closing
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


def test_copy_database_preserves_all_binary_values(tmp_path: Path) -> None:
    source = sqlite3.connect(":memory:", autocommit=True)
    value = bytes(range(256)) * 100
    source.execute("CREATE TABLE sample (value BLOB)")
    source.execute("INSERT INTO sample VALUES (?)", (value,))
    destination_path = tmp_path / "binary.db"
    try:
        copy_database(source, destination_path)
    finally:
        source.close()

    with closing(sqlite3.connect(destination_path)) as destination:
        assert destination.execute("PRAGMA integrity_check").fetchone() == ("ok",)
        assert destination.execute("SELECT value FROM sample").fetchone() == (value,)


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


def test_copy_database_preserves_destination_if_identity_cannot_be_checked(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source = sqlite3.connect(":memory:", autocommit=True)
    destination = tmp_path / "backup.db"

    def reject_fstat(descriptor: int) -> os.stat_result:
        raise PermissionError("コピー先の実体を確認できません")

    monkeypatch.setattr(backup.os, "fstat", reject_fstat)
    try:
        with pytest.raises(PermissionError, match="実体を確認できません") as failure:
            copy_database(source, destination)
        assert destination.exists()
        assert any(str(destination) in note for note in failure.value.__notes__)
    finally:
        source.close()
        destination.unlink(missing_ok=True)


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


def test_copy_database_times_out_on_a_locked_source_and_removes_partial_output(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source_path = tmp_path / "locked.db"
    writer = sqlite3.connect(source_path, autocommit=True, timeout=0)
    writer.execute("CREATE TABLE sample (value INTEGER)")
    writer.execute("BEGIN EXCLUSIVE")
    writer.execute("INSERT INTO sample VALUES (1)")
    source = sqlite3.connect(source_path, autocommit=True, timeout=0)
    destination_path = tmp_path / "backup.db"
    clock_values = iter((0.0, 6.0))
    monkeypatch.setattr(backup, "monotonic", lambda: next(clock_values))
    try:
        with pytest.raises(sqlite3.OperationalError, match="ロック"):
            copy_database(source, destination_path)
    finally:
        source.close()
        writer.execute("ROLLBACK")
        writer.close()
    assert not destination_path.exists()


def test_copy_database_does_not_remove_a_replaced_destination(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source = sqlite3.connect(":memory:", autocommit=True)
    source.execute("CREATE TABLE sample (value TEXT)")
    destination = tmp_path / "backup.db"
    replacement = tmp_path / "unrelated.db"
    replacement.write_bytes(b"unrelated file")
    real_open = os.open
    real_close = os.close
    reservation: int | None = None

    def reserve_destination(path: Path, flags: int, mode: int = 0o777) -> int:
        nonlocal reservation
        descriptor = real_open(path, flags, mode)
        if path == destination:
            reservation = descriptor
        return descriptor

    def replace_after_close(descriptor: int) -> None:
        nonlocal reservation
        real_close(descriptor)
        if descriptor == reservation:
            reservation = None
            os.replace(replacement, destination)

    monkeypatch.setattr(backup.os, "open", reserve_destination)
    monkeypatch.setattr(backup.os, "close", replace_after_close)
    try:
        with pytest.raises(BaseExceptionGroup, match="途中ファイル"):
            copy_database(source, destination)
    finally:
        source.close()
    assert destination.read_bytes() == b"unrelated file"


def test_copy_database_does_not_open_the_reserved_destination_with_sqlite(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source = sqlite3.connect(":memory:", autocommit=True)
    source.execute("CREATE TABLE sample (value TEXT)")
    source.execute("INSERT INTO sample VALUES ('backup')")
    destination = tmp_path / "backup.db"
    unrelated = tmp_path / "unrelated.db"
    real_connect = sqlite3.connect
    with closing(real_connect(unrelated, autocommit=True)) as conn:
        conn.execute("CREATE TABLE important (value TEXT)")
        conn.execute("INSERT INTO important VALUES ('keep')")
    original = unrelated.read_bytes()

    def replace_before_open(path: Path, *, autocommit: bool) -> sqlite3.Connection:
        if path == destination:
            os.replace(unrelated, path)
        return real_connect(path, autocommit=autocommit)

    monkeypatch.setattr(backup.sqlite3, "connect", replace_before_open)
    try:
        copy_database(source, destination)
    finally:
        source.close()

    assert unrelated.read_bytes() == original
    with closing(real_connect(destination, autocommit=True)) as conn:
        assert conn.execute("SELECT value FROM sample").fetchall() == [("backup",)]


def test_copy_database_preserves_sidecars_created_outside_its_workspace(tmp_path: Path) -> None:
    destination = tmp_path / "backup.db"
    sidecar = Path(f"{destination}-wal")

    class ConcurrentSidecarConnection(sqlite3.Connection):
        def backup(
            self,
            target: sqlite3.Connection,
            *,
            pages: int = -1,
            progress: Callable[[int, int, int], object] | None = None,
            name: str = "main",
            sleep: float = 0.250,
        ) -> None:
            sidecar.write_bytes(b"unrelated wal")
            raise sqlite3.OperationalError("injected backup failure")

    source = sqlite3.connect(":memory:", autocommit=True, factory=ConcurrentSidecarConnection)
    try:
        with pytest.raises(sqlite3.OperationalError, match="injected backup failure"):
            copy_database(source, destination)
    finally:
        source.close()

    assert not destination.exists()
    assert sidecar.read_bytes() == b"unrelated wal"


@pytest.mark.parametrize("suffix", ["-wal", "-shm", "-journal"])
def test_copy_database_rejects_sidecars_created_during_successful_backup(
    tmp_path: Path, suffix: str
) -> None:
    destination = tmp_path / "backup.db"
    sidecar = Path(f"{destination}{suffix}")

    class ConcurrentSidecarConnection(sqlite3.Connection):
        def backup(
            self,
            target: sqlite3.Connection,
            *,
            pages: int = -1,
            progress: Callable[[int, int, int], object] | None = None,
            name: str = "main",
            sleep: float = 0.250,
        ) -> None:
            super().backup(target, pages=pages, progress=progress, name=name, sleep=sleep)
            sidecar.write_bytes(b"unrelated sidecar")

    source = sqlite3.connect(":memory:", autocommit=True, factory=ConcurrentSidecarConnection)
    source.execute("CREATE TABLE sample (value TEXT)")
    try:
        with pytest.raises(FileExistsError, match="関連ファイル"):
            copy_database(source, destination)
    finally:
        source.close()

    assert not destination.exists()
    assert sidecar.read_bytes() == b"unrelated sidecar"
    assert not list(tmp_path.glob(".meter-reading-backup-*"))


def test_copy_database_handles_partial_output_writes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source = sqlite3.connect(":memory:", autocommit=True)
    source.execute("CREATE TABLE sample (value TEXT)")
    source.execute("INSERT INTO sample VALUES ('backup')")
    destination = tmp_path / "backup.db"
    real_write = os.write
    calls = 0

    def short_write(descriptor: int, data: bytes | memoryview) -> int:
        nonlocal calls
        calls += 1
        return real_write(descriptor, data[:100])

    monkeypatch.setattr(backup.os, "write", short_write)
    try:
        copy_database(source, destination)
    finally:
        source.close()

    assert calls > 1
    with closing(sqlite3.connect(destination)) as conn:
        assert conn.execute("SELECT value FROM sample").fetchall() == [("backup",)]
    assert not list(tmp_path.glob(".meter-reading-backup-*"))


def test_copy_database_cleans_output_if_write_makes_no_progress(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source = sqlite3.connect(":memory:", autocommit=True)
    source.execute("CREATE TABLE sample (value TEXT)")
    destination = tmp_path / "backup.db"
    monkeypatch.setattr(backup.os, "write", lambda descriptor, data: 0)
    try:
        with pytest.raises(OSError, match="書き込めません"):
            copy_database(source, destination)
    finally:
        source.close()

    assert not destination.exists()
    assert not list(tmp_path.glob(".meter-reading-backup-*"))


def test_copy_database_preserves_unexpected_files_in_staging(
    tmp_path: Path,
) -> None:
    destination = tmp_path / "backup.db"
    unexpected_files: list[Path] = []

    class UnexpectedWorkspaceFileConnection(sqlite3.Connection):
        def backup(
            self,
            target: sqlite3.Connection,
            *,
            pages: int = -1,
            progress: Callable[[int, int, int], object] | None = None,
            name: str = "main",
            sleep: float = 0.250,
        ) -> None:
            super().backup(target, pages=pages, progress=progress, name=name, sleep=sleep)
            row = target.execute("PRAGMA database_list").fetchone()
            unexpected = Path(row[2]).parent / "unrelated.txt"
            unexpected.write_bytes(b"do not delete")
            unexpected_files.append(unexpected)

    source = sqlite3.connect(":memory:", autocommit=True, factory=UnexpectedWorkspaceFileConnection)
    source.execute("CREATE TABLE sample (value TEXT)")
    try:
        with pytest.raises(ExceptionGroup, match="途中ファイル"):
            copy_database(source, destination)
        assert not destination.exists()
        assert len(unexpected_files) == 1
        assert unexpected_files[0].read_bytes() == b"do not delete"
    finally:
        source.close()
        for path in unexpected_files:
            path.unlink()
            path.parent.rmdir()


def test_copy_database_reports_unknown_staging_identity(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source = sqlite3.connect(":memory:", autocommit=True)
    destination = tmp_path / "backup.db"
    original_lstat = Path.lstat

    def reject_staging_lstat(path: Path) -> os.stat_result:
        if path.name.startswith(".meter-reading-backup-"):
            raise PermissionError("一時領域の実体を確認できません")
        return original_lstat(path)

    monkeypatch.setattr(Path, "lstat", reject_staging_lstat)
    try:
        with pytest.raises(PermissionError, match="実体を確認できません") as failure:
            copy_database(source, destination)
        directories = list(tmp_path.glob(".meter-reading-backup-*"))
        assert len(directories) == 1
        assert any(str(directories[0]) in note for note in failure.value.__notes__)
        assert not destination.exists()
    finally:
        source.close()
        for directory in tmp_path.glob(".meter-reading-backup-*"):
            directory.rmdir()


def test_copy_database_refuses_cleanup_if_staging_directory_changes(tmp_path: Path) -> None:
    staging = tmp_path / "staging"
    staging.mkdir()
    snapshot = staging / "snapshot.db"
    snapshot.write_bytes(b"do not delete")
    other = tmp_path / "other"
    other.mkdir()

    with pytest.raises(OSError, match="一時ディレクトリが変更"):
        backup._remove_staging(staging, other.lstat())

    assert snapshot.read_bytes() == b"do not delete"


def test_copy_database_reports_copy_and_staging_close_failures(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source = sqlite3.connect(
        tmp_path / "source.db", autocommit=True, factory=FailingBackupConnection
    )
    destination = tmp_path / "backup.db"
    real_connect = sqlite3.connect

    class CloseFailureConnection(sqlite3.Connection):
        def close(self) -> None:
            super().close()
            raise sqlite3.OperationalError("一時 DB のクローズ失敗")

    def connect_with_close_failure(path: Path, *, autocommit: bool) -> sqlite3.Connection:
        return real_connect(path, autocommit=autocommit, factory=CloseFailureConnection)

    monkeypatch.setattr(backup.sqlite3, "connect", connect_with_close_failure)
    try:
        with pytest.raises(ExceptionGroup, match="コピーと一時 DB のクローズ") as failure:
            copy_database(source, destination)
        assert len(failure.value.exceptions) == 2
        assert "injected backup failure" in str(failure.value.exceptions[0])
        assert "クローズ失敗" in str(failure.value.exceptions[1])
        assert not destination.exists()
        assert not list(tmp_path.glob(".meter-reading-backup-*"))
    finally:
        source.close()


def test_copy_database_removes_partial_output_when_destination_close_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source = sqlite3.connect(":memory:", autocommit=True)
    source.execute("CREATE TABLE sample (value INTEGER)")
    destination = tmp_path / "backup.db"
    real_connect = sqlite3.connect

    class CloseFailureConnection(sqlite3.Connection):
        def close(self) -> None:
            super().close()
            raise sqlite3.OperationalError("クローズ失敗")

    def connect_with_close_failure(path: Path, *, autocommit: bool) -> sqlite3.Connection:
        return real_connect(path, autocommit=autocommit, factory=CloseFailureConnection)

    monkeypatch.setattr(backup.sqlite3, "connect", connect_with_close_failure)
    try:
        with pytest.raises(sqlite3.OperationalError, match="クローズ失敗"):
            copy_database(source, destination)
    finally:
        source.close()
    assert not destination.exists()


def test_copy_database_cleans_output_if_reservation_close_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source = sqlite3.connect(":memory:", autocommit=True)
    destination = tmp_path / "backup.db"
    real_open = os.open
    real_close = os.close
    created_descriptors: set[int] = set()

    def tracked_open(path: Path, flags: int, mode: int = 0o777) -> int:
        descriptor = real_open(path, flags, mode)
        if path == destination:
            created_descriptors.add(descriptor)
        return descriptor

    def failing_close(descriptor: int) -> None:
        real_close(descriptor)
        if descriptor in created_descriptors:
            created_descriptors.remove(descriptor)
            raise OSError("予約ファイルのクローズ失敗")

    monkeypatch.setattr(backup.os, "open", tracked_open)
    monkeypatch.setattr(backup.os, "close", failing_close)
    try:
        with pytest.raises(OSError, match="クローズ失敗"):
            copy_database(source, destination)
    finally:
        source.close()
    assert not destination.exists()


def test_copy_and_cleanup_failures_are_reported_together(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source = sqlite3.connect(
        tmp_path / "source.db", autocommit=True, factory=FailingBackupConnection
    )
    destination = tmp_path / "backup.db"
    real_unlink = Path.unlink

    def deny_unlink(path: Path, missing_ok: bool = False) -> None:
        if path == destination:
            raise PermissionError("途中ファイルの削除を拒否")
        real_unlink(path, missing_ok=missing_ok)

    monkeypatch.setattr(Path, "unlink", deny_unlink)
    try:
        with pytest.raises(ExceptionGroup, match="途中ファイル") as failure:
            copy_database(source, destination)
        assert isinstance(failure.value.exceptions[0], sqlite3.OperationalError)
        assert isinstance(failure.value.exceptions[1], PermissionError)
        assert destination.exists()
    finally:
        source.close()
        real_unlink(destination, missing_ok=True)
