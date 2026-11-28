from __future__ import annotations

import logging
import os
import runpy
import sqlite3
import subprocess
import sys
from collections.abc import Callable, Iterator, Mapping
from contextlib import closing
from functools import partial
from pathlib import Path

import pytest
from PySide6.QtGui import QCloseEvent
from PySide6.QtWidgets import QApplication, QMainWindow, QMessageBox

from building_manager_mini.db.recovery import prepare_operation
from building_manager_mini.paths import (
    DATA_DIR_ENV,
    LEGACY_DATA_DIR_ENV,
    AppPaths,
    StartupPaths,
    resolve_paths,
)
from building_manager_mini.settings import load_ui_settings
from building_manager_mini.ui.application_host import ApplicationHostWindow
from meter_reading_mini import app as app_module
from meter_reading_mini.core.errors import ValidationError
from meter_reading_mini.core.timeutil import local_timestamp_for_filename
from meter_reading_mini.db.backup import copy_database
from meter_reading_mini.db.connection import connect_readonly, transaction
from meter_reading_mini.db.migrations import SCHEMA_VERSION, open_database
from meter_reading_mini.ui.display_settings import DisplaySettings


class FakeLock:
    def __init__(self, events: list[str], acquired: bool = True) -> None:
        self.events = events
        self.acquired = acquired

    def try_acquire(self, timeout_ms: int = 100) -> bool:
        self.events.append("lock.acquire")
        return self.acquired

    def release(self) -> None:
        self.events.append("lock.release")


@pytest.fixture
def restore_root_handlers() -> Iterator[None]:
    root_logger = logging.getLogger()
    original_handlers = root_logger.handlers[:]
    for handler in original_handlers:
        root_logger.removeHandler(handler)

    try:
        yield
    finally:
        for handler in root_logger.handlers[:]:
            root_logger.removeHandler(handler)
            if handler not in original_handlers:
                handler.close()
        for handler in original_handlers:
            if handler not in root_logger.handlers:
                root_logger.addHandler(handler)


@pytest.fixture(autouse=True)
def choose_meter_reading_by_default(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        app_module,
        "run_app",
        partial(
            app_module.run_app,
            application_selector=lambda applications, *, last_application=None: "meter-reading",
        ),
    )


def _tracked_database(events: list[str]) -> sqlite3.Connection:
    class Connection(sqlite3.Connection):
        def close(self) -> None:
            events.append("db.close")
            super().close()

    return sqlite3.connect(":memory:", factory=Connection)


def test_pending_recovery_blocks_before_ui_and_database_open(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    paths = AppPaths.from_root(tmp_path / "data")
    paths.recovery_pending_path.parent.mkdir(parents=True)
    paths.recovery_pending_path.write_text("未完了", encoding="utf-8")
    events: list[str] = []
    messages: list[str] = []
    lock = FakeLock(events)

    def fail_ui() -> QApplication:
        raise AssertionError("中断記録がある場合は UI を作成しません")

    def fail_database(
        path: Path, backup_dir: Path, *, backup_timestamp: Callable[[], str]
    ) -> sqlite3.Connection:
        raise AssertionError("中断記録がある場合は DB を開きません")

    monkeypatch.setattr(
        app_module,
        "_report_startup_error",
        lambda message, *, gui_message=None: messages.append(str(gui_message)),
    )
    result = app_module.run_app(
        paths,
        lock_factory=lambda _path: lock,
        install_guard_factory=lambda: None,
        logging_configurator=lambda *, log_dir: events.append("logging"),
        application_factory=fail_ui,
        database_opener=fail_database,
    )

    assert result == app_module.STARTUP_FAILURE
    assert events == ["lock.acquire", "logging", "lock.release"]
    assert str(paths.recovery_pending_path) in messages[0]
    assert str(paths.safety_backup_root) in messages[0]
    assert paths.recovery_pending_path.read_text(encoding="utf-8") == "未完了"
    assert not paths.db_path.exists()


def test_pending_recovery_restores_database_before_startup(
    tmp_path: Path, qapp: QApplication
) -> None:
    paths = AppPaths.from_root(tmp_path / "data")
    paths.db_path.parent.mkdir(parents=True)
    paths.backup_dir.mkdir(parents=True)
    connection = open_database(
        paths.db_path,
        paths.backup_dir,
        backup_timestamp=local_timestamp_for_filename,
    )
    connection.execute("INSERT INTO staff (name) VALUES ('保全時点')")
    connection.close()

    def create_backup(snapshots: Mapping[str, Path]) -> Path:
        backup_path = paths.backup_root / "automatic.db"
        with closing(sqlite3.connect(snapshots["meter-reading"], autocommit=True)) as snapshot:
            copy_database(snapshot, backup_path)
        app_module._validate_meter_reading_database(backup_path)
        return backup_path

    prepare_operation(
        paths.recovery_pending_path,
        paths.safety_backup_root,
        {
            "common": app_module.RegisteredDatabase(
                paths.common_db_path, app_module._validate_common_database
            ),
            "meter-reading": app_module.RegisteredDatabase(
                paths.db_path, app_module._validate_meter_reading_database
            ),
        },
        ["meter-reading"],
        operation_id="a" * 32,
        operation_kind="initial_migration",
        copy_database=copy_database,
        create_and_verify_backup=create_backup,
    )
    with closing(sqlite3.connect(paths.db_path, autocommit=True)) as changed:
        changed.execute("UPDATE staff SET name = '変更後'")

    result = app_module.run_app(
        paths,
        application_selector=lambda applications, *, last_application=None: None,
        lock_factory=lambda _path: FakeLock([]),
        install_guard_factory=lambda: None,
        logging_configurator=lambda *, log_dir: None,
        event_loop_runner=lambda _application: 0,
    )

    assert result == app_module.STARTUP_SUCCESS
    with closing(connect_readonly(paths.db_path)) as restored:
        assert restored.execute("SELECT name FROM staff").fetchone() == ("保全時点",)
    assert not paths.recovery_pending_path.exists()
    assert (paths.backup_root / "automatic.db").exists()


def test_run_app_initializes_in_order_and_reuses_existing_qapplication(
    tmp_path: Path, qapp: QApplication, monkeypatch: pytest.MonkeyPatch
) -> None:
    paths = AppPaths.from_root(tmp_path / "data")
    events: list[str] = []
    connection = _tracked_database(events)
    factory_calls: list[bool] = []
    original_excepthook = sys.excepthook
    install_excepthook = app_module.install_excepthook

    class TrackedWindow(QMainWindow):
        def show(self) -> None:
            events.append("window.show")
            super().show()

        def closeEvent(self, event: QCloseEvent) -> None:
            events.append("window.close")
            super().closeEvent(event)

    def install_hook(path: Path) -> None:
        events.append("hook.install")
        assert path == paths.log_path
        install_excepthook(path)

    def recover_pending(
        pending_path: Path,
        safety_root: Path,
        databases: dict[str, app_module.RegisteredDatabase],
        *,
        validate_set: Callable[[], None],
    ) -> bool:
        events.append("recovery")
        assert pending_path == paths.recovery_pending_path
        assert safety_root == paths.safety_backup_root
        assert set(databases) == {"common", "meter-reading"}
        validate_set()
        return False

    def make_window(context: app_module.AppContext) -> QMainWindow:
        events.append("window.create")
        assert sys.excepthook is not original_excepthook
        return TrackedWindow()

    def run_loop(application: QApplication) -> int:
        events.append("loop")
        assert application is qapp
        assert not application.windowIcon().isNull()
        assert connection.execute("SELECT 1").fetchone() == (1,)
        assert "lock.release" not in events
        assert "db.close" not in events
        return 0

    def ensure_dirs(actual_paths: AppPaths) -> None:
        events.append("directories")
        assert actual_paths is paths

    def open_database(
        path: Path, backup_dir: Path, *, backup_timestamp: Callable[[], str]
    ) -> sqlite3.Connection:
        events.append("db.open")
        assert path == paths.db_path
        assert backup_dir == paths.backup_dir
        assert backup_timestamp is local_timestamp_for_filename
        return connection

    monkeypatch.setattr(app_module, "ensure_dirs", ensure_dirs)
    monkeypatch.setattr(app_module, "recover_pending", recover_pending)
    monkeypatch.setattr(app_module, "install_excepthook", install_hook)
    monkeypatch.setattr(
        app_module,
        "load_display_settings",
        lambda path: events.append("settings.load") or DisplaySettings(),
    )
    monkeypatch.setattr(
        app_module, "apply_display_settings", lambda settings: events.append("settings.apply")
    )

    result = app_module.run_app(
        paths,
        application_factory=lambda: factory_calls.append(True) or qapp,
        database_opener=open_database,
        lock_factory=lambda path: events.append("lock.create") or FakeLock(events),
        logging_configurator=lambda *, log_dir: events.append("logging"),
        startup_preparer=lambda conn: events.append("startup.prepare"),
        main_window_factory=make_window,
        event_loop_runner=run_loop,
    )

    assert result == app_module.STARTUP_SUCCESS
    assert sys.excepthook is original_excepthook
    assert factory_calls == []
    assert QApplication.instance() is qapp
    assert events == [
        "lock.create",
        "lock.acquire",
        "directories",
        "logging",
        "recovery",
        "hook.install",
        "settings.load",
        "settings.apply",
        "db.open",
        "startup.prepare",
        "window.create",
        "window.show",
        "loop",
        "window.close",
        "db.close",
        "lock.release",
    ]
    with pytest.raises(sqlite3.ProgrammingError):
        connection.execute("SELECT 1")


def test_selector_defers_database_and_reuses_prepared_application(
    tmp_path: Path, qapp: QApplication
) -> None:
    paths = AppPaths.from_root(tmp_path / "data")
    events: list[str] = []

    def run_loop(application: QApplication) -> int:
        host = next(
            widget
            for widget in application.topLevelWidgets()
            if isinstance(widget, ApplicationHostWindow) and widget.isVisible()
        )
        assert host.selection.isVisible()
        assert host.current_application is None
        assert not paths.db_path.exists()
        assert events == ["lock.acquire", "logging"]

        assert host.open_application(app_module.METER_READING_APPLICATION_ID)
        first_page = host.current_page
        assert first_page is not None
        assert paths.db_path.is_file()
        assert events == ["lock.acquire", "logging", "database.open", "startup.prepare"]

        host.return_to_selection()
        assert host.selection.isVisible()
        assert host.open_application(app_module.METER_READING_APPLICATION_ID)
        assert host.current_page is first_page
        assert events == ["lock.acquire", "logging", "database.open", "startup.prepare"]
        return app_module.STARTUP_SUCCESS

    def open_db(
        path: Path, backup_dir: Path, *, backup_timestamp: Callable[[], str]
    ) -> sqlite3.Connection:
        events.append("database.open")
        return open_database(path, backup_dir, backup_timestamp=backup_timestamp)

    result = app_module.run_app(
        paths,
        database_opener=open_db,
        lock_factory=lambda path: FakeLock(events),
        logging_configurator=lambda **kwargs: events.append("logging"),
        startup_preparer=lambda conn: (
            events.append("startup.prepare")
            or app_module.create_app_context(conn).startup_preparation.prepare()
        ),
        startup_error_notifier=lambda error: pytest.fail(str(error)),
        application_selector=None,
        event_loop_runner=run_loop,
    )

    assert result == app_module.STARTUP_SUCCESS
    assert load_ui_settings(paths.ui_settings_path).last_application == (
        app_module.METER_READING_APPLICATION_ID
    )


def test_failed_application_preparation_releases_only_its_resources_and_can_retry(
    tmp_path: Path, qapp: QApplication
) -> None:
    paths = AppPaths.from_root(tmp_path / "data")
    connections: list[sqlite3.Connection] = []
    connection_closed: list[bool] = []
    windows: list[QMainWindow] = []
    failures: list[str] = []
    preparation_calls = 0

    class TrackedConnection(sqlite3.Connection):
        def close(self) -> None:
            for index, connection in enumerate(connections):
                if connection is self:
                    connection_closed[index] = True
                    break
            super().close()

    def open_db(
        path: Path, backup_dir: Path, *, backup_timestamp: Callable[[], str]
    ) -> sqlite3.Connection:
        connection = sqlite3.connect(":memory:", factory=TrackedConnection)
        connections.append(connection)
        connection_closed.append(False)
        return connection

    def prepare(conn: sqlite3.Connection) -> None:
        nonlocal preparation_calls
        preparation_calls += 1
        if preparation_calls == 1:
            raise RuntimeError("起動時準備に失敗")

    def run_loop(application: QApplication) -> int:
        host = next(
            widget
            for widget in application.topLevelWidgets()
            if isinstance(widget, ApplicationHostWindow) and widget.isVisible()
        )
        assert not host.open_application(app_module.METER_READING_APPLICATION_ID)
        assert host.current_application is None
        assert connection_closed[0]
        assert failures == ["起動時準備に失敗"]

        assert host.open_application(app_module.METER_READING_APPLICATION_ID)
        assert host.current_application == app_module.METER_READING_APPLICATION_ID
        assert len(windows) == 1
        assert (
            len(
                [
                    widget
                    for widget in application.topLevelWidgets()
                    if isinstance(widget, QMainWindow) and widget.isVisible()
                ]
            )
            == 1
        )
        return app_module.STARTUP_SUCCESS

    result = app_module.run_app(
        paths,
        database_opener=open_db,
        lock_factory=lambda path: FakeLock([]),
        logging_configurator=lambda **kwargs: None,
        startup_preparer=prepare,
        startup_error_notifier=lambda error: failures.append(str(error)),
        main_window_factory=lambda context: windows.append(QMainWindow()) or windows[-1],
        application_selector=None,
        event_loop_runner=run_loop,
    )

    assert result == app_module.STARTUP_SUCCESS
    assert preparation_calls == 2
    assert connection_closed[1]


def test_run_app_lock_contention_stops_before_logging_database_and_ui(
    tmp_path: Path,
    qapp: QApplication,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    paths = AppPaths.from_root(tmp_path / "data")
    events: list[str] = []

    monkeypatch.setattr(
        app_module, "ensure_dirs", lambda actual_paths: events.append("directories")
    )
    result = app_module.run_app(
        paths,
        application_factory=lambda: events.append("application") or qapp,
        database_opener=lambda path, backup_dir, *, backup_timestamp: (
            events.append("database") or sqlite3.connect(":memory:")
        ),
        lock_factory=lambda path: FakeLock(events, acquired=False),
        logging_configurator=lambda **kwargs: events.append("logging"),
    )

    assert result == app_module.STARTUP_LOCKED
    assert events == ["lock.acquire"]
    assert capsys.readouterr().err == "アプリケーションは既に起動しています。\n"


def test_install_guard_contention_stops_before_logging_and_releases_data_lock(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    paths = AppPaths.from_root(tmp_path / "data")
    events: list[str] = []

    class BusyGuard:
        def try_acquire(self) -> bool:
            events.append("guard.acquire")
            return False

        def release(self) -> None:
            pytest.fail("取得できなかったガードを解放しました")

    result = app_module.run_app(
        paths,
        lock_factory=lambda path: FakeLock(events),
        install_guard_factory=BusyGuard,
        logging_configurator=lambda **kwargs: events.append("logging"),
    )

    assert result == app_module.STARTUP_LOCKED
    assert events == ["lock.acquire", "guard.acquire", "lock.release"]
    assert "更新または削除が進行中" in capsys.readouterr().err
    assert not paths.db_path.exists()


def test_install_guard_failure_notifies_and_releases_data_lock(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    paths = AppPaths.from_root(tmp_path / "data")
    events: list[str] = []

    class BrokenGuard:
        def try_acquire(self) -> bool:
            raise app_module.InstallationGuardError("ガードの保存先を確認してください。")

        def release(self) -> None:
            pytest.fail("取得できなかったガードを解放しました")

    result = app_module.run_app(
        paths,
        lock_factory=lambda path: FakeLock(events),
        install_guard_factory=BrokenGuard,
    )

    assert result == app_module.STARTUP_FAILURE
    assert events == ["lock.acquire", "lock.release"]
    assert "ガードの保存先を確認" in capsys.readouterr().err
    assert not paths.db_path.exists()


def test_windowed_startup_reports_lock_contention_without_stderr(
    tmp_path: Path, qapp: QApplication, monkeypatch: pytest.MonkeyPatch
) -> None:
    paths = AppPaths.from_root(tmp_path / "data")
    messages: list[tuple[str, str]] = []
    monkeypatch.setattr(sys, "stderr", None)
    monkeypatch.setattr(
        QMessageBox,
        "critical",
        lambda parent, title, text: messages.append((title, text)) or QMessageBox.StandardButton.Ok,
    )

    result = app_module.run_app(paths, lock_factory=lambda path: FakeLock([], acquired=False))

    assert result == app_module.STARTUP_LOCKED
    assert messages == [
        ("アプリケーションを起動できません", "アプリケーションは既に起動しています。")
    ]
    assert not paths.db_path.exists()


def test_windowed_startup_creates_an_application_for_early_errors(
    qapp: QApplication, monkeypatch: pytest.MonkeyPatch
) -> None:
    messages: list[tuple[str, str]] = []
    monkeypatch.setattr(sys, "stderr", None)
    monkeypatch.setattr(QApplication, "instance", lambda: None)
    monkeypatch.setattr(app_module, "_create_qapplication", lambda: qapp)
    monkeypatch.setattr(
        QMessageBox,
        "critical",
        lambda parent, title, text: messages.append((title, text)) or QMessageBox.StandardButton.Ok,
    )

    app_module._report_startup_error("起動に失敗しました。")

    assert messages == [("アプリケーションを起動できません", "起動に失敗しました。")]


def test_windowed_startup_reports_database_failure_without_stderr(
    tmp_path: Path,
    qapp: QApplication,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    paths = AppPaths.from_root(tmp_path / "data")
    messages: list[tuple[str, str]] = []
    monkeypatch.setattr(sys, "stderr", None)
    monkeypatch.setattr(
        QMessageBox,
        "critical",
        lambda parent, title, text: messages.append((title, text)) or QMessageBox.StandardButton.Ok,
    )

    def fail_open(*args: object, **kwargs: object) -> sqlite3.Connection:
        raise OSError("DB を開けません")

    result = app_module.run_app(
        paths,
        lock_factory=lambda path: FakeLock([]),
        logging_configurator=lambda **kwargs: None,
        database_opener=fail_open,
    )

    assert result == app_module.STARTUP_FAILURE
    assert len(messages) == 1
    assert messages[0][0] == "エラー"
    assert messages[0][1].startswith(
        "予期しないエラーが発生しました。詳細はログを確認してください。"
    )
    assert "DB を開けません" in caplog.text


def test_windowed_startup_reports_domain_failure_without_stderr(
    tmp_path: Path, qapp: QApplication, monkeypatch: pytest.MonkeyPatch
) -> None:
    paths = AppPaths.from_root(tmp_path / "data")
    messages: list[tuple[str, str]] = []
    monkeypatch.setattr(sys, "stderr", None)
    monkeypatch.setattr(
        QMessageBox,
        "critical",
        lambda parent, title, text: messages.append((title, text)) or QMessageBox.StandardButton.Ok,
    )
    monkeypatch.setattr(
        QMessageBox,
        "warning",
        lambda parent, title, text: messages.append((title, text)) or QMessageBox.StandardButton.Ok,
    )

    def fail_open(*args: object, **kwargs: object) -> sqlite3.Connection:
        raise ValidationError("復元候補が不正です")

    result = app_module.run_app(
        paths,
        lock_factory=lambda path: FakeLock([]),
        logging_configurator=lambda **kwargs: None,
        database_opener=fail_open,
    )

    assert result == app_module.STARTUP_FAILURE
    assert messages == [("入力内容を確認してください", "復元候補が不正です")]


def test_windowed_startup_reports_failure_before_logging_without_stderr(
    tmp_path: Path, qapp: QApplication, monkeypatch: pytest.MonkeyPatch
) -> None:
    paths = AppPaths.from_root(tmp_path / "data")
    messages: list[tuple[str, str]] = []
    monkeypatch.setattr(sys, "stderr", None)
    monkeypatch.setattr(
        QMessageBox,
        "critical",
        lambda parent, title, text: messages.append((title, text)) or QMessageBox.StandardButton.Ok,
    )

    def fail_logging(*, log_dir: Path) -> None:
        raise OSError("ログを作成できません")

    result = app_module.run_app(
        paths,
        lock_factory=lambda path: FakeLock([]),
        logging_configurator=fail_logging,
    )

    assert result == app_module.STARTUP_FAILURE
    assert messages == [
        (
            "アプリケーションを起動できません",
            "アプリケーションの起動に失敗しました。保存先と実行環境を確認してください。",
        )
    ]


@pytest.mark.parametrize(
    ("failure", "message"),
    [
        (
            ValidationError("表示設定が不正です"),
            "アプリケーションの起動に失敗しました: 表示設定が不正です",
        ),
        (
            RuntimeError("内部エラー"),
            "予期しないエラーが発生しました。詳細はログを確認してください。",
        ),
    ],
)
def test_windowed_startup_reports_failure_before_ui_without_stderr(
    tmp_path: Path,
    qapp: QApplication,
    monkeypatch: pytest.MonkeyPatch,
    failure: Exception,
    message: str,
) -> None:
    paths = AppPaths.from_root(tmp_path / "data")
    messages: list[tuple[str, str]] = []
    monkeypatch.setattr(sys, "stderr", None)
    monkeypatch.setattr(QApplication, "instance", lambda: None)
    monkeypatch.setattr(app_module, "_create_qapplication", lambda: qapp)
    monkeypatch.setattr(
        QMessageBox,
        "critical",
        lambda parent, title, text: messages.append((title, text)) or QMessageBox.StandardButton.Ok,
    )

    def fail_ui(*args: object, **kwargs: object) -> QApplication:
        raise failure

    monkeypatch.setattr(app_module, "_initialize_ui", fail_ui)
    result = app_module.run_app(
        paths,
        lock_factory=lambda path: FakeLock([]),
        logging_configurator=lambda **kwargs: None,
        database_opener=lambda *args, **kwargs: sqlite3.connect(":memory:"),
    )

    assert result == app_module.STARTUP_FAILURE
    assert messages == [("アプリケーションを起動できません", message)]


def test_run_app_reports_database_open_failure_and_releases_lock(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], caplog: pytest.LogCaptureFixture
) -> None:
    paths = AppPaths.from_root(tmp_path / "data")
    events: list[str] = []

    def fail_open(
        path: Path, backup_dir: Path, *, backup_timestamp: Callable[[], str]
    ) -> sqlite3.Connection:
        raise OSError("DB を開けません")

    with caplog.at_level(logging.ERROR):
        result = app_module.run_app(
            paths,
            database_opener=fail_open,
            lock_factory=lambda path: FakeLock(events),
            logging_configurator=lambda **kwargs: None,
            startup_error_notifier=lambda error: events.append(str(error)),
        )

    assert result == app_module.STARTUP_FAILURE
    assert events == ["lock.acquire", "DB を開けません", "lock.release"]
    assert "DB を開けません" in caplog.text
    assert capsys.readouterr().err == ""


def test_run_app_closes_database_before_releasing_lock_on_ui_failure(
    tmp_path: Path,
    qapp: QApplication,
    caplog: pytest.LogCaptureFixture,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    paths = AppPaths.from_root(tmp_path / "data")
    events: list[str] = []
    connection = _tracked_database(events)
    monkeypatch.setattr(app_module, "ensure_dirs", lambda actual_paths: None)
    monkeypatch.setattr(app_module, "load_display_settings", lambda path: DisplaySettings())
    monkeypatch.setattr(app_module, "apply_display_settings", lambda settings: None)

    def fail_window(context: app_module.AppContext) -> QMainWindow:
        raise RuntimeError("UI failed")

    with caplog.at_level(logging.ERROR):
        result = app_module.run_app(
            paths,
            database_opener=lambda *args, **kwargs: connection,
            lock_factory=lambda path: FakeLock(events),
            logging_configurator=lambda **kwargs: None,
            startup_preparer=lambda conn: None,
            startup_error_notifier=lambda error: events.append("startup.error"),
            main_window_factory=fail_window,
        )

    assert result == app_module.STARTUP_FAILURE
    assert events == ["lock.acquire", "db.close", "startup.error", "lock.release"]
    assert "アプリケーションを開けませんでした" in caplog.text
    with pytest.raises(sqlite3.ProgrammingError):
        connection.execute("SELECT 1")


def test_run_app_startup_failure_notifies_after_rollback(
    tmp_path: Path,
    qapp: QApplication,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    paths = AppPaths.from_root(tmp_path / "data")
    events: list[str] = []
    connection = _tracked_database(events)
    connection.execute("CREATE TABLE startup_probe (value TEXT NOT NULL)")
    monkeypatch.setattr(app_module, "ensure_dirs", lambda actual_paths: None)
    monkeypatch.setattr(app_module, "load_display_settings", lambda path: DisplaySettings())
    monkeypatch.setattr(app_module, "apply_display_settings", lambda settings: None)

    def fail_startup(conn: sqlite3.Connection) -> None:
        try:
            with transaction(conn):
                conn.execute("INSERT INTO startup_probe VALUES ('partial')")
                raise RuntimeError("startup failed")
        except RuntimeError:
            rollback_counts.append(conn.execute("SELECT COUNT(*) FROM startup_probe").fetchone()[0])
            raise

    rollback_counts: list[int] = []

    def notify_startup_error(error: BaseException) -> None:
        events.append("startup.error")

    result = app_module.run_app(
        paths,
        database_opener=lambda *args, **kwargs: connection,
        lock_factory=lambda path: FakeLock(events),
        logging_configurator=lambda **kwargs: None,
        startup_preparer=fail_startup,
        startup_error_notifier=notify_startup_error,
        main_window_factory=lambda context: pytest.fail("起動失敗時に画面を作成しました"),
        event_loop_runner=lambda application: pytest.fail("起動失敗時にループを実行しました"),
    )

    assert result == app_module.STARTUP_FAILURE
    assert events == ["lock.acquire", "db.close", "startup.error", "lock.release"]
    assert rollback_counts == [0]


def test_run_app_migrates_database_and_writes_log_to_app_paths(
    tmp_path: Path,
    qapp: QApplication,
    restore_root_handlers: None,
) -> None:
    data_dir = tmp_path / "configured-data"
    paths = resolve_paths({DATA_DIR_ENV: str(data_dir)})

    result = app_module.run_app(paths, event_loop_runner=lambda application: 0)

    assert result == app_module.STARTUP_SUCCESS
    assert paths.db_path.is_file()
    assert paths.log_path == data_dir / "logs" / "app.log"
    assert paths.log_path.is_file()
    with closing(sqlite3.connect(paths.db_path)) as connection:
        assert connection.execute("PRAGMA user_version").fetchone() == (SCHEMA_VERSION,)


def test_legacy_environment_alone_stops_before_creating_paths_or_lock(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    legacy_data = tmp_path / "old-data"
    monkeypatch.setenv(LEGACY_DATA_DIR_ENV, str(legacy_data))
    monkeypatch.delenv(DATA_DIR_ENV, raising=False)
    calls: list[str] = []

    result = app_module.run_app(
        lock_factory=lambda path: calls.append("lock") or FakeLock([]),
        database_opener=lambda *args, **kwargs: (
            calls.append("database") or sqlite3.connect(":memory:")
        ),
    )

    assert result == app_module.STARTUP_FAILURE
    assert calls == []
    assert not legacy_data.exists()
    assert DATA_DIR_ENV in capsys.readouterr().err


def test_legacy_environment_error_is_explained_without_stderr(
    tmp_path: Path, qapp: QApplication, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv(LEGACY_DATA_DIR_ENV, str(tmp_path / "old-data"))
    monkeypatch.delenv(DATA_DIR_ENV, raising=False)
    monkeypatch.setattr(sys, "stderr", None)
    messages: list[tuple[str, str]] = []
    monkeypatch.setattr(
        QMessageBox,
        "critical",
        lambda parent, title, text: messages.append((title, text)) or QMessageBox.StandardButton.Ok,
    )

    result = app_module.run_app()

    assert result == app_module.STARTUP_FAILURE
    assert messages[0][1].startswith(f"{LEGACY_DATA_DIR_ENV} は廃止されました")


def test_run_app_migrates_legacy_database_after_lock_and_notifies_user(
    tmp_path: Path, qapp: QApplication, monkeypatch: pytest.MonkeyPatch
) -> None:
    old_database_path = tmp_path / "old" / "meter_reading.db"
    old_database_path.parent.mkdir()
    with closing(
        open_database(
            old_database_path,
            old_database_path.parent / "backups",
            backup_timestamp=lambda: "test",
        )
    ) as old_connection:
        old_connection.execute("INSERT INTO staff (name) VALUES (?)", ("旧版担当者",))
    old_ui_settings = old_database_path.parent / "ui_settings.json"
    old_ui_settings.write_bytes(b"old display settings")

    paths = AppPaths.from_root(tmp_path / "new")
    monkeypatch.setattr(
        app_module,
        "resolve_startup_paths",
        lambda env: StartupPaths(paths, old_database_path),
    )
    monkeypatch.setattr(app_module, "load_display_settings", lambda path: DisplaySettings())
    events: list[str] = []
    notified: list[tuple[Path, Path]] = []

    class OldDatabaseLock:
        def __init__(self, path: Path) -> None:
            assert path == old_database_path.parent / "meter_reading.lock"
            events.append("legacy_lock.create")

        def try_acquire(self, timeout_ms: int = 100) -> bool:
            events.append("legacy_lock.acquire")
            return True

        def release(self) -> None:
            events.append("legacy_lock.release")

    monkeypatch.setattr(app_module, "SingleInstanceLock", OldDatabaseLock)

    def open_db(
        path: Path, backup_dir: Path, *, backup_timestamp: Callable[[], str]
    ) -> sqlite3.Connection:
        events.append("database.open")
        return open_database(path, backup_dir, backup_timestamp=backup_timestamp)

    result = app_module.run_app(
        lock_factory=lambda path: events.append("app_lock.acquire") or FakeLock(events),
        logging_configurator=lambda *, log_dir: events.append("logging"),
        database_opener=open_db,
        application_selector=lambda applications, *, last_application=None: (
            events.append("application.select") or "meter-reading"
        ),
        startup_preparer=lambda conn: events.append("startup.prepare"),
        migration_notifier=lambda legacy_database_path, new_database_path: (
            notified.append((legacy_database_path, new_database_path))
            or events.append("migration.notice")
        ),
        main_window_factory=lambda context: events.append("window.create") or QMainWindow(),
        event_loop_runner=lambda application: events.append("event_loop") or 0,
    )

    assert result == app_module.STARTUP_SUCCESS
    assert notified == [(old_database_path, paths.db_path)]
    assert events.index("app_lock.acquire") < events.index("legacy_lock.acquire")
    assert events.index("logging") < events.index("legacy_lock.acquire")
    assert events.index("application.select") < events.index("legacy_lock.acquire")
    assert events.index("legacy_lock.release") < events.index("database.open")
    assert events.index("database.open") < events.index("migration.notice")
    assert old_ui_settings.read_bytes() == b"old display settings"
    assert load_ui_settings(paths.ui_settings_path).last_application == (
        app_module.METER_READING_APPLICATION_ID
    )
    with closing(connect_readonly(paths.db_path)) as migrated:
        assert migrated.execute("SELECT name FROM staff").fetchall() == [("旧版担当者",)]


def test_cancelling_application_selection_does_not_inspect_or_open_databases(
    tmp_path: Path,
    qapp: QApplication,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    old_database_path = tmp_path / "old" / "meter_reading.db"
    old_database_path.parent.mkdir()
    with closing(
        open_database(
            old_database_path,
            old_database_path.parent / "backups",
            backup_timestamp=lambda: "test",
        )
    ):
        pass

    paths = AppPaths.from_root(tmp_path / "new")
    monkeypatch.setattr(
        app_module,
        "resolve_startup_paths",
        lambda env: StartupPaths(paths, old_database_path),
    )
    monkeypatch.setattr(app_module, "load_display_settings", lambda path: DisplaySettings())
    monkeypatch.setattr(app_module, "apply_display_settings", lambda settings: None)
    events: list[str] = []
    result = app_module.run_app(
        lock_factory=lambda path: FakeLock(events),
        logging_configurator=lambda *, log_dir: events.append("logging"),
        application_selector=lambda applications, *, last_application=None: (
            events.append("selection.cancel") or None
        ),
        database_opener=lambda *args, **kwargs: pytest.fail("選択取消後に DB を開きました"),
    )

    assert result == app_module.STARTUP_SUCCESS
    assert events == ["lock.acquire", "logging", "selection.cancel", "lock.release"]
    assert not paths.db_path.exists()
    assert not paths.ui_settings_path.exists()


def test_legacy_migration_failure_prevents_opening_meter_reading_database(
    tmp_path: Path,
    qapp: QApplication,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    old_database_path = tmp_path / "old" / "meter_reading.db"
    old_database_path.parent.mkdir()
    old_database_path.write_bytes(b"not a database")
    paths = AppPaths.from_root(tmp_path / "new")
    monkeypatch.setattr(
        app_module,
        "resolve_startup_paths",
        lambda env: StartupPaths(paths, old_database_path),
    )
    monkeypatch.setattr(app_module, "load_display_settings", lambda path: DisplaySettings())
    monkeypatch.setattr(app_module, "apply_display_settings", lambda settings: None)
    failures: list[str] = []

    result = app_module.run_app(
        lock_factory=lambda path: FakeLock([]),
        logging_configurator=lambda *, log_dir: None,
        startup_error_notifier=lambda error: failures.append(str(error)),
        database_opener=lambda *args, **kwargs: pytest.fail(
            "移行に失敗した後で検針 DB を開きました"
        ),
    )

    assert result == app_module.STARTUP_FAILURE
    assert failures and "旧 DB" in failures[0]
    assert not paths.db_path.exists()


def test_run_app_releases_resources_when_event_loop_fails(
    tmp_path: Path, qapp: QApplication, monkeypatch: pytest.MonkeyPatch
) -> None:
    paths = AppPaths.from_root(tmp_path / "data")
    events: list[str] = []
    connection = _tracked_database(events)
    original_excepthook = sys.excepthook
    monkeypatch.setattr(app_module, "load_display_settings", lambda path: DisplaySettings())
    monkeypatch.setattr(app_module, "apply_display_settings", lambda settings: None)

    def fail_loop(application: QApplication) -> int:
        assert connection.execute("SELECT 1").fetchone() == (1,)
        assert events == ["lock.acquire"]
        raise RuntimeError("event loop failed")

    result = app_module.run_app(
        paths,
        database_opener=lambda *args, **kwargs: connection,
        lock_factory=lambda path: FakeLock(events),
        logging_configurator=lambda **kwargs: None,
        startup_preparer=lambda conn: None,
        startup_error_notifier=lambda error: events.append(str(error)),
        main_window_factory=lambda context: QMainWindow(),
        event_loop_runner=fail_loop,
    )

    assert result == app_module.STARTUP_FAILURE
    assert events == ["lock.acquire", "event loop failed", "db.close", "lock.release"]
    assert sys.excepthook is original_excepthook
    with pytest.raises(sqlite3.ProgrammingError):
        connection.execute("SELECT 1")


def test_run_app_does_not_show_window_when_initial_refresh_has_domain_error(
    tmp_path: Path, qapp: QApplication, monkeypatch: pytest.MonkeyPatch
) -> None:
    paths = AppPaths.from_root(tmp_path / "data")
    events: list[str] = []
    connection = _tracked_database(events)
    monkeypatch.setattr(app_module, "load_display_settings", lambda path: DisplaySettings())
    monkeypatch.setattr(app_module, "apply_display_settings", lambda settings: None)
    monkeypatch.setattr(
        app_module,
        "show_domain_error",
        lambda parent, error: events.append(error.message),
    )

    def fail_window(context: app_module.AppContext) -> QMainWindow:
        raise ValidationError("予定枠の初期更新に失敗しました")

    result = app_module.run_app(
        paths,
        database_opener=lambda *args, **kwargs: connection,
        lock_factory=lambda path: FakeLock(events),
        logging_configurator=lambda **kwargs: None,
        startup_preparer=lambda conn: None,
        main_window_factory=fail_window,
        event_loop_runner=lambda application: pytest.fail("エラー時にループを実行しました"),
    )

    assert result == app_module.STARTUP_FAILURE
    assert events == [
        "lock.acquire",
        "db.close",
        "予定枠の初期更新に失敗しました",
        "lock.release",
    ]


def test_legacy_module_entry_point_guides_to_new_command_without_creating_data(
    tmp_path: Path,
) -> None:
    data_dir = tmp_path / "legacy-data"
    environment = os.environ.copy()
    environment[DATA_DIR_ENV] = str(data_dir)
    environment["PYTHONIOENCODING"] = "utf-8"
    completed = subprocess.run(
        [sys.executable, "-m", "meter_reading_mini"],
        env=environment,
        capture_output=True,
        encoding="utf-8",
        timeout=30,
        check=False,
    )

    assert completed.returncode == 2
    assert "uv run python -m building_manager_mini" in completed.stderr
    assert not data_dir.exists()


def test_common_module_entry_point_exits_with_run_app_result(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(app_module, "run_app", lambda: 23)

    with pytest.raises(SystemExit) as exc:
        runpy.run_module("building_manager_mini", run_name="__main__")

    assert exc.value.code == 23


def test_real_event_loop_and_environment_data_directory(tmp_path: Path) -> None:
    data_dir = tmp_path / "temporary-data"
    environment = os.environ.copy()
    environment[DATA_DIR_ENV] = str(data_dir)
    script = """
from pathlib import Path
import os
import runpy
from PySide6.QtCore import QTimer
from PySide6.QtWidgets import QMainWindow
from meter_reading_mini import app as app_module

def run_loop(application):
    data_dir = Path(os.environ["BUILDING_MANAGER_MINI_DATA_DIR"])
    assert (data_dir / "meter_reading" / "meter_reading.db").is_file()
    assert (data_dir / "app.lock").is_file()
    assert any(isinstance(widget, QMainWindow) and widget.isVisible()
               for widget in application.topLevelWidgets())
    QTimer.singleShot(0, application.quit)
    return application.exec()

run_app = app_module.run_app
def configured_run_app():
    return run_app(
        application_selector=lambda applications, *, last_application=None: "meter-reading",
        event_loop_runner=run_loop,
    )
app_module.run_app = configured_run_app
runpy.run_module("building_manager_mini", run_name="__main__")
"""

    completed = subprocess.run(
        [sys.executable, "-c", script],
        env=environment,
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )

    assert completed.returncode == 0, completed.stderr
    assert (data_dir / "logs" / "app.log").is_file()
    assert not (data_dir / "app.lock").exists()
    with closing(sqlite3.connect(data_dir / "meter_reading" / "meter_reading.db")) as connection:
        assert connection.execute("PRAGMA user_version").fetchone() == (SCHEMA_VERSION,)


@pytest.mark.parametrize(
    ("setup", "argument", "expected"),
    [
        (
            "from PySide6.QtCore import QCoreApplication\napplication = QCoreApplication([])",
            "",
            "QApplication 以外のアプリケーション",
        ),
        ("", "application_factory=lambda: None", "QApplication ファクトリ"),
    ],
)
def test_invalid_application_instance_fails_without_a_dialog(
    tmp_path: Path, setup: str, argument: str, expected: str
) -> None:
    environment = os.environ.copy()
    environment[DATA_DIR_ENV] = str(tmp_path / "temporary-data")
    environment["PYTHONIOENCODING"] = "utf-8"
    script = f"""
{setup}
from meter_reading_mini.app import run_app
raise SystemExit(run_app({argument}))
"""
    completed = subprocess.run(
        [sys.executable, "-c", script],
        env=environment,
        capture_output=True,
        encoding="utf-8",
        timeout=30,
        check=False,
    )

    assert completed.returncode == app_module.STARTUP_FAILURE
    assert expected in completed.stderr
