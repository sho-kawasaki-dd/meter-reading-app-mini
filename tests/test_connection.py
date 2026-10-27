from __future__ import annotations

import sqlite3
from pathlib import Path
from typing import Any

import pytest

from meter_reading_mini.db.connection import (
    connect,
    connect_memory,
    connect_readonly,
    read_transaction,
    savepoint,
    transaction,
)


class CommitFailureConnection(sqlite3.Connection):
    def execute(self, sql: str, parameters: Any = (), /) -> sqlite3.Cursor:
        if sql == "COMMIT":
            raise sqlite3.OperationalError("injected commit failure")
        return super().execute(sql, parameters)


def test_connect_configures_file_database(tmp_path: Path) -> None:
    conn = connect(tmp_path / "meter_reading.db")
    try:
        assert conn.execute("PRAGMA foreign_keys").fetchone() == (1,)
        assert conn.execute("PRAGMA journal_mode").fetchone() == ("wal",)
        assert conn.execute("PRAGMA busy_timeout").fetchone() == (5000,)
    finally:
        conn.close()


def test_connect_readonly_rejects_writes_without_changing_journal_mode(
    tmp_path: Path,
) -> None:
    path = tmp_path / "readonly.db"
    setup_conn = sqlite3.connect(path, autocommit=True)
    setup_conn.execute("PRAGMA journal_mode = DELETE")
    setup_conn.close()

    conn = connect_readonly(path)
    try:
        assert conn.execute("PRAGMA foreign_keys").fetchone() == (1,)
        assert conn.execute("PRAGMA busy_timeout").fetchone() == (5000,)
        with pytest.raises(sqlite3.OperationalError):
            conn.execute("CREATE TABLE forbidden (id INTEGER)")
    finally:
        conn.close()

    verify_conn = sqlite3.connect(path)
    try:
        assert verify_conn.execute("PRAGMA journal_mode").fetchone() == ("delete",)
    finally:
        verify_conn.close()


def test_connect_readonly_supports_unicode_and_spaces_in_path(tmp_path: Path) -> None:
    path = tmp_path / "日本語 directory" / "検針 database.db"
    path.parent.mkdir()
    setup_conn = sqlite3.connect(path)
    setup_conn.execute("CREATE TABLE sample (value TEXT)")
    setup_conn.close()

    conn = connect_readonly(path)
    try:
        assert conn.execute("SELECT name FROM sqlite_master WHERE name = 'sample'").fetchone() == (
            "sample",
        )
    finally:
        conn.close()


def test_connect_readonly_requires_existing_file(tmp_path: Path) -> None:
    with pytest.raises(FileNotFoundError):
        connect_readonly(tmp_path / "missing.db")


def test_connect_memory_configures_foreign_keys() -> None:
    conn = connect_memory()
    try:
        assert conn.execute("PRAGMA foreign_keys").fetchone() == (1,)
        assert conn.execute("PRAGMA busy_timeout").fetchone() == (5000,)
    finally:
        conn.close()


def test_transaction_commits_rolls_back_and_rejects_nesting() -> None:
    conn = connect_memory()
    conn.execute("CREATE TABLE sample (value INTEGER)")
    try:
        with transaction(conn):
            conn.execute("INSERT INTO sample VALUES (1)")
        assert not conn.in_transaction
        assert conn.execute("SELECT value FROM sample").fetchall() == [(1,)]

        with pytest.raises(ValueError), transaction(conn):
            conn.execute("INSERT INTO sample VALUES (2)")
            raise ValueError("rollback")
        assert not conn.in_transaction
        assert conn.execute("SELECT value FROM sample").fetchall() == [(1,)]

        with transaction(conn), pytest.raises(RuntimeError), transaction(conn):
            pass
    finally:
        conn.close()


def test_transaction_rolls_back_when_commit_fails() -> None:
    conn = sqlite3.connect(":memory:", autocommit=True, factory=CommitFailureConnection)
    conn.execute("CREATE TABLE sample (value INTEGER)")
    try:
        with (
            pytest.raises(sqlite3.OperationalError, match="injected commit failure"),
            transaction(conn),
        ):
            conn.execute("INSERT INTO sample VALUES (1)")
        assert not conn.in_transaction
        assert conn.execute("SELECT value FROM sample").fetchall() == []
    finally:
        conn.close()


def test_savepoint_requires_transaction_and_valid_name() -> None:
    conn = connect_memory()
    try:
        with pytest.raises(RuntimeError, match="トランザクション"), savepoint(conn, "nested_work"):
            pass

        with transaction(conn):
            with (
                pytest.raises(ValueError, match="SAVEPOINT"),
                savepoint(conn, "nested; DROP TABLE sample"),
            ):
                pass
            with pytest.raises(ValueError, match="SAVEPOINT"), savepoint(conn, "1invalid"):
                pass
    finally:
        conn.close()


def test_savepoint_releases_on_success_and_rolls_back_only_its_changes() -> None:
    conn = connect_memory()
    conn.execute("CREATE TABLE sample (value INTEGER)")
    try:
        with transaction(conn):
            conn.execute("INSERT INTO sample VALUES (1)")
            with savepoint(conn, "successful_work"):
                conn.execute("INSERT INTO sample VALUES (2)")
            with (
                pytest.raises(ValueError, match="rollback savepoint"),
                savepoint(conn, "failed_work"),
            ):
                conn.execute("INSERT INTO sample VALUES (3)")
                raise ValueError("rollback savepoint")
            conn.execute("INSERT INTO sample VALUES (4)")

        assert conn.execute("SELECT value FROM sample ORDER BY value").fetchall() == [
            (1,),
            (2,),
            (4,),
        ]
        assert not conn.in_transaction
    finally:
        conn.close()


def test_read_transaction_owns_commit_and_rollback() -> None:
    conn = connect_memory()
    conn.execute("CREATE TABLE sample (value INTEGER)")
    try:
        with read_transaction(conn):
            assert conn.in_transaction
            conn.execute("INSERT INTO sample VALUES (1)")
        assert not conn.in_transaction
        assert conn.execute("SELECT value FROM sample").fetchall() == [(1,)]

        with pytest.raises(ValueError), read_transaction(conn):
            conn.execute("INSERT INTO sample VALUES (2)")
            raise ValueError("rollback")
        assert not conn.in_transaction
        assert conn.execute("SELECT value FROM sample").fetchall() == [(1,)]
    finally:
        conn.close()


def test_read_transaction_participates_in_existing_transaction() -> None:
    conn = connect_memory()
    conn.execute("CREATE TABLE sample (value INTEGER)")
    conn.execute("BEGIN IMMEDIATE")
    try:
        with read_transaction(conn):
            conn.execute("INSERT INTO sample VALUES (1)")
            assert conn.in_transaction
        assert conn.in_transaction
        conn.execute("ROLLBACK")
        assert conn.execute("SELECT value FROM sample").fetchall() == []
    finally:
        if conn.in_transaction:
            conn.execute("ROLLBACK")
        conn.close()
