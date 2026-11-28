from __future__ import annotations

import os
import sqlite3
import stat
import tempfile
from collections.abc import Callable
from contextlib import closing
from pathlib import Path
from typing import Protocol

from meter_reading_mini.core.errors import (
    DomainError,
    MigrationError,
    SchemaTooNewError,
    UnsupportedSchemaError,
)
from meter_reading_mini.db.backup import check_sqlite_integrity, copy_database
from meter_reading_mini.db.connection import connect, connect_readonly
from meter_reading_mini.db.integrity import check_business_integrity
from meter_reading_mini.db.migrations import (
    MIN_SUPPORTED_SCHEMA_VERSION,
    SCHEMA_VERSION,
    check_migration_path,
    check_migration_prechecks,
    inspect_schema,
    migrate_schema,
)


class LegacyDatabaseLock(Protocol):
    def try_acquire(self, timeout_ms: int = 100) -> bool: ...

    def release(self) -> None: ...


LockFactory = Callable[[Path], LegacyDatabaseLock]
_SIDECAR_SUFFIXES = ("-wal", "-shm", "-journal")


def migrate_legacy_database(
    new_database_path: Path,
    legacy_database_path: Path | None,
    *,
    lock_factory: LockFactory,
    before_publish: Callable[[], None] | None = None,
    after_publish: Callable[[], None] | None = None,
    on_publication_conflict: Callable[[], None] | None = None,
) -> bool:
    """旧 DB を変更せず、一時コピーを検査・移行して新しい正式パスへ反映する。"""

    new_database_path = Path(new_database_path)
    if _path_exists(new_database_path):
        return False
    if legacy_database_path is None or not _path_exists(legacy_database_path):
        return False
    try:
        legacy_mode = legacy_database_path.stat().st_mode
    except OSError as error:
        raise MigrationError(
            f"旧 DB の状態を確認できません: {legacy_database_path} ({error})"
        ) from error
    if not stat.S_ISREG(legacy_mode):
        raise MigrationError(f"旧 DB のパスが通常ファイルではありません: {legacy_database_path}")

    lock = lock_factory(legacy_database_path.parent / "meter_reading.lock")
    if not lock.try_acquire():
        raise MigrationError(
            "旧版の検針アプリが使用中のため移行を中止しました。"
            "旧版を終了してから新アプリを再起動してください。"
        )
    try:
        return _copy_and_validate(
            new_database_path,
            legacy_database_path,
            before_publish=before_publish,
            after_publish=after_publish,
            on_publication_conflict=on_publication_conflict,
        )
    finally:
        lock.release()


def _path_exists(path: Path) -> bool:
    try:
        path.stat()
    except FileNotFoundError:
        return False
    except OSError as error:
        raise MigrationError(f"DB の存在を確認できません: {path} ({error})") from error
    return True


def _copy_and_validate(
    new_database_path: Path,
    legacy_database_path: Path,
    *,
    before_publish: Callable[[], None] | None,
    after_publish: Callable[[], None] | None,
    on_publication_conflict: Callable[[], None] | None,
) -> bool:
    published = False
    try:
        with tempfile.TemporaryDirectory(
            prefix=".meter-reading-migration-", dir=new_database_path.parent
        ) as temporary_directory:
            candidate_path = Path(temporary_directory) / "meter_reading.db"
            with closing(connect_readonly(legacy_database_path)) as source:
                version_row = source.execute("PRAGMA user_version").fetchone()
                if version_row is None:
                    raise UnsupportedSchemaError("旧 DB のスキーマ版を確認できません。")
                source_version = int(version_row[0])
                if source_version > SCHEMA_VERSION:
                    raise SchemaTooNewError(
                        f"旧 DB のスキーマ版 {source_version} は対応版 "
                        f"{SCHEMA_VERSION} より新しいため移行できません。"
                    )
                if source_version < MIN_SUPPORTED_SCHEMA_VERSION:
                    raise UnsupportedSchemaError(
                        f"旧 DB のスキーマ版 {source_version} はサポートされていません。"
                    )
                schema_reasons = inspect_schema(source, source_version)
                if schema_reasons:
                    raise UnsupportedSchemaError(
                        "旧 DB のスキーマが不正です: " + "、".join(schema_reasons)
                    )
                integrity_reasons = check_sqlite_integrity(source)
                if integrity_reasons:
                    raise MigrationError(
                        "旧 DB の検査に失敗しました: " + "、".join(integrity_reasons)
                    )
                copy_database(source, candidate_path)

            with closing(connect(candidate_path)) as candidate:
                check_migration_path(source_version, SCHEMA_VERSION)
                check_migration_prechecks(candidate, source_version, SCHEMA_VERSION)
                migrate_schema(candidate, source_version, SCHEMA_VERSION)
                business_reasons = check_business_integrity(candidate)
                if business_reasons:
                    raise MigrationError(
                        "複製した DB の業務整合性検査に失敗しました: " + "、".join(business_reasons)
                    )
                checkpoint = candidate.execute("PRAGMA wal_checkpoint(TRUNCATE)").fetchone()
                if checkpoint is None or tuple(checkpoint) != (0, 0, 0):
                    raise MigrationError(f"一時 DB の確定データを整理できません: {checkpoint}")

            _ensure_standalone_database(candidate_path)
            if before_publish is not None:
                before_publish()
            try:
                os.link(candidate_path, new_database_path)
            except FileExistsError:
                if on_publication_conflict is not None:
                    on_publication_conflict()
                return False
            published = True
        if published and after_publish is not None:
            after_publish()
        return published
    except (sqlite3.DatabaseError, OSError) as error:
        raise MigrationError(
            f"旧 DB を安全に複製できませんでした: {error}。"
            "旧 DB は変更していません。保存先の空き容量と権限を確認し、再起動してください。"
        ) from error
    except DomainError as error:
        raise MigrationError(
            f"旧 DB の移行を中止しました: {error.message} "
            "旧 DB は変更していません。原因を解消してから再起動してください。"
        ) from error


def _ensure_standalone_database(path: Path) -> None:
    for suffix in _SIDECAR_SUFFIXES:
        sidecar = Path(f"{path}{suffix}")
        if sidecar.exists():
            if sidecar.stat().st_size:
                raise MigrationError(f"一時 DB に未確定の関連ファイルがあります: {sidecar}")
            sidecar.unlink()
