from __future__ import annotations

import os
import sqlite3
import stat
import tempfile
from contextlib import suppress
from pathlib import Path
from time import monotonic

_BACKUP_BUSY_TIMEOUT_SECONDS = 5.0
_BACKUP_PAGES_PER_STEP = 128
_COPY_BUFFER_SIZE = 1024 * 1024
_SIDECARS = ("-wal", "-shm", "-journal")
_STAGING_DATABASE = "snapshot.db"


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


def _require_no_sidecars(path: Path) -> None:
    for suffix in _SIDECARS:
        sidecar = Path(f"{path}{suffix}")
        if os.path.lexists(sidecar):
            raise FileExistsError(f"コピー先の関連ファイルが既に存在します: {sidecar}")


def _copy_to_staging(src_conn: sqlite3.Connection, path: Path) -> None:
    dest_conn = sqlite3.connect(path, autocommit=True)
    try:
        last_progress = monotonic()

        def check_progress(status: int, remaining: int, total: int) -> None:
            nonlocal last_progress
            current = monotonic()
            if status & 0xFF in (sqlite3.SQLITE_BUSY, sqlite3.SQLITE_LOCKED):
                if current - last_progress >= _BACKUP_BUSY_TIMEOUT_SECONDS:
                    raise sqlite3.OperationalError(
                        "データベースのロックが解除されず、コピーを中止しました"
                    )
            else:
                last_progress = current

        src_conn.backup(
            dest_conn, pages=_BACKUP_PAGES_PER_STEP, progress=check_progress, sleep=0.05
        )
    except BaseException as error:
        try:
            dest_conn.close()
        except BaseException as close_error:
            raise BaseExceptionGroup(
                "データベースのコピーと一時 DB のクローズに失敗しました", [error, close_error]
            ) from error
        raise
    else:
        dest_conn.close()


def _write_reserved_file(source: Path, file_descriptor: int) -> None:
    with source.open("rb") as stream:
        while chunk := stream.read(_COPY_BUFFER_SIZE):
            remaining = memoryview(chunk)
            while remaining:
                written = os.write(file_descriptor, remaining)
                if written <= 0:
                    raise OSError("コピー先に書き込めませんでした")
                remaining = remaining[written:]


def _remove_staging(directory: Path, identity: os.stat_result) -> None:
    current = directory.lstat()
    if not stat.S_ISDIR(current.st_mode) or (current.st_dev, current.st_ino) != (
        identity.st_dev,
        identity.st_ino,
    ):
        raise OSError(f"コピー用の一時ディレクトリが変更されました: {directory}")
    path = directory / _STAGING_DATABASE
    for suffix in (*_SIDECARS, ""):
        with suppress(FileNotFoundError):
            Path(f"{path}{suffix}").unlink()
    directory.rmdir()


def copy_database(src_conn: sqlite3.Connection, dest_path: Path) -> None:
    if src_conn.in_transaction:
        raise RuntimeError("トランザクション中はデータベースをコピーできません")

    dest_path = Path(dest_path)
    _require_no_sidecars(dest_path)
    flags = os.O_CREAT | os.O_EXCL | os.O_WRONLY | getattr(os, "O_BINARY", 0)
    file_descriptor = os.open(dest_path, flags, 0o666)
    try:
        created = os.fstat(file_descriptor)
    except BaseException as error:
        try:
            os.close(file_descriptor)
        except BaseException as close_error:
            raise BaseExceptionGroup(
                "コピー先の確認とファイルのクローズに失敗しました", [error, close_error]
            ) from error
        error.add_note(f"コピー先の所有を確認できないため削除しませんでした: {dest_path}")
        raise

    def destination_is_owned() -> bool:
        try:
            current = dest_path.lstat()
        except FileNotFoundError:
            return False
        return stat.S_ISREG(current.st_mode) and (current.st_dev, current.st_ino) == (
            created.st_dev,
            created.st_ino,
        )

    descriptor_open = True
    staging: tuple[Path, os.stat_result] | None = None
    try:
        _require_no_sidecars(dest_path)
        directory = Path(tempfile.mkdtemp(prefix=".meter-reading-backup-", dir=dest_path.parent))
        try:
            directory_identity = directory.lstat()
        except OSError as error:
            error.add_note(
                f"一時ディレクトリの所有を確認できないため削除しませんでした: {directory}"
            )
            raise
        if not stat.S_ISDIR(directory_identity.st_mode):
            raise OSError(f"コピー用の一時ディレクトリが変更されました: {directory}")
        staging = (directory, directory_identity)
        staging_path = directory / _STAGING_DATABASE
        _copy_to_staging(src_conn, staging_path)
        _require_no_sidecars(staging_path)
        if not destination_is_owned():
            raise OSError("コピー先が処理中に差し替えられました")
        _require_no_sidecars(dest_path)
        _write_reserved_file(staging_path, file_descriptor)
        _remove_staging(*staging)
        staging = None
        descriptor_open = False
        os.close(file_descriptor)
        if not destination_is_owned():
            raise OSError("コピー先が処理中に差し替えられました")
        _require_no_sidecars(dest_path)
    except BaseException as error:
        cleanup_errors: list[BaseException] = []
        if descriptor_open:
            try:
                os.close(file_descriptor)
            except BaseException as cleanup_error:
                cleanup_errors.append(cleanup_error)
        if staging is not None:
            try:
                _remove_staging(*staging)
            except BaseException as cleanup_error:
                cleanup_errors.append(cleanup_error)
        try:
            if destination_is_owned():
                dest_path.unlink()
            else:
                cleanup_errors.append(OSError("コピー先の所有を確認できないため削除しませんでした"))
        except BaseException as cleanup_error:
            cleanup_errors.append(cleanup_error)
        if cleanup_errors:
            raise BaseExceptionGroup(
                "データベースのコピー失敗後に途中ファイルを削除できません",
                [error, *cleanup_errors],
            ) from error
        raise
