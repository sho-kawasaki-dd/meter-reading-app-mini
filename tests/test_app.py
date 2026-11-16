from __future__ import annotations

import logging
import os
import runpy
import sqlite3
import subprocess
import sys
from collections.abc import Callable, Iterator
from contextlib import closing
from pathlib import Path

import pytest
from PySide6.QtGui import QCloseEvent
from PySide6.QtWidgets import QApplication, QMainWindow, QMessageBox

from meter_reading_mini import app as app_module
from meter_reading_mini.config import AppPaths, resolve_paths
from meter_reading_mini.core.errors import ValidationError
from meter_reading_mini.core.timeutil import local_timestamp_for_filename
from meter_reading_mini.db.connection import transaction
from meter_reading_mini.db.migrations import SCHEMA_VERSION
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


def _tracked_database(events: list[str]) -> sqlite3.Connection:
    class Connection(sqlite3.Connection):
        def close(self) -> None:
            events.append("db.close")
            super().close()

    return sqlite3.connect(":memory:", factory=Connection)


def test_run_app_initializes_in_order_and_reuses_existing_qapplication(
    tmp_path: Path, qapp: QApplication, monkeypatch: pytest.MonkeyPatch
) -> None:
    paths = AppPaths.from_dirs(tmp_path / "data", tmp_path / "logs")
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

    def make_window(context: app_module.AppContext) -> QMainWindow:
        events.append("window.create")
        assert sys.excepthook is not original_excepthook
        return TrackedWindow()

    def run_loop(application: QApplication) -> int:
        events.append("loop")
        assert application is qapp
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
        "directories",
        "lock.create",
        "lock.acquire",
        "logging",
        "hook.install",
        "db.open",
        "settings.load",
        "settings.apply",
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


def test_run_app_lock_contention_stops_before_logging_database_and_ui(
    tmp_path: Path,
    qapp: QApplication,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    paths = AppPaths.from_dirs(tmp_path / "data", tmp_path / "logs")
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
    assert events == ["directories", "lock.acquire"]
    assert capsys.readouterr().err == "アプリケーションは既に起動しています。\n"


def test_install_guard_contention_stops_before_logging_and_releases_data_lock(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    paths = AppPaths.from_dirs(tmp_path / "data", tmp_path / "logs")
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
    paths = AppPaths.from_dirs(tmp_path / "data", tmp_path / "logs")
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
    paths = AppPaths.from_dirs(tmp_path / "data", tmp_path / "logs")
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
    paths = AppPaths.from_dirs(tmp_path / "data", tmp_path / "logs")
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
    assert messages == [
        (
            "アプリケーションを起動できません",
            "予期しないエラーが発生しました。詳細はログを確認してください。",
        )
    ]
    assert "DB を開けません" in caplog.text


def test_windowed_startup_reports_domain_failure_without_stderr(
    tmp_path: Path, qapp: QApplication, monkeypatch: pytest.MonkeyPatch
) -> None:
    paths = AppPaths.from_dirs(tmp_path / "data", tmp_path / "logs")
    messages: list[tuple[str, str]] = []
    monkeypatch.setattr(sys, "stderr", None)
    monkeypatch.setattr(
        QMessageBox,
        "critical",
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
    assert messages == [
        (
            "アプリケーションを起動できません",
            "アプリケーションの起動に失敗しました: 復元候補が不正です",
        )
    ]


def test_windowed_startup_reports_failure_before_logging_without_stderr(
    tmp_path: Path, qapp: QApplication, monkeypatch: pytest.MonkeyPatch
) -> None:
    paths = AppPaths.from_dirs(tmp_path / "data", tmp_path / "logs")
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
    paths = AppPaths.from_dirs(tmp_path / "data", tmp_path / "logs")
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
    paths = AppPaths.from_dirs(tmp_path / "data", tmp_path / "logs")
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
        )

    assert result == app_module.STARTUP_FAILURE
    assert events == ["lock.acquire", "lock.release"]
    assert "DB を開けません" in caplog.text
    assert "DB を開けません" in capsys.readouterr().err


def test_run_app_closes_database_before_releasing_lock_on_ui_failure(
    tmp_path: Path,
    qapp: QApplication,
    caplog: pytest.LogCaptureFixture,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    paths = AppPaths.from_dirs(tmp_path / "data", tmp_path / "logs")
    events: list[str] = []
    connection = _tracked_database(events)
    monkeypatch.setattr(app_module, "ensure_dirs", lambda actual_paths: None)
    monkeypatch.setattr(app_module, "load_display_settings", lambda path: DisplaySettings())

    def fail_to_apply(settings: DisplaySettings) -> None:
        raise RuntimeError("UI failed")

    monkeypatch.setattr(app_module, "apply_display_settings", fail_to_apply)

    with caplog.at_level(logging.ERROR):
        result = app_module.run_app(
            paths,
            database_opener=lambda *args, **kwargs: connection,
            lock_factory=lambda path: FakeLock(events),
            logging_configurator=lambda **kwargs: None,
            startup_error_notifier=lambda error: events.append("startup.error"),
        )

    assert result == app_module.STARTUP_FAILURE
    assert events == ["lock.acquire", "startup.error", "db.close", "lock.release"]
    assert "アプリケーションの起動または実行に失敗しました" in caplog.text
    with pytest.raises(sqlite3.ProgrammingError):
        connection.execute("SELECT 1")


def test_run_app_startup_failure_notifies_after_rollback(
    tmp_path: Path,
    qapp: QApplication,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    paths = AppPaths.from_dirs(tmp_path / "data", tmp_path / "logs")
    events: list[str] = []
    connection = _tracked_database(events)
    connection.execute("CREATE TABLE startup_probe (value TEXT NOT NULL)")
    monkeypatch.setattr(app_module, "ensure_dirs", lambda actual_paths: None)
    monkeypatch.setattr(app_module, "load_display_settings", lambda path: DisplaySettings())
    monkeypatch.setattr(app_module, "apply_display_settings", lambda settings: None)

    def fail_startup(conn: sqlite3.Connection) -> None:
        with transaction(conn):
            conn.execute("INSERT INTO startup_probe VALUES ('partial')")
            raise RuntimeError("startup failed")

    rollback_counts: list[int] = []

    def notify_startup_error(error: BaseException) -> None:
        rollback_counts.append(
            connection.execute("SELECT COUNT(*) FROM startup_probe").fetchone()[0]
        )
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
    assert events == ["lock.acquire", "startup.error", "db.close", "lock.release"]
    assert rollback_counts == [0]


def test_run_app_migrates_database_and_writes_log_to_app_paths(
    tmp_path: Path,
    qapp: QApplication,
    restore_root_handlers: None,
) -> None:
    data_dir = tmp_path / "configured-data"
    paths = resolve_paths({"METER_READING_MINI_DATA_DIR": str(data_dir)})

    result = app_module.run_app(paths, event_loop_runner=lambda application: 0)

    assert result == app_module.STARTUP_SUCCESS
    assert paths.db_path.is_file()
    assert paths.log_path == data_dir / "logs" / "app.log"
    assert paths.log_path.is_file()
    with closing(sqlite3.connect(paths.db_path)) as connection:
        assert connection.execute("PRAGMA user_version").fetchone() == (SCHEMA_VERSION,)


def test_run_app_releases_resources_when_event_loop_fails(
    tmp_path: Path, qapp: QApplication, monkeypatch: pytest.MonkeyPatch
) -> None:
    paths = AppPaths.from_dirs(tmp_path / "data", tmp_path / "logs")
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
    paths = AppPaths.from_dirs(tmp_path / "data", tmp_path / "logs")
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
        "予定枠の初期更新に失敗しました",
        "db.close",
        "lock.release",
    ]


def test_module_entry_point_exits_with_run_app_result(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(app_module, "run_app", lambda: 23)

    with pytest.raises(SystemExit) as exc:
        runpy.run_module("meter_reading_mini", run_name="__main__")

    assert exc.value.code == 23


def test_real_event_loop_and_environment_data_directory(tmp_path: Path) -> None:
    data_dir = tmp_path / "temporary-data"
    environment = os.environ.copy()
    environment["METER_READING_MINI_DATA_DIR"] = str(data_dir)
    script = """
from pathlib import Path
import os
from PySide6.QtCore import QTimer
from PySide6.QtWidgets import QMainWindow
from meter_reading_mini.app import run_app

def run_loop(application):
    data_dir = Path(os.environ["METER_READING_MINI_DATA_DIR"])
    assert (data_dir / "meter_reading.db").is_file()
    assert (data_dir / "meter_reading.lock").is_file()
    assert any(isinstance(widget, QMainWindow) and widget.isVisible()
               for widget in application.topLevelWidgets())
    QTimer.singleShot(0, application.quit)
    return application.exec()

raise SystemExit(run_app(event_loop_runner=run_loop))
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
    assert not (data_dir / "meter_reading.lock").exists()
    with closing(sqlite3.connect(data_dir / "meter_reading.db")) as connection:
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
    environment["METER_READING_MINI_DATA_DIR"] = str(tmp_path / "temporary-data")
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
