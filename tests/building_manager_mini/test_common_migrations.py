from __future__ import annotations

import sqlite3
from pathlib import Path
from uuid import uuid4

import pytest

from building_manager_mini.db import migrations


def test_new_database_initializes_and_reopens_without_changing_records(tmp_path: Path) -> None:
    path = tmp_path / "common.db"
    staff_id = str(uuid4())
    source_id = str(uuid4())
    conn = migrations.open_database(path)
    assert conn.execute("PRAGMA journal_mode").fetchone() == ("wal",)
    assert conn.execute("PRAGMA foreign_keys").fetchone() == (1,)
    assert conn.execute("PRAGMA busy_timeout").fetchone() == (5000,)
    assert conn.execute("PRAGMA user_version").fetchone() == (1,)
    assert conn.execute("SELECT timezone FROM common_settings").fetchone() == ("Asia/Tokyo",)
    conn.execute(
        "INSERT INTO staff VALUES (?, ?, ?, ?)", (staff_id, "担当者", 0, "Test@Example.com")
    )
    conn.execute(
        "INSERT INTO staff_legacy_id_map VALUES (?, ?, ?, ?)",
        ("meter-reading", source_id, 42, staff_id),
    )
    conn.close()
    conn = migrations.open_database(path)
    try:
        assert conn.execute("SELECT * FROM staff").fetchall() == [
            (staff_id, "担当者", 0, "Test@Example.com")
        ]
        assert conn.execute("SELECT * FROM staff_legacy_id_map").fetchall() == [
            ("meter-reading", source_id, 42, staff_id)
        ]
    finally:
        conn.close()


def test_readonly_connection_validates_and_rejects_writes(tmp_path: Path) -> None:
    path = tmp_path / "common.db"
    with pytest.raises(FileNotFoundError):
        migrations.open_database_readonly(path)
    migrations.open_database(path).close()
    conn = migrations.open_database_readonly(path)
    try:
        assert conn.execute("PRAGMA query_only").fetchone() == (1,)
        assert conn.execute("PRAGMA foreign_keys").fetchone() == (1,)
        assert conn.execute("SELECT timezone FROM common_settings").fetchone() == ("Asia/Tokyo",)
        with pytest.raises(sqlite3.OperationalError, match="readonly"):
            conn.execute("UPDATE common_settings SET timezone = 'UTC'")
    finally:
        conn.close()
    writer = migrations.open_database(path)
    writer.execute("DROP TABLE staff_legacy_id_map")
    writer.close()
    with pytest.raises(migrations.CommonSchemaError, match="一致しません"):
        migrations.open_database_readonly(path)


def test_schema_enforces_staff_identifiers_names_and_references(tmp_path: Path) -> None:
    conn = migrations.open_database(tmp_path / "common.db")
    try:
        staff_id = str(uuid4())
        conn.execute("INSERT INTO staff VALUES (?, '担当者', 1, '')", (staff_id,))
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute("INSERT INTO staff VALUES (?, '担当者', 1, '')", (str(uuid4()),))
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute("INSERT INTO staff VALUES ('invalid', '別人', 1, '')")
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute("INSERT INTO staff VALUES (?, '別人', 2, '')", (str(uuid4()),))
        conn.execute("INSERT INTO staff VALUES (?, '別人', 1, 'same@example.com')", (str(uuid4()),))
        conn.execute(
            "UPDATE staff SET sender_address = 'same@example.com' WHERE staff_id = ?", (staff_id,)
        )
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute(
                "INSERT INTO staff_legacy_id_map VALUES (?, ?, ?, ?)",
                ("meter-reading", "bad-uuid", 1, staff_id),
            )
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute(
                "INSERT INTO staff_legacy_id_map VALUES (?, ?, ?, ?)",
                ("meter-reading", str(uuid4()), 1, str(uuid4())),
            )
        values = ("meter-reading", str(uuid4()), 1, staff_id)
        conn.execute("INSERT INTO staff_legacy_id_map VALUES (?, ?, ?, ?)", values)
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute("INSERT INTO staff_legacy_id_map VALUES (?, ?, ?, ?)", values)
    finally:
        conn.close()


@pytest.mark.parametrize("version", [2, 100])
def test_unsupported_version_is_rejected_without_changing_schema(
    tmp_path: Path, version: int
) -> None:
    path = tmp_path / "common.db"
    conn = sqlite3.connect(path, autocommit=True)
    conn.execute(f"PRAGMA user_version = {version}")
    conn.close()
    with pytest.raises(migrations.CommonSchemaError, match="未対応"):
        migrations.open_database(path)
    conn = sqlite3.connect(path, autocommit=True)
    try:
        assert conn.execute("PRAGMA user_version").fetchone() == (version,)
        assert conn.execute("SELECT name FROM sqlite_master").fetchall() == []
    finally:
        conn.close()


def test_unversioned_nonempty_database_is_rejected(tmp_path: Path) -> None:
    path = tmp_path / "common.db"
    conn = sqlite3.connect(path, autocommit=True)
    conn.execute("CREATE TABLE unrelated (id INTEGER)")
    conn.close()
    with pytest.raises(migrations.CommonSchemaError, match="既存のスキーマ"):
        migrations.open_database(path)
    conn = sqlite3.connect(path, autocommit=True)
    try:
        assert conn.execute("PRAGMA user_version").fetchone() == (0,)
        assert conn.execute("SELECT name FROM sqlite_master").fetchall() == [("unrelated",)]
    finally:
        conn.close()


def test_schema_mismatch_and_broken_foreign_keys_are_rejected(tmp_path: Path) -> None:
    path = tmp_path / "common.db"
    conn = migrations.open_database(path)
    conn.execute("CREATE TABLE unexpected (id INTEGER)")
    with pytest.raises(migrations.CommonSchemaError, match="一致しません"):
        migrations.validate_schema(conn)
    conn.execute("DROP TABLE unexpected")
    conn.execute("PRAGMA foreign_keys = OFF")
    conn.execute(
        "INSERT INTO staff_legacy_id_map VALUES (?, ?, ?, ?)",
        ("meter-reading", str(uuid4()), 1, str(uuid4())),
    )
    with pytest.raises(migrations.CommonSchemaError, match="担当者参照"):
        migrations.validate_schema(conn)
    conn.close()


def test_missing_settings_and_failed_check_are_rejected(tmp_path: Path) -> None:
    conn = migrations.open_database(tmp_path / "common.db")
    try:
        conn.execute("DELETE FROM common_settings")
        with pytest.raises(migrations.CommonSchemaError, match="タイムゾーン"):
            migrations.validate_schema(conn)
        conn.execute("INSERT INTO common_settings VALUES (1, 'Asia/Tokyo')")
        conn.execute("PRAGMA ignore_check_constraints = ON")
        conn.execute("INSERT INTO staff VALUES (?, '担当者', 3, '')", (str(uuid4()),))
        conn.execute("PRAGMA ignore_check_constraints = OFF")
        with pytest.raises(migrations.CommonSchemaError, match="整合性検査"):
            migrations.validate_schema(conn)
    finally:
        conn.close()


def test_initialization_rejects_an_existing_transaction(tmp_path: Path) -> None:
    conn = sqlite3.connect(tmp_path / "common.db", autocommit=True)
    try:
        conn.execute("BEGIN")
        with pytest.raises(migrations.CommonSchemaError, match="トランザクション外"):
            migrations.initialize_schema(conn)
        conn.execute("ROLLBACK")
    finally:
        conn.close()


@pytest.mark.parametrize("timezone", ["Invalid/Zone", "../bad"])
def test_invalid_timezone_is_rejected(tmp_path: Path, timezone: str) -> None:
    conn = migrations.open_database(tmp_path / "common.db")
    try:
        conn.execute("UPDATE common_settings SET timezone = ?", (timezone,))
        with pytest.raises(migrations.CommonSchemaError, match="タイムゾーン"):
            migrations.validate_schema(conn)
    finally:
        conn.close()


@pytest.mark.parametrize("address", ["not-an-address", " valid@example.com "])
def test_invalid_stored_sender_address_is_rejected(tmp_path: Path, address: str) -> None:
    conn = migrations.open_database(tmp_path / "common.db")
    try:
        conn.execute("INSERT INTO staff VALUES (?, '担当者', 1, ?)", (str(uuid4()), address))
        with pytest.raises(migrations.CommonSchemaError, match="差出人"):
            migrations.validate_schema(conn)
    finally:
        conn.close()


def test_invalid_stored_name_is_rejected(tmp_path: Path) -> None:
    conn = migrations.open_database(tmp_path / "common.db")
    try:
        conn.execute("INSERT INTO staff VALUES (?, ?, 1, '')", (str(uuid4()), " 担当者 "))
        with pytest.raises(migrations.CommonSchemaError, match="担当者名"):
            migrations.validate_schema(conn)
    finally:
        conn.close()


def test_initialization_rolls_back_if_validation_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    conn = sqlite3.connect(tmp_path / "common.db", autocommit=True)

    def fail(_conn: sqlite3.Connection) -> None:
        raise migrations.CommonSchemaError("検査失敗")

    monkeypatch.setattr(migrations, "validate_schema", fail)
    with pytest.raises(migrations.CommonSchemaError, match="検査失敗"):
        migrations.initialize_schema(conn)
    assert conn.execute("PRAGMA user_version").fetchone() == (0,)
    assert conn.execute("SELECT name FROM sqlite_master").fetchall() == []
    conn.close()
