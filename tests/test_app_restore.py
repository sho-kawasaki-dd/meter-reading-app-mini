from __future__ import annotations

import json
import os
import sqlite3
import subprocess
import sys
from collections.abc import Callable, Mapping
from contextlib import closing
from datetime import datetime, tzinfo
from pathlib import Path

import pytest
from PySide6.QtCore import QEventLoop, QTimer
from PySide6.QtWidgets import QApplication, QMenu, QMessageBox

from meter_reading_mini import app, restart
from meter_reading_mini.config import DATA_DIR_ENV, AppPaths
from meter_reading_mini.core.backup_restore_contracts import BackupRestoreRepository
from meter_reading_mini.core.backup_restore_service import BackupRestoreService
from meter_reading_mini.db import restore
from meter_reading_mini.db.connection import connect, connect_readonly
from meter_reading_mini.db.migrations import create_schema
from meter_reading_mini.ui.dialogs.backup_restore_dialog import BackupRestoreDialog
from meter_reading_mini.ui.display_settings import DisplaySettings, save_display_settings
from meter_reading_mini.ui.main_window import MainWindow
from meter_reading_mini.ui.single_instance import SingleInstanceLock


class RunningProcess:
    def poll(self) -> int | None:
        return None


class TrackedLock:
    def __init__(self, path: Path, events: list[str]) -> None:
        self.lock = SingleInstanceLock(path)
        self.events = events

    def try_acquire(self, timeout_ms: int = 100) -> bool:
        self.events.append("lock.acquire")
        return self.lock.try_acquire(timeout_ms)

    def release(self) -> None:
        self.lock.release()
        self.events.append("lock.release")


class TrackedInstallGuard:
    def __init__(self, events: list[str]) -> None:
        self.events = events

    def try_acquire(self) -> bool:
        self.events.append("guard.acquire")
        return True

    def release(self) -> None:
        self.events.append("guard.release")


@pytest.fixture(autouse=True)
def avoid_blocking_messages(monkeypatch: pytest.MonkeyPatch, restore_app_font: object) -> None:
    for kind in ("warning", "critical"):
        monkeypatch.setattr(
            QMessageBox, kind, lambda *args, **kwargs: QMessageBox.StandardButton.Ok
        )


def _create_database(path: Path, staff: str) -> None:
    with closing(connect(path)) as connection:
        create_schema(connection)
        connection.execute("INSERT INTO staff (name) VALUES (?)", (staff,))


def _staff(path: Path) -> list[str]:
    with closing(connect_readonly(path, immutable=True)) as connection:
        return [str(row[0]) for row in connection.execute("SELECT name FROM staff ORDER BY id")]


def _prepare_paths(tmp_path: Path) -> tuple[AppPaths, Path, bytes]:
    paths = AppPaths.from_dirs(tmp_path / "data", tmp_path / "logs")
    paths.data_dir.mkdir()
    paths.backup_dir.mkdir()
    paths.log_dir.mkdir()
    _create_database(paths.db_path, "現行担当")
    source = tmp_path / "source.db"
    _create_database(source, "復元担当")
    save_display_settings(paths.ui_settings_path, DisplaySettings(None, 125))
    return paths, source, paths.ui_settings_path.read_bytes()


def _tracked_opener(
    events: list[str], *, fail_close: bool = False
) -> Callable[..., sqlite3.Connection]:
    class TrackedConnection(sqlite3.Connection):
        def close(self) -> None:
            events.append("db.close")
            super().close()
            if fail_close:
                raise sqlite3.OperationalError("接続の終了に失敗")

    def open_tracked(
        path: Path, backup_dir: Path, *, backup_timestamp: Callable[[], str]
    ) -> sqlite3.Connection:
        connection = sqlite3.connect(path, autocommit=True, factory=TrackedConnection)
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute("PRAGMA busy_timeout = 5000")
        connection.execute("PRAGMA journal_mode = WAL")
        return connection

    return open_tracked


def _exercise_restore(
    events: list[str], monkeypatch: pytest.MonkeyPatch
) -> Callable[[QApplication], int]:
    def run_loop(application: QApplication) -> int:
        window = next(
            widget
            for widget in application.topLevelWidgets()
            if isinstance(widget, MainWindow) and widget.isVisible()
        )
        assert isinstance(window, MainWindow)
        assert window.backup_restore_action.isEnabled()
        menu = window.menuBar().actions()[0].menu()
        assert isinstance(menu, QMenu)
        assert window.backup_restore_action in menu.actions()
        failures: list[BaseException] = []

        def invoke() -> None:
            try:
                window.backup_restore_action.trigger()
                assert not window.boundary_timer.isActive()
                assert not window.isEnabled()
                events.append("ui.stopped")
            except BaseException as error:
                failures.append(error)
                application.quit()

        loop = QEventLoop()
        with monkeypatch.context() as scoped:
            scoped.setattr(application, "quit", loop.quit)
            QTimer.singleShot(0, invoke)
            result = loop.exec()
        events.append("loop.exit")
        if failures:
            raise failures[0]
        return result

    return run_loop


def _confirm_dialog(
    source: Path,
    monkeypatch: pytest.MonkeyPatch,
    *,
    change_source: bool = False,
) -> None:
    monkeypatch.setattr(
        QMessageBox, "question", lambda *args, **kwargs: QMessageBox.StandardButton.Yes
    )

    def execute(dialog: BackupRestoreDialog) -> int:
        dialog.source_edit.setText(str(source))
        dialog._inspect_source()
        assert dialog.restore_button.isEnabled()
        dialog._confirm_restore()
        if change_source:
            with source.open("ab") as output:
                output.write(b"changed")
        return dialog.result()

    monkeypatch.setattr(BackupRestoreDialog, "exec", execute)


def _assert_lock_held(path: Path) -> None:
    other = SingleInstanceLock(path)
    if other.try_acquire(0):
        other.release()
        pytest.fail("DB 置換前にロックが解放されました")


def _assert_lock_released(path: Path) -> None:
    other = SingleInstanceLock(path)
    assert other.try_acquire(0)
    other.release()


def test_app_displays_the_opened_database_path(
    tmp_path: Path, qapp: QApplication, monkeypatch: pytest.MonkeyPatch
) -> None:
    paths, _, _ = _prepare_paths(tmp_path)
    messages: list[tuple[str, str]] = []
    monkeypatch.setattr(
        QMessageBox,
        "information",
        lambda parent, title, text, *args: (
            messages.append((title, text)) or QMessageBox.StandardButton.Ok
        ),
    )

    def inspect_window(application: QApplication) -> int:
        window = next(
            widget
            for widget in application.topLevelWidgets()
            if isinstance(widget, MainWindow) and widget.isVisible()
        )
        window.database_path_action.trigger()
        return 0

    assert app.run_app(paths, event_loop_runner=inspect_window) == app.STARTUP_SUCCESS
    assert messages == [("データベースの保存先", str(paths.db_path.resolve()))]


def test_restore_shutdown_order_and_confirmed_restart(
    tmp_path: Path, qapp: QApplication, monkeypatch: pytest.MonkeyPatch
) -> None:
    paths, source, settings_before = _prepare_paths(tmp_path)
    events: list[str] = []
    _confirm_dialog(source, monkeypatch)
    real_replace = restore.os.replace

    def replace(src: str | os.PathLike[str], dst: str | os.PathLike[str]) -> None:
        if Path(dst) == paths.db_path:
            _assert_lock_held(paths.lock_path)
            assert events.count("db.close") == 1
            events.append("db.replace")
        real_replace(src, dst)

    monkeypatch.setattr(restore.os, "replace", replace)
    launched_backup: list[Path] = []

    def launch(command: list[str], *, env: Mapping[str, str]) -> RunningProcess:
        events.append("restart.launch")
        _assert_lock_released(paths.lock_path)
        assert "guard.release" not in events
        assert _staff(paths.db_path) == ["復元担当"]
        notice = restart.take_restart_notice(dict(env), paths.data_dir)
        assert notice is not None
        assert _staff(notice.backup_path) == ["現行担当"]
        launched_backup.append(notice.backup_path)
        restart.signal_restart_ready(notice)
        return RunningProcess()

    result = app.run_app(
        paths,
        database_opener=_tracked_opener(events),
        lock_factory=lambda path: TrackedLock(path, events),
        install_guard_factory=lambda: TrackedInstallGuard(events),
        logging_configurator=lambda **kwargs: None,
        event_loop_runner=_exercise_restore(events, monkeypatch),
        restart_launcher=launch,
        restart_timeout=1,
    )
    assert result == app.STARTUP_SUCCESS
    assert events == [
        "lock.acquire",
        "guard.acquire",
        "ui.stopped",
        "loop.exit",
        "db.close",
        "db.replace",
        "lock.release",
        "restart.launch",
        "guard.release",
    ]
    assert len(launched_backup) == 1
    assert paths.ui_settings_path.read_bytes() == settings_before
    assert not list(paths.data_dir.glob(".meter-reading-restore-*"))


def test_app_uses_the_configured_business_timezone_for_both_backup_names(
    tmp_path: Path,
    qapp: QApplication,
    monkeypatch: pytest.MonkeyPatch,
    fixed_clock: datetime,
) -> None:
    paths, source, _ = _prepare_paths(tmp_path)
    with closing(connect(paths.db_path)) as connection:
        connection.execute(
            "INSERT INTO meter_app_settings (setting_key, setting_value, updated_at) "
            "VALUES ('app.timezone.v1', 'America/Los_Angeles', '2026-02-04 03:00:00')"
        )
    events: list[str] = []
    _confirm_dialog(source, monkeypatch)

    def service_factory(
        repository: BackupRestoreRepository,
        backup_dir: Path,
        *,
        timezone_provider: Callable[[], tzinfo],
    ) -> BackupRestoreService:
        return BackupRestoreService(
            repository, backup_dir, timezone_provider=timezone_provider, clock=lambda: fixed_clock
        )

    monkeypatch.setattr(app, "BackupRestoreService", service_factory)

    def run_loop(application: QApplication) -> int:
        window = next(
            widget
            for widget in application.topLevelWidgets()
            if isinstance(widget, MainWindow) and widget.isVisible()
        )
        assert isinstance(window, MainWindow)
        assert window._backup_restore_service is not None
        assert (
            window._backup_restore_service.default_backup_filename()
            == "meter_reading_20260203_190000.db"
        )
        return _exercise_restore(events, monkeypatch)(application)

    def launch(command: list[str], *, env: Mapping[str, str]) -> RunningProcess:
        notice = restart.take_restart_notice(dict(env), paths.data_dir)
        assert notice is not None
        assert notice.backup_path.name == "pre-restore_20260203_190000.db"
        restart.signal_restart_ready(notice)
        return RunningProcess()

    result = app.run_app(
        paths,
        database_opener=_tracked_opener(events),
        lock_factory=lambda path: TrackedLock(path, events),
        logging_configurator=lambda **kwargs: None,
        event_loop_runner=run_loop,
        restart_launcher=launch,
        restart_timeout=1,
    )
    assert result == app.STARTUP_SUCCESS


def test_abnormal_event_loop_exit_cancels_the_confirmed_restore(
    tmp_path: Path, qapp: QApplication, monkeypatch: pytest.MonkeyPatch
) -> None:
    paths, source, _ = _prepare_paths(tmp_path)
    events: list[str] = []
    messages: list[str] = []
    _confirm_dialog(source, monkeypatch)
    monkeypatch.setattr(
        QMessageBox,
        "critical",
        lambda parent, title, text, *args: messages.append(text) or QMessageBox.StandardButton.Ok,
    )

    def fail_loop(application: QApplication) -> int:
        window = next(
            widget
            for widget in application.topLevelWidgets()
            if isinstance(widget, MainWindow) and widget.isVisible()
        )
        assert isinstance(window, MainWindow)
        window.backup_restore_action.trigger()
        assert not window.boundary_timer.isActive()
        return 9

    result = app.run_app(
        paths,
        database_opener=_tracked_opener(events),
        lock_factory=lambda path: TrackedLock(path, events),
        logging_configurator=lambda **kwargs: None,
        event_loop_runner=fail_loop,
        restart_launcher=lambda command, *, env: pytest.fail("異常終了後に再起動しました"),
    )
    assert result == app.STARTUP_FAILURE
    assert "正常終了しなかった" in messages[0]
    assert "置換は行っていません" in messages[0]
    assert _staff(paths.db_path) == ["現行担当"]
    assert not list(paths.backup_dir.glob("pre-restore_*.db"))
    assert not list(paths.data_dir.glob(".meter-reading-restore-*"))
    assert events.count("db.close") == 1


@pytest.mark.parametrize(
    "failure",
    [
        "changed-source",
        "backup",
        "checkpoint",
        "close",
        "replace",
        "cleanup",
        "cleanup-after-error",
        "restart",
    ],
)
def test_restore_and_restart_failures_preserve_data_and_notify(
    tmp_path: Path,
    qapp: QApplication,
    monkeypatch: pytest.MonkeyPatch,
    failure: str,
) -> None:
    paths, source, settings_before = _prepare_paths(tmp_path)
    events: list[str] = []
    messages: list[tuple[str, str]] = []
    monkeypatch.setattr(
        QMessageBox,
        "critical",
        lambda parent, title, text, *args: (
            messages.append((title, text)) or QMessageBox.StandardButton.Ok
        ),
    )
    _confirm_dialog(source, monkeypatch, change_source=failure == "changed-source")
    real_copy = restore.copy_database
    real_replace = restore.os.replace
    real_checkpoint = restore._checkpoint
    real_remove = restore._Workspace.remove

    def copy_database(connection: sqlite3.Connection, destination: Path) -> None:
        if failure == "backup" and Path(destination).parent == paths.backup_dir:
            raise OSError("容量不足")
        real_copy(connection, destination)

    def replace(src: str | os.PathLike[str], dst: str | os.PathLike[str]) -> None:
        if failure == "replace" and Path(dst) == paths.db_path:
            _assert_lock_held(paths.lock_path)
            raise OSError("置換失敗")
        real_replace(src, dst)

    def checkpoint(connection: sqlite3.Connection) -> None:
        database_path = Path(str(connection.execute("PRAGMA database_list").fetchone()[2]))
        if failure in {"checkpoint", "cleanup-after-error"} and database_path == paths.db_path:
            raise sqlite3.OperationalError("チェックポイントがビジーです")
        real_checkpoint(connection)

    def remove_workspace(workspace: restore._Workspace) -> None:
        if failure == "cleanup" and not (workspace.path / "candidate.db").exists():
            raise OSError("一時領域を削除できません")
        if failure == "cleanup-after-error":
            raise OSError("失敗後の一時領域を削除できません")
        real_remove(workspace)

    monkeypatch.setattr(restore, "copy_database", copy_database)
    monkeypatch.setattr(restore.os, "replace", replace)
    monkeypatch.setattr(restore, "_checkpoint", checkpoint)
    monkeypatch.setattr(restore._Workspace, "remove", remove_workspace)

    def launch(command: list[str], *, env: Mapping[str, str]) -> RunningProcess:
        assert failure == "restart"
        _assert_lock_released(paths.lock_path)
        raise OSError("再起動失敗")

    result = app.run_app(
        paths,
        database_opener=_tracked_opener(events, fail_close=failure == "close"),
        lock_factory=lambda path: TrackedLock(path, events),
        logging_configurator=lambda **kwargs: None,
        event_loop_runner=_exercise_restore(events, monkeypatch),
        restart_launcher=launch,
        restart_timeout=1,
    )

    assert result == app.STARTUP_FAILURE
    assert events.count("db.close") == 1
    assert events[-1] == "lock.release"
    assert len(messages) == 1
    assert "手動" in messages[0][1]
    backups = list(paths.backup_dir.glob("pre-restore_*.db"))
    if failure in {"restart", "cleanup"}:
        assert _staff(paths.db_path) == ["復元担当"]
        if failure == "restart":
            assert "再起動を確認できません" in messages[0][0]
        else:
            assert "置換済み" in messages[0][1]
    else:
        assert _staff(paths.db_path) == ["現行担当"]
        if failure == "cleanup-after-error":
            assert "置換状態を確認" in messages[0][1]
        else:
            assert "置換は行っていません" in messages[0][1]
    if failure in {"checkpoint", "close", "replace", "cleanup", "cleanup-after-error", "restart"}:
        assert len(backups) == 1
        assert _staff(backups[0]) == ["現行担当"]
        assert str(backups[0]) in messages[0][1]
    else:
        assert backups == []
    assert paths.ui_settings_path.read_bytes() == settings_before
    _assert_lock_released(paths.lock_path)
    if failure in {"cleanup", "cleanup-after-error"}:
        workspace = list(paths.data_dir.glob(".meter-reading-restore-*"))
        assert len(workspace) == 1
        if failure == "cleanup-after-error":
            (workspace[0] / "candidate.db").unlink()
        workspace[0].rmdir()
    assert not list(paths.data_dir.glob(".meter-reading-restore-*"))


@pytest.mark.parametrize("failure", ["replace", "restart"])
def test_shutdown_failure_is_visible_after_the_real_event_loop(
    tmp_path: Path, failure: str
) -> None:
    paths, source, _ = _prepare_paths(tmp_path)
    result_path = tmp_path / "failure-result.json"
    script = """
import json
import os
from pathlib import Path
from PySide6.QtCore import QTimer
from PySide6.QtWidgets import QApplication, QMessageBox
from meter_reading_mini.app import run_app
from meter_reading_mini.config import AppPaths
from meter_reading_mini.db import restore
from meter_reading_mini.ui.dialogs.backup_restore_dialog import BackupRestoreDialog
from meter_reading_mini.ui.main_window import MainWindow

paths = AppPaths.from_dirs(
    Path(os.environ["METER_READING_MINI_DATA_DIR"]), Path(os.environ["RESTORE_LOG_DIR"])
)
source = Path(os.environ["RESTORE_SOURCE"])
mode = os.environ["RESTORE_FAILURE"]
messages = []
original_replace = restore.os.replace

def fail_replace(src, dst):
    if mode == "replace" and Path(dst) == paths.db_path:
        raise OSError("置換失敗")
    return original_replace(src, dst)

restore.os.replace = fail_replace
QMessageBox.question = lambda *args, **kwargs: QMessageBox.StandardButton.Yes

def execute(dialog):
    dialog.source_edit.setText(str(source))
    dialog._inspect_source()
    dialog._confirm_restore()
    return dialog.result()

BackupRestoreDialog.exec = execute

def loop(application):
    window = next(
        widget for widget in application.topLevelWidgets()
        if isinstance(widget, MainWindow) and widget.isVisible()
    )
    timer = QTimer(application)
    timer.setInterval(30)
    def close_notification():
        box = QApplication.activeModalWidget()
        if isinstance(box, QMessageBox):
            messages.append(box.text())
            box.accept()
            timer.stop()
    timer.timeout.connect(close_notification)
    timer.start()
    QTimer.singleShot(0, window.backup_restore_action.trigger)
    QTimer.singleShot(10000, application.quit)
    return application.exec()

def fail_launch(command, *, env):
    raise OSError("再起動失敗")

result = run_app(paths, event_loop_runner=loop, restart_launcher=fail_launch)
Path(os.environ["RESTORE_RESULT"]).write_text(
    json.dumps({"result": result, "messages": messages}, ensure_ascii=False), encoding="utf-8"
)
"""
    environment = os.environ.copy()
    environment.update(
        {
            DATA_DIR_ENV: str(paths.data_dir),
            "RESTORE_LOG_DIR": str(paths.log_dir),
            "RESTORE_SOURCE": str(source),
            "RESTORE_FAILURE": failure,
            "RESTORE_RESULT": str(result_path),
            "PYTHONIOENCODING": "utf-8",
        }
    )
    completed = subprocess.run(
        [sys.executable, "-c", script],
        env=environment,
        capture_output=True,
        encoding="utf-8",
        timeout=30,
        check=False,
    )
    assert completed.returncode == 0, (completed.stdout, completed.stderr)
    observed = json.loads(result_path.read_text(encoding="utf-8"))
    assert observed["result"] == app.STARTUP_FAILURE
    assert len(observed["messages"]) == 1
    assert "保全バックアップ" in observed["messages"][0]
    assert "手動" in observed["messages"][0]
    backups = list(paths.backup_dir.glob("pre-restore_*.db"))
    assert len(backups) == 1
    assert _staff(backups[0]) == ["現行担当"]
    expected_staff = "現行担当" if failure == "replace" else "復元担当"
    assert _staff(paths.db_path) == [expected_staff]


def test_ready_notification_failure_is_reported_without_claiming_success(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    notices: list[str] = []
    successes: list[str] = []
    monkeypatch.setattr(
        QMessageBox,
        "warning",
        lambda parent, title, text, *args: notices.append(text) or QMessageBox.StandardButton.Ok,
    )
    monkeypatch.setattr(
        QMessageBox,
        "information",
        lambda parent, title, text, *args: successes.append(text) or QMessageBox.StandardButton.Ok,
    )
    notice = restart.RestartNotice(
        tmp_path / "missing" / "ready", "a" * 32, tmp_path / "backup.db", tmp_path
    )
    app._announce_restart_ready(notice)
    assert len(notices) == 1
    assert "通知できませんでした" in notices[0]
    assert str(notice.backup_path) in notices[0]
    assert successes == []


def test_restore_restarts_a_real_python_process_with_the_same_data_directory(
    tmp_path: Path,
) -> None:
    paths, source, settings_before = _prepare_paths(tmp_path)
    marker = tmp_path / "child-started.json"
    parent_result = tmp_path / "parent-result.json"
    child_script = """
import json
import os
import threading
from pathlib import Path
from PySide6.QtCore import QTimer
from PySide6.QtWidgets import QApplication, QMessageBox
from meter_reading_mini.config import resolve_paths

watchdog = threading.Timer(15, lambda: os._exit(124))
watchdog.daemon = True
watchdog.start()

def information(parent, title, text, *args):
    paths = resolve_paths()
    Path(os.environ["RESTORE_TEST_MARKER"]).write_text(
        json.dumps({"data": str(paths.data_dir), "lock": paths.lock_path.exists(),
                    "title": title, "text": text}, ensure_ascii=False),
        encoding="utf-8",
    )
    QTimer.singleShot(500, QApplication.instance().quit)
    return QMessageBox.StandardButton.Ok

QMessageBox.information = information
QMessageBox.warning = lambda *args, **kwargs: QMessageBox.StandardButton.Ok
QMessageBox.critical = lambda *args, **kwargs: QMessageBox.StandardButton.Ok
"""
    hook_directory = tmp_path / "child-hook"
    hook_directory.mkdir()
    (hook_directory / "sitecustomize.py").write_text(child_script, encoding="utf-8")
    parent_script = """
import json
import os
import subprocess
from pathlib import Path
from PySide6.QtCore import QTimer
from PySide6.QtWidgets import QMessageBox
from meter_reading_mini.app import run_app
from meter_reading_mini.config import AppPaths
from meter_reading_mini.ui.dialogs.backup_restore_dialog import BackupRestoreDialog
from meter_reading_mini.ui.main_window import MainWindow
from meter_reading_mini.ui.single_instance import SingleInstanceLock

paths = AppPaths.from_dirs(
    Path(os.environ["METER_READING_MINI_DATA_DIR"]), Path(os.environ["RESTORE_LOG_DIR"])
)
source = Path(os.environ["RESTORE_SOURCE"])
events = []
errors = []

def report(parent, title, text, *args):
    errors.append(f"{title}: {text}")
    return QMessageBox.StandardButton.Ok

def execute(dialog):
    dialog.source_edit.setText(str(source))
    dialog._inspect_source()
    dialog._confirm_restore()
    return dialog.result()

QMessageBox.question = lambda *args, **kwargs: QMessageBox.StandardButton.Yes
QMessageBox.warning = report
QMessageBox.critical = report
BackupRestoreDialog.exec = execute

def loop(application):
    window = next(
        widget for widget in application.topLevelWidgets()
        if isinstance(widget, MainWindow) and widget.isVisible()
    )
    def invoke():
        try:
            window.backup_restore_action.trigger()
            if window.boundary_timer.isActive() or window.isEnabled():
                errors.append("復元後も画面またはタイマーが有効です")
                application.quit()
            else:
                events.append("ui.stopped")
        except Exception as error:
            errors.append(str(error))
            application.quit()
    QTimer.singleShot(0, invoke)
    QTimer.singleShot(10000, application.quit)
    result = application.exec()
    events.append("loop.exit")
    return result

def launch(command, *, env):
    lock = SingleInstanceLock(paths.lock_path)
    if not lock.try_acquire(0):
        raise RuntimeError("再起動前にロックが解放されていません")
    lock.release()
    events.append("lock.released")
    child_env = dict(env)
    child_env["PYTHONPATH"] = os.pathsep.join(
        (os.environ["RESTORE_CHILD_HOOK"], child_env.get("PYTHONPATH", ""))
    )
    return subprocess.Popen(command, env=child_env)

result = run_app(paths, event_loop_runner=loop, restart_launcher=launch, restart_timeout=20)
Path(os.environ["RESTORE_PARENT_RESULT"]).write_text(
    json.dumps({"result": result, "events": events, "errors": errors}, ensure_ascii=False),
    encoding="utf-8",
)
raise SystemExit(result if not errors else 1)
"""
    environment = os.environ.copy()
    environment.update(
        {
            DATA_DIR_ENV: str(paths.data_dir),
            "RESTORE_LOG_DIR": str(paths.log_dir),
            "RESTORE_SOURCE": str(source),
            "RESTORE_TEST_MARKER": str(marker),
            "RESTORE_PARENT_RESULT": str(parent_result),
            "RESTORE_CHILD_HOOK": str(hook_directory),
            "PYTHONIOENCODING": "utf-8",
        }
    )
    completed = subprocess.run(
        [sys.executable, "-c", parent_script],
        env=environment,
        capture_output=True,
        encoding="utf-8",
        timeout=40,
        check=False,
    )
    assert completed.returncode == 0, (completed.stdout, completed.stderr)
    parent = json.loads(parent_result.read_text(encoding="utf-8"))
    assert parent["result"] == 0
    assert parent["events"] == ["ui.stopped", "loop.exit", "lock.released"]
    assert parent["errors"] == []
    child = json.loads(marker.read_text(encoding="utf-8"))
    assert Path(child["data"]) == paths.data_dir
    assert child["lock"] is True
    assert child["title"] == "復元が完了しました"
    assert "保全バックアップ" in child["text"]
    assert paths.log_path.is_file()
    assert _staff(paths.db_path) == ["復元担当"]
    assert paths.ui_settings_path.read_bytes() == settings_before
    _assert_lock_released(paths.lock_path)
