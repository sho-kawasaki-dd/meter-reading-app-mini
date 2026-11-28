from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import sqlite3
import stat
import tempfile
from collections.abc import Callable, Mapping, Sequence
from contextlib import closing, suppress
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, ValidationError

from building_manager_mini.core.recovery import PendingRecoveryError
from building_manager_mini.core.settings_contracts import CommonSettingsError

logger = logging.getLogger(__name__)
_SIDECARS = ("-wal", "-shm", "-journal")
_IDENTIFIER = re.compile(r"[a-z][a-z0-9-]*\Z")
_OPERATION_ID = re.compile(r"[0-9a-f]{32}\Z")
_SHA256 = re.compile(r"[0-9a-f]{64}\Z")


class RecoveryTargetRecord(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    database_id: str
    path: str
    existed: bool
    safety_path: str | None
    sha256: str | None
    size: int | None
    modified: bool = True


class RecoveryRecord(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    format_version: Literal[1]
    operation_id: str
    operation_kind: Literal["initial_migration", "restore_bundle", "restore_legacy"]
    stage: Literal["prepared", "applying", "recovering"]
    automatic_backup_path: str | None
    targets: list[RecoveryTargetRecord]


@dataclass(frozen=True, slots=True)
class RegisteredDatabase:
    """検査関数は読取専用で DB を開き、戻る前に接続を閉じる。"""

    path: Path
    validate: Callable[[Path], None]


def _regular_file(path: Path) -> os.stat_result:
    info = path.lstat()
    if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
        raise ValueError(f"通常の単独ファイルではありません: {path}")
    return info


def _absolute_unlinked_path(path: Path) -> Path:
    absolute = path.absolute()
    if absolute.resolve() != absolute:
        raise ValueError(f"リンクを含むパスは使用できません: {absolute}")
    return absolute


def _load_record(pending_path: Path) -> RecoveryRecord:
    info = _regular_file(pending_path)
    if info.st_size > 1024 * 1024:
        raise ValueError("中断記録が大きすぎます。")

    def reject_duplicate_keys(pairs: list[tuple[str, object]]) -> dict[str, object]:
        result: dict[str, object] = {}
        for key, value in pairs:
            if key in result:
                raise ValueError(f"中断記録に重複する項目があります: {key}")
            result[key] = value
        return result

    data: object = json.loads(
        pending_path.read_text(encoding="utf-8"), object_pairs_hook=reject_duplicate_keys
    )
    return RecoveryRecord.model_validate(data)


def _validate_record(
    record: RecoveryRecord,
    safety_root: Path,
    databases: Mapping[str, RegisteredDatabase],
) -> Path:
    if not _OPERATION_ID.fullmatch(record.operation_id) or not record.targets:
        raise ValueError("中断記録の操作 ID または対象が不正です。")
    safety_directory = _absolute_unlinked_path(safety_root / record.operation_id)
    if not stat.S_ISDIR(safety_directory.lstat().st_mode):
        raise ValueError(f"保全先がディレクトリではありません: {safety_directory}")
    seen: set[str] = set()
    seen_paths: set[Path] = set()
    for target in record.targets:
        database = databases.get(target.database_id)
        if (
            database is None
            or not _IDENTIFIER.fullmatch(target.database_id)
            or target.database_id in seen
            or _absolute_unlinked_path(database.path) != Path(target.path)
        ):
            raise ValueError(f"中断記録の対象 DB が不正です: {target.database_id}")
        seen.add(target.database_id)
        registered_path = _absolute_unlinked_path(database.path)
        if registered_path in seen_paths:
            raise ValueError(f"中断記録に重複する DB パスがあります: {database.path}")
        seen_paths.add(registered_path)
        expected_safety = safety_directory / f"{target.database_id}.db"
        if target.existed:
            if (
                target.safety_path != str(expected_safety)
                or target.sha256 is None
                or _SHA256.fullmatch(target.sha256) is None
                or target.size is None
                or target.size <= 0
            ):
                raise ValueError(f"保全情報が不正です: {target.database_id}")
            _absolute_unlinked_path(expected_safety)
            _regular_file(expected_safety)
        elif target.safety_path is not None or target.sha256 is not None or target.size is not None:
            raise ValueError(f"不存在 DB の保全情報が不正です: {target.database_id}")
        if not target.modified and not target.existed:
            raise ValueError(f"変更しない DB の保全情報が不正です: {target.database_id}")
    if not any(target.modified for target in record.targets):
        raise ValueError("中断記録に変更対象 DB がありません。")
    original_database_exists = any(target.modified and target.existed for target in record.targets)
    if original_database_exists != (record.automatic_backup_path is not None):
        raise ValueError("中断記録の自動保全先が不正です。")
    if record.automatic_backup_path is not None:
        automatic_path = Path(record.automatic_backup_path)
        if (
            not automatic_path.is_absolute()
            or automatic_path.parent != _absolute_unlinked_path(safety_root.parent)
            or automatic_path.suffix.lower() not in {".zip", ".db"}
        ):
            raise ValueError("中断記録の自動保全先が不正です。")
    return safety_directory


def _copy_verified(source: Path, destination: Path, target: RecoveryTargetRecord) -> None:
    digest = hashlib.sha256()
    with source.open("rb") as source_file, destination.open("wb") as output:
        before = os.fstat(source_file.fileno())
        while chunk := source_file.read(1024 * 1024):
            output.write(chunk)
            digest.update(chunk)
        after = os.fstat(source_file.fileno())
        output.flush()
        os.fsync(output.fileno())
    if (
        (before.st_dev, before.st_ino, before.st_size, before.st_mtime_ns)
        != (after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns)
        or after.st_size != target.size
        or digest.hexdigest() != target.sha256
    ):
        raise ValueError(f"保全データが一致しません: {source}")


def _remove_regular_if_present(path: Path) -> None:
    try:
        _regular_file(path)
    except FileNotFoundError:
        return
    path.unlink()


def _file_digest(path: Path) -> tuple[int, str]:
    digest = hashlib.sha256()
    size = 0
    with path.open("rb") as source:
        while chunk := source.read(1024 * 1024):
            size += len(chunk)
            digest.update(chunk)
    return size, digest.hexdigest()


def _exists(path: Path) -> bool:
    try:
        path.lstat()
    except FileNotFoundError:
        return False
    return True


def _require_no_sidecars(path: Path) -> None:
    for suffix in _SIDECARS:
        sidecar = Path(f"{path}{suffix}")
        if _exists(sidecar):
            raise ValueError(f"DB の関連ファイルが残っています: {sidecar}")


def _prepare_recovery_staging(destination: Path, operation_id: str, identifier: str) -> Path:
    directory = destination.parent / f".recovery-{operation_id}-{identifier}"
    _absolute_unlinked_path(directory)
    if _exists(directory):
        if not stat.S_ISDIR(directory.lstat().st_mode):
            raise ValueError(f"復旧用の一時保存先が不正です: {directory}")
        allowed = {f"snapshot.db{suffix}" for suffix in ("", *_SIDECARS)}
        if any(path.name not in allowed for path in directory.iterdir()):
            raise ValueError(f"復旧用の一時保存先に不明なファイルがあります: {directory}")
        for suffix in (*_SIDECARS, ""):
            _remove_regular_if_present(directory / f"snapshot.db{suffix}")
    else:
        directory.mkdir()
    return directory


def prepare_operation(
    pending_path: Path,
    safety_root: Path,
    databases: Mapping[str, RegisteredDatabase],
    target_ids: Sequence[str],
    *,
    operation_id: str,
    operation_kind: Literal["initial_migration", "restore_bundle", "restore_legacy"],
    copy_database: Callable[[sqlite3.Connection, Path], None],
    create_and_verify_backup: Callable[[Mapping[str, Path]], Path],
) -> RecoveryRecord:
    """候補検査・全体ロック取得・全 DB 接続の終了後、現行 DB の変更前に実行する。

    自動保全のコールバックは、新しいファイルを確定し復元経路で検査してから返す。
    """
    try:
        pending_path = _absolute_unlinked_path(pending_path)
        target_ids = tuple(target_ids)
        if _exists(pending_path):
            raise ValueError("未完了の DB 操作が既にあります。")
        if not _OPERATION_ID.fullmatch(operation_id) or not target_ids:
            raise ValueError("操作 ID または対象が不正です。")
        if len(set(target_ids)) != len(target_ids):
            raise ValueError("変更対象 DB が重複しています。")

        paths: dict[str, Path] = {}
        existing: dict[str, bool] = {}
        for identifier in target_ids:
            database = databases.get(identifier)
            if database is None or not _IDENTIFIER.fullmatch(identifier):
                raise ValueError(f"未登録の DB です: {identifier}")
            path = _absolute_unlinked_path(database.path)
            if path in paths.values():
                raise ValueError(f"変更対象 DB のパスが重複しています: {path}")
            paths[identifier] = path
            existing[identifier] = _exists(path)
            if existing[identifier]:
                _regular_file(path)
            else:
                _require_no_sidecars(path)

        if any(existing.values()) and "common" in databases and "common" not in paths:
            common_path = _absolute_unlinked_path(databases["common"].path)
            if common_path in paths.values():
                raise ValueError(f"共通 DB のパスが重複しています: {common_path}")
            if _exists(common_path):
                _regular_file(common_path)
                paths["common"] = common_path
                existing["common"] = True

        safety_directory = _absolute_unlinked_path(safety_root / operation_id)
        safety_directory.mkdir(parents=True, exist_ok=False)
        targets: list[RecoveryTargetRecord] = []
        snapshots: dict[str, Path] = {}
        for identifier, path in paths.items():
            safety_path: Path | None = None
            size: int | None = None
            digest: str | None = None
            if existing[identifier]:
                safety_path = safety_directory / f"{identifier}.db"
                with closing(
                    sqlite3.connect(f"{path.as_uri()}?mode=ro", uri=True, autocommit=True)
                ) as source:
                    source.execute("PRAGMA query_only = ON")
                    source.execute("PRAGMA busy_timeout = 5000")
                    copy_database(source, safety_path)
                _regular_file(safety_path)
                _require_no_sidecars(safety_path)
                with closing(sqlite3.connect(safety_path, autocommit=True)) as snapshot_connection:
                    mode = snapshot_connection.execute("PRAGMA journal_mode = DELETE").fetchone()
                    if mode is None or str(mode[0]).lower() != "delete":
                        raise ValueError(f"保全 DB を単独ファイルにできません: {safety_path}")
                _require_no_sidecars(safety_path)
                databases[identifier].validate(safety_path)
                _require_no_sidecars(safety_path)
                with safety_path.open("r+b") as snapshot:
                    os.fsync(snapshot.fileno())
                size, digest = _file_digest(safety_path)
                snapshots[identifier] = safety_path
            targets.append(
                RecoveryTargetRecord(
                    database_id=identifier,
                    path=str(path),
                    existed=existing[identifier],
                    safety_path=str(safety_path) if safety_path is not None else None,
                    sha256=digest,
                    size=size,
                    modified=identifier in target_ids,
                )
            )

        automatic_backup_path: Path | None = None
        if any(existing[identifier] for identifier in target_ids):
            legacy_only = "common" not in snapshots
            if legacy_only:
                if operation_kind != "initial_migration" or set(snapshots) != {"meter-reading"}:
                    raise ValueError("共通 DB のない環境では旧検針 DB の初回移行のみ保全できます。")
                with closing(
                    sqlite3.connect(
                        f"{snapshots['meter-reading'].as_uri()}?mode=ro",
                        uri=True,
                        autocommit=True,
                    )
                ) as legacy:
                    version = legacy.execute("PRAGMA user_version").fetchone()
                if version is None or not 1 <= version[0] <= 6:
                    raise ValueError("単体で自動保全できるのは旧検針 DB v1～v6 のみです。")
            automatic_backup_path = _absolute_unlinked_path(create_and_verify_backup(snapshots))
            if automatic_backup_path.parent != _absolute_unlinked_path(safety_root.parent):
                raise ValueError("自動保全先がバックアップ保存先ではありません。")
            if automatic_backup_path.suffix.lower() != (".db" if legacy_only else ".zip"):
                raise ValueError("自動保全先の形式が不正です。")
            _regular_file(automatic_backup_path)
            with automatic_backup_path.open("r+b") as backup:
                os.fsync(backup.fileno())

        record = RecoveryRecord(
            format_version=1,
            operation_id=operation_id,
            operation_kind=operation_kind,
            stage="prepared",
            automatic_backup_path=(
                str(automatic_backup_path) if automatic_backup_path is not None else None
            ),
            targets=targets,
        )
        _validate_record(record, safety_root, databases)
        for target in targets:
            if target.existed:
                assert target.safety_path is not None
                snapshot_path = Path(target.safety_path)
                _require_no_sidecars(snapshot_path)
                if _file_digest(snapshot_path) != (target.size, target.sha256):
                    raise ValueError(f"保全データが処理中に変更されました: {snapshot_path}")

        pending_path.parent.mkdir(parents=True, exist_ok=True)
        descriptor, name = tempfile.mkstemp(
            prefix=".pending-", suffix=".tmp", dir=pending_path.parent
        )
        temporary = Path(name)
        try:
            with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as output:
                output.write(record.model_dump_json())
                output.flush()
                os.fsync(output.fileno())
            if _exists(pending_path):
                raise ValueError("未完了の DB 操作が既にあります。")
            os.replace(temporary, pending_path)
        finally:
            with suppress(FileNotFoundError):
                temporary.unlink()
        return record
    except (OSError, ValueError, sqlite3.Error, CommonSettingsError) as error:
        raise PendingRecoveryError(
            f"DB 操作の保全を準備できません。\n中断記録: {pending_path}\n"
            f"保全先: {safety_root}\n現行 DB を変更せず、管理者に相談してください。"
        ) from error


def recover_pending(
    pending_path: Path,
    safety_root: Path,
    databases: Mapping[str, RegisteredDatabase],
    *,
    validate_set: Callable[[], None],
) -> bool:
    """全体ロック取得・全 DB 接続の終了後に実行する。組の検査も接続を閉じる。"""
    try:
        pending_path.lstat()
    except FileNotFoundError:
        return False
    except OSError as error:
        raise PendingRecoveryError(f"中断記録を確認できません: {pending_path}") from error

    staged: dict[str, Path] = {}
    staging_directories: list[Path] = []
    try:
        record = _load_record(pending_path)
        safety_directory = _validate_record(record, safety_root, databases)
        for target in record.targets:
            if not target.modified or not target.existed:
                continue
            destination = databases[target.database_id].path
            destination.parent.mkdir(parents=True, exist_ok=True)
            directory = _prepare_recovery_staging(
                destination, record.operation_id, target.database_id
            )
            staging_directories.append(directory)
            staged[target.database_id] = directory / "snapshot.db"
            assert target.safety_path is not None
            _copy_verified(Path(target.safety_path), staged[target.database_id], target)
            databases[target.database_id].validate(staged[target.database_id])
            _require_no_sidecars(staged[target.database_id])

        for target in record.targets:
            if not target.modified:
                continue
            destination = databases[target.database_id].path
            for suffix in _SIDECARS:
                _remove_regular_if_present(Path(f"{destination}{suffix}"))
            if target.existed:
                with suppress(FileNotFoundError):
                    _regular_file(destination)
                os.replace(staged[target.database_id], destination)
                staged.pop(target.database_id)
            else:
                _remove_regular_if_present(destination)

        for target in record.targets:
            if not target.modified:
                continue
            destination = databases[target.database_id].path
            if target.existed:
                assert target.safety_path is not None
                if _file_digest(destination) != (target.size, target.sha256):
                    raise ValueError(f"復旧後の DB が保全データと一致しません: {destination}")
                databases[target.database_id].validate(destination)
            else:
                try:
                    destination.lstat()
                except FileNotFoundError:
                    continue
                raise ValueError(f"復旧後に不要な DB が残っています: {destination}")
        validate_set()
        pending_path.unlink()
        for target in record.targets:
            if target.safety_path is not None:
                try:
                    _remove_regular_if_present(Path(target.safety_path))
                except (OSError, ValueError):
                    logger.warning(
                        "復旧後の保全ファイルを削除できません: %s",
                        target.safety_path,
                        exc_info=True,
                    )
        try:
            safety_directory.rmdir()
        except OSError:
            logger.warning(
                "復旧後の保全ディレクトリを削除できません: %s", safety_directory, exc_info=True
            )
        return True
    except (OSError, ValueError, sqlite3.Error, ValidationError, CommonSettingsError) as error:
        raise PendingRecoveryError(
            f"中断した DB 操作を復旧できません。\n中断記録: {pending_path}\n"
            f"保全先: {safety_root}\n記録と保全先を削除せず、管理者に相談してください。"
        ) from error
    finally:
        for path in staged.values():
            try:
                path.unlink()
            except FileNotFoundError:
                pass
            except OSError:
                logger.warning("復旧用の一時ファイルを削除できません: %s", path, exc_info=True)
        for directory in staging_directories:
            for suffix in _SIDECARS:
                try:
                    _remove_regular_if_present(directory / f"snapshot.db{suffix}")
                except (OSError, ValueError):
                    logger.warning(
                        "復旧用の関連ファイルを削除できません: %s", directory, exc_info=True
                    )
            try:
                directory.rmdir()
            except OSError:
                logger.warning("復旧用の一時保存先を削除できません: %s", directory, exc_info=True)
