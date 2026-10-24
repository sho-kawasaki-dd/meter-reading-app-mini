from __future__ import annotations

import logging
import sqlite3
import sys
from collections.abc import Callable
from contextlib import ExitStack
from pathlib import Path
from typing import Protocol

from PySide6.QtWidgets import QApplication
from shiboken6 import delete as delete_qobject

from meter_reading_mini.config import AppPaths, ensure_dirs, resolve_paths
from meter_reading_mini.core.logging_config import configure_logging
from meter_reading_mini.core.timeutil import local_timestamp_for_filename
from meter_reading_mini.db.migrations import open_database
from meter_reading_mini.ui.display_settings import apply_display_settings, load_display_settings
from meter_reading_mini.ui.single_instance import SingleInstanceLock

logger = logging.getLogger(__name__)

STARTUP_SUCCESS = 0
STARTUP_FAILURE = 1
STARTUP_LOCKED = 2


class DatabaseOpener(Protocol):
    def __call__(
        self,
        path: Path,
        backup_dir: Path,
        *,
        backup_timestamp: Callable[[], str],
    ) -> sqlite3.Connection: ...


class LoggingConfigurator(Protocol):
    def __call__(self, *, log_dir: Path) -> None: ...


class InstanceLock(Protocol):
    def try_acquire(self, timeout_ms: int = 100) -> bool: ...

    def release(self) -> None: ...


def _create_qapplication() -> QApplication:
    return QApplication([])


def _initialize_ui(
    resources: ExitStack,
    application_factory: Callable[[], QApplication] | None,
    paths: AppPaths,
) -> None:
    application = QApplication.instance()
    if application is None:
        factory = application_factory or _create_qapplication
        application = factory()
        if QApplication.instance() is not application:
            raise RuntimeError("QApplication ファクトリがアプリケーションを作成しませんでした")

        application_ref = [application]

        def release_application() -> None:
            created_application = application_ref.pop()
            created_application.quit()
            delete_qobject(created_application)

        resources.callback(release_application)

    settings = load_display_settings(paths.ui_settings_path)
    apply_display_settings(settings)


def run_app(
    paths: AppPaths | None = None,
    *,
    application_factory: Callable[[], QApplication] | None = None,
    database_opener: DatabaseOpener | None = None,
    lock_factory: Callable[[Path], InstanceLock] | None = None,
    logging_configurator: LoggingConfigurator | None = None,
) -> int:
    logging_initialized = False

    try:
        resolved_paths = paths or resolve_paths()
        ensure_dirs(resolved_paths)

        lock = (lock_factory or SingleInstanceLock)(resolved_paths.lock_path)
        if not lock.try_acquire():
            print("アプリケーションは既に起動しています。", file=sys.stderr)
            return STARTUP_LOCKED

        with ExitStack() as resources:
            resources.callback(lock.release)

            (logging_configurator or configure_logging)(log_dir=resolved_paths.log_dir)
            logging_initialized = True

            connection = (database_opener or open_database)(
                resolved_paths.db_path,
                resolved_paths.backup_dir,
                backup_timestamp=local_timestamp_for_filename,
            )
            resources.callback(connection.close)

            _initialize_ui(resources, application_factory, resolved_paths)

        return STARTUP_SUCCESS
    except Exception as error:
        if logging_initialized:
            logger.exception("アプリケーションの起動に失敗しました")
        else:
            print(f"アプリケーションの起動に失敗しました: {error}", file=sys.stderr)
        return STARTUP_FAILURE
