from __future__ import annotations

import logging
import sqlite3
from collections.abc import Callable, Iterator
from pathlib import Path

import pytest
from PySide6.QtWidgets import QApplication

from meter_reading_mini import app as app_module
from meter_reading_mini.config import AppPaths, resolve_paths
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
    )

    assert result == app_module.STARTUP_SUCCESS
    assert factory_calls == []
    assert QApplication.instance() is qapp
    assert events == [
        "directories",
        "lock.create",
        "lock.acquire",
        "logging",
        "db.open",
        "settings.load",
        "settings.apply",
        "startup.prepare",
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
        )

    assert result == app_module.STARTUP_FAILURE
    assert events == ["lock.acquire", "db.close", "lock.release"]
    assert "アプリケーションの起動に失敗しました" in caplog.text
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

    result = app_module.run_app(paths)

    assert result == app_module.STARTUP_SUCCESS
    assert paths.db_path.is_file()
    assert paths.log_path == data_dir / "logs" / "app.log"
    assert paths.log_path.is_file()
    with sqlite3.connect(paths.db_path) as connection:
        assert connection.execute("PRAGMA user_version").fetchone() == (SCHEMA_VERSION,)
