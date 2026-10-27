from __future__ import annotations

import re
import sqlite3
from collections.abc import Generator
from contextlib import contextmanager
from pathlib import Path

_SAVEPOINT_NAME_PATTERN = re.compile(r"[A-Za-z_][A-Za-z0-9_]*\Z")


def _configure_connection(conn: sqlite3.Connection) -> None:
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA busy_timeout = 5000")


def connect(path: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(path, autocommit=True)
    try:
        _configure_connection(conn)
        result = conn.execute("PRAGMA journal_mode = WAL").fetchone()
        if result is None or str(result[0]).lower() != "wal":
            raise RuntimeError("SQLite が WAL モードを有効化できませんでした")
    except BaseException:
        conn.close()
        raise
    return conn


def connect_readonly(path: Path) -> sqlite3.Connection:
    if not path.exists():
        raise FileNotFoundError(path)

    uri = f"{path.resolve().as_uri()}?mode=ro"
    conn = sqlite3.connect(uri, uri=True, autocommit=True)
    try:
        _configure_connection(conn)
    except BaseException:
        conn.close()
        raise
    return conn


def connect_memory() -> sqlite3.Connection:
    conn = sqlite3.connect(":memory:", autocommit=True)
    try:
        _configure_connection(conn)
    except BaseException:
        conn.close()
        raise
    return conn


@contextmanager
def transaction(conn: sqlite3.Connection) -> Generator[sqlite3.Connection]:
    if conn.in_transaction:
        raise RuntimeError("トランザクションをネストできません")

    conn.execute("BEGIN IMMEDIATE")
    try:
        yield conn
    except BaseException:
        if conn.in_transaction:
            conn.execute("ROLLBACK")
        raise
    else:
        try:
            conn.execute("COMMIT")
        except BaseException:
            if conn.in_transaction:
                conn.execute("ROLLBACK")
            raise


@contextmanager
def savepoint(conn: sqlite3.Connection, name: str) -> Generator[sqlite3.Connection]:
    if not _SAVEPOINT_NAME_PATTERN.fullmatch(name):
        raise ValueError("SAVEPOINT 名は英字または _ で始まる英数字と _ のみ指定できます")
    if not conn.in_transaction:
        raise RuntimeError("SAVEPOINT はトランザクション内でのみ使用できます")

    conn.execute(f"SAVEPOINT {name}")
    try:
        yield conn
    except BaseException as original_error:
        cleanup_errors: list[BaseException] = []
        for statement in (
            f"ROLLBACK TO SAVEPOINT {name}",
            f"RELEASE SAVEPOINT {name}",
        ):
            try:
                conn.execute(statement)
            except BaseException as cleanup_error:
                cleanup_errors.append(cleanup_error)
        if cleanup_errors:
            raise BaseExceptionGroup(
                f"SAVEPOINT {name} のロールバックに失敗しました",
                [original_error, *cleanup_errors],
            ) from original_error
        raise
    else:
        conn.execute(f"RELEASE SAVEPOINT {name}")


@contextmanager
def read_transaction(conn: sqlite3.Connection) -> Generator[sqlite3.Connection]:
    if conn.in_transaction:
        yield conn
        return

    conn.execute("BEGIN")
    try:
        yield conn
    except BaseException:
        if conn.in_transaction:
            conn.execute("ROLLBACK")
        raise
    else:
        try:
            conn.execute("COMMIT")
        except BaseException:
            if conn.in_transaction:
                conn.execute("ROLLBACK")
            raise
