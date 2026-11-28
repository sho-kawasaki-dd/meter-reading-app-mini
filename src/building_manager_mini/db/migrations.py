from __future__ import annotations

import sqlite3
from importlib.resources import files
from pathlib import Path
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from building_manager_mini.core.sender_address import normalize_sender_address
from building_manager_mini.core.settings_contracts import CommonSettingsError

SCHEMA_VERSION = 1
DEFAULT_TIMEZONE = "Asia/Tokyo"


class CommonSchemaError(CommonSettingsError):
    pass


def _schema_ddl() -> str:
    return files("building_manager_mini.db").joinpath("schema.sql").read_text(encoding="utf-8")


def _statements(ddl: str) -> list[str]:
    statements: list[str] = []
    pending = ""
    for line in ddl.splitlines(keepends=True):
        pending += line
        if sqlite3.complete_statement(pending):
            statements.append(pending.strip())
            pending = ""
    if pending.strip():
        raise CommonSchemaError("共通 DB のスキーマ定義が不完全です。")
    return statements


def _schema_objects(conn: sqlite3.Connection) -> list[tuple[str, str, str, str]]:
    return [
        (str(kind), str(name), str(table), str(sql))
        for kind, name, table, sql in conn.execute(
            "SELECT type, name, tbl_name, sql FROM sqlite_master "
            "WHERE name NOT GLOB 'sqlite_*' ORDER BY type, name"
        )
    ]


def _expected_schema() -> list[tuple[str, str, str, str]]:
    conn = sqlite3.connect(":memory:", autocommit=True)
    try:
        for statement in _statements(_schema_ddl()):
            conn.execute(statement)
        return _schema_objects(conn)
    finally:
        conn.close()


def validate_schema(conn: sqlite3.Connection) -> None:
    version = conn.execute("PRAGMA user_version").fetchone()
    if version is None or version[0] != SCHEMA_VERSION:
        raise CommonSchemaError(f"共通 DB のスキーマ版が未対応です: {version}")
    if _schema_objects(conn) != _expected_schema():
        raise CommonSchemaError("共通 DB のスキーマが版の定義と一致しません。")
    if conn.execute("PRAGMA quick_check").fetchall() != [("ok",)]:
        raise CommonSchemaError("共通 DB の整合性検査に失敗しました。")
    if conn.execute("PRAGMA foreign_key_check").fetchone() is not None:
        raise CommonSchemaError("共通 DB の担当者参照が不正です。")
    settings = conn.execute("SELECT timezone FROM common_settings").fetchall()
    if len(settings) != 1:
        raise CommonSchemaError("共通 DB の業務タイムゾーン設定が不正です。")
    try:
        ZoneInfo(settings[0][0])
    except (ValueError, ZoneInfoNotFoundError) as error:
        raise CommonSchemaError("共通 DB の業務タイムゾーン設定が不正です。") from error
    for name, address in conn.execute("SELECT display_name, sender_address FROM staff"):
        if not name.strip() or name.strip() != name:
            raise CommonSchemaError("共通 DB の担当者名が不正です。")
        try:
            if normalize_sender_address(address) != address:
                raise ValueError("差出人アドレスが正規化されていません。")
        except ValueError as error:
            raise CommonSchemaError("共通 DB の差出人アドレスが不正です。") from error


def initialize_schema(conn: sqlite3.Connection) -> None:
    if conn.in_transaction:
        raise CommonSchemaError("共通 DB の初期化はトランザクション外で行ってください。")
    version = conn.execute("PRAGMA user_version").fetchone()
    if version is None:
        raise CommonSchemaError("共通 DB のスキーマ版を取得できません。")
    if version[0] != 0:
        validate_schema(conn)
        return
    conn.execute("BEGIN IMMEDIATE")
    try:
        if conn.execute("PRAGMA user_version").fetchone() != (0,) or _schema_objects(conn):
            raise CommonSchemaError("版が未設定の共通 DB に既存のスキーマがあります。")
        for statement in _statements(_schema_ddl()):
            conn.execute(statement)
        conn.execute(
            "INSERT INTO common_settings (singleton, timezone) VALUES (1, ?)",
            (DEFAULT_TIMEZONE,),
        )
        conn.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")
        validate_schema(conn)
        conn.execute("COMMIT")
    except BaseException:
        if conn.in_transaction:
            conn.execute("ROLLBACK")
        raise


def open_database(path: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(path, autocommit=True)
    try:
        conn.execute("PRAGMA foreign_keys = ON")
        conn.execute("PRAGMA busy_timeout = 5000")
        initialize_schema(conn)
        mode = conn.execute("PRAGMA journal_mode = WAL").fetchone()
        if mode is None or str(mode[0]).lower() != "wal":
            raise CommonSchemaError("共通 DB で WAL モードを有効化できません。")
        return conn
    except BaseException:
        conn.close()
        raise


def open_database_readonly(path: Path) -> sqlite3.Connection:
    path.stat()
    conn = sqlite3.connect(f"{path.resolve().as_uri()}?mode=ro", uri=True, autocommit=True)
    try:
        conn.execute("PRAGMA foreign_keys = ON")
        conn.execute("PRAGMA busy_timeout = 5000")
        conn.execute("PRAGMA query_only = ON")
        validate_schema(conn)
        return conn
    except BaseException:
        conn.close()
        raise
