from __future__ import annotations

import sqlite3
from importlib.resources import files
from pathlib import Path

import pytest

from meter_reading_mini.core.errors import (
    MigrationError,
    SchemaTooNewError,
    UnsupportedSchemaError,
)
from meter_reading_mini.db import migrations
from meter_reading_mini.db.connection import connect_memory
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
    _create_database(path, version=1)
    migration_sql = "ALTER TABLE meter_app_settings ADD COLUMN note TEXT;"
    spec_v2 = migrations._schema_spec_from_ddl(_schema_sql() + "\n" + migration_sql)

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
    _create_database(path, version=1)
    migration_sql = "ALTER TABLE meter_app_settings ADD COLUMN note TEXT;"
    spec_v2 = migrations._schema_spec_from_ddl(_schema_sql() + "\n" + migration_sql)

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
