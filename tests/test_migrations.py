from __future__ import annotations

import sqlite3
from contextlib import closing
from importlib.resources import files
from pathlib import Path

import pytest

from meter_reading_mini.core.errors import (
    MigrationError,
    SchemaTooNewError,
    UnsupportedSchemaError,
)
from meter_reading_mini.db import migrations
from meter_reading_mini.db.backup import check_sqlite_integrity, copy_database
from meter_reading_mini.db.connection import connect_memory, connect_readonly
from meter_reading_mini.db.integrity import (
    check_business_integrity,
    list_business_integrity_warnings,
)
from meter_reading_mini.db.migrations import (
    SCHEMA_SPECS,
    SCHEMA_VERSION,
    MigrationStep,
    check_migration_path,
    create_schema,
    inspect_schema,
    normalize_sql,
    open_database,
)


def _schema_sql() -> str:
    return files("meter_reading_mini.db").joinpath("schema.sql").read_text(encoding="utf-8")


def _schema_v1_sql() -> str:
    return files("meter_reading_mini.db").joinpath("schema_v1.sql").read_text(encoding="utf-8")


def _create_database(
    path: Path, *, schema_sql: str | None = None, version: int = SCHEMA_VERSION
) -> None:
    conn = sqlite3.connect(path, autocommit=True)
    try:
        conn.executescript(_schema_sql() if schema_sql is None else schema_sql)
        conn.execute(f"PRAGMA user_version = {version}")
    finally:
        conn.close()


def test_new_database_is_created_and_persists_schema(tmp_path: Path) -> None:
    path = tmp_path / "meter_reading.db"
    conn = open_database(path, tmp_path / "backups", backup_timestamp=lambda: "20261020_120000")
    try:
        assert conn.execute("PRAGMA user_version").fetchone() == (SCHEMA_VERSION,)
        assert conn.execute("PRAGMA journal_mode").fetchone() == ("wal",)
        assert inspect_schema(conn, SCHEMA_VERSION) == []
    finally:
        conn.close()

    reopened = open_database(path, tmp_path / "backups", backup_timestamp=lambda: "unused")
    reopened.close()


def test_create_schema_rolls_back_partial_ddl() -> None:
    conn = connect_memory()
    try:
        with pytest.raises(sqlite3.OperationalError):
            create_schema(conn, "CREATE TABLE partial (id INTEGER); INVALID DDL;")
        assert (
            conn.execute("SELECT name FROM sqlite_master WHERE name = 'partial'").fetchone() is None
        )
        assert conn.execute("PRAGMA user_version").fetchone() == (0,)
        assert not conn.in_transaction
    finally:
        conn.close()


def test_new_database_failure_removes_database_and_sidecars(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "failed.db"

    def fail_create(
        conn: sqlite3.Connection,
        *,
        version: int,
        specs: dict[int, migrations.SchemaSpec],
    ) -> None:
        create_schema(
            conn, "CREATE TABLE partial (id INTEGER); INVALID DDL;", version=version, specs=specs
        )

    monkeypatch.setattr(migrations, "create_schema", fail_create)
    with pytest.raises(sqlite3.OperationalError):
        open_database(path, tmp_path / "backups", backup_timestamp=lambda: "unused")
    assert not path.exists()
    assert not Path(f"{path}-wal").exists()
    assert not Path(f"{path}-shm").exists()


def test_existing_version_zero_is_rejected(tmp_path: Path) -> None:
    path = tmp_path / "empty.db"
    path.touch()
    with pytest.raises(UnsupportedSchemaError, match="版 0"):
        open_database(path, tmp_path / "backups", backup_timestamp=lambda: "unused")
    assert path.exists()


def test_newer_schema_is_rejected(tmp_path: Path) -> None:
    path = tmp_path / "newer.db"
    _create_database(path, version=SCHEMA_VERSION + 1)
    with pytest.raises(SchemaTooNewError):
        open_database(path, tmp_path / "backups", backup_timestamp=lambda: "unused")


@pytest.mark.parametrize(
    ("schema_version", "min_supported"),
    [(True, 1), (1, True), (0, 1), (1, 0), (1, 2)],
)
def test_invalid_version_configuration_is_rejected(
    tmp_path: Path, schema_version: int, min_supported: int
) -> None:
    with pytest.raises(ValueError):
        open_database(
            tmp_path / "meter_reading.db",
            tmp_path / "backups",
            backup_timestamp=lambda: "unused",
            schema_version=schema_version,
            min_supported=min_supported,
        )


@pytest.mark.parametrize(
    ("old", "new"),
    [
        ("  meter_serial TEXT,\n", ""),
        (
            "name TEXT NOT NULL UNIQUE,",
            "name TEXT NOT NULL,",
        ),
        (
            "meter_id TEXT REFERENCES physical_meters(meter_id),",
            "meter_id TEXT,",
        ),
        (
            "CREATE INDEX idx_physical_meters_location ON physical_meters(location_id);\n",
            "",
        ),
    ],
    ids=[
        "missing-column",
        "missing-unique",
        "missing-fk",
        "missing-index",
    ],
)
def test_inspect_schema_rejects_schema_changes(old: str, new: str) -> None:
    modified_sql = _schema_sql().replace(old, new, 1)
    assert modified_sql != _schema_sql()
    conn = connect_memory()
    try:
        conn.executescript(modified_sql)
        reasons = inspect_schema(conn, SCHEMA_VERSION)
        assert reasons
    finally:
        conn.close()


def test_normalize_sql_preserves_literals_and_quoted_identifiers() -> None:
    original = "CREATE TABLE \"MiXeD\" (value TEXT DEFAULT 'A  -- /* ''x'' */'); -- comment\n"
    formatted = (
        " create\n table \"MiXeD\"( value text default 'A  -- /* ''x'' */' ) ; /* comment */"
    )
    assert normalize_sql(original) == normalize_sql(formatted)
    assert normalize_sql(original) != normalize_sql(original.replace("'A  --", "'a  --"))
    assert normalize_sql(original) == normalize_sql(original.replace('"MiXeD"', '"mixed"'))


def test_check_migration_path_requires_every_intermediate_version() -> None:
    check_migration_path(1, 3, {2: "SELECT 1;", 3: "SELECT 2;"})
    with pytest.raises(UnsupportedSchemaError, match="経路"):
        check_migration_path(1, 3, {3: "SELECT 2;"})


def test_migrates_one_version_and_creates_timestamped_backup(tmp_path: Path) -> None:
    path = tmp_path / "meter_reading.db"
    _create_database(path, schema_sql=_schema_v1_sql(), version=1)
    migration_sql = "ALTER TABLE meter_app_settings ADD COLUMN note TEXT;"
    spec_v2 = migrations._schema_spec_from_ddl(_schema_v1_sql() + "\n" + migration_sql)

    conn = open_database(
        path,
        tmp_path / "backups",
        backup_timestamp=lambda: "20261020_120000",
        schema_version=2,
        min_supported=1,
        migrations={2: MigrationStep(migration_sql)},
        specs={**SCHEMA_SPECS, 2: spec_v2},
    )
    try:
        assert conn.execute("PRAGMA user_version").fetchone() == (2,)
        assert (tmp_path / "backups" / "pre-migrate_v1_20261020_120000.db").is_file()
    finally:
        conn.close()


def test_migration_precheck_failure_aborts_before_backup(tmp_path: Path) -> None:
    path = tmp_path / "meter_reading.db"
    _create_database(path, schema_sql=_schema_v1_sql(), version=1)
    migration_sql = "ALTER TABLE meter_app_settings ADD COLUMN note TEXT;"
    spec_v2 = migrations._schema_spec_from_ddl(_schema_v1_sql() + "\n" + migration_sql)

    with pytest.raises(MigrationError, match="データベースは変更されていません"):
        open_database(
            path,
            tmp_path / "backups",
            backup_timestamp=lambda: "unused",
            schema_version=2,
            min_supported=1,
            migrations={
                2: MigrationStep(
                    migration_sql,
                    precheck=lambda _conn: ["設定値が不正です"],
                )
            },
            specs={**SCHEMA_SPECS, 2: spec_v2},
        )
    assert list((tmp_path / "backups").glob("pre-migrate_*.db")) == []


def _insert_v1_assignment_history(conn: sqlite3.Connection) -> None:
    conn.executemany(
        "INSERT INTO physical_meters "
        "(meter_id, location_id, location_name, meter_name, category) VALUES (?, ?, ?, ?, ?)",
        [
            ("old-meter", "location", "試験場所", "旧メーター", "water"),
            ("new-meter", "location", "試験場所", "新メーター", "water"),
        ],
    )
    conn.execute(
        "INSERT INTO measurement_definitions "
        "(measurement_definition_id, location_id, meter_id, display_name, category, unit, "
        "value_schema, multiplier, decimal_places, rollover_value, calculation_method, "
        "reading_cycle, purpose, generation_start_date) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            "definition",
            "location",
            "new-meter",
            "試験対象",
            "water",
            "m3",
            "decimal_string",
            "10",
            2,
            "1000",
            "delta",
            "daily_1x",
            "operational",
            "2026-02-01",
        ),
    )
    conn.executemany(
        "INSERT INTO meter_assignments "
        "(measurement_definition_id, meter_id, effective_from, effective_to) VALUES (?, ?, ?, ?)",
        [
            ("definition", "old-meter", "2026-02-01 00:00:00", "2026-02-04 04:00:00"),
            ("definition", "new-meter", "2026-02-04 04:00:00", None),
        ],
    )
    conn.execute(
        "INSERT INTO planned_readings "
        "(planned_reading_id, measurement_definition_id, meter_id, location_id, business_date, "
        "release_date, reading_session, scheduled_at, task_state) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            "old-reading",
            "definition",
            "old-meter",
            "location",
            "2026-02-04",
            "2026-02-04",
            "08:00",
            "2026-02-04 03:00:00",
            "completed",
        ),
    )
    conn.execute(
        "INSERT INTO reading_transactions "
        "(planned_reading_id, result_status, recorded_at, client_updated_at, raw_reading, "
        "last_reading, usage_delta, updated_by, signature, result_source) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            "old-reading",
            "measured",
            "2026-02-04 03:00:00",
            "2026-02-04 03:00:00",
            "100",
            "80",
            "200",
            "担当者",
            "署名",
            "manual",
        ),
    )
    conn.execute(
        "INSERT INTO replacement_events "
        "(replacement_event_id, location_id, old_meter_id, new_meter_id, replaced_at, "
        "old_final_reading, new_initial_reading, client_updated_at, updated_by) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            "event",
            "location",
            "old-meter",
            "new-meter",
            "2026-02-04 04:00:00",
            "120",
            "5",
            "2026-02-04 04:00:00",
            "担当者",
        ),
    )
    conn.execute(
        "INSERT INTO replacement_boundaries "
        "(replacement_event_id, measurement_definition_id, location_id, business_date, "
        "old_meter_id, new_meter_id, replaced_at, previous_measured_reading, "
        "previous_measured_planned_reading_id, old_final_reading, old_meter_usage_delta, "
        "new_initial_reading, created_at, updated_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            "event",
            "definition",
            "location",
            "2026-02-04",
            "old-meter",
            "new-meter",
            "2026-02-04 04:00:00",
            "100",
            "old-reading",
            "120",
            "200",
            "5",
            "2026-02-04 04:00:00",
            "2026-02-04 04:00:00",
        ),
    )


def test_v1_migration_initializes_assignment_conditions_without_recalculating(
    tmp_path: Path,
) -> None:
    path = tmp_path / "v1.db"
    _create_database(path, schema_sql=_schema_v1_sql(), version=1)
    with closing(sqlite3.connect(path, autocommit=True)) as original:
        _insert_v1_assignment_history(original)
        old_reading = original.execute("SELECT * FROM reading_transactions").fetchone()

    conn = open_database(path, tmp_path / "backups", backup_timestamp=lambda: "test")
    try:
        assert conn.execute("PRAGMA user_version").fetchone() == (SCHEMA_VERSION,)
        assert inspect_schema(conn, SCHEMA_VERSION) == []
        assert conn.execute("PRAGMA foreign_key_check").fetchall() == []
        assert conn.execute(
            "SELECT meter_id, multiplier, rollover_value FROM meter_assignments ORDER BY meter_id"
        ).fetchall() == [("new-meter", "10", "1000"), ("old-meter", "10", "1000")]
        assert conn.execute(
            "SELECT old_assignment_effective_from, new_assignment_effective_from, "
            "old_meter_usage_delta FROM replacement_boundaries"
        ).fetchone() == ("2026-02-01 00:00:00", "2026-02-04 04:00:00", "200")
        assert conn.execute(
            "SELECT old_final_reading, new_initial_reading FROM replacement_events"
        ).fetchone() == (None, None)
        assert conn.execute("SELECT * FROM reading_transactions").fetchone() == old_reading
        assert conn.execute(
            "SELECT COUNT(*) FROM assignment_calculation_corrections"
        ).fetchone() == (0,)
        assert check_business_integrity(conn) == []
        assert (tmp_path / "backups" / "pre-migrate_v1_test.db").is_file()
    finally:
        conn.close()


def test_v1_migration_flags_historical_values_conflicting_with_copied_terms(
    tmp_path: Path,
) -> None:
    path = tmp_path / "v1-review.db"
    _create_database(path, schema_sql=_schema_v1_sql(), version=1)
    with closing(sqlite3.connect(path, autocommit=True)) as original:
        _insert_v1_assignment_history(original)
        original.execute(
            "UPDATE measurement_definitions SET rollover_value = '50', decimal_places = 0"
        )

    conn = open_database(path, tmp_path / "backups", backup_timestamp=lambda: "review")
    try:
        flags = conn.execute(
            "SELECT measurement_definition_id, meter_id, reason FROM assignment_review_flags"
        ).fetchall()
        assert len(flags) == 1
        assert flags[0][1:] == (
            "old-meter",
            "過去実測値が現在のロールオーバー値以上です、"
            "交換境界の前回値が現在のロールオーバー値以上です、"
            "交換境界の旧最終値が現在のロールオーバー値以上です",
        )
        assert check_business_integrity(conn) == []
        assert len(list_business_integrity_warnings(conn)) == 1
    finally:
        conn.close()


def test_v1_migration_keeps_historical_precision_without_review_flag(
    tmp_path: Path,
) -> None:
    path = tmp_path / "v1-precision.db"
    _create_database(path, schema_sql=_schema_v1_sql(), version=1)
    with closing(sqlite3.connect(path, autocommit=True)) as original:
        _insert_v1_assignment_history(original)
        original.execute(
            "UPDATE measurement_definitions SET decimal_places = 0, rollover_value = '1000'"
        )
        original.execute(
            "UPDATE reading_transactions SET raw_reading = '100.25', usage_delta = '202.5'"
        )
        original.execute("UPDATE replacement_boundaries SET previous_measured_reading = '100.25'")
        original.execute("UPDATE replacement_boundaries SET old_meter_usage_delta = '197.5'")

    conn = open_database(path, tmp_path / "backups", backup_timestamp=lambda: "precision")
    try:
        assert conn.execute("SELECT raw_reading FROM reading_transactions").fetchone() == (
            "100.25",
        )
        assert conn.execute(
            "SELECT previous_measured_reading FROM replacement_boundaries"
        ).fetchone() == ("100.25",)
        assert conn.execute("SELECT COUNT(*) FROM assignment_review_flags").fetchone() == (0,)
        assert check_business_integrity(conn) == []
    finally:
        conn.close()


def test_v1_migration_rejects_unresolvable_boundary_without_modifying_database(
    tmp_path: Path,
) -> None:
    path = tmp_path / "invalid-v1.db"
    _create_database(path, schema_sql=_schema_v1_sql(), version=1)
    with closing(sqlite3.connect(path, autocommit=True)) as conn:
        _insert_v1_assignment_history(conn)
        conn.execute("DELETE FROM meter_assignments WHERE meter_id = 'old-meter'")

    with pytest.raises(MigrationError, match="一意に対応付けられません"):
        open_database(path, tmp_path / "backups", backup_timestamp=lambda: "test")
    with closing(sqlite3.connect(path, autocommit=True)) as conn:
        assert conn.execute("PRAGMA user_version").fetchone() == (1,)
        assert inspect_schema(conn, 1) == []
        assert conn.execute(
            "SELECT old_meter_usage_delta FROM replacement_boundaries"
        ).fetchone() == ("200",)
    assert not (tmp_path / "backups").exists()


def test_v1_migration_rolls_back_table_rebuild_and_version_on_schema_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "rollback-v1.db"
    _create_database(path, schema_sql=_schema_v1_sql(), version=1)
    conn = migrations.connect(path)
    try:
        _insert_v1_assignment_history(conn)
        inspect = migrations.inspect_schema

        def fail_v2_schema_check(connection, version, specs=SCHEMA_SPECS):
            return ["検査失敗"] if version == 2 else inspect(connection, version, specs)

        monkeypatch.setattr(migrations, "inspect_schema", fail_v2_schema_check)
        with pytest.raises(UnsupportedSchemaError, match="検査失敗"):
            migrations.migrate_schema(conn, 1, 2)
        assert conn.execute("PRAGMA user_version").fetchone() == (1,)
        assert conn.execute("PRAGMA foreign_keys").fetchone() == (1,)
        assert inspect(conn, 1) == []
        assert conn.execute("SELECT COUNT(*) FROM meter_assignments").fetchone() == (2,)
        assert conn.execute("SELECT usage_delta FROM reading_transactions").fetchone() == ("200",)
        assert not conn.in_transaction
    finally:
        conn.close()


@pytest.mark.parametrize(
    ("column", "value"),
    [
        ("multiplier", "NaN"),
        ("multiplier", "1e2"),
        ("multiplier", "0"),
        ("multiplier", "-1"),
        ("rollover_value", "Infinity"),
        ("rollover_value", "0"),
    ],
)
def test_v1_migration_rejects_invalid_calculation_terms(
    tmp_path: Path, column: str, value: str
) -> None:
    path = tmp_path / "invalid-terms.db"
    _create_database(path, schema_sql=_schema_v1_sql(), version=1)
    with closing(sqlite3.connect(path, autocommit=True)) as conn:
        _insert_v1_assignment_history(conn)
        conn.execute(f"UPDATE measurement_definitions SET {column} = ?", (value,))
    with pytest.raises(MigrationError, match="不正"):
        open_database(path, tmp_path / "backups", backup_timestamp=lambda: "test")
    with closing(sqlite3.connect(path, autocommit=True)) as conn:
        assert conn.execute("PRAGMA user_version").fetchone() == (1,)
        assert inspect_schema(conn, 1) == []
        assert conn.execute("SELECT usage_delta FROM reading_transactions").fetchone() == ("200",)
    assert not (tmp_path / "backups").exists()


def test_v1_migration_rejects_overlapping_assignment_periods(tmp_path: Path) -> None:
    path = tmp_path / "overlap.db"
    _create_database(path, schema_sql=_schema_v1_sql(), version=1)
    with closing(sqlite3.connect(path, autocommit=True)) as conn:
        _insert_v1_assignment_history(conn)
        conn.execute(
            "INSERT INTO meter_assignments VALUES "
            "('definition', 'new-meter', '2026-02-02 00:00:00', '2026-02-04 03:00:00')"
        )
    with pytest.raises(MigrationError, match="割当期間が重複"):
        open_database(path, tmp_path / "backups", backup_timestamp=lambda: "test")
    with closing(sqlite3.connect(path, autocommit=True)) as conn:
        assert conn.execute("PRAGMA user_version").fetchone() == (1,)
        assert inspect_schema(conn, 1) == []


def test_v1_migration_commit_failure_rolls_back_schema_and_data(tmp_path: Path) -> None:
    class CommitFailingConnection(sqlite3.Connection):
        fail_commit = False

        def execute(self, sql, parameters=()):
            if self.fail_commit and sql.strip().upper() == "COMMIT":
                raise sqlite3.OperationalError("simulated commit failure")
            return super().execute(sql, parameters)

    conn = sqlite3.connect(
        tmp_path / "commit-failure.db", autocommit=True, factory=CommitFailingConnection
    )
    try:
        conn.execute("PRAGMA foreign_keys = ON")
        conn.executescript(_schema_v1_sql())
        conn.execute("PRAGMA user_version = 1")
        _insert_v1_assignment_history(conn)
        before = conn.execute("SELECT * FROM reading_transactions").fetchall()
        conn.fail_commit = True
        with pytest.raises(sqlite3.OperationalError, match="simulated commit failure"):
            migrations.migrate_schema(conn, 1, 2)
        assert conn.execute("PRAGMA user_version").fetchone() == (1,)
        assert conn.execute("PRAGMA foreign_keys").fetchone() == (1,)
        assert inspect_schema(conn, 1) == []
        assert conn.execute("SELECT * FROM reading_transactions").fetchall() == before
        assert not conn.in_transaction
    finally:
        conn.close()


def test_v1_restore_candidate_is_copied_migrated_and_checked_without_altering_source(
    tmp_path: Path,
) -> None:
    source_path = tmp_path / "source-v1.db"
    _create_database(source_path, schema_sql=_schema_v1_sql(), version=1)
    with closing(sqlite3.connect(source_path, autocommit=True)) as original:
        _insert_v1_assignment_history(original)
    readonly = connect_readonly(source_path)
    temporary_path = tmp_path / "temporary-copy.db"
    try:
        assert readonly.execute("PRAGMA user_version").fetchone() == (1,)
        assert inspect_schema(readonly, 1) == []
        assert check_sqlite_integrity(readonly) == []
        copy_database(readonly, temporary_path)
    finally:
        readonly.close()
    temporary = open_database(
        temporary_path, tmp_path / "migration-backups", backup_timestamp=lambda: "test"
    )
    try:
        assert check_business_integrity(temporary) == []
        assert temporary.execute("SELECT usage_delta FROM reading_transactions").fetchone() == (
            "200",
        )
    finally:
        temporary.close()
    source = connect_readonly(source_path)
    try:
        assert source.execute("PRAGMA user_version").fetchone() == (1,)
        assert inspect_schema(source, 1) == []
    finally:
        source.close()
