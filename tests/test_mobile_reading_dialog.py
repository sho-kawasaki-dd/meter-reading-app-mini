from __future__ import annotations

import sqlite3
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from PySide6.QtCore import QDate
from PySide6.QtGui import QFont
from PySide6.QtWidgets import QApplication, QFileDialog, QMessageBox, QScrollArea, QWidget
from pytestqt.qtbot import QtBot
from reading_service_helpers import DEFINITION_ID, build_services
from test_mobile_reading_sheet import (
    DAILY_TWICE,
    WEEKLY_WEDNESDAY,
    _add_definition,
    _extract_data,
)

from meter_reading_mini.app_context import AppContext, create_app_context
from meter_reading_mini.core import mobile_reading_sheet
from meter_reading_mini.core.contract_models import ReadingCycle, ReadingSession
from meter_reading_mini.db.repositories import SQLiteRepository
from meter_reading_mini.ui import atomic_write
from meter_reading_mini.ui.dialogs import mobile_reading_dialog
from meter_reading_mini.ui.dialogs.mobile_reading_dialog import MobileReadingDialog
from meter_reading_mini.ui.display_settings import DisplaySettings, apply_display_settings
from meter_reading_mini.ui.sheet_save_directory_memory import SheetSaveDirectoryMemory
from meter_reading_mini.ui.staff_selection_memory import StaffSelectionMemory

NOW = datetime(2026, 2, 4, 3, 0, tzinfo=UTC)


@dataclass
class Clock:
    value: datetime

    def __call__(self) -> datetime:
        return self.value


@dataclass
class SheetData:
    connection: sqlite3.Connection
    clock: Clock
    services: AppContext
    memory: SheetSaveDirectoryMemory


@dataclass
class Messages:
    warnings: list[str] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)
    information: list[str] = field(default_factory=list)


@pytest.fixture
def sheet_data(
    file_database_repository: tuple[sqlite3.Connection, SQLiteRepository],
) -> SheetData:
    connection, repository = file_database_repository
    build_services(connection, repository)
    _add_definition(
        connection, repository, "def-twice", cycle=ReadingCycle.DAILY_2X, schedule_json=DAILY_TWICE
    )
    _add_definition(
        connection,
        repository,
        "def-weekly",
        cycle=ReadingCycle.WEEKLY,
        schedule_json=WEEKLY_WEDNESDAY,
    )
    clock = Clock(NOW)
    return SheetData(
        connection,
        clock,
        create_app_context(connection, clock=clock),
        SheetSaveDirectoryMemory(),
    )


@pytest.fixture
def messages(monkeypatch: pytest.MonkeyPatch) -> Messages:
    captured = Messages()
    monkeypatch.setattr(
        QMessageBox, "warning", lambda _parent, _title, text: captured.warnings.append(text)
    )
    monkeypatch.setattr(
        QMessageBox, "critical", lambda _parent, _title, text: captured.errors.append(text)
    )
    monkeypatch.setattr(
        QMessageBox, "information", lambda _parent, _title, text: captured.information.append(text)
    )
    return captured


def _dialog(data: SheetData, qtbot: QtBot) -> MobileReadingDialog:
    dialog = MobileReadingDialog(data.services, data.memory, StaffSelectionMemory())
    qtbot.addWidget(dialog)
    return dialog


def _current_slot_count(connection: sqlite3.Connection) -> int:
    row = connection.execute(
        "SELECT COUNT(*) FROM planned_readings WHERE business_date = '2026-02-04'"
    ).fetchone()
    assert row is not None
    return int(row[0])


@pytest.mark.parametrize(
    ("sessions", "groups", "expected_filter", "expected_slots", "filename"),
    [
        (
            (),
            (),
            {"reading_sessions": None, "cycle_groups": None},
            (
                ("08:00", "def-twice"),
                ("08:00", DEFINITION_ID),
                ("14:00", "def-weekly"),
                ("22:00", "def-twice"),
            ),
            "meter_reading_20260204_120000.html",
        ),
        (
            (ReadingSession.SESSION_0800,),
            (),
            {"reading_sessions": ["08:00"], "cycle_groups": None},
            (("08:00", "def-twice"), ("08:00", DEFINITION_ID)),
            "meter_reading_20260204_0800_120000.html",
        ),
        (
            (),
            ("weekly",),
            {"reading_sessions": None, "cycle_groups": ["weekly"]},
            (("14:00", "def-weekly"),),
            "meter_reading_20260204_weekly_120000.html",
        ),
        (
            (ReadingSession.SESSION_2200,),
            ("daily",),
            {"reading_sessions": ["22:00"], "cycle_groups": ["daily"]},
            (("22:00", "def-twice"),),
            "meter_reading_20260204_filtered_120000.html",
        ),
        (
            (ReadingSession.SESSION_0800, ReadingSession.SESSION_2200),
            ("daily",),
            {"reading_sessions": ["08:00", "22:00"], "cycle_groups": ["daily"]},
            (("08:00", "def-twice"), ("08:00", DEFINITION_ID), ("22:00", "def-twice")),
            "meter_reading_20260204_filtered_120000.html",
        ),
        (
            (ReadingSession.SESSION_0800, ReadingSession.SESSION_1400),
            ("daily", "weekly"),
            {"reading_sessions": ["08:00", "14:00"], "cycle_groups": ["daily", "weekly"]},
            (("08:00", "def-twice"), ("08:00", DEFINITION_ID), ("14:00", "def-weekly")),
            "meter_reading_20260204_filtered_120000.html",
        ),
    ],
)
def test_export_filters_match_saved_html_and_suggested_filename(
    sheet_data: SheetData,
    qtbot: QtBot,
    messages: Messages,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    sessions: tuple[ReadingSession, ...],
    groups: tuple[str, ...],
    expected_filter: dict[str, list[str] | None],
    expected_slots: tuple[tuple[str, str], ...],
    filename: str,
) -> None:
    dialog = _dialog(sheet_data, qtbot)
    directory = tmp_path / "sheets"
    directory.mkdir()
    for session in sessions:
        dialog.session_checks[session].setChecked(True)
    for group in groups:
        dialog.cycle_checks[group].setChecked(True)
    defaults: list[Path] = []

    def choose(_parent: QWidget, _caption: str, initial: str, file_filter: str) -> tuple[str, str]:
        defaults.append(Path(initial))
        return str(directory / filename), file_filter

    monkeypatch.setattr(QFileDialog, "getSaveFileName", choose)
    notifications: list[int] = []
    dialog.reload_requested.connect(
        lambda: notifications.append(_current_slot_count(sheet_data.connection))
    )

    dialog.export_button.click()

    target = directory / filename
    data = _extract_data(target.read_text(encoding="utf-8"))
    assert defaults == [Path(filename)]
    assert data["filter"] == expected_filter
    assert (
        tuple(
            (slot["reading_session"], slot["measurement_definition_id"]) for slot in data["slots"]
        )
        == expected_slots
    )
    assert notifications == [4]
    assert sheet_data.memory.directory == directory
    assert messages.warnings == messages.errors == []
    assert len(messages.information) == 1
    assert str(target) in messages.information[0]
    assert f"{len(expected_slots)} 件" in messages.information[0]
    assert "Quick Share" in messages.information[0] and "Chrome" in messages.information[0]
    assert list(directory.iterdir()) == [target]


def test_dialog_is_modal_centered_and_updates_business_date_limit(
    sheet_data: SheetData, qtbot: QtBot, messages: Messages, monkeypatch: pytest.MonkeyPatch
) -> None:
    sheet_data.clock.value = datetime(2026, 2, 3, 23, 30, tzinfo=UTC)
    dialog = _dialog(sheet_data, qtbot)
    centered: list[QWidget] = []
    monkeypatch.setattr(mobile_reading_dialog, "center_dialog", centered.append)

    assert dialog.isModal()
    assert [dialog.tabs.tabText(i) for i in range(dialog.tabs.count())] == [
        "シート出力",
        "結果取込・確定",
    ]
    assert dialog.business_date_edit.date() == QDate(2026, 2, 4)
    assert dialog.business_date_edit.maximumDate() == QDate(2026, 2, 4)
    dialog.business_date_edit.setDate(QDate(2026, 2, 5))
    assert dialog.business_date_edit.date() == QDate(2026, 2, 4)
    dialog.show()
    assert centered == [dialog]
    assert dialog._date_timer.isActive()

    sheet_data.clock.value += timedelta(days=1)
    dialog._date_timer.timeout.emit()
    assert dialog.business_date_edit.maximumDate() == QDate(2026, 2, 5)
    dialog.hide()
    assert not dialog._date_timer.isActive()
    assert messages.warnings == messages.errors == []


def test_future_date_after_clock_change_is_rejected_before_generation(
    sheet_data: SheetData, qtbot: QtBot, messages: Messages
) -> None:
    dialog = _dialog(sheet_data, qtbot)
    notifications: list[bool] = []
    dialog.reload_requested.connect(lambda: notifications.append(True))
    sheet_data.clock.value -= timedelta(days=1)

    dialog.export_button.click()

    assert "未来の業務日" in messages.warnings[-1]
    assert messages.information == messages.errors == []
    assert dialog.business_date_edit.maximumDate() == QDate(2026, 2, 3)
    assert dialog.business_date_edit.date() == QDate(2026, 2, 3)
    assert notifications == []
    assert _current_slot_count(sheet_data.connection) == 0


def test_empty_sheet_notifies_reload_after_backfill_and_auto_close(
    sheet_data: SheetData,
    qtbot: QtBot,
    messages: Messages,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    dialog = _dialog(sheet_data, qtbot)
    dialog.cycle_checks["monthly"].setChecked(True)
    notifications: list[tuple[int, int]] = []

    def reloaded() -> None:
        row = sheet_data.connection.execute(
            "SELECT COUNT(*) FROM planned_readings "
            "WHERE business_date < '2026-02-04' AND task_state = 'auto_closed'"
        ).fetchone()
        assert row is not None
        notifications.append((_current_slot_count(sheet_data.connection), int(row[0])))

    def unexpected_dialog(*_args: object) -> tuple[str, str]:
        pytest.fail("対象が空の場合は保存先を選択しません")

    dialog.reload_requested.connect(reloaded)
    monkeypatch.setattr(QFileDialog, "getSaveFileName", unexpected_dialog)
    dialog.export_button.click()

    assert len(notifications) == 1
    assert notifications[0][0] == 4 and notifications[0][1] > 0
    assert "出力できる検針枠がありません" in messages.warnings[-1]
    assert messages.information == messages.errors == []
    assert sheet_data.memory.directory is None


def test_cancel_after_generation_notifies_reload_without_success(
    sheet_data: SheetData, qtbot: QtBot, messages: Messages, monkeypatch: pytest.MonkeyPatch
) -> None:
    dialog = _dialog(sheet_data, qtbot)
    notifications: list[int] = []
    dialog.reload_requested.connect(
        lambda: notifications.append(_current_slot_count(sheet_data.connection))
    )
    monkeypatch.setattr(QFileDialog, "getSaveFileName", lambda *_args: ("", ""))

    dialog.export_button.click()

    assert notifications == [4]
    assert messages.information == messages.warnings == messages.errors == []
    assert sheet_data.memory.directory is None


def test_generation_failure_after_commit_notifies_reload_and_reports_unexpected_error(
    sheet_data: SheetData,
    qtbot: QtBot,
    messages: Messages,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    dialog = _dialog(sheet_data, qtbot)
    notifications: list[int] = []
    dialog.reload_requested.connect(
        lambda: notifications.append(_current_slot_count(sheet_data.connection))
    )

    def fail_render(_data: object) -> str:
        raise RuntimeError("シートの描画失敗")

    monkeypatch.setattr(mobile_reading_sheet, "render_sheet_html", fail_render)
    dialog.export_button.click()

    assert notifications == [4]
    assert messages.information == messages.warnings == []
    assert len(messages.errors) == 1 and "予期しないエラー" in messages.errors[0]
    assert "シートの描画失敗" in caplog.text
    assert sheet_data.memory.directory is None


@pytest.mark.parametrize("failure", ["open", "write", "short_write", "replace"])
def test_save_failure_preserves_existing_file_and_notifies_reload(
    sheet_data: SheetData,
    qtbot: QtBot,
    messages: Messages,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
    failure: str,
) -> None:
    dialog = _dialog(sheet_data, qtbot)
    directory = tmp_path / "sheets"
    directory.mkdir()
    target = directory / "existing.html"
    target.write_text("変更前", encoding="utf-8")
    previous_directory = tmp_path / "previous"
    previous_directory.mkdir()
    sheet_data.memory.directory = previous_directory
    notifications: list[int] = []
    temporary_paths: list[Path] = []
    dialog.reload_requested.connect(
        lambda: notifications.append(_current_slot_count(sheet_data.connection))
    )
    monkeypatch.setattr(QFileDialog, "getSaveFileName", lambda *_args: (str(target), ""))

    if failure == "open":

        def failing_open(**_kwargs: object) -> None:
            raise PermissionError("一時ファイルの作成失敗")

        monkeypatch.setattr(atomic_write.tempfile, "NamedTemporaryFile", failing_open)
    elif failure in {"write", "short_write"}:
        original_temporary_file = atomic_write.tempfile.NamedTemporaryFile

        def failing_temporary_file(**kwargs: object):
            assert kwargs["dir"] == directory
            temporary = original_temporary_file(
                mode="w",
                encoding="utf-8",
                newline="\n",
                dir=directory,
                prefix=f"{target.name}.",
                suffix=".tmp",
                delete=False,
            )
            temporary_paths.append(Path(temporary.name))
            write = temporary.write

            def failing_write(text: str) -> int:
                written = write(text[:20])
                if failure == "short_write":
                    return written
                raise PermissionError("一時ファイルの書込失敗")

            monkeypatch.setattr(temporary, "write", failing_write)
            return temporary

        monkeypatch.setattr(atomic_write.tempfile, "NamedTemporaryFile", failing_temporary_file)
    else:

        def failing_replace(source: Path, destination: Path) -> None:
            assert destination == target
            assert source.parent == directory
            assert source.stat().st_size > 0
            temporary_paths.append(source)
            raise PermissionError("ファイルの置換失敗")

        monkeypatch.setattr(atomic_write.os, "replace", failing_replace)

    dialog.export_button.click()

    assert notifications == [4]
    assert target.read_text(encoding="utf-8") == "変更前"
    if failure == "open":
        assert temporary_paths == []
    else:
        assert len(temporary_paths) == 1 and not temporary_paths[0].exists()
    assert list(directory.iterdir()) == [target]
    assert messages.information == messages.errors == []
    assert len(messages.warnings) == 1 and "保存できませんでした" in messages.warnings[0]
    assert str(target) in messages.warnings[0]
    assert "出力に失敗しました" in caplog.text
    assert sheet_data.memory.directory == previous_directory


def test_save_directory_is_shared_across_dialogs_only_in_memory(
    sheet_data: SheetData,
    qtbot: QtBot,
    messages: Messages,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    directory = tmp_path / "保存先"
    directory.mkdir()
    defaults: list[Path] = []

    def choose(_parent: QWidget, _caption: str, initial: str, file_filter: str) -> tuple[str, str]:
        defaults.append(Path(initial))
        if len(defaults) == 1:
            return str(directory / "任意の名前.html"), file_filter
        return "", file_filter

    monkeypatch.setattr(QFileDialog, "getSaveFileName", choose)
    first = _dialog(sheet_data, qtbot)
    first.export_button.click()
    second = _dialog(sheet_data, qtbot)
    second.cycle_checks["weekly"].setChecked(True)
    second.export_button.click()

    assert defaults == [
        Path("meter_reading_20260204_120000.html"),
        directory / "meter_reading_20260204_weekly_120000.html",
    ]
    assert sheet_data.memory.directory == directory
    assert SheetSaveDirectoryMemory().directory is None
    assert list(directory.iterdir()) == [directory / "任意の名前.html"]
    assert len(messages.information) == 1
    assert messages.warnings == messages.errors == []


@pytest.mark.parametrize("overwrite", [False, True])
def test_extension_completion_confirms_overwrite(
    sheet_data: SheetData,
    qtbot: QtBot,
    messages: Messages,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    overwrite: bool,
) -> None:
    dialog = _dialog(sheet_data, qtbot)
    target = tmp_path / "sheet.html"
    target.write_text("変更前", encoding="utf-8")
    monkeypatch.setattr(
        QFileDialog, "getSaveFileName", lambda *_args: (str(tmp_path / "sheet"), "")
    )
    questions: list[str] = []

    def confirm(
        _parent: QWidget, _title: str, text: str, *_args: object
    ) -> QMessageBox.StandardButton:
        questions.append(text)
        return QMessageBox.StandardButton.Yes if overwrite else QMessageBox.StandardButton.No

    monkeypatch.setattr(QMessageBox, "question", confirm)
    notifications: list[bool] = []
    dialog.reload_requested.connect(lambda: notifications.append(True))

    dialog.export_button.click()

    assert notifications == [True]
    assert len(questions) == 1 and "sheet.html" in questions[0]
    if overwrite:
        assert len(_extract_data(target.read_text(encoding="utf-8"))["slots"]) == 4
        assert len(messages.information) == 1
    else:
        assert target.read_text(encoding="utf-8") == "変更前"
        assert messages.information == []
    assert messages.warnings == messages.errors == []


def test_sheet_controls_remain_reachable_at_150_percent(
    sheet_data: SheetData, qtbot: QtBot, restore_app_font: QFont
) -> None:
    apply_display_settings(DisplaySettings(None, 150))
    dialog = _dialog(sheet_data, qtbot)
    dialog.resize(1280, 680)
    dialog.show()
    QApplication.processEvents()
    assert dialog.width() <= 1280 and dialog.height() <= 680

    area = dialog.tabs.widget(0)
    assert isinstance(area, QScrollArea)
    for target in (
        dialog.business_date_edit,
        *dialog.session_checks.values(),
        *dialog.cycle_checks.values(),
        dialog.export_button,
    ):
        area.ensureWidgetVisible(target, 0, 0)
        QApplication.processEvents()
        center = target.mapToGlobal(target.rect().center())
        assert area.viewport().rect().contains(area.viewport().mapFromGlobal(center))
        assert dialog.rect().contains(dialog.mapFromGlobal(center))

    dialog.tabs.setCurrentIndex(1)
    result_area = dialog.tabs.widget(1)
    assert isinstance(result_area, QScrollArea)
    for target in (
        dialog.paste_button,
        dialog.load_json_button,
        dialog.preview_button,
        dialog.result_edit,
        dialog.preview_table,
        dialog.staff_combo,
        dialog.commit_button,
    ):
        result_area.ensureWidgetVisible(target, 0, 0)
        QApplication.processEvents()
        center = target.mapToGlobal(target.rect().center())
        assert result_area.viewport().rect().contains(result_area.viewport().mapFromGlobal(center))
        assert dialog.rect().contains(dialog.mapFromGlobal(center))
