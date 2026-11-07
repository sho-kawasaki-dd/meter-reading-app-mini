"""読取専用検査と SQLite バックアップを使う復元基盤。"""

from __future__ import annotations

import hashlib
import os
import shutil
import sqlite3
import stat
import tempfile
from collections.abc import Callable
from contextlib import closing
from dataclasses import dataclass
from pathlib import Path

from meter_reading_mini.core.backup_restore_contracts import RestorePreview, RestoreResult
from meter_reading_mini.core.errors import (
    FileOutputError,
    InvalidBackupError,
    MigrationError,
    RestoreError,
    UnsupportedSchemaError,
)
from meter_reading_mini.db.backup import check_sqlite_integrity, copy_database
from meter_reading_mini.db.connection import connect, connect_readonly
from meter_reading_mini.db.integrity import (
    check_business_integrity,
    list_business_integrity_warnings,
)
from meter_reading_mini.db.migrations import (
    MIN_SUPPORTED_SCHEMA_VERSION,
    SCHEMA_VERSION,
    check_migration_path,
    check_migration_prechecks,
    inspect_schema,
    migrate_schema,
)

_SIDECARS = ("-wal", "-shm", "-journal")
_SOURCE_SIDECARS = ("-wal", "-journal")
_CANDIDATE_NAME = "candidate.db"
_CURRENT_COPY_NAME = "current-check.db"
_SOURCE_COPY_NAME = "source-snapshot.db"


@dataclass(frozen=True, slots=True)
class _FileIdentity:
    device: int
    inode: int


@dataclass(frozen=True, slots=True)
class _FileFingerprint:
    identity: _FileIdentity
    size: int
    modified_ns: int
    digest: bytes


@dataclass(frozen=True, slots=True)
class _SourceFingerprint:
    resolved_path: Path
    main: _FileFingerprint
    sidecars: tuple[_FileFingerprint | None, ...]


def _identity(info: os.stat_result) -> _FileIdentity:
    return _FileIdentity(info.st_dev, info.st_ino)


def _stat_signature(info: os.stat_result) -> tuple[_FileIdentity, int, int]:
    return _identity(info), info.st_size, info.st_mtime_ns


def _fingerprint(path: Path) -> _FileFingerprint:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        before = os.fstat(stream.fileno())
        if not stat.S_ISREG(before.st_mode):
            raise OSError(f"通常のファイルではありません: {path}")
        while chunk := stream.read(1024 * 1024):
            digest.update(chunk)
        after = os.fstat(stream.fileno())
    current = path.stat()
    if _stat_signature(before) != _stat_signature(after) or _stat_signature(after) != (
        _stat_signature(current)
    ):
        raise OSError(f"検査中にファイルが変更されました: {path}")
    return _FileFingerprint(_identity(after), after.st_size, after.st_mtime_ns, digest.digest())


def _source_fingerprint(path: Path) -> _SourceFingerprint:
    resolved_path = path.resolve(strict=True)
    main = _fingerprint(path)
    sidecars: list[_FileFingerprint | None] = []
    for suffix in _SOURCE_SIDECARS:
        sidecar = Path(f"{resolved_path}{suffix}")
        sidecars.append(_fingerprint(sidecar) if os.path.lexists(sidecar) else None)
    return _SourceFingerprint(resolved_path, main, tuple(sidecars))


def _require_single_file(path: Path) -> None:
    for suffix in _SIDECARS:
        sidecar = Path(f"{path}{suffix}")
        if os.path.lexists(sidecar):
            raise OSError(f"関連ファイルが残っているため安全に扱えません: {sidecar}")


def _require_private_file(path: Path) -> None:
    info = path.lstat()
    if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
        raise OSError(f"ファイルの実体を一意に確認できません: {path}")


def _data_version(conn: sqlite3.Connection) -> int:
    row = conn.execute("PRAGMA data_version").fetchone()
    if row is None:
        raise sqlite3.OperationalError("SQLite のデータ版を確認できません")
    return int(row[0])


def _checkpoint(conn: sqlite3.Connection) -> None:
    if conn.in_transaction:
        raise sqlite3.OperationalError("トランザクション中はチェックポイントできません")
    row = conn.execute("PRAGMA wal_checkpoint(TRUNCATE)").fetchone()
    if row is None or tuple(row) != (0, 0, 0):
        raise sqlite3.OperationalError(f"WAL のチェックポイントを完了できません: {row}")


def _inspect_supported_database(conn: sqlite3.Connection) -> int:
    try:
        row = conn.execute("PRAGMA user_version").fetchone()
        if row is None:
            raise InvalidBackupError(("スキーマ版を確認できません。",))
        version = int(row[0])
        if version > SCHEMA_VERSION:
            raise InvalidBackupError((f"スキーマ版 {version} は対応版より新しいです。",))
        if version < MIN_SUPPORTED_SCHEMA_VERSION:
            raise InvalidBackupError((f"スキーマ版 {version} はサポートされていません。",))
        schema_reasons = inspect_schema(conn, version)
        if schema_reasons:
            raise InvalidBackupError(
                tuple(f"スキーマが不正です: {item}" for item in schema_reasons)
            )
        sqlite_reasons = check_sqlite_integrity(conn)
        if sqlite_reasons:
            raise InvalidBackupError(tuple(sqlite_reasons))
        return version
    except sqlite3.DatabaseError as error:
        raise InvalidBackupError((f"SQLite の検査に失敗しました: {error}",)) from error


def _business_warnings(conn: sqlite3.Connection) -> tuple[str, ...]:
    try:
        reasons = check_business_integrity(conn)
        if reasons:
            raise InvalidBackupError(tuple(reasons))
        return tuple(list_business_integrity_warnings(conn))
    except sqlite3.DatabaseError as error:
        raise InvalidBackupError((f"業務整合性検査に失敗しました: {error}",)) from error


@dataclass(frozen=True, slots=True)
class _Workspace:
    path: Path
    identity: _FileIdentity

    @classmethod
    def create(cls, parent: Path) -> _Workspace:
        path = Path(tempfile.mkdtemp(prefix=".meter-reading-restore-", dir=parent))
        return cls(path, _identity(path.lstat()))

    def check_identity(self) -> None:
        info = self.path.lstat()
        if not stat.S_ISDIR(info.st_mode) or _identity(info) != self.identity:
            raise OSError(f"復元用の一時ディレクトリが変更されました: {self.path}")

    def remove(self) -> None:
        if not os.path.lexists(self.path):
            return
        self.check_identity()
        for name in (_CANDIDATE_NAME, _CURRENT_COPY_NAME, _SOURCE_COPY_NAME):
            self.remove_database(name)
        self.path.rmdir()

    def remove_database(self, name: str) -> None:
        self.check_identity()
        for suffix in _SIDECARS:
            Path(f"{self.path / name}{suffix}").unlink(missing_ok=True)
        (self.path / name).unlink(missing_ok=True)


@dataclass(frozen=True, slots=True)
class _Candidate:
    preview: RestorePreview
    workspace: _Workspace
    source_fingerprint: _SourceFingerprint
    candidate_fingerprint: _FileFingerprint

    @property
    def path(self) -> Path:
        return self.workspace.path / _CANDIDATE_NAME


def _cleanup_after_error(workspace: _Workspace, error: BaseException) -> None:
    try:
        workspace.remove()
    except BaseException as cleanup_error:
        raise BaseExceptionGroup(
            "復元の中断後に一時ファイルを削除できません",
            [error, cleanup_error],
        ) from error


class SQLiteBackupRestoreRepository:
    """候補を一つだけ保持し、確認後も同じ内容だけを置換する。"""

    def __init__(self, connection: sqlite3.Connection, database_path: Path) -> None:
        self._connection = connection
        self._configured_path = Path(database_path).absolute()
        self._database_path = self._configured_path.resolve(strict=True)
        self._database_identity = _identity(self._database_path.stat())
        self._candidate: _Candidate | None = None

    def _check_current_path(self) -> None:
        if self._configured_path.resolve(strict=True) != self._database_path:
            raise OSError("現行 DB の参照先が変更されました")
        if _identity(self._database_path.stat()) != self._database_identity:
            raise OSError("現行 DB のファイルが差し替えられました")

    def _check_current_connection(self) -> int:
        self._check_current_path()
        if self._connection.in_transaction:
            raise RuntimeError("トランザクション中はバックアップ・復元できません")
        databases = self._connection.execute("PRAGMA database_list").fetchall()
        main_paths = [str(row[2]) for row in databases if row[1] == "main"]
        if len(main_paths) != 1 or not main_paths[0]:
            raise RuntimeError("ファイル DB の接続を確認できません")
        if not Path(main_paths[0]).samefile(self._database_path):
            raise RuntimeError("接続中の DB と現行 DB が一致しません")
        return _data_version(self._connection)

    def create_backup(self, destination: Path) -> None:
        try:
            self._check_current_connection()
            copy_database(self._connection, Path(destination))
            _require_single_file(Path(destination))
        except (OSError, sqlite3.Error, RuntimeError) as error:
            raise FileOutputError(f"バックアップを保存できません: {error}") from error

    def discard_preview(self) -> None:
        candidate, self._candidate = self._candidate, None
        if candidate is not None:
            candidate.workspace.remove()

    def _check_source(
        self, path: Path, expected: _SourceFingerprint | None = None
    ) -> _SourceFingerprint:
        try:
            if path.samefile(self._database_path):
                raise InvalidBackupError(("現行 DB と同じファイルは復元元に指定できません。",))
            current = _source_fingerprint(path)
        except OSError as error:
            raise InvalidBackupError((f"復元元を確認できません: {error}",)) from error
        if expected is not None and current != expected:
            raise InvalidBackupError(("復元元が検査後に変更されました。再検査してください。",))
        return current

    def preview_restore(self, source: Path) -> RestorePreview:
        self.discard_preview()
        source_path = Path(source).absolute()
        try:
            self._check_current_path()
        except OSError as error:
            raise RestoreError(
                f"現行 DB を確認できません: {error}", stage="pre_backup", recovered=True
            ) from error
        source_before = self._check_source(source_path)
        journal = source_before.sidecars[1]
        if journal is not None and journal.size:
            raise InvalidBackupError(
                ("復元元に未処理のロールバックジャーナルがあるため安全に検査できません。",)
            )
        try:
            workspace = _Workspace.create(self._database_path.parent)
        except OSError as error:
            raise RestoreError(
                f"復元用の一時ディレクトリを作成できません: {error}",
                stage="pre_backup",
                recovered=True,
            ) from error

        try:
            preview = self._prepare_candidate(source_path, source_before, workspace)
        except BaseException as error:
            _cleanup_after_error(workspace, error)
            raise
        self._candidate = preview
        return preview.preview

    def _prepare_candidate(
        self, source: Path, source_before: _SourceFingerprint, workspace: _Workspace
    ) -> _Candidate:
        candidate_path = workspace.path / _CANDIDATE_NAME
        wal = source_before.sidecars[0]
        needs_snapshot = wal is not None and wal.size > 0
        if needs_snapshot:
            source_conn = self._open_source_snapshot(source, source_before, workspace)
        else:
            try:
                source_conn = connect_readonly(source, immutable=True)
            except (OSError, sqlite3.Error, ValueError) as error:
                raise InvalidBackupError((f"復元元を読取専用で開けません: {error}",)) from error
        with closing(source_conn):
            try:
                before_version = _data_version(source_conn)
            except sqlite3.DatabaseError as error:
                raise InvalidBackupError((f"SQLite の検査に失敗しました: {error}",)) from error
            source_version = _inspect_supported_database(source_conn)
            try:
                copy_database(source_conn, candidate_path)
            except (OSError, sqlite3.Error, RuntimeError) as error:
                raise RestoreError(
                    f"復元元を一時 DB にコピーできません: {error}",
                    stage="pre_backup",
                    recovered=True,
                ) from error
            try:
                after_version = _data_version(source_conn)
            except sqlite3.DatabaseError as error:
                raise InvalidBackupError((f"SQLite の検査に失敗しました: {error}",)) from error
            if after_version != before_version:
                raise InvalidBackupError(("検査中に復元元が変更されました。",))
        if needs_snapshot:
            try:
                workspace.remove_database(_SOURCE_COPY_NAME)
            except OSError as error:
                raise RestoreError(
                    f"復元元の一時スナップショットを削除できません: {error}",
                    stage="pre_backup",
                    recovered=True,
                ) from error
        self._check_source(source, source_before)

        try:
            with closing(connect(candidate_path)) as candidate_conn:
                copied_version = _inspect_supported_database(candidate_conn)
                if copied_version != source_version:
                    raise InvalidBackupError(("コピー前後でスキーマ版が一致しません。",))
                if copied_version < SCHEMA_VERSION:
                    check_migration_path(copied_version, SCHEMA_VERSION)
                    check_migration_prechecks(candidate_conn, copied_version, SCHEMA_VERSION)
                    migrate_schema(candidate_conn, copied_version, SCHEMA_VERSION)
                warnings = _business_warnings(candidate_conn)
                _checkpoint(candidate_conn)
        except (MigrationError, UnsupportedSchemaError) as error:
            raise InvalidBackupError((f"旧版 DB を移行できません: {error}",)) from error
        except sqlite3.Error as error:
            raise RestoreError(
                f"一時 DB の移行またはチェックポイントに失敗しました: {error}",
                stage="pre_backup",
                recovered=True,
            ) from error

        try:
            workspace.check_identity()
            _require_single_file(candidate_path)
            _require_private_file(candidate_path)
            candidate_fingerprint = _fingerprint(candidate_path)
        except OSError as error:
            raise RestoreError(
                f"一時 DB を確認できません: {error}", stage="pre_backup", recovered=True
            ) from error
        self._check_source(source, source_before)
        return _Candidate(
            RestorePreview(source, source_version, warnings),
            workspace,
            source_before,
            candidate_fingerprint,
        )

    def _open_source_snapshot(
        self, source: Path, expected: _SourceFingerprint, workspace: _Workspace
    ) -> sqlite3.Connection:
        wal = expected.sidecars[0]
        if wal is None:
            raise InvalidBackupError(("復元元の WAL を確認できません。",))
        snapshot = workspace.path / _SOURCE_COPY_NAME
        try:
            # WAL を直接読むと復元元の共有メモリが変更・作成される。
            for original, destination, fingerprint in (
                (expected.resolved_path, snapshot, expected.main),
                (Path(f"{expected.resolved_path}-wal"), Path(f"{snapshot}-wal"), wal),
            ):
                with original.open("rb") as reader, destination.open("xb") as writer:
                    shutil.copyfileobj(reader, writer, length=1024 * 1024)
                copied = _fingerprint(destination)
                if (copied.size, copied.digest) != (fingerprint.size, fingerprint.digest):
                    raise InvalidBackupError(("検査中に復元元が変更されました。",))
            self._check_source(source, expected)
            return connect_readonly(snapshot)
        except (OSError, sqlite3.Error) as error:
            raise RestoreError(
                f"復元元の一時スナップショットを作成できません: {error}",
                stage="pre_backup",
                recovered=True,
            ) from error

    def _verify_candidate(self, candidate: _Candidate) -> None:
        self._check_source(candidate.preview.source_path, candidate.source_fingerprint)
        try:
            candidate.workspace.check_identity()
            _require_single_file(candidate.path)
            _require_private_file(candidate.path)
            if _fingerprint(candidate.path) != candidate.candidate_fingerprint:
                raise InvalidBackupError(("一時候補が変更されました。再検査してください。",))
            with closing(connect_readonly(candidate.path, immutable=True)) as conn:
                if _inspect_supported_database(conn) != SCHEMA_VERSION:
                    raise InvalidBackupError(("一時候補が最新版ではありません。",))
                if _business_warnings(conn) != candidate.preview.warnings:
                    raise InvalidBackupError(("一時候補の警告が変わりました。",))
            _require_single_file(candidate.path)
            _require_private_file(candidate.path)
            if _fingerprint(candidate.path) != candidate.candidate_fingerprint:
                raise InvalidBackupError(("検査中に一時候補が変更されました。",))
        except (OSError, sqlite3.Error, ValueError) as error:
            raise InvalidBackupError((f"一時候補を再検査できません: {error}",)) from error
        self._check_source(candidate.preview.source_path, candidate.source_fingerprint)

    def _verify_current_backup(
        self, candidate: _Candidate, backup_path: Path, backup_state: _FileFingerprint
    ) -> _FileFingerprint:
        self._check_current_path()
        _require_single_file(self._database_path)
        before = _fingerprint(self._database_path)
        check_path = candidate.workspace.path / _CURRENT_COPY_NAME
        with closing(connect_readonly(self._database_path, immutable=True)) as conn:
            before_version = _data_version(conn)
            copy_database(conn, check_path)
            if _data_version(conn) != before_version:
                raise OSError("現行 DB が保全後に変更されました")
        _require_single_file(self._database_path)
        if _fingerprint(self._database_path) != before:
            raise OSError("現行 DB が保全後に変更されました")
        _require_single_file(check_path)
        _require_private_file(check_path)
        if _fingerprint(check_path).digest != backup_state.digest:
            raise OSError("保全バックアップと現行 DB が一致しません")
        check_path.unlink()
        _require_single_file(backup_path)
        _require_private_file(backup_path)
        if _fingerprint(backup_path) != backup_state:
            raise OSError("保全バックアップが変更されました")
        return before

    def restore_after_shutdown(
        self, backup_path: Path, close_connection: Callable[[], None]
    ) -> RestoreResult:
        candidate = self._candidate
        if candidate is None:
            raise InvalidBackupError(("検査済みの復元候補がありません。",))
        try:
            return self._restore(candidate, Path(backup_path), close_connection)
        except BaseException as error:
            if self._candidate is not None:
                self._candidate = None
                _cleanup_after_error(candidate.workspace, error)
            raise

    def _restore(
        self, candidate: _Candidate, backup_path: Path, close_connection: Callable[[], None]
    ) -> RestoreResult:
        self._verify_candidate(candidate)
        try:
            current_version = self._check_current_connection()
            backup_path.parent.mkdir(parents=True, exist_ok=True)
            copy_database(self._connection, backup_path)
        except (OSError, sqlite3.Error, RuntimeError) as error:
            raise RestoreError(
                f"現行 DB の保全バックアップを作成できません: {error}",
                stage="pre_backup",
                recovered=True,
            ) from error

        try:
            _require_single_file(backup_path)
            _require_private_file(backup_path)
            backup_state = _fingerprint(backup_path)
            if _data_version(self._connection) != current_version:
                raise OSError("現行 DB が保全中に変更されました")
            _checkpoint(self._connection)
            if _data_version(self._connection) != current_version:
                raise OSError("現行 DB がチェックポイント中に変更されました")
            close_connection()
            try:
                self._connection.execute("PRAGMA database_list")
            except sqlite3.ProgrammingError as error:
                if str(error) != "Cannot operate on a closed database.":
                    raise
            else:
                raise RuntimeError("現行 DB の接続が閉じられていません")
            self._verify_candidate(candidate)
            current_state = self._verify_current_backup(candidate, backup_path, backup_state)
            self._verify_candidate(candidate)
            self._check_current_path()
            _require_single_file(self._database_path)
            if _fingerprint(self._database_path) != current_state:
                raise OSError("現行 DB が保全後に変更されました")
            _require_single_file(backup_path)
            _require_private_file(backup_path)
            if _fingerprint(backup_path) != backup_state:
                raise OSError("保全バックアップが変更されました")
            candidate.workspace.check_identity()
            _require_single_file(candidate.path)
            _require_private_file(candidate.path)
            if _fingerprint(candidate.path) != candidate.candidate_fingerprint:
                raise OSError("一時候補が変更されました")
            os.replace(candidate.path, self._database_path)
        except (OSError, sqlite3.Error, RuntimeError, ValueError, InvalidBackupError) as error:
            raise RestoreError(
                f"現行 DB の置換を中止しました。保全バックアップ: {backup_path}。理由: {error}",
                stage="overwrite",
                recovered=True,
                backup_path=backup_path,
            ) from error

        self._candidate = None
        try:
            candidate.workspace.remove()
        except OSError as error:
            raise RestoreError(
                f"DB は置換済みですが、一時ディレクトリを削除できません: {error}。"
                f"保全バックアップ: {backup_path}",
                stage="recovery",
                recovered=False,
                backup_path=backup_path,
            ) from error
        return RestoreResult(backup_path)
