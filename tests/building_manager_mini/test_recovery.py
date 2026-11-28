from __future__ import annotations

import hashlib
import json
import os
import sqlite3
from contextlib import closing
from pathlib import Path

import pytest

from building_manager_mini.core.recovery import PendingRecoveryError
from building_manager_mini.db.recovery import (
    RecoveryRecord,
    RecoveryTargetRecord,
    RegisteredDatabase,
    recover_pending,
)


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
        automatic_backup_path=None,
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
