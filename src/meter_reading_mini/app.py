from __future__ import annotations

import logging
import os
import sqlite3
import sys
from collections.abc import Callable
from contextlib import ExitStack
from pathlib import Path
from typing import Protocol

from PySide6.QtCore import QTimer
from PySide6.QtWidgets import QApplication, QMainWindow, QMessageBox
from shiboken6 import delete as delete_qobject

from meter_reading_mini.app_context import AppContext, create_app_context
from meter_reading_mini.config import AppPaths, ensure_dirs, resolve_paths
from meter_reading_mini.core.backup_restore_contracts import RestoreResult
from meter_reading_mini.core.backup_restore_service import BackupRestoreService
from meter_reading_mini.core.errors import DomainError, InvalidBackupError, RestoreError
from meter_reading_mini.core.logging_config import configure_logging
from meter_reading_mini.core.timeutil import local_timestamp_for_filename
from meter_reading_mini.db.migrations import open_database
from meter_reading_mini.db.restore import SQLiteBackupRestoreRepository
from meter_reading_mini.installation_guard import InstallationGuardError, create_installation_guard
from meter_reading_mini.restart import (
    RestartLauncher,
    RestartNotice,
    restart_application,
    signal_restart_ready,
    take_restart_notice,
)
from meter_reading_mini.ui.display_settings import apply_display_settings, load_display_settings
from meter_reading_mini.ui.error_handling import (
    install_excepthook,
    show_domain_error,
    show_unexpected_error,
)
from meter_reading_mini.ui.main_window import MainWindow
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


class StartupPreparer(Protocol):
    def __call__(self, conn: sqlite3.Connection) -> None: ...


class StartupErrorNotifier(Protocol):
    def __call__(self, error: BaseException) -> None: ...


class InstanceLock(Protocol):
    def try_acquire(self, timeout_ms: int = 100) -> bool: ...

    def release(self) -> None: ...


class InstallGuard(Protocol):
    def try_acquire(self) -> bool: ...

    def release(self) -> None: ...


def _create_qapplication() -> QApplication:
    return QApplication([])


def _report_startup_error(message: str, *, gui_message: str | None = None) -> None:
    if sys.stderr is not None:
        print(message, file=sys.stderr)
        return

    application = QApplication.instance()
    if application is None:
        application = _create_qapplication()
    if not isinstance(application, QApplication):
        raise RuntimeError("起動エラーを表示できません: QApplication が利用できません。")
    QMessageBox.critical(
        None,
        "アプリケーションを起動できません",
        gui_message if gui_message is not None else message,
    )


def _initialize_ui(
    resources: ExitStack,
    application_factory: Callable[[], QApplication] | None,
    paths: AppPaths,
) -> QApplication:
    application = QApplication.instance()
    if application is None:
        factory = application_factory or _create_qapplication
        application = factory()
        if not isinstance(application, QApplication) or QApplication.instance() is not application:
            raise RuntimeError("QApplication ファクトリがアプリケーションを作成しませんでした")

        application_ref = [application]

        def release_application() -> None:
            created_application = application_ref.pop()
            created_application.quit()
            delete_qobject(created_application)

        resources.callback(release_application)
    if not isinstance(application, QApplication):
        raise RuntimeError("QApplication 以外のアプリケーションが既に存在します")

    settings = load_display_settings(paths.ui_settings_path)
    apply_display_settings(settings)
    return application


def _restore_backup_path(error: BaseException) -> Path | None:
    if isinstance(error, RestoreError) and error.backup_path is not None:
        return error.backup_path
    if isinstance(error, BaseExceptionGroup):
        for nested in error.exceptions:
            path = _restore_backup_path(nested)
            if path is not None:
                return path
    return None


def _show_restore_failure(error: Exception) -> None:
    if isinstance(error, DomainError):
        detail = error.message
    else:
        logger.error(
            "復元処理で予期しないエラーが発生しました",
            exc_info=(type(error), error, error.__traceback__),
        )
        detail = "予期しないエラーが発生しました。詳細はログを確認してください。"
    if isinstance(error, InvalidBackupError) or (
        isinstance(error, RestoreError) and error.recovered
    ):
        state = "復元を中止し、現行 DB の置換は行っていません。"
    else:
        state = "DB の置換状態を確認してください。"
    backup_path = _restore_backup_path(error)
    backup = f"\n保全バックアップ: {backup_path}" if backup_path is not None else ""
    QMessageBox.critical(
        None,
        "復元を完了できません",
        f"{state}\n{detail}{backup}\nアプリを手動で起動し、必要に応じて保全先を確認してください。",
    )


def _show_restart_failure(error: Exception, backup_path: Path) -> None:
    logger.error(
        "復元後の再起動を確認できませんでした",
        exc_info=(type(error), error, error.__traceback__),
    )
    QMessageBox.critical(
        None,
        "再起動を確認できません",
        "DB は復元されましたが、アプリの再起動を確認できませんでした。\n"
        f"保全バックアップ: {backup_path}\n"
        "アプリが起動していなければ手動で起動してください。"
        "問題がある場合は保全バックアップを確認してください。",
    )


def _announce_restart_ready(notice: RestartNotice) -> None:
    try:
        signal_restart_ready(notice)
    except OSError as error:
        logger.error(
            "再起動の確認を送信できませんでした",
            exc_info=(type(error), error, error.__traceback__),
        )
        QMessageBox.warning(
            None,
            "再起動の確認",
            "アプリは起動しましたが、元のプロセスへ起動完了を通知できませんでした。\n"
            f"保全バックアップ: {notice.backup_path}",
        )
        return
    QMessageBox.information(
        None,
        "復元が完了しました",
        f"DB を復元してアプリを再起動しました。\n保全バックアップ: {notice.backup_path}",
    )


def run_app(
    paths: AppPaths | None = None,
    *,
    application_factory: Callable[[], QApplication] | None = None,
    database_opener: DatabaseOpener | None = None,
    lock_factory: Callable[[Path], InstanceLock] | None = None,
    install_guard_factory: Callable[[], InstallGuard | None] | None = None,
    logging_configurator: LoggingConfigurator | None = None,
    startup_preparer: StartupPreparer | None = None,
    startup_error_notifier: StartupErrorNotifier | None = None,
    main_window_factory: Callable[[AppContext], QMainWindow] | None = None,
    event_loop_runner: Callable[[QApplication], int] | None = None,
    restart_launcher: RestartLauncher | None = None,
    restart_timeout: float = 30.0,
) -> int:
    logging_initialized = False

    try:
        resolved_paths = paths or resolve_paths()
        restart_notice = take_restart_notice(os.environ, resolved_paths.data_dir)
        if restart_notice is not None and paths is None:
            resolved_paths = AppPaths.from_dirs(resolved_paths.data_dir, restart_notice.log_dir)
        ensure_dirs(resolved_paths)

        lock = (lock_factory or SingleInstanceLock)(resolved_paths.lock_path)
        if not lock.try_acquire():
            _report_startup_error("アプリケーションは既に起動しています。")
            return STARTUP_LOCKED

        with ExitStack() as startup_resources:
            startup_resources.callback(lock.release)
            install_guard = (install_guard_factory or create_installation_guard)()
            if install_guard is not None:
                if not install_guard.try_acquire():
                    _report_startup_error(
                        "アプリケーションの更新または削除が進行中です。完了後に起動してください。"
                    )
                    return STARTUP_LOCKED
                startup_resources.callback(install_guard.release)
            startup_resources.pop_all()

        restore_result: RestoreResult | None = None
        with ExitStack() as ui_resources:
            if install_guard is not None:
                ui_resources.callback(install_guard.release)
            try:
                with ExitStack() as resources:
                    resources.callback(lock.release)

                    (logging_configurator or configure_logging)(log_dir=resolved_paths.log_dir)
                    logging_initialized = True
                    previous_excepthook = sys.excepthook
                    install_excepthook(resolved_paths.log_path)
                    resources.callback(setattr, sys, "excepthook", previous_excepthook)

                    connection = (database_opener or open_database)(
                        resolved_paths.db_path,
                        resolved_paths.backup_dir,
                        backup_timestamp=local_timestamp_for_filename,
                    )
                    connection_closed = False

                    def close_connection_once() -> None:
                        nonlocal connection_closed
                        if not connection_closed:
                            connection_closed = True
                            connection.close()

                    resources.callback(close_connection_once)

                    try:
                        application = _initialize_ui(
                            ui_resources, application_factory, resolved_paths
                        )
                        context = create_app_context(connection)
                        try:
                            if startup_preparer is not None:
                                startup_preparer(connection)
                            else:
                                context.startup_preparation.prepare()
                        except Exception as error:
                            logger.exception("起動時準備に失敗しました")
                            (
                                startup_error_notifier
                                or (lambda failure: show_unexpected_error(None, failure))
                            )(error)
                            return STARTUP_FAILURE

                        restore_requested = False
                        backup_service: BackupRestoreService | None = None

                        def request_restore() -> None:
                            nonlocal restore_requested
                            if restore_requested:
                                raise RuntimeError("復元の終了処理は既に開始されています。")
                            restore_requested = True
                            application.quit()

                        if main_window_factory is not None:
                            window = main_window_factory(context)
                        else:
                            backup_service = BackupRestoreService(
                                SQLiteBackupRestoreRepository(connection, resolved_paths.db_path),
                                resolved_paths.backup_dir,
                                timezone_provider=context.timezone_settings.get_timezone,
                            )
                            resources.callback(backup_service.discard_preview)
                            window = MainWindow(
                                context,
                                database_path=resolved_paths.db_path,
                                display_settings_path=resolved_paths.ui_settings_path,
                                backup_restore_service=backup_service,
                                backup_directory=resolved_paths.backup_dir,
                                request_restore=request_restore,
                            )

                        window_closed = False

                        def close_window_once() -> None:
                            nonlocal window_closed
                            if not window_closed:
                                window_closed = True
                                if not window.close():
                                    raise RuntimeError("メイン画面を終了できませんでした。")

                        resources.callback(close_window_once)
                        window.show()
                        if restart_notice is not None:
                            QTimer.singleShot(0, lambda: _announce_restart_ready(restart_notice))
                        loop_result = (event_loop_runner or (lambda app: app.exec()))(application)
                        if not restore_requested:
                            return loop_result
                        if backup_service is None:
                            raise RuntimeError("復元サービスが利用できません。")
                        try:
                            if loop_result != STARTUP_SUCCESS:
                                raise RestoreError(
                                    "イベントループが正常終了しなかったため復元を中止しました。",
                                    stage="pre_backup",
                                    recovered=True,
                                )
                            close_window_once()
                            restore_result = backup_service.restore_after_shutdown(
                                close_connection_once
                            )
                        except Exception as error:
                            _show_restore_failure(error)
                            return STARTUP_FAILURE
                    except DomainError as error:
                        logger.warning("アプリケーションの起動または実行を中断しました: %s", error)
                        if startup_error_notifier is not None:
                            startup_error_notifier(error)
                        elif isinstance(QApplication.instance(), QApplication):
                            show_domain_error(None, error)
                        else:
                            _report_startup_error(f"アプリケーションの起動に失敗しました: {error}")
                        return STARTUP_FAILURE
                    except Exception as error:
                        logger.exception("アプリケーションの起動または実行に失敗しました")
                        if isinstance(QApplication.instance(), QApplication):
                            (
                                startup_error_notifier
                                or (lambda failure: show_unexpected_error(None, failure))
                            )(error)
                        else:
                            _report_startup_error(
                                f"アプリケーションの起動に失敗しました: {error}",
                                gui_message="予期しないエラーが発生しました。詳細はログを確認してください。",
                            )
                        return STARTUP_FAILURE
            except Exception as error:
                if restore_result is None:
                    raise
                _show_restart_failure(error, restore_result.backup_path)
                return STARTUP_FAILURE

            if restore_result is None:
                raise RuntimeError("復元結果を確認できません。")
            try:
                restart_application(
                    resolved_paths,
                    restore_result.backup_path,
                    launcher=restart_launcher,
                    timeout=restart_timeout,
                )
            except Exception as error:
                _show_restart_failure(error, restore_result.backup_path)
                return STARTUP_FAILURE
            return STARTUP_SUCCESS

    except Exception as error:
        if logging_initialized:
            logger.exception("アプリケーションの起動に失敗しました")
        if isinstance(error, DomainError):
            gui_message = f"アプリケーションの起動に失敗しました: {error.message}"
        elif isinstance(error, InstallationGuardError):
            gui_message = str(error)
        elif logging_initialized:
            gui_message = "予期しないエラーが発生しました。詳細はログを確認してください。"
        else:
            gui_message = (
                "アプリケーションの起動に失敗しました。保存先と実行環境を確認してください。"
            )
        _report_startup_error(
            f"アプリケーションの起動に失敗しました: {error}", gui_message=gui_message
        )
        return STARTUP_FAILURE
