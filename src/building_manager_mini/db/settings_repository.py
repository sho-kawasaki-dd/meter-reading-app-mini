from __future__ import annotations

import sqlite3
from collections.abc import Generator
from contextlib import contextmanager

from building_manager_mini.core.settings_contracts import CommonSettingsError, Staff


def _execute(
    conn: sqlite3.Connection, sql: str, values: tuple[str | int, ...] = ()
) -> sqlite3.Cursor:
    try:
        return conn.execute(sql, values)
    except sqlite3.IntegrityError as error:
        raise CommonSettingsError(
            "設定を保存できません。氏名の重複等を確認してください。"
        ) from error
    except sqlite3.DatabaseError as error:
        raise CommonSettingsError("共通 DB の操作に失敗しました。") from error


@contextmanager
def transaction(conn: sqlite3.Connection) -> Generator[None]:
    if conn.in_transaction:
        raise CommonSettingsError("トランザクションをネストできません。")
    _execute(conn, "BEGIN IMMEDIATE")
    try:
        yield
        _execute(conn, "COMMIT")
    except BaseException as original_error:
        if conn.in_transaction:
            try:
                _execute(conn, "ROLLBACK")
            except BaseException as rollback_error:
                raise BaseExceptionGroup(
                    "共通 DB のロールバックに失敗しました。", [original_error, rollback_error]
                ) from original_error
        raise


class SQLiteSettingsRepository:
    def __init__(self, conn: sqlite3.Connection) -> None:
        self._conn = conn

    def list_staff(self, *, include_inactive: bool) -> list[Staff]:
        rows = _execute(
            self._conn,
            "SELECT staff_id, display_name, is_active, sender_address FROM staff "
            "WHERE (? = 1 OR is_active = 1) ORDER BY display_name, staff_id",
            (int(include_inactive),),
        )
        return [Staff(str(row[0]), str(row[1]), bool(row[2]), str(row[3])) for row in rows]

    def get_staff(self, staff_id: str) -> Staff | None:
        row = _execute(
            self._conn,
            "SELECT staff_id, display_name, is_active, sender_address FROM staff "
            "WHERE staff_id = ?",
            (staff_id,),
        ).fetchone()
        return None if row is None else Staff(str(row[0]), str(row[1]), bool(row[2]), str(row[3]))

    def insert_staff(self, staff: Staff) -> None:
        _execute(
            self._conn,
            "INSERT INTO staff (staff_id, display_name, is_active, sender_address) "
            "VALUES (?, ?, ?, ?)",
            (staff.staff_id, staff.display_name, int(staff.is_active), staff.sender_address),
        )

    def update_staff(self, staff: Staff) -> None:
        cursor = _execute(
            self._conn,
            "UPDATE staff SET display_name = ?, is_active = ?, sender_address = ? "
            "WHERE staff_id = ?",
            (staff.display_name, int(staff.is_active), staff.sender_address, staff.staff_id),
        )
        if cursor.rowcount != 1:
            raise CommonSettingsError("担当者が見つかりません。")

    def get_timezone(self) -> str:
        row = _execute(
            self._conn, "SELECT timezone FROM common_settings WHERE singleton = 1"
        ).fetchone()
        if row is None:
            raise CommonSettingsError("業務タイムゾーン設定が見つかりません。")
        return str(row[0])

    def set_timezone(self, timezone: str) -> None:
        cursor = _execute(
            self._conn, "UPDATE common_settings SET timezone = ? WHERE singleton = 1", (timezone,)
        )
        if cursor.rowcount != 1:
            raise CommonSettingsError("業務タイムゾーン設定が見つかりません。")
