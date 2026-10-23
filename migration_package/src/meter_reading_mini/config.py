from __future__ import annotations

import os
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path

from platformdirs import user_data_dir, user_log_dir

APP_NAME = "meter-reading-mini"
DATA_DIR_ENV = "METER_READING_MINI_DATA_DIR"


@dataclass(frozen=True, slots=True)
class AppPaths:
    data_dir: Path
    db_path: Path
    backup_dir: Path
    lock_path: Path
    ui_settings_path: Path
    log_dir: Path
    log_path: Path

    @classmethod
    def from_dirs(cls, data_dir: Path, log_dir: Path) -> AppPaths:
        return cls(
            data_dir=data_dir,
            db_path=data_dir / "meter_reading.db",
            backup_dir=data_dir / "backups",
            lock_path=data_dir / "meter_reading.lock",
            ui_settings_path=data_dir / "ui_settings.json",
            log_dir=log_dir,
            log_path=log_dir / "app.log",
        )


def resolve_paths(env: Mapping[str, str] = os.environ) -> AppPaths:
    data_dir_override = env.get(DATA_DIR_ENV)
    if data_dir_override:
        data_dir = Path(data_dir_override)
        return AppPaths.from_dirs(data_dir, data_dir / "logs")

    data_dir = Path(user_data_dir(APP_NAME, appauthor=False, roaming=True))
    log_dir = Path(user_log_dir(APP_NAME, appauthor=False))
    return AppPaths.from_dirs(data_dir, log_dir)


def ensure_dirs(paths: AppPaths) -> None:
    for directory in (paths.data_dir, paths.backup_dir, paths.log_dir):
        directory.mkdir(parents=True, exist_ok=True)
