from __future__ import annotations

import errno
import hashlib
import os
import shutil
import sqlite3
from collections.abc import Iterator
from contextlib import closing
from dataclasses import dataclass
from datetime import UTC, datetime
from importlib.resources import files
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

from meter_reading_mini.core.backup_restore_service import BackupRestoreService
from meter_reading_mini.core.errors import FileOutputError, InvalidBackupError, RestoreError
from meter_reading_mini.db import restore
from meter_reading_mini.db.connection import connect, connect_readonly
from meter_reading_mini.db.migrations import SCHEMA_VERSION, create_schema, migrate_schema
from meter_reading_mini.db.restore import SQLiteBackupRestoreRepository


@dataclass
class RestoreHarness:
    path: Path
    conn: sqlite3.Connection
    repository: SQLiteBackupRestoreRepository
    service: BackupRestoreService
    closed: bool = False
    close_calls: int = 0

    def close(self) -> None:
        self.close_calls += 1
        if self.closed:
            raise RuntimeError("接続は既に閉じられています")
        self.conn.close()
        self.closed = True


@pytest.fixture
def harness(tmp_path: Path) -> Iterator[RestoreHarness]:
    path = tmp_path / "current.db"
    conn = connect(path)
    conn.execute("PRAGMA wal_autocheckpoint = 0")
    create_schema(conn)
    conn.execute("INSERT INTO staff (name) VALUES ('現行担当')")
    repository = SQLiteBackupRestoreRepository(conn, path)
    service = BackupRestoreService(
        repository,
        tmp_path / "backups",
        clock=lambda: datetime(2026, 2, 4, 3, 0, tzinfo=UTC),
        timezone_provider=lambda: ZoneInfo("Asia/Tokyo"),
    )
    instance = RestoreHarness(path, conn, repository, service)
    try:
        yield instance
    finally:
        repository.discard_preview()
        if not instance.closed:
            instance.close()


def _create_source(path: Path, version: int = SCHEMA_VERSION) -> None:
    with closing(connect(path)) as conn:
        if version < SCHEMA_VERSION:
            schema = files("meter_reading_mini.db").joinpath("schema_v1.sql")
            conn.executescript(schema.read_text(encoding="utf-8"))
            conn.execute("PRAGMA user_version = 1")
            conn.execute("INSERT INTO staff (name) VALUES ('候補担当')")
            if version > 1:
                migrate_schema(conn, 1, version)
        else:
            create_schema(conn)
            conn.execute("INSERT INTO staff (name) VALUES ('候補担当')")


def _seed_formula(conn: sqlite3.Connection) -> None:
    conn.execute(
        "INSERT INTO physical_meters "
        "(meter_id, location_id, location_name, meter_name, category) "
        "VALUES ('meter', 'location', '試験場所', '試験メーター', 'water')"
    )
    conn.execute(
        "INSERT INTO measurement_definitions "
        "(measurement_definition_id, location_id, meter_id, display_name, category, unit, "
        "value_schema, multiplier, calculation_method, reading_cycle, purpose, "
        "generation_start_date) VALUES "
        "('definition', 'location', 'meter', '試験対象', 'water', 'm3', "
        "'decimal_string', '1', 'delta', 'daily_1x', 'operational', '2026-01-01')"
    )
    conn.execute(
        "INSERT INTO meter_assignments "
        "(measurement_definition_id, meter_id, effective_from, multiplier) "
        "VALUES ('definition', 'meter', '2026-01-01 00:00:00', '1')"
    )
    conn.execute(
        "INSERT INTO aggregation_formulas "
        "(formula_id, display_name, unit, category, cycle_group, purpose) "
        "VALUES ('formula', '試験式', 'm3', 'water', 'daily', 'operational')"
    )
    conn.execute(
        "INSERT INTO aggregation_formula_versions "
        "(formula_id, effective_from_date, is_active, is_initial, created_at, "
        "registered_business_date) VALUES "
        "('formula', '2026-01-01', 1, 1, '2026-01-01 00:00:00', '2026-01-01')"
    )
    conn.execute(
        "INSERT INTO aggregation_formula_terms "
        "(formula_id, effective_from_date, measurement_definition_id, coefficient, sort_order) "
        "VALUES ('formula', '2026-01-01', 'definition', '-0.5', 0)"
    )


def _seed_legacy_history(conn: sqlite3.Connection, *, multiplier: str = "1") -> None:
    conn.execute(
        "INSERT INTO physical_meters "
        "(meter_id, location_id, location_name, meter_name, category) "
        "VALUES ('meter', 'location', '試験場所', '試験メーター', 'water')"
    )
    conn.execute(
        "INSERT INTO measurement_definitions "
        "(measurement_definition_id, location_id, meter_id, display_name, category, unit, "
        "value_schema, multiplier, rollover_value, calculation_method, reading_cycle, purpose, "
        "generation_start_date) VALUES "
        "('definition', 'location', 'meter', '試験対象', 'water', 'm3', "
        "'decimal_string', ?, '10', 'delta', 'daily_1x', 'operational', '2026-01-01')",
        (multiplier,),
    )
    conn.execute(
        "INSERT INTO meter_assignments VALUES ('definition', 'meter', '2026-01-01 00:00:00', NULL)"
    )
    conn.execute(
        "INSERT INTO planned_readings "
        "(planned_reading_id, measurement_definition_id, meter_id, location_id, business_date, "
        "release_date, reading_session, scheduled_at, task_state) VALUES "
        "('reading', 'definition', 'meter', 'location', '2026-01-02', '2026-01-02', "
        "'08:00', '2026-01-02 00:00:00', 'completed')"
    )
    conn.execute(
        "INSERT INTO reading_transactions "
        "(planned_reading_id, result_status, recorded_at, client_updated_at, raw_reading, "
        "usage_delta, updated_by, signature, result_source) VALUES "
        "('reading', 'measured', '2026-01-02 00:00:00', '2026-01-02 00:00:00', "
        "'20', '0', '担当者', '署名', 'manual')"
    )


def _read_staff(path: Path) -> list[str]:
    immutable = not Path(f"{path}-wal").exists()
    with closing(connect_readonly(path, immutable=immutable)) as conn:
        return [str(row[0]) for row in conn.execute("SELECT name FROM staff ORDER BY id")]


def _workspace(harness: RestoreHarness) -> Path:
    paths = list(harness.path.parent.glob(".meter-reading-restore-*"))
    assert len(paths) == 1
    return paths[0]


def _assert_no_workspace(harness: RestoreHarness) -> None:
    assert not list(harness.path.parent.glob(".meter-reading-restore-*"))


def _backup_path(harness: RestoreHarness) -> Path:
    return harness.path.parent / "backups" / "pre-restore_20260204_120000.db"


@pytest.mark.parametrize("version", [1, 2, 3, 4])
def test_preview_migrates_all_supported_versions_without_changing_source(
    harness: RestoreHarness, tmp_path: Path, version: int
) -> None:
    source = tmp_path / f"source-v{version}.db"
    _create_source(source, version)
    original = hashlib.sha256(source.read_bytes()).digest()

    preview = harness.service.preview_restore(source)

    assert preview.source_path == source
    assert preview.source_schema_version == version
    assert preview.warnings == ()
    candidate = _workspace(harness) / "candidate.db"
    with closing(connect_readonly(candidate, immutable=True)) as conn:
        assert conn.execute("PRAGMA user_version").fetchone() == (SCHEMA_VERSION,)
        assert conn.execute("SELECT name FROM staff").fetchall() == [("候補担当",)]
    assert not Path(f"{candidate}-wal").exists()
    assert not Path(f"{candidate}-shm").exists()
    assert hashlib.sha256(source.read_bytes()).digest() == original
    assert not Path(f"{source}-wal").exists()
    assert not Path(f"{source}-shm").exists()
    with closing(connect_readonly(source, immutable=True)) as conn:
        assert conn.execute("PRAGMA user_version").fetchone() == (version,)
    result = harness.service.restore_after_shutdown(harness.close)
    assert _read_staff(result.backup_path) == ["現行担当"]
    assert _read_staff(harness.path) == ["候補担当"]
    assert hashlib.sha256(source.read_bytes()).digest() == original
    _assert_no_workspace(harness)


def test_restore_preserves_v4_formula_and_wal_backup(
    harness: RestoreHarness, tmp_path: Path
) -> None:
    source = tmp_path / "formula.db"
    _create_source(source)
    with closing(connect(source)) as conn:
        _seed_formula(conn)
    assert Path(f"{harness.path}-wal").stat().st_size > 0
    before = hashlib.sha256(source.read_bytes()).digest()
    display_settings = tmp_path / "ui_settings.json"
    display_settings.write_text('{"font": "試験"}', encoding="utf-8")

    preview = harness.service.preview_restore(source)
    result = harness.service.restore_after_shutdown(harness.close)

    assert preview.warnings == ()
    assert result.backup_path == _backup_path(harness)
    assert harness.close_calls == 1
    assert _read_staff(result.backup_path) == ["現行担当"]
    assert _read_staff(harness.path) == ["候補担当"]
    with closing(connect_readonly(harness.path)) as conn:
        assert conn.execute("SELECT coefficient FROM aggregation_formula_terms").fetchone() == (
            "-0.5",
        )
    assert hashlib.sha256(source.read_bytes()).digest() == before
    assert display_settings.read_text(encoding="utf-8") == '{"font": "試験"}'
    _assert_no_workspace(harness)


def test_live_wal_source_is_copied_without_changing_it(
    harness: RestoreHarness, tmp_path: Path
) -> None:
    source = tmp_path / "source-wal.db"
    _create_source(source)
    with closing(connect(source)) as writer:
        writer.execute("PRAGMA wal_autocheckpoint = 0")
        writer.execute("INSERT INTO staff (name) VALUES ('WAL 内の担当')")
        wal = Path(f"{source}-wal")
        shm = Path(f"{source}-shm")
        before = (source.read_bytes(), wal.read_bytes(), shm.read_bytes())

        harness.service.preview_restore(source)

        candidate = _workspace(harness) / "candidate.db"
        assert _read_staff(candidate) == ["候補担当", "WAL 内の担当"]
        assert (source.read_bytes(), wal.read_bytes(), shm.read_bytes()) == before


def test_source_wal_without_shared_memory_is_read_without_modifying_source(
    harness: RestoreHarness, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    original = tmp_path / "original.db"
    source = tmp_path / "wal-without-shm.db"
    _create_source(original)
    with closing(connect(original)) as writer:
        writer.execute("PRAGMA wal_autocheckpoint = 0")
        writer.execute("INSERT INTO staff (name) VALUES ('WAL 内の担当')")
        shutil.copyfile(original, source)
        shutil.copyfile(Path(f"{original}-wal"), Path(f"{source}-wal"))
    wal = Path(f"{source}-wal")
    before = (source.read_bytes(), wal.read_bytes())
    assert not Path(f"{source}-shm").exists()

    original_connect_readonly = restore.connect_readonly

    def open_snapshot_only(path: Path, *, immutable: bool = False) -> sqlite3.Connection:
        assert path != source
        return original_connect_readonly(path, immutable=immutable)

    monkeypatch.setattr(restore, "connect_readonly", open_snapshot_only)
    harness.service.preview_restore(source)

    assert _read_staff(_workspace(harness) / "candidate.db") == ["候補担当", "WAL 内の担当"]
    assert (source.read_bytes(), wal.read_bytes()) == before
    assert not Path(f"{source}-shm").exists()
    assert not (_workspace(harness) / "source-snapshot.db").exists()
    result = harness.service.restore_after_shutdown(harness.close)
    assert _read_staff(harness.path) == ["候補担当", "WAL 内の担当"]
    assert _read_staff(result.backup_path) == ["現行担当"]
    assert (source.read_bytes(), wal.read_bytes()) == before
    assert not Path(f"{source}-shm").exists()


def test_source_snapshot_copy_failure_preserves_source(
    harness: RestoreHarness, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    original = tmp_path / "original.db"
    source = tmp_path / "wal-without-shm.db"
    _create_source(original)
    with closing(connect(original)) as writer:
        writer.execute("PRAGMA wal_autocheckpoint = 0")
        writer.execute("INSERT INTO staff (name) VALUES ('WAL 内の担当')")
        shutil.copyfile(original, source)
        shutil.copyfile(Path(f"{original}-wal"), Path(f"{source}-wal"))
    wal = Path(f"{source}-wal")
    before = (source.read_bytes(), wal.read_bytes())

    def fail_copy(reader: object, writer: object, length: int) -> None:
        raise OSError(errno.ENOSPC, "スナップショットの容量不足")

    monkeypatch.setattr(restore.shutil, "copyfileobj", fail_copy)
    with pytest.raises(RestoreError, match="容量不足"):
        harness.service.preview_restore(source)
    assert (source.read_bytes(), wal.read_bytes()) == before
    assert not Path(f"{source}-shm").exists()
    _assert_no_workspace(harness)


def test_restore_warnings_are_separate_from_invalid_data(
    harness: RestoreHarness, tmp_path: Path
) -> None:
    source = tmp_path / "warnings.db"
    _create_source(source)
    with closing(connect(source)) as conn:
        _seed_formula(conn)
        conn.execute(
            "INSERT INTO physical_meters "
            "(meter_id, location_id, location_name, meter_name, category) "
            "VALUES ('other', 'location', '旧名称', '別メーター', 'water')"
        )
        conn.execute(
            "INSERT INTO assignment_review_flags VALUES "
            "('definition', 'meter', '2026-01-01 00:00:00', "
            "'割当条件を確認', '2026-01-01 00:00:00')"
        )

    preview = harness.service.preview_restore(source)

    assert len(preview.warnings) == 2
    assert any("割当条件の確認" in warning for warning in preview.warnings)
    assert any("異なる場所名" in warning for warning in preview.warnings)
    result = harness.service.restore_after_shutdown(harness.close)
    assert result.backup_path.exists()


def test_v1_migration_surfaces_review_warning_without_changing_original(
    harness: RestoreHarness, tmp_path: Path
) -> None:
    source = tmp_path / "legacy.db"
    _create_source(source, version=1)
    with closing(sqlite3.connect(source, autocommit=True)) as conn:
        _seed_legacy_history(conn)
    before = source.read_bytes()

    preview = harness.service.preview_restore(source)

    assert preview.source_schema_version == 1
    assert any("ロールオーバー" in warning for warning in preview.warnings)
    result = harness.service.restore_after_shutdown(harness.close)
    assert result.backup_path.exists()
    with closing(connect_readonly(harness.path, immutable=True)) as conn:
        assert conn.execute("SELECT raw_reading FROM reading_transactions").fetchone() == ("20",)
        assert conn.execute("SELECT COUNT(*) FROM assignment_review_flags").fetchone() == (1,)
    assert source.read_bytes() == before


def test_default_backup_filename_uses_business_timezone(harness: RestoreHarness) -> None:
    assert harness.service.default_backup_filename() == "meter_reading_20260204_120000.db"
    other_timezone = BackupRestoreService(
        harness.repository,
        harness.path.parent / "backups",
        timezone_provider=lambda: ZoneInfo("America/Los_Angeles"),
        clock=lambda: datetime(2026, 2, 4, 3, 0, tzinfo=UTC),
    )
    assert other_timezone.default_backup_filename() == "meter_reading_20260203_190000.db"


def test_manual_backup_includes_wal_and_never_overwrites(
    harness: RestoreHarness, tmp_path: Path
) -> None:
    destination = tmp_path / "manual.db"
    harness.service.create_backup(destination)
    assert _read_staff(destination) == ["現行担当"]
    original = destination.read_bytes()
    with pytest.raises(FileOutputError, match="バックアップを保存できません"):
        harness.service.create_backup(destination)
    assert destination.read_bytes() == original
    with pytest.raises(FileOutputError):
        harness.service.create_backup(harness.path)


@pytest.mark.parametrize("suffix", ["-wal", "-shm", "-journal"])
def test_manual_backup_does_not_remove_existing_sidecars(
    harness: RestoreHarness, tmp_path: Path, suffix: str
) -> None:
    destination = tmp_path / "manual.db"
    sidecar = Path(f"{destination}{suffix}")
    sidecar.write_bytes(b"do not touch")
    with pytest.raises(FileOutputError, match="関連ファイル"):
        harness.service.create_backup(destination)
    assert sidecar.read_bytes() == b"do not touch"
    assert not destination.exists()


@pytest.mark.parametrize(
    ("version", "message"),
    [(0, "サポート"), (5, "新しい")],
)
def test_unsupported_versions_are_rejected(
    harness: RestoreHarness, tmp_path: Path, version: int, message: str
) -> None:
    source = tmp_path / "unsupported.db"
    _create_source(source)
    with closing(sqlite3.connect(source, autocommit=True)) as conn:
        conn.execute(f"PRAGMA user_version = {version}")
    with pytest.raises(InvalidBackupError, match=message):
        harness.service.preview_restore(source)
    _assert_no_workspace(harness)
    assert not _backup_path(harness).exists()


def test_corrupt_database_and_schema_are_rejected(harness: RestoreHarness, tmp_path: Path) -> None:
    corrupt = tmp_path / "corrupt.db"
    corrupt.write_bytes(b"not a sqlite database")
    with pytest.raises(InvalidBackupError):
        harness.service.preview_restore(corrupt)
    _assert_no_workspace(harness)

    source = tmp_path / "bad-schema.db"
    _create_source(source)
    with closing(sqlite3.connect(source, autocommit=True)) as conn:
        conn.execute("DROP INDEX idx_physical_meters_location")
    before = source.read_bytes()
    with pytest.raises(InvalidBackupError, match="スキーマ"):
        harness.service.preview_restore(source)
    assert source.read_bytes() == before
    _assert_no_workspace(harness)


def test_source_open_failure_is_explicit_and_cleans_workspace(
    harness: RestoreHarness, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source = tmp_path / "source.db"
    _create_source(source)

    def deny_open(path: Path, *, immutable: bool = False) -> sqlite3.Connection:
        raise PermissionError("読取を拒否")

    monkeypatch.setattr(restore, "connect_readonly", deny_open)
    with pytest.raises(InvalidBackupError, match="読取を拒否"):
        harness.service.preview_restore(source)
    assert not harness.closed
    _assert_no_workspace(harness)


def test_source_with_nonempty_rollback_journal_is_rejected_without_changes(
    harness: RestoreHarness, tmp_path: Path
) -> None:
    source = tmp_path / "source.db"
    _create_source(source)
    journal = Path(f"{source}-journal")
    journal.write_bytes(b"unprocessed journal")
    before = source.read_bytes()
    with pytest.raises(InvalidBackupError, match="ロールバックジャーナル"):
        harness.service.preview_restore(source)
    assert source.read_bytes() == before
    assert journal.read_bytes() == b"unprocessed journal"
    assert not harness.closed
    _assert_no_workspace(harness)


def test_workspace_creation_failure_is_explicit(
    harness: RestoreHarness, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source = tmp_path / "source.db"
    _create_source(source)

    def deny_temporary_directory(*args: object, **kwargs: object) -> str:
        raise PermissionError("一時領域を拒否")

    monkeypatch.setattr(restore.tempfile, "mkdtemp", deny_temporary_directory)
    with pytest.raises(RestoreError, match="一時領域を拒否") as failure:
        harness.service.preview_restore(source)
    assert failure.value.stage == "pre_backup"
    assert not harness.closed
    _assert_no_workspace(harness)


def test_preview_failure_and_cleanup_failure_are_both_reported(
    harness: RestoreHarness, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source = tmp_path / "corrupt.db"
    source.write_bytes(b"not a database")
    original_remove = restore._Workspace.remove

    def deny_cleanup(workspace: restore._Workspace) -> None:
        raise PermissionError("一時領域の削除を拒否")

    monkeypatch.setattr(restore._Workspace, "remove", deny_cleanup)
    try:
        with pytest.raises(ExceptionGroup, match="一時ファイル") as failure:
            harness.service.preview_restore(source)
        assert isinstance(failure.value.exceptions[0], InvalidBackupError)
        assert isinstance(failure.value.exceptions[1], PermissionError)
    finally:
        workspace_path = _workspace(harness)
        original_remove(
            restore._Workspace(workspace_path, restore._identity(workspace_path.lstat()))
        )
    _assert_no_workspace(harness)


@pytest.mark.parametrize(
    ("sql", "message"),
    [
        ("UPDATE measurement_definitions SET multiplier = 'NaN'", "plain decimal"),
        ("UPDATE aggregation_formula_terms SET coefficient = '01'", "係数"),
        ("UPDATE measurement_definitions SET meter_id = 'missing'", "外部キー"),
    ],
)
def test_business_and_foreign_key_corruption_is_rejected(
    harness: RestoreHarness, tmp_path: Path, sql: str, message: str
) -> None:
    source = tmp_path / "bad-business.db"
    _create_source(source)
    with closing(sqlite3.connect(source, autocommit=True)) as conn:
        _seed_formula(conn)
        conn.execute("PRAGMA ignore_check_constraints = ON")
        conn.execute(sql)
    before = source.read_bytes()
    with pytest.raises(InvalidBackupError, match=message):
        harness.service.preview_restore(source)
    assert source.read_bytes() == before
    _assert_no_workspace(harness)


@pytest.mark.parametrize("corruption", ["state", "boundary"])
def test_status_and_replacement_boundary_corruption_is_rejected(
    harness: RestoreHarness, tmp_path: Path, corruption: str
) -> None:
    source = tmp_path / "bad-history.db"
    _create_source(source)
    with closing(sqlite3.connect(source, autocommit=True)) as conn:
        _seed_formula(conn)
        if corruption == "state":
            conn.execute(
                "INSERT INTO planned_readings "
                "(planned_reading_id, measurement_definition_id, meter_id, location_id, "
                "business_date, release_date, reading_session, scheduled_at, task_state) VALUES "
                "('reading', 'definition', 'meter', 'location', '2026-01-02', '2026-01-02', "
                "'08:00', '2026-01-02 00:00:00', 'open')"
            )
            conn.execute(
                "INSERT INTO reading_transactions "
                "(planned_reading_id, result_status, recorded_at, client_updated_at, "
                "raw_reading, usage_delta, updated_by, signature, result_source) VALUES "
                "('reading', 'measured', '2026-01-02 00:00:00', '2026-01-02 00:00:00', "
                "'1', '0', '担当者', '署名', 'manual')"
            )
        else:
            conn.execute(
                "INSERT INTO physical_meters "
                "(meter_id, location_id, location_name, meter_name, category) VALUES "
                "('new-meter', 'location', '試験場所', '新メーター', 'water')"
            )
            conn.execute(
                "INSERT INTO replacement_events "
                "(replacement_event_id, location_id, old_meter_id, new_meter_id, replaced_at, "
                "client_updated_at, updated_by) VALUES "
                "('event', 'location', 'meter', 'new-meter', '2026-01-02 00:00:00', "
                "'2026-01-02 00:00:00', '担当者')"
            )
    before = source.read_bytes()
    with pytest.raises(InvalidBackupError, match="未完了|交換境界"):
        harness.service.preview_restore(source)
    assert source.read_bytes() == before
    _assert_no_workspace(harness)


def test_same_file_and_hard_link_are_rejected(harness: RestoreHarness, tmp_path: Path) -> None:
    with pytest.raises(InvalidBackupError, match="同じファイル"):
        harness.service.preview_restore(harness.path)
    link = tmp_path / "hardlink.db"
    os.link(harness.path, link)
    with pytest.raises(InvalidBackupError, match="同じファイル"):
        harness.service.preview_restore(link)
    assert _read_staff(harness.path) == ["現行担当"]


def test_changed_source_invalidates_preview_before_backup(
    harness: RestoreHarness, tmp_path: Path
) -> None:
    source = tmp_path / "source.db"
    _create_source(source)
    harness.service.preview_restore(source)
    with closing(connect(source)) as conn:
        conn.execute("INSERT INTO staff (name) VALUES ('変更後')")
    with pytest.raises(InvalidBackupError, match="変更"):
        harness.service.restore_after_shutdown(harness.close)
    assert not harness.closed
    assert not _backup_path(harness).exists()
    assert _read_staff(harness.path) == ["現行担当"]
    _assert_no_workspace(harness)


def test_source_replaced_after_preview_is_rejected(harness: RestoreHarness, tmp_path: Path) -> None:
    source = tmp_path / "source.db"
    replacement = tmp_path / "replacement.db"
    _create_source(source)
    _create_source(replacement)
    harness.service.preview_restore(source)
    os.replace(replacement, source)
    with pytest.raises(InvalidBackupError, match="変更"):
        harness.service.restore_after_shutdown(harness.close)
    assert not _backup_path(harness).exists()
    assert not harness.closed
    _assert_no_workspace(harness)


def test_source_changed_during_copy_is_rejected(
    harness: RestoreHarness, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source = tmp_path / "source.db"
    _create_source(source)
    with closing(connect(source)) as writer:
        writer.execute("PRAGMA wal_autocheckpoint = 0")
        original_copy = restore.copy_database

        def copy_then_change(conn: sqlite3.Connection, destination: Path) -> None:
            original_copy(conn, destination)
            writer.execute("INSERT INTO staff (name) VALUES ('同時変更')")

        monkeypatch.setattr(restore, "copy_database", copy_then_change)
        with pytest.raises(InvalidBackupError, match="変更"):
            harness.service.preview_restore(source)
    assert not harness.closed
    assert _read_staff(harness.path) == ["現行担当"]
    _assert_no_workspace(harness)


def test_changed_candidate_invalidates_preview_before_backup(
    harness: RestoreHarness, tmp_path: Path
) -> None:
    source = tmp_path / "source.db"
    _create_source(source)
    harness.service.preview_restore(source)
    candidate = _workspace(harness) / "candidate.db"
    with closing(connect(candidate)) as conn:
        conn.execute("INSERT INTO staff (name) VALUES ('改変')")
    with pytest.raises(InvalidBackupError, match="一時候補が変更"):
        harness.service.restore_after_shutdown(harness.close)
    assert not _backup_path(harness).exists()
    assert not harness.closed
    _assert_no_workspace(harness)


def test_candidate_sidecar_invalidates_preview(harness: RestoreHarness, tmp_path: Path) -> None:
    source = tmp_path / "source.db"
    _create_source(source)
    harness.service.preview_restore(source)
    sidecar = _workspace(harness) / "candidate.db-wal"
    sidecar.write_bytes(b"unexpected wal")
    with pytest.raises(InvalidBackupError, match="関連ファイル"):
        harness.service.restore_after_shutdown(harness.close)
    assert not _backup_path(harness).exists()
    _assert_no_workspace(harness)


def test_candidate_hardlink_blocks_replacement_without_deleting_the_alias(
    harness: RestoreHarness, tmp_path: Path
) -> None:
    source = tmp_path / "source.db"
    _create_source(source)
    harness.service.preview_restore(source)
    candidate = _workspace(harness) / "candidate.db"
    alias = tmp_path / "candidate-alias.db"
    os.link(candidate, alias)
    with pytest.raises(InvalidBackupError, match="実体を一意"):
        harness.service.restore_after_shutdown(harness.close)
    assert _read_staff(alias) == ["候補担当"]
    assert not _backup_path(harness).exists()
    _assert_no_workspace(harness)


def test_failed_new_preview_and_cancel_discard_old_candidate(
    harness: RestoreHarness, tmp_path: Path
) -> None:
    source = tmp_path / "source.db"
    _create_source(source)
    harness.service.preview_restore(source)
    with pytest.raises(InvalidBackupError):
        harness.service.preview_restore(tmp_path / "missing.db")
    _assert_no_workspace(harness)
    with pytest.raises(InvalidBackupError, match="検査済み"):
        harness.service.restore_after_shutdown(harness.close)

    harness.service.preview_restore(source)
    harness.service.discard_preview()
    harness.service.discard_preview()
    _assert_no_workspace(harness)
    with pytest.raises(InvalidBackupError, match="検査済み"):
        harness.service.restore_after_shutdown(harness.close)


def test_discard_does_not_remove_unknown_workspace_files(
    harness: RestoreHarness, tmp_path: Path
) -> None:
    source = tmp_path / "source.db"
    _create_source(source)
    harness.service.preview_restore(source)
    workspace = _workspace(harness)
    unexpected = workspace / "other-file.txt"
    unexpected.write_text("他のファイル", encoding="utf-8")
    try:
        with pytest.raises(OSError):
            harness.service.discard_preview()
        assert unexpected.read_text(encoding="utf-8") == "他のファイル"
        with pytest.raises(InvalidBackupError, match="検査済み"):
            harness.service.restore_after_shutdown(harness.close)
    finally:
        unexpected.unlink()
        workspace.rmdir()


def test_pre_restore_backup_collision_does_not_replace_current(
    harness: RestoreHarness, tmp_path: Path
) -> None:
    source = tmp_path / "source.db"
    _create_source(source)
    harness.service.preview_restore(source)
    backup = _backup_path(harness)
    backup.parent.mkdir()
    backup.write_bytes(b"existing backup")
    with pytest.raises(RestoreError) as failure:
        harness.service.restore_after_shutdown(harness.close)
    assert failure.value.stage == "pre_backup"
    assert failure.value.backup_path is None
    assert backup.read_bytes() == b"existing backup"
    assert _read_staff(harness.path) == ["現行担当"]
    assert not harness.closed
    _assert_no_workspace(harness)


def test_checkpoint_busy_keeps_current_and_backup(harness: RestoreHarness, tmp_path: Path) -> None:
    source = tmp_path / "source.db"
    _create_source(source)
    harness.service.preview_restore(source)
    with closing(connect_readonly(harness.path)) as reader:
        reader.execute("BEGIN")
        reader.execute("SELECT * FROM staff").fetchall()
        harness.conn.execute("INSERT INTO staff (name) VALUES ('追加担当')")
        harness.conn.execute("PRAGMA busy_timeout = 0")
        with pytest.raises(RestoreError, match="チェックポイント") as failure:
            harness.service.restore_after_shutdown(harness.close)
        assert failure.value.stage == "overwrite"
        assert failure.value.backup_path == _backup_path(harness)
        assert _read_staff(_backup_path(harness)) == ["現行担当", "追加担当"]
        assert not harness.closed
        reader.execute("ROLLBACK")
    assert _read_staff(harness.path) == ["現行担当", "追加担当"]
    _assert_no_workspace(harness)


def test_open_reader_sidecars_block_replacement_without_being_deleted(
    harness: RestoreHarness, tmp_path: Path
) -> None:
    source = tmp_path / "source.db"
    _create_source(source)
    harness.service.preview_restore(source)
    with closing(connect_readonly(harness.path)) as reader:
        assert reader.execute("SELECT name FROM staff").fetchall() == [("現行担当",)]
        with pytest.raises(RestoreError, match="関連ファイル") as failure:
            harness.service.restore_after_shutdown(harness.close)
        assert failure.value.backup_path == _backup_path(harness)
        assert Path(f"{harness.path}-shm").exists()
        assert reader.execute("SELECT name FROM staff").fetchall() == [("現行担当",)]
    assert _read_staff(harness.path) == ["現行担当"]
    assert _read_staff(_backup_path(harness)) == ["現行担当"]
    _assert_no_workspace(harness)


def test_unclosed_connection_blocks_replacement_and_keeps_backup(
    harness: RestoreHarness, tmp_path: Path
) -> None:
    source = tmp_path / "source.db"
    _create_source(source)
    harness.service.preview_restore(source)
    with pytest.raises(RestoreError, match="接続が閉じられていません") as failure:
        harness.service.restore_after_shutdown(lambda: None)
    assert failure.value.backup_path == _backup_path(harness)
    assert not harness.closed
    assert _read_staff(harness.path) == ["現行担当"]
    assert _read_staff(_backup_path(harness)) == ["現行担当"]


def test_current_change_during_backup_is_detected_before_closing(
    harness: RestoreHarness, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source = tmp_path / "source.db"
    _create_source(source)
    harness.service.preview_restore(source)
    real_data_version = restore._data_version
    calls = 0

    def changed_data_version(conn: sqlite3.Connection) -> int:
        nonlocal calls
        value = real_data_version(conn)
        if conn is harness.conn:
            calls += 1
            if calls > 1:
                return value + 1
        return value

    monkeypatch.setattr(restore, "_data_version", changed_data_version)
    with pytest.raises(RestoreError, match="保全中に変更") as failure:
        harness.service.restore_after_shutdown(harness.close)
    assert failure.value.backup_path == _backup_path(harness)
    assert not harness.closed
    assert _read_staff(harness.path) == ["現行担当"]
    assert _read_staff(_backup_path(harness)) == ["現行担当"]


def test_modified_pre_restore_backup_blocks_replacement(
    harness: RestoreHarness, tmp_path: Path
) -> None:
    source = tmp_path / "source.db"
    _create_source(source)
    harness.service.preview_restore(source)

    def close_and_change_backup() -> None:
        harness.close()
        with closing(sqlite3.connect(_backup_path(harness), autocommit=True)) as conn:
            conn.execute("INSERT INTO staff (name) VALUES ('保全後の改変')")

    with pytest.raises(RestoreError, match="バックアップが変更") as failure:
        harness.service.restore_after_shutdown(close_and_change_backup)
    assert failure.value.backup_path == _backup_path(harness)
    assert _read_staff(harness.path) == ["現行担当"]
    assert _read_staff(_backup_path(harness)) == ["現行担当", "保全後の改変"]


def test_pre_restore_backup_hardlink_blocks_replacement(
    harness: RestoreHarness, tmp_path: Path
) -> None:
    source = tmp_path / "source.db"
    _create_source(source)
    harness.service.preview_restore(source)
    alias = tmp_path / "backup-alias.db"

    def close_and_link_backup() -> None:
        harness.close()
        os.link(_backup_path(harness), alias)

    with pytest.raises(RestoreError, match="実体を一意") as failure:
        harness.service.restore_after_shutdown(close_and_link_backup)
    assert failure.value.backup_path == _backup_path(harness)
    assert _read_staff(harness.path) == ["現行担当"]
    assert _read_staff(alias) == ["現行担当"]


@pytest.mark.parametrize("target", ["source", "candidate"])
def test_changes_after_backup_abort_restore_but_keep_the_backup(
    harness: RestoreHarness, tmp_path: Path, target: str
) -> None:
    source = tmp_path / "source.db"
    _create_source(source)
    harness.service.preview_restore(source)
    candidate = _workspace(harness) / "candidate.db"

    def close_and_change() -> None:
        harness.close()
        with closing(connect(source if target == "source" else candidate)) as conn:
            conn.execute("INSERT INTO staff (name) VALUES ('確認後の変更')")

    with pytest.raises(RestoreError, match="変更") as failure:
        harness.service.restore_after_shutdown(close_and_change)
    assert failure.value.backup_path == _backup_path(harness)
    assert _read_staff(harness.path) == ["現行担当"]
    assert _read_staff(_backup_path(harness)) == ["現行担当"]
    _assert_no_workspace(harness)


def test_current_change_after_backup_blocks_replacement(
    harness: RestoreHarness, tmp_path: Path
) -> None:
    source = tmp_path / "source.db"
    _create_source(source)
    harness.service.preview_restore(source)

    def close_and_change() -> None:
        harness.close()
        with closing(connect(harness.path)) as conn:
            conn.execute("INSERT INTO staff (name) VALUES ('保全後の変更')")

    with pytest.raises(RestoreError, match="一致しません") as failure:
        harness.service.restore_after_shutdown(close_and_change)
    assert failure.value.backup_path == _backup_path(harness)
    assert _read_staff(harness.path) == ["現行担当", "保全後の変更"]
    assert _read_staff(_backup_path(harness)) == ["現行担当"]
    _assert_no_workspace(harness)


def test_unknown_current_sidecar_is_never_deleted(harness: RestoreHarness, tmp_path: Path) -> None:
    source = tmp_path / "source.db"
    _create_source(source)
    harness.service.preview_restore(source)
    sidecar = Path(f"{harness.path}-wal")

    def close_and_add_sidecar() -> None:
        harness.close()
        sidecar.write_bytes(b"foreign wal")

    with pytest.raises(RestoreError, match="関連ファイル"):
        harness.service.restore_after_shutdown(close_and_add_sidecar)
    assert sidecar.read_bytes() == b"foreign wal"
    sidecar.unlink()
    assert _read_staff(harness.path) == ["現行担当"]
    assert _read_staff(_backup_path(harness)) == ["現行担当"]


def test_replace_failure_preserves_current_and_pre_restore_backup(
    harness: RestoreHarness, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source = tmp_path / "source.db"
    _create_source(source)
    harness.service.preview_restore(source)

    def fail_replace(src: object, dest: object) -> None:
        raise PermissionError("置換を拒否")

    monkeypatch.setattr(restore.os, "replace", fail_replace)
    with pytest.raises(RestoreError, match="置換を拒否") as failure:
        harness.service.restore_after_shutdown(harness.close)
    assert failure.value.stage == "overwrite"
    assert failure.value.backup_path == _backup_path(harness)
    assert _read_staff(harness.path) == ["現行担当"]
    assert _read_staff(_backup_path(harness)) == ["現行担当"]
    _assert_no_workspace(harness)


@pytest.mark.parametrize("during_preview", [True, False])
def test_copy_failure_keeps_current_and_does_not_leave_partial_candidate(
    harness: RestoreHarness, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, during_preview: bool
) -> None:
    source = tmp_path / "source.db"
    _create_source(source)
    if not during_preview:
        harness.service.preview_restore(source)

    def fail_copy(conn: sqlite3.Connection, destination: Path) -> None:
        raise OSError(errno.ENOSPC, "容量不足")

    monkeypatch.setattr(restore, "copy_database", fail_copy)
    with pytest.raises(RestoreError, match="容量不足") as failure:
        if during_preview:
            harness.service.preview_restore(source)
        else:
            harness.service.restore_after_shutdown(harness.close)
    assert failure.value.stage == "pre_backup"
    assert not harness.closed
    assert _read_staff(harness.path) == ["現行担当"]
    assert not _backup_path(harness).exists()
    _assert_no_workspace(harness)


def test_invalid_legacy_migration_precheck_keeps_original(
    harness: RestoreHarness, tmp_path: Path
) -> None:
    source = tmp_path / "legacy-invalid.db"
    _create_source(source, version=1)
    with closing(sqlite3.connect(source, autocommit=True)) as conn:
        _seed_legacy_history(conn, multiplier="NaN")
    before = source.read_bytes()
    with pytest.raises(InvalidBackupError, match="移行前検査"):
        harness.service.preview_restore(source)
    assert source.read_bytes() == before
    assert not harness.closed
    _assert_no_workspace(harness)


def test_migration_commit_failure_rolls_back_and_keeps_original(
    harness: RestoreHarness, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source = tmp_path / "legacy.db"
    _create_source(source, version=1)
    before = source.read_bytes()
    statements: list[str] = []

    class CommitFailureConnection(sqlite3.Connection):
        fail_commit = False

        def execute(self, sql: str, parameters: object = (), /) -> sqlite3.Cursor:
            if self.fail_commit and sql == "COMMIT":
                raise sqlite3.OperationalError("移行 COMMIT 失敗")
            if self.fail_commit and sql == "ROLLBACK":
                statements.append(sql)
            return super().execute(sql, parameters)  # type: ignore[arg-type]

    def connect_with_commit_failure(path: Path) -> sqlite3.Connection:
        conn = sqlite3.connect(path, autocommit=True, factory=CommitFailureConnection)
        conn.execute("PRAGMA foreign_keys = ON")
        conn.execute("PRAGMA journal_mode = WAL")
        conn.fail_commit = True
        return conn

    monkeypatch.setattr(restore, "connect", connect_with_commit_failure)
    with pytest.raises(RestoreError, match="COMMIT"):
        harness.service.preview_restore(source)
    assert statements == ["ROLLBACK"]
    assert source.read_bytes() == before
    assert not harness.closed
    assert not _backup_path(harness).exists()
    _assert_no_workspace(harness)


def test_candidate_checkpoint_failure_preserves_original(
    harness: RestoreHarness, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source = tmp_path / "source.db"
    _create_source(source)
    before = source.read_bytes()

    def fail_checkpoint(conn: sqlite3.Connection) -> None:
        raise sqlite3.OperationalError("候補のチェックポイント失敗")

    monkeypatch.setattr(restore, "_checkpoint", fail_checkpoint)
    with pytest.raises(RestoreError, match="チェックポイント失敗"):
        harness.service.preview_restore(source)
    assert source.read_bytes() == before
    assert not harness.closed
    _assert_no_workspace(harness)


def test_open_transaction_prevents_backup_and_restore(
    harness: RestoreHarness, tmp_path: Path
) -> None:
    source = tmp_path / "source.db"
    _create_source(source)
    harness.service.preview_restore(source)
    harness.conn.execute("BEGIN IMMEDIATE")
    try:
        with pytest.raises(FileOutputError, match="トランザクション"):
            harness.service.create_backup(tmp_path / "manual.db")
        with pytest.raises(RestoreError, match="トランザクション") as failure:
            harness.service.restore_after_shutdown(harness.close)
        assert failure.value.stage == "pre_backup"
        assert not _backup_path(harness).exists()
    finally:
        harness.conn.execute("ROLLBACK")
    assert _read_staff(harness.path) == ["現行担当"]
    _assert_no_workspace(harness)


def test_connection_to_another_database_is_rejected(
    harness: RestoreHarness, tmp_path: Path
) -> None:
    other = tmp_path / "other.db"
    _create_source(other)
    repository = SQLiteBackupRestoreRepository(harness.conn, other)
    with pytest.raises(FileOutputError, match="接続中の DB"):
        repository.create_backup(tmp_path / "incorrect.db")
    assert _read_staff(harness.path) == ["現行担当"]
    assert _read_staff(other) == ["候補担当"]


def test_cleanup_failure_after_replacement_reports_that_db_was_replaced(
    harness: RestoreHarness, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source = tmp_path / "source.db"
    _create_source(source)
    harness.service.preview_restore(source)
    candidate = harness.repository._candidate
    assert candidate is not None
    original_remove = restore._Workspace.remove

    def fail_remove(workspace: restore._Workspace) -> None:
        raise PermissionError("一時ディレクトリの削除を拒否")

    monkeypatch.setattr(restore._Workspace, "remove", fail_remove)
    try:
        with pytest.raises(RestoreError, match="置換済み") as failure:
            harness.service.restore_after_shutdown(harness.close)
        assert failure.value.stage == "recovery"
        assert not failure.value.recovered
        assert failure.value.backup_path == _backup_path(harness)
        assert _read_staff(harness.path) == ["候補担当"]
        assert _read_staff(_backup_path(harness)) == ["現行担当"]
    finally:
        original_remove(candidate.workspace)
