from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import sqlite3
import stat
import tempfile
from collections.abc import Callable, Mapping
from contextlib import suppress
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
    try:
        record = _load_record(pending_path)
        safety_directory = _validate_record(record, safety_root, databases)
        for target in record.targets:
            if not target.existed:
                continue
            destination = databases[target.database_id].path
            destination.parent.mkdir(parents=True, exist_ok=True)
            descriptor, name = tempfile.mkstemp(
                prefix=".recovery-", suffix=".db", dir=destination.parent
            )
            os.close(descriptor)
            staged[target.database_id] = Path(name)
            assert target.safety_path is not None
            _copy_verified(Path(target.safety_path), staged[target.database_id], target)
            databases[target.database_id].validate(staged[target.database_id])

        for target in record.targets:
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
