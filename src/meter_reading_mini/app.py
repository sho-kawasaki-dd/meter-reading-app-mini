from __future__ import annotations

import logging
import os
import sqlite3
import stat
import sys
from collections.abc import Callable, Mapping, Sequence
from contextlib import ExitStack, closing
from pathlib import Path
from typing import Protocol

from PySide6.QtCore import QTimer
from PySide6.QtGui import QIcon
from PySide6.QtWidgets import QApplication, QMainWindow, QMessageBox
from shiboken6 import delete as delete_qobject

from building_manager_mini.core.recovery import PendingRecoveryError
from building_manager_mini.db.migrations import (
    open_database_readonly as open_common_database_readonly,
)
from building_manager_mini.db.recovery import RegisteredDatabase, recover_pending
from building_manager_mini.paths import (
    AppPaths,
    PathConfigurationError,
    ensure_dirs,
    resolve_startup_paths,
)
from building_manager_mini.settings import load_ui_settings, set_last_application
from building_manager_mini.ui.application_host import ApplicationHostWindow
from building_manager_mini.ui.application_selection import ApplicationChoice
from meter_reading_mini.app_context import AppContext, create_app_context
from meter_reading_mini.core.backup_restore_contracts import RestoreResult
from meter_reading_mini.core.backup_restore_service import BackupRestoreService
from meter_reading_mini.core.errors import DomainError, InvalidBackupError, RestoreError
from meter_reading_mini.core.logging_config import configure_logging
from meter_reading_mini.core.timeutil import local_timestamp_for_filename
from meter_reading_mini.db.backup import check_sqlite_integrity
from meter_reading_mini.db.connection import connect_readonly
from meter_reading_mini.db.integrity import check_business_integrity
from meter_reading_mini.db.legacy_migration import migrate_legacy_database
from meter_reading_mini.db.migrations import (
    MIN_SUPPORTED_SCHEMA_VERSION,
    SCHEMA_VERSION,
    check_migration_path,
    check_migration_prechecks,
    inspect_schema,
    open_database,
)
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


class ApplicationSelector(Protocol):
    def __call__(
        self,
        applications: Sequence[ApplicationChoice],
        *,
        last_application: str | None = None,
    ) -> str | None: ...


class StartupErrorNotifier(Protocol):
    def __call__(self, error: BaseException) -> None: ...


class MigrationNotifier(Protocol):
    def __call__(self, legacy_database_path: Path, new_database_path: Path) -> None: ...


class InstanceLock(Protocol):
    def try_acquire(self, timeout_ms: int = 100) -> bool: ...

    def release(self) -> None: ...


class InstallGuard(Protocol):
    def try_acquire(self) -> bool: ...

    def release(self) -> None: ...


METER_READING_APPLICATION_ID = "meter-reading"
REGISTERED_APPLICATIONS: tuple[ApplicationChoice, ...] = ((METER_READING_APPLICATION_ID, "検針"),)


def _validate_meter_reading_database(path: Path) -> None:
    with closing(connect_readonly(path)) as connection:
        version_row = connection.execute("PRAGMA user_version").fetchone()
        if version_row is None or type(version_row[0]) is not int:
            raise ValueError("検針 DB のスキーマ版を確認できません。")
        version = version_row[0]
        if not MIN_SUPPORTED_SCHEMA_VERSION <= version <= SCHEMA_VERSION:
            raise ValueError(f"検針 DB のスキーマ版が未対応です: {version}")
        schema_reasons = inspect_schema(connection, version)
        if schema_reasons:
            raise ValueError("検針 DB のスキーマが不正です: " + "、".join(schema_reasons))
        integrity_reasons = check_sqlite_integrity(connection)
        if integrity_reasons:
            raise ValueError("検針 DB の整合性が不正です: " + "、".join(integrity_reasons))
        try:
            check_migration_path(version, SCHEMA_VERSION)
            check_migration_prechecks(connection, version, SCHEMA_VERSION)
        except DomainError as error:
            raise ValueError(f"検針 DB の移行前検査に失敗しました: {error}") from error
        if version == SCHEMA_VERSION:
            business_reasons = check_business_integrity(connection)
            if business_reasons:
                raise ValueError("検針 DB の業務整合性が不正です: " + "、".join(business_reasons))


def _validate_common_database(path: Path) -> None:
    with closing(open_common_database_readonly(path)):
        pass


def _validate_recovery_database_set(databases: Mapping[str, RegisteredDatabase]) -> None:
    for database in databases.values():
        try:
            info = database.path.lstat()
        except FileNotFoundError:
            continue
        if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
            raise ValueError(f"DB が通常の単独ファイルではありません: {database.path}")
        database.validate(database.path)


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

    icon_path = Path(__file__).parent / "assets" / "meter-reading-mini.ico"
    icon = QIcon(str(icon_path))
    if icon.isNull():
        raise RuntimeError(f"アプリケーションアイコンを読み込めません: {icon_path}")
    application.setWindowIcon(icon)

    settings = load_display_settings(paths.ui_settings_path)
    apply_display_settings(settings)
    return application


def _choose_application(
    paths: AppPaths,
    selector: ApplicationSelector,
) -> str | None:
    settings = load_ui_settings(paths.ui_settings_path)
    selected_application = selector(
        REGISTERED_APPLICATIONS,
        last_application=settings.last_application,
    )
    if selected_application is None:
        return None
    registered_application_ids = {application_id for application_id, _ in REGISTERED_APPLICATIONS}
    if selected_application not in registered_application_ids:
        raise LookupError(f"未登録のアプリです: {selected_application}")
    set_last_application(paths.ui_settings_path, selected_application)
    return selected_application


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
    migration_notifier: MigrationNotifier | None = None,
    application_selector: ApplicationSelector | None = None,
    main_window_factory: Callable[[AppContext], QMainWindow] | None = None,
    event_loop_runner: Callable[[QApplication], int] | None = None,
    restart_launcher: RestartLauncher | None = None,
    restart_timeout: float = 30.0,
) -> int:
    logging_initialized = False

    try:
        startup_paths = resolve_startup_paths(os.environ)
        resolved_paths = paths if paths is not None else startup_paths.paths
        legacy_database_path = startup_paths.legacy_meter_reading_db_path if paths is None else None
        restart_notice = take_restart_notice(os.environ, resolved_paths.data_dir)
        if (
            restart_notice is not None
            and restart_notice.log_dir.resolve() != resolved_paths.log_dir
        ):
            raise RuntimeError("復元後の再起動情報に含まれるログ保存先が不正です。")
        resolved_paths.data_dir.mkdir(parents=True, exist_ok=True)

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

                    ensure_dirs(resolved_paths)
                    (logging_configurator or configure_logging)(log_dir=resolved_paths.log_dir)
                    logging_initialized = True
                    recovery_databases = {
                        "common": RegisteredDatabase(
                            resolved_paths.common_db_path, _validate_common_database
                        ),
                        METER_READING_APPLICATION_ID: RegisteredDatabase(
                            resolved_paths.db_path, _validate_meter_reading_database
                        ),
                    }
                    recover_pending(
                        resolved_paths.recovery_pending_path,
                        resolved_paths.safety_backup_root,
                        recovery_databases,
                        validate_set=lambda: _validate_recovery_database_set(recovery_databases),
                    )
                    previous_excepthook = sys.excepthook
                    install_excepthook(resolved_paths.log_path)
                    resources.callback(setattr, sys, "excepthook", previous_excepthook)

                    application = _initialize_ui(ui_resources, application_factory, resolved_paths)
                    settings = load_ui_settings(resolved_paths.ui_settings_path)
                    restore_requested = False
                    meter_window: QMainWindow | None = None
                    backup_service: BackupRestoreService | None = None
                    close_meter_window: Callable[[], None] | None = None
                    close_meter_connection: Callable[[], None] | None = None

                    def report_application_error(error: Exception) -> None:
                        if isinstance(error, DomainError):
                            logger.warning("アプリケーションを開けませんでした: %s", error)
                            if startup_error_notifier is not None:
                                startup_error_notifier(error)
                            else:
                                show_domain_error(host, error)
                        elif startup_error_notifier is not None:
                            logger.error(
                                "アプリケーションを開けませんでした",
                                exc_info=(type(error), error, error.__traceback__),
                            )
                            startup_error_notifier(error)
                        else:
                            show_unexpected_error(host, error)

                    def persist_application_choice(application_id: str) -> None:
                        registered_ids = {
                            registered_id for registered_id, _ in REGISTERED_APPLICATIONS
                        }
                        if application_id not in registered_ids:
                            raise LookupError(f"未登録のアプリです: {application_id}")
                        set_last_application(resolved_paths.ui_settings_path, application_id)

                    def open_application(application_id: str) -> QMainWindow:
                        nonlocal backup_service
                        nonlocal close_meter_connection
                        nonlocal close_meter_window
                        nonlocal meter_window
                        if application_id != METER_READING_APPLICATION_ID:
                            raise LookupError(f"未登録のアプリです: {application_id}")

                        if meter_window is not None:
                            if not isinstance(meter_window, MainWindow):
                                raise RuntimeError("検針画面の型が不正です。")
                            meter_window.reload_for_activation()
                            return meter_window

                        app_resources = ExitStack()
                        try:
                            migration_completed = False
                            if legacy_database_path is not None:
                                migration_completed = migrate_legacy_database(
                                    resolved_paths.db_path,
                                    legacy_database_path,
                                    lock_factory=SingleInstanceLock,
                                )

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

                            app_resources.callback(close_connection_once)

                            if migration_completed and legacy_database_path is not None:
                                if migration_notifier is not None:
                                    migration_notifier(
                                        legacy_database_path,
                                        resolved_paths.db_path,
                                    )
                                else:
                                    QMessageBox.information(
                                        host,
                                        "検針 DB の移行が完了しました",
                                        "旧版の検針 DB を新しい保存先へ複製しました。\n"
                                        "旧 DB と旧バックアップは変更していません。\n"
                                        "以後、旧版で入力した内容は新しい DB には反映されません。",
                                    )

                            context = create_app_context(connection)
                            if startup_preparer is not None:
                                startup_preparer(connection)
                            else:
                                context.startup_preparation.prepare()

                            new_backup_service: BackupRestoreService | None = None

                            def request_restore() -> None:
                                nonlocal restore_requested
                                if restore_requested:
                                    raise RuntimeError("復元の終了処理は既に開始されています。")
                                restore_requested = True
                                application.quit()

                            if main_window_factory is not None:
                                window = main_window_factory(context)
                            else:
                                new_backup_service = BackupRestoreService(
                                    SQLiteBackupRestoreRepository(
                                        connection,
                                        resolved_paths.db_path,
                                    ),
                                    resolved_paths.backup_dir,
                                    timezone_provider=context.timezone_settings.get_timezone,
                                )
                                app_resources.callback(new_backup_service.discard_preview)
                                window = MainWindow(
                                    context,
                                    database_path=resolved_paths.db_path,
                                    display_settings_path=resolved_paths.ui_settings_path,
                                    backup_restore_service=new_backup_service,
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

                            app_resources.callback(close_window_once)
                        except BaseException:
                            app_resources.close()
                            raise

                        resources.callback(app_resources.close)
                        close_meter_connection = close_connection_once
                        close_meter_window = close_window_once
                        backup_service = new_backup_service
                        meter_window = window
                        return window

                    host = ApplicationHostWindow(
                        REGISTERED_APPLICATIONS,
                        opener=open_application,
                        on_selected=persist_application_choice,
                        on_error=report_application_error,
                        last_application=settings.last_application,
                    )
                    host_closed = False

                    def close_host_once() -> None:
                        nonlocal host_closed
                        if not host_closed:
                            host_closed = True
                            if not host.close():
                                raise RuntimeError("アプリ選択画面を終了できませんでした。")

                    resources.callback(close_host_once)

                    if restart_notice is not None:
                        if not host.open_application(METER_READING_APPLICATION_ID):
                            return STARTUP_FAILURE
                    elif application_selector is not None:
                        selected_application = _choose_application(
                            resolved_paths,
                            application_selector,
                        )
                        if selected_application is None:
                            return STARTUP_SUCCESS
                        if not host.open_application(selected_application):
                            return STARTUP_FAILURE

                    host.show()
                    if restart_notice is not None:
                        QTimer.singleShot(0, lambda: _announce_restart_ready(restart_notice))

                    try:
                        loop_result = (event_loop_runner or (lambda app: app.exec()))(application)
                    except Exception as error:
                        logger.exception("アプリケーションの実行に失敗しました")
                        if startup_error_notifier is not None:
                            startup_error_notifier(error)
                        elif isinstance(QApplication.instance(), QApplication):
                            show_unexpected_error(host, error)
                        else:
                            _report_startup_error(
                                f"アプリケーションの起動に失敗しました: {error}",
                                gui_message=(
                                    "予期しないエラーが発生しました。詳細はログを確認してください。"
                                ),
                            )
                        return STARTUP_FAILURE
                    if not restore_requested:
                        return loop_result
                    if backup_service is None or close_meter_connection is None:
                        raise RuntimeError("復元サービスが利用できません。")
                    try:
                        if loop_result != STARTUP_SUCCESS:
                            raise RestoreError(
                                "イベントループが正常終了しなかったため復元を中止しました。",
                                stage="pre_backup",
                                recovered=True,
                            )
                        if close_meter_window is None:
                            raise RuntimeError("検針画面が利用できません。")
                        close_meter_window()
                        close_host_once()
                        restore_result = backup_service.restore_after_shutdown(
                            close_meter_connection
                        )
                    except Exception as error:
                        _show_restore_failure(error)
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
        elif isinstance(
            error, (InstallationGuardError, PathConfigurationError, PendingRecoveryError)
        ):
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
