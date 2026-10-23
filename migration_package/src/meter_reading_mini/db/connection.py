from __future__ import annotations

import sqlite3
from collections.abc import Generator
from contextlib import contextmanager
from pathlib import Path


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
