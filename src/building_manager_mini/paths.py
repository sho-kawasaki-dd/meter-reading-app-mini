from __future__ import annotations

import os
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path

from platformdirs import user_data_dir, user_log_dir

APP_NAME = "building-manager-mini"
DATA_DIR_ENV = "BUILDING_MANAGER_MINI_DATA_DIR"
LEGACY_DATA_DIR_ENV = "METER_READING_MINI_DATA_DIR"
LEGACY_APP_NAME = "meter-reading-mini"


class PathConfigurationError(ValueError):
    pass


@dataclass(frozen=True, slots=True)
class AppPaths:
    data_dir: Path
    lock_path: Path
    ui_settings_path: Path
    common_db_path: Path
    meter_reading_dir: Path
    db_path: Path
    work_log_db_path: Path
    backup_root: Path
    backup_dir: Path
    safety_backup_root: Path
    recovery_pending_path: Path
    log_dir: Path
    log_path: Path

    @classmethod
    def from_root(cls, data_dir: Path) -> AppPaths:
        root = Path(data_dir).resolve()
        backup_root = root / "backups"
        log_dir = root / "logs"
        return cls(
            data_dir=root,
            lock_path=root / "app.lock",
            ui_settings_path=root / "ui_settings.json",
            common_db_path=root / "common" / "common.db",
            meter_reading_dir=root / "meter_reading",
            db_path=root / "meter_reading" / "meter_reading.db",
            work_log_db_path=root / "work_log" / "work_log.db",
            backup_root=backup_root,
            backup_dir=backup_root / "meter_reading",
            safety_backup_root=backup_root / "safety",
            recovery_pending_path=root / "recovery" / "pending.json",
            log_dir=log_dir,
            log_path=log_dir / "app.log",
        )


@dataclass(frozen=True, slots=True)
class StartupPaths:
    paths: AppPaths
    legacy_meter_reading_db_path: Path | None


def _resolved_path(value: str | Path) -> Path:
    try:
        return Path(value).expanduser().resolve()
    except (OSError, RuntimeError) as error:
        raise PathConfigurationError(f"保存先を解決できません: {value} ({error})") from error


def _overlaps(first: Path, second: Path) -> bool:
    first_parts = tuple(part.casefold() for part in first.parts)
    second_parts = tuple(part.casefold() for part in second.parts)
    common_length = min(len(first_parts), len(second_parts))
    return first_parts[:common_length] == second_parts[:common_length]


def resolve_startup_paths(env: Mapping[str, str] = os.environ) -> StartupPaths:
    has_new_override = DATA_DIR_ENV in env
    has_legacy_override = LEGACY_DATA_DIR_ENV in env
    if has_legacy_override and not has_new_override:
        raise PathConfigurationError(
            f"{LEGACY_DATA_DIR_ENV} は廃止されました。{DATA_DIR_ENV} を指定して起動してください。"
        )
    if has_new_override and not env[DATA_DIR_ENV].strip():
        raise PathConfigurationError(f"{DATA_DIR_ENV} に空でない保存先を指定してください。")
    if has_legacy_override and not env[LEGACY_DATA_DIR_ENV].strip():
        raise PathConfigurationError(
            f"{LEGACY_DATA_DIR_ENV} の保存先が空です。旧変数を解除するか、"
            f"{DATA_DIR_ENV} と旧 DB の保存先を指定してください。"
        )

    default_data_dir = _resolved_path(user_data_dir(APP_NAME, appauthor=False, roaming=True))
    legacy_default_data_dir = _resolved_path(
        user_data_dir(LEGACY_APP_NAME, appauthor=False, roaming=True)
    )
    legacy_default_log_dir = _resolved_path(user_log_dir(LEGACY_APP_NAME, appauthor=False))
    data_dir = _resolved_path(env[DATA_DIR_ENV]) if has_new_override else default_data_dir

    legacy_data_dir: Path | None
    if has_legacy_override:
        legacy_data_dir = _resolved_path(env[LEGACY_DATA_DIR_ENV])
    elif not has_new_override:
        legacy_data_dir = legacy_default_data_dir
    else:
        legacy_data_dir = None

    protected_legacy_dirs = [legacy_default_data_dir, legacy_default_log_dir]
    if has_legacy_override:
        protected_legacy_dirs.append(_resolved_path(env[LEGACY_DATA_DIR_ENV]))
    for protected_dir in protected_legacy_dirs:
        if _overlaps(data_dir, protected_dir):
            raise PathConfigurationError(
                "新しい保存先が旧版の保存領域またはログ領域と重なっています。"
                f"別の保存先を指定してください: {data_dir}"
            )

    paths = AppPaths.from_root(data_dir)
    legacy_db_path = legacy_data_dir / "meter_reading.db" if legacy_data_dir is not None else None
    return StartupPaths(paths, legacy_db_path)


def resolve_paths(env: Mapping[str, str] = os.environ) -> AppPaths:
    return resolve_startup_paths(env).paths


def ensure_dirs(paths: AppPaths) -> None:
    for directory in (
        paths.data_dir,
        paths.meter_reading_dir,
        paths.backup_root,
        paths.backup_dir,
        paths.log_dir,
    ):
        directory.mkdir(parents=True, exist_ok=True)
