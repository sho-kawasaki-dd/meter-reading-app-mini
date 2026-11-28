from __future__ import annotations

import hashlib
import json
import os
import shutil
import sqlite3
import subprocess
import sys
import zipfile
from collections.abc import Mapping
from contextlib import closing
from pathlib import Path

import pytest

from building_manager_mini.core.recovery import PendingRecoveryError
from building_manager_mini.db.recovery import (
    RecoveryRecord,
    RecoveryTargetRecord,
    RegisteredDatabase,
    complete_operation,
    mark_operation_applying,
    prepare_operation,
    recover_pending,
)
from meter_reading_mini.db.backup import copy_database


def _create_database(path: Path, value: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with closing(sqlite3.connect(path)) as conn:
        conn.execute("CREATE TABLE item (value TEXT NOT NULL)")
        conn.execute("INSERT INTO item VALUES (?)", (value,))
        conn.commit()


def _read_database(path: Path) -> str:
    with closing(sqlite3.connect(path)) as conn:
        row = conn.execute("SELECT value FROM item").fetchone()
    assert row is not None
    return str(row[0])


def _validate_database(path: Path) -> None:
    with closing(sqlite3.connect(f"{path.as_uri()}?mode=ro", uri=True)) as conn:
        if conn.execute("PRAGMA integrity_check").fetchall() != [("ok",)]:
            raise ValueError("DB の整合性が不正です")
        conn.execute("SELECT value FROM item").fetchall()


def _setup(tmp_path: Path) -> tuple[Path, Path, dict[str, RegisteredDatabase], RecoveryRecord]:
    operation_id = "a" * 32
    safety_root = tmp_path / "safety"
    safety = safety_root / operation_id
    safety.mkdir(parents=True)
    databases = {
        "common": RegisteredDatabase(tmp_path / "common.db", _validate_database),
        "meter-reading": RegisteredDatabase(tmp_path / "meter.db", _validate_database),
        "new": RegisteredDatabase(tmp_path / "new.db", _validate_database),
    }
    targets: list[RecoveryTargetRecord] = []
    for identifier, value in (("common", "共通"), ("meter-reading", "検針")):
        original = databases[identifier].path
        backup = safety / f"{identifier}.db"
        _create_database(original, value)
        with (
            closing(sqlite3.connect(original)) as source,
            closing(sqlite3.connect(backup)) as destination,
        ):
            source.backup(destination)
        data = backup.read_bytes()
        targets.append(
            RecoveryTargetRecord(
                database_id=identifier,
                path=str(original),
                existed=True,
                safety_path=str(backup),
                size=len(data),
                sha256=hashlib.sha256(data).hexdigest(),
            )
        )
        with closing(sqlite3.connect(original)) as conn:
            conn.execute("UPDATE item SET value = '変更後'")
            conn.commit()
    targets.append(
        RecoveryTargetRecord(
            database_id="new",
            path=str(databases["new"].path),
            existed=False,
            safety_path=None,
            size=None,
            sha256=None,
        )
    )
    _create_database(databases["new"].path, "新規")
    pending = tmp_path / "pending.json"
    record = RecoveryRecord(
        format_version=1,
        operation_id=operation_id,
        operation_kind="initial_migration",
        stage="applying",
        automatic_backup_path=str(tmp_path / "automatic.zip"),
        targets=targets,
    )
    pending.write_text(record.model_dump_json(), encoding="utf-8")
    return pending, safety_root, databases, record


def test_missing_record_does_not_create_files(tmp_path: Path) -> None:
    pending = tmp_path / "pending.json"
    assert not recover_pending(pending, tmp_path / "safety", {}, validate_set=lambda: None)
    assert list(tmp_path.iterdir()) == []


def test_recovers_all_targets_and_removes_sidecars(tmp_path: Path) -> None:
    pending, safety, databases, _ = _setup(tmp_path)
    for suffix in ("-wal", "-shm", "-journal"):
        Path(f"{databases['new'].path}{suffix}").write_bytes(b"sidecar")
    validated = False

    def validate_set() -> None:
        nonlocal validated
        assert _read_database(databases["common"].path) == "共通"
        assert _read_database(databases["meter-reading"].path) == "検針"
        assert not databases["new"].path.exists()
        assert pending.exists()
        validated = True

    assert recover_pending(pending, safety, databases, validate_set=validate_set)
    assert validated
    assert not pending.exists()
    assert list(safety.iterdir()) == []
    for suffix in ("-wal", "-shm", "-journal"):
        assert not Path(f"{databases['new'].path}{suffix}").exists()


def test_corrupt_backup_does_not_change_any_target(tmp_path: Path) -> None:
    pending, safety, databases, record = _setup(tmp_path)
    assert record.targets[1].safety_path is not None
    Path(record.targets[1].safety_path).write_bytes(b"corrupted")
    with pytest.raises(PendingRecoveryError):
        recover_pending(pending, safety, databases, validate_set=lambda: None)
    assert pending.exists()
    assert _read_database(databases["common"].path) == "変更後"
    assert _read_database(databases["meter-reading"].path) == "変更後"
    assert _read_database(databases["new"].path) == "新規"


def test_interrupted_recovery_can_be_retried(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    pending, safety, databases, _ = _setup(tmp_path)
    replace = os.replace
    calls = 0

    def fail_second(source: str | os.PathLike[str], dest: str | os.PathLike[str]) -> None:
        nonlocal calls
        calls += 1
        if calls == 2:
            raise OSError("強制中断")
        replace(source, dest)

    with monkeypatch.context() as scoped:
        scoped.setattr(os, "replace", fail_second)
        with pytest.raises(PendingRecoveryError):
            recover_pending(pending, safety, databases, validate_set=lambda: None)
    assert pending.exists()
    assert _read_database(databases["common"].path) == "共通"
    assert _read_database(databases["meter-reading"].path) == "変更後"
    assert recover_pending(pending, safety, databases, validate_set=lambda: None)
    assert _read_database(databases["meter-reading"].path) == "検針"
    assert not databases["new"].path.exists()


def test_failed_final_validation_keeps_record_and_snapshots_for_retry(tmp_path: Path) -> None:
    pending, safety, databases, record = _setup(tmp_path)

    def fail_validation() -> None:
        raise ValueError("DB 間の整合性が不正です")

    with pytest.raises(PendingRecoveryError):
        recover_pending(pending, safety, databases, validate_set=fail_validation)
    assert pending.exists()
    assert all(
        Path(target.safety_path).exists()
        for target in record.targets
        if target.safety_path is not None
    )
    assert recover_pending(pending, safety, databases, validate_set=lambda: None)


def test_unexpected_staging_file_is_preserved_and_recovery_stops(tmp_path: Path) -> None:
    pending, safety, databases, record = _setup(tmp_path)
    directory = tmp_path / f".recovery-{record.operation_id}-common"
    directory.mkdir()
    unexpected = directory / "other"
    unexpected.write_text("保持", encoding="utf-8")
    with pytest.raises(PendingRecoveryError):
        recover_pending(pending, safety, databases, validate_set=lambda: None)
    assert unexpected.read_text(encoding="utf-8") == "保持"
    assert pending.exists()
    assert _read_database(databases["common"].path) == "変更後"


def test_process_exit_during_recovery_can_be_retried_without_staging_leaks(tmp_path: Path) -> None:
    pending, safety, databases, _ = _setup(tmp_path)
    script = """
import os
import sqlite3
import sys
from contextlib import closing
from pathlib import Path
from building_manager_mini.db import recovery

def validate(path):
    with closing(sqlite3.connect(path)) as connection:
        if connection.execute("PRAGMA integrity_check").fetchone() != ("ok",):
            raise ValueError("integrity")
        connection.execute("SELECT value FROM item").fetchall()

databases = {
    "common": recovery.RegisteredDatabase(Path(sys.argv[3]), validate),
    "meter-reading": recovery.RegisteredDatabase(Path(sys.argv[4]), validate),
    "new": recovery.RegisteredDatabase(Path(sys.argv[5]), validate),
}
original_replace = os.replace
def interrupt(source, destination):
    original_replace(source, destination)
    os._exit(73)
recovery.os.replace = interrupt
recovery.recover_pending(Path(sys.argv[1]), Path(sys.argv[2]), databases, validate_set=lambda: None)
"""
    result = subprocess.run(
        [
            sys.executable,
            "-c",
            script,
            str(pending),
            str(safety),
            str(databases["common"].path),
            str(databases["meter-reading"].path),
            str(databases["new"].path),
        ],
        capture_output=True,
        timeout=30,
        check=False,
    )
    assert result.returncode == 73, result.stderr.decode(errors="replace")
    assert pending.exists()
    assert _read_database(databases["common"].path) == "共通"
    assert _read_database(databases["meter-reading"].path) == "変更後"
    assert recover_pending(pending, safety, databases, validate_set=lambda: None)
    assert _read_database(databases["meter-reading"].path) == "検針"
    assert not list(tmp_path.glob(".recovery-*"))


def _create_verified_backup(root: Path, snapshots: Mapping[str, Path]) -> Path:
    destination = root / "automatic.zip"
    with zipfile.ZipFile(destination, "x") as archive:
        for identifier, path in snapshots.items():
            archive.write(path, f"{identifier}.db")
    with zipfile.ZipFile(destination) as archive:
        for identifier, path in snapshots.items():
            assert archive.read(f"{identifier}.db") == path.read_bytes()
    return destination


def test_prepare_and_recover_preserves_unmodified_common_database(tmp_path: Path) -> None:
    common = tmp_path / "common.db"
    meter = tmp_path / "meter.db"
    _create_database(common, "共通")
    _create_database(meter, "検針")
    databases = {
        "common": RegisteredDatabase(common, _validate_database),
        "meter-reading": RegisteredDatabase(meter, _validate_database),
    }
    pending = tmp_path / "recovery" / "pending.json"
    safety = tmp_path / "backups" / "safety"
    backup_root = safety.parent

    def create_backup(snapshots: Mapping[str, Path]) -> Path:
        assert set(snapshots) == {"common", "meter-reading"}
        assert not pending.exists()
        return _create_verified_backup(backup_root, snapshots)

    record = prepare_operation(
        pending,
        safety,
        databases,
        ["meter-reading"],
        operation_id="b" * 32,
        operation_kind="restore_bundle",
        copy_database=copy_database,
        create_and_verify_backup=create_backup,
    )
    assert pending.exists()
    assert record.automatic_backup_path == str(backup_root / "automatic.zip")
    assert [(item.database_id, item.modified) for item in record.targets] == [
        ("meter-reading", True),
        ("common", False),
    ]
    with closing(sqlite3.connect(meter)) as connection:
        connection.execute("UPDATE item SET value = '変更後'")
        connection.commit()
    with closing(sqlite3.connect(common)) as connection:
        connection.execute("UPDATE item SET value = '共通は置換しない'")
        connection.commit()

    assert recover_pending(pending, safety, databases, validate_set=lambda: None)
    assert _read_database(meter) == "検針"
    assert _read_database(common) == "共通は置換しない"
    assert (backup_root / "automatic.zip").exists()
    assert list(safety.iterdir()) == []


def test_prepare_new_database_needs_no_automatic_backup(tmp_path: Path) -> None:
    database = tmp_path / "new.db"
    pending = tmp_path / "pending.json"
    safety = tmp_path / "backups" / "safety"
    databases = {"new": RegisteredDatabase(database, _validate_database)}

    def unexpected_backup(_snapshots: Mapping[str, Path]) -> Path:
        raise AssertionError("新規作成で自動保全を実行しました")

    record = prepare_operation(
        pending,
        safety,
        databases,
        ["new"],
        operation_id="c" * 32,
        operation_kind="initial_migration",
        copy_database=copy_database,
        create_and_verify_backup=unexpected_backup,
    )
    assert record.automatic_backup_path is None
    assert not record.targets[0].existed
    _create_database(database, "作成後")
    assert recover_pending(pending, safety, databases, validate_set=lambda: None)
    assert not database.exists()


def test_mark_and_complete_operation_lifecycle(tmp_path: Path) -> None:
    database = tmp_path / "new.db"
    pending = tmp_path / "recovery" / "pending.json"
    safety = tmp_path / "backups" / "safety"
    databases = {"new": RegisteredDatabase(database, _validate_database)}
    record = prepare_operation(
        pending,
        safety,
        databases,
        ["new"],
        operation_id="3" * 32,
        operation_kind="initial_migration",
        copy_database=copy_database,
        create_and_verify_backup=lambda _snapshots: pytest.fail("新規作成で自動保全を実行しました"),
    )
    assert record.stage == "prepared"
    applying_record = mark_operation_applying(pending, safety, databases)
    assert applying_record.stage == "applying"
    assert mark_operation_applying(pending, safety, databases) == applying_record
    _create_database(database, "作成済み")

    def validate_set() -> None:
        assert _read_database(database) == "作成済み"
        assert pending.exists()

    assert complete_operation(pending, safety, databases, validate_set=validate_set)
    assert not pending.exists()
    assert not (safety / record.operation_id).exists()
    assert database.exists()


def test_completion_validation_failure_keeps_pending_and_snapshots(tmp_path: Path) -> None:
    pending, safety, databases, record = _setup(tmp_path)
    assert record.automatic_backup_path is not None
    automatic_backup = Path(record.automatic_backup_path)
    automatic_backup.write_bytes(b"verified backup")

    def fail_validation() -> None:
        raise ValueError("DB 間の整合性が不正です")

    with pytest.raises(PendingRecoveryError):
        complete_operation(pending, safety, databases, validate_set=fail_validation)
    assert pending.exists()
    assert all(
        Path(target.safety_path).exists()
        for target in record.targets
        if target.safety_path is not None
    )
    assert automatic_backup.exists()
    assert complete_operation(pending, safety, databases, validate_set=lambda: None)


def test_complete_operation_without_record_is_a_noop(tmp_path: Path) -> None:
    pending = tmp_path / "recovery" / "pending.json"
    safety = tmp_path / "backups" / "safety"
    assert not complete_operation(pending, safety, {}, validate_set=lambda: None)
    assert not pending.parent.exists()
    assert not safety.exists()


def test_complete_operation_requires_applying_stage(tmp_path: Path) -> None:
    database = tmp_path / "new.db"
    pending = tmp_path / "recovery" / "pending.json"
    safety = tmp_path / "backups" / "safety"
    databases = {"new": RegisteredDatabase(database, _validate_database)}
    prepare_operation(
        pending,
        safety,
        databases,
        ["new"],
        operation_id="4" * 32,
        operation_kind="initial_migration",
        copy_database=copy_database,
        create_and_verify_backup=lambda _snapshots: pytest.fail("新規作成で自動保全を実行しました"),
    )
    with pytest.raises(PendingRecoveryError, match="完了検査"):
        complete_operation(pending, safety, databases, validate_set=lambda: None)
    assert pending.exists()
    assert not database.exists()


def test_mark_operation_rejects_recovering_record(tmp_path: Path) -> None:
    pending, safety, databases, record = _setup(tmp_path)
    record.stage = "recovering"
    pending.write_text(record.model_dump_json(), encoding="utf-8")
    with pytest.raises(PendingRecoveryError, match="更新できません"):
        mark_operation_applying(pending, safety, databases)
    assert pending.read_text(encoding="utf-8") == record.model_dump_json()


def test_process_exit_immediately_after_publishing_record_allows_retry(tmp_path: Path) -> None:
    pending = tmp_path / "recovery" / "pending.json"
    safety = tmp_path / "backups" / "safety"
    database = tmp_path / "new.db"
    script = """
import os
import sys
from pathlib import Path
from building_manager_mini.db import recovery

original_replace = os.replace
def interrupt(source, destination):
    original_replace(source, destination)
    os._exit(74)
recovery.os.replace = interrupt
def unexpected(*args):
    raise AssertionError("新規 DB では保全を実行しません")
recovery.prepare_operation(
    Path(sys.argv[1]), Path(sys.argv[2]),
    {"new": recovery.RegisteredDatabase(Path(sys.argv[3]), unexpected)},
    ["new"], operation_id="2" * 32, operation_kind="initial_migration",
    copy_database=unexpected, create_and_verify_backup=unexpected,
)
"""
    result = subprocess.run(
        [sys.executable, "-c", script, str(pending), str(safety), str(database)],
        capture_output=True,
        timeout=30,
        check=False,
    )
    assert result.returncode == 74, result.stderr.decode(errors="replace")
    assert pending.exists()
    assert not database.exists()
    assert recover_pending(
        pending,
        safety,
        {"new": RegisteredDatabase(database, _validate_database)},
        validate_set=lambda: None,
    )
    assert not pending.exists()
    assert not database.exists()


def test_prepare_legacy_database_creates_single_database_backup(tmp_path: Path) -> None:
    database = tmp_path / "meter.db"
    _create_database(database, "旧検針")
    with closing(sqlite3.connect(database)) as connection:
        connection.execute("PRAGMA user_version = 6")
    safety = tmp_path / "backups" / "safety"
    pending = tmp_path / "pending.json"
    databases = {"meter-reading": RegisteredDatabase(database, _validate_database)}

    def create_legacy_backup(snapshots: Mapping[str, Path]) -> Path:
        destination = safety.parent / "legacy.db"
        shutil.copyfile(snapshots["meter-reading"], destination)
        _validate_database(destination)
        return destination

    record = prepare_operation(
        pending,
        safety,
        databases,
        ["meter-reading"],
        operation_id="f" * 32,
        operation_kind="initial_migration",
        copy_database=copy_database,
        create_and_verify_backup=create_legacy_backup,
    )
    assert record.automatic_backup_path == str(safety.parent / "legacy.db")
    assert record.automatic_backup_path is not None
    assert _read_database(Path(record.automatic_backup_path)) == "旧検針"
    assert recover_pending(pending, safety, databases, validate_set=lambda: None)


def test_preparation_snapshots_committed_wal_after_process_exit(tmp_path: Path) -> None:
    database = tmp_path / "meter.db"
    _create_database(database, "旧値")
    script = """
import os
import sqlite3
import sys
connection = sqlite3.connect(sys.argv[1], autocommit=True)
connection.execute("PRAGMA journal_mode = WAL")
connection.execute("PRAGMA wal_autocheckpoint = 0")
connection.execute("PRAGMA user_version = 6")
connection.execute("UPDATE item SET value = 'WAL'")
os._exit(0)
"""
    result = subprocess.run(
        [sys.executable, "-c", script, str(database)],
        capture_output=True,
        timeout=30,
        check=False,
    )
    assert result.returncode == 0, result.stderr.decode(errors="replace")
    assert Path(f"{database}-wal").stat().st_size > 0
    safety = tmp_path / "backups" / "safety"
    pending = tmp_path / "pending.json"
    databases = {"meter-reading": RegisteredDatabase(database, _validate_database)}

    def create_legacy_backup(snapshots: Mapping[str, Path]) -> Path:
        destination = safety.parent / "legacy.db"
        shutil.copyfile(snapshots["meter-reading"], destination)
        _validate_database(destination)
        return destination

    record = prepare_operation(
        pending,
        safety,
        databases,
        ["meter-reading"],
        operation_id="1" * 32,
        operation_kind="initial_migration",
        copy_database=copy_database,
        create_and_verify_backup=create_legacy_backup,
    )
    assert record.targets[0].safety_path is not None
    assert _read_database(Path(record.targets[0].safety_path)) == "WAL"
    assert _read_database(safety.parent / "legacy.db") == "WAL"
    assert recover_pending(pending, safety, databases, validate_set=lambda: None)
    assert _read_database(database) == "WAL"


def test_prepare_refuses_newer_meter_without_common_database(tmp_path: Path) -> None:
    database = tmp_path / "meter.db"
    _create_database(database, "検針")
    with closing(sqlite3.connect(database)) as connection:
        connection.execute("PRAGMA user_version = 7")
    pending = tmp_path / "pending.json"
    with pytest.raises(PendingRecoveryError):
        prepare_operation(
            pending,
            tmp_path / "backups" / "safety",
            {"meter-reading": RegisteredDatabase(database, _validate_database)},
            ["meter-reading"],
            operation_id="a" * 32,
            operation_kind="initial_migration",
            copy_database=copy_database,
            create_and_verify_backup=lambda _snapshots: tmp_path / "unused",
        )
    assert not pending.exists()
    assert _read_database(database) == "検針"


@pytest.mark.parametrize(
    "failure", ["copy", "backup", "snapshot-change", "fsync", "journal-fsync", "replace"]
)
def test_preparation_failure_never_changes_existing_databases(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, failure: str
) -> None:
    database = tmp_path / "meter.db"
    _create_database(database, "変更前")
    with closing(sqlite3.connect(database)) as connection:
        connection.execute("PRAGMA user_version = 6")
    pending = tmp_path / "recovery" / "pending.json"
    safety = tmp_path / "backups" / "safety"
    databases = {"meter-reading": RegisteredDatabase(database, _validate_database)}

    def snapshot(source: sqlite3.Connection, destination: Path) -> None:
        if failure == "copy":
            raise sqlite3.OperationalError("保全失敗")
        copy_database(source, destination)

    def backup(snapshots: Mapping[str, Path]) -> Path:
        if failure == "backup":
            raise ValueError("自動保全失敗")
        result = safety.parent / "automatic.db"
        shutil.copyfile(snapshots["meter-reading"], result)
        assert result.read_bytes() == snapshots["meter-reading"].read_bytes()
        if failure == "snapshot-change":
            snapshots["meter-reading"].write_bytes(b"corrupted")
        return result

    if failure in {"fsync", "journal-fsync"}:
        fsync = os.fsync
        calls = 0

        def fail_fsync(_fd: int) -> None:
            nonlocal calls
            calls += 1
            if failure == "fsync" or calls == 3:
                raise OSError("fsync 失敗")
            fsync(_fd)

        monkeypatch.setattr(os, "fsync", fail_fsync)
    if failure == "replace":

        def fail_replace(_source: object, _destination: object) -> None:
            raise OSError("中断記録の確定失敗")

        monkeypatch.setattr(os, "replace", fail_replace)
    with pytest.raises(PendingRecoveryError):
        prepare_operation(
            pending,
            safety,
            databases,
            ["meter-reading"],
            operation_id="d" * 32,
            operation_kind="initial_migration",
            copy_database=snapshot,
            create_and_verify_backup=backup,
        )
    assert not pending.exists()
    if pending.parent.exists():
        assert not list(pending.parent.glob(".pending-*.tmp"))
    assert _read_database(database) == "変更前"


def test_prepare_refuses_existing_pending_record(tmp_path: Path) -> None:
    pending = tmp_path / "pending.json"
    pending.write_text("既存", encoding="utf-8")
    with pytest.raises(PendingRecoveryError):
        prepare_operation(
            pending,
            tmp_path / "safety",
            {"new": RegisteredDatabase(tmp_path / "new.db", _validate_database)},
            ["new"],
            operation_id="e" * 32,
            operation_kind="initial_migration",
            copy_database=copy_database,
            create_and_verify_backup=lambda _snapshots: tmp_path / "unused",
        )
    assert pending.read_text(encoding="utf-8") == "既存"
    assert not (tmp_path / "safety").exists()


@pytest.mark.parametrize("kind", ["duplicate", "unknown", "path", "invalid-json"])
def test_untrusted_record_is_rejected_without_changing_databases(tmp_path: Path, kind: str) -> None:
    pending, safety, databases, record = _setup(tmp_path)
    if kind == "duplicate":
        record.targets.append(record.targets[0])
    elif kind == "unknown":
        record.targets[0].database_id = "unknown"
    elif kind == "path":
        record.targets[0].path = str(tmp_path / "other.db")
    if kind == "invalid-json":
        pending.write_text('{"format_version":1,"format_version":1}', encoding="utf-8")
    else:
        pending.write_text(json.dumps(record.model_dump()), encoding="utf-8")
    with pytest.raises(PendingRecoveryError):
        recover_pending(pending, safety, databases, validate_set=lambda: None)
    assert pending.exists()
    assert _read_database(databases["common"].path) == "変更後"
    assert _read_database(databases["meter-reading"].path) == "変更後"
