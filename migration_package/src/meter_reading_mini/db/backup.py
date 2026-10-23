from __future__ import annotations

import os
import sqlite3
from contextlib import suppress
from pathlib import Path


def check_sqlite_integrity(conn: sqlite3.Connection) -> list[str]:
    reasons: list[str] = []
    integrity_results = [row[0] for row in conn.execute("PRAGMA integrity_check").fetchall()]
    if integrity_results != ["ok"]:
        details = "、".join(integrity_results) if integrity_results else "結果がありません"
        reasons.append(f"SQLite 整合性検査に失敗しました: {details}")

    foreign_key_results = conn.execute("PRAGMA foreign_key_check").fetchall()
    for table, rowid, parent, foreign_key_id in foreign_key_results:
        reasons.append(
            f"外部キー制約に違反しています: テーブル {table}、行 {rowid}、"
            f"参照先 {parent}、制約 {foreign_key_id}"
        )
    return reasons


def copy_database(src_conn: sqlite3.Connection, dest_path: Path) -> None:
    if src_conn.in_transaction:
        raise RuntimeError("トランザクション中はデータベースをコピーできません")

    dest_path = Path(dest_path)
    file_descriptor = os.open(dest_path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o666)
    os.close(file_descriptor)

    dest_conn: sqlite3.Connection | None = None
    try:
        dest_conn = sqlite3.connect(dest_path, autocommit=True)
        src_conn.backup(dest_conn)
        dest_conn.close()
        dest_conn = None
    except BaseException:
        if dest_conn is not None:
            dest_conn.close()
        for suffix in ("", "-wal", "-shm", "-journal"):
            with suppress(FileNotFoundError):
                Path(f"{dest_path}{suffix}").unlink()
        raise
