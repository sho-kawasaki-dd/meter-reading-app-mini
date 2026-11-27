from __future__ import annotations

import sqlite3
from datetime import date, datetime
from pathlib import Path

import pytest
from PySide6.QtCore import QDate, QTime
from PySide6.QtGui import QFont
from PySide6.QtWidgets import QApplication, QMessageBox
from pytestqt.qtbot import QtBot

from meter_reading_mini.app_context import AppContext, create_app_context
from meter_reading_mini.core.contract_models import ReadingCycle, ReadingSession
from meter_reading_mini.core.meter_reading_sync_repositories import (
    EveryTwoMonthsSchedule,
    MonthlyScheduleSelectorType,
    MonthParity,
)
from meter_reading_mini.db.repositories import SQLiteRepository
from meter_reading_mini.ui.dialogs import settings_dialog as settings_module
from meter_reading_mini.ui.dialogs.settings_dialog import SettingsDialog
from meter_reading_mini.ui.display_settings import DisplaySettings, load_display_settings


@pytest.fixture
def settings_services(
    file_database_repository: tuple[sqlite3.Connection, SQLiteRepository], fixed_clock: datetime
) -> AppContext:
    connection, _ = file_database_repository
    return create_app_context(connection, clock=lambda: fixed_clock)


@pytest.fixture
def settings_dialog(settings_services: AppContext, tmp_path: Path, qtbot: QtBot) -> SettingsDialog:
    dialog = SettingsDialog(settings_services, tmp_path / "ui_settings.json")
    qtbot.addWidget(dialog)
    dialog.show()
    return dialog


def test_cutoff_and_timezone(
    settings_dialog: SettingsDialog, settings_services: AppContext
) -> None:
    notifications: list[str] = []
    settings_dialog.saved.connect(lambda: notifications.append("saved"))

    assert settings_dialog.timezone_label.text() == "Asia/Tokyo"
    settings_dialog.cutoff_edit.setTime(QTime(6, 30))
    settings_dialog.cutoff_save_button.click()

    assert settings_services.cutoff_settings.get_cutoff_time().strftime("%H:%M") == "06:30"
    assert notifications == ["saved"]


def test_cycle_schedule_saves_all_fields_and_loads_history_for_future_date(
    settings_dialog: SettingsDialog, settings_services: AppContext
) -> None:
    panel = settings_dialog.schedule_panel
    panel.select_subject(ReadingCycle.EVERY_TWO_MONTHS.value)
    panel.editor.effective_date_edit.setDate(QDate(2026, 2, 8))
    editor = panel.editor
    editor.selector.setCurrentIndex(
        editor.selector.findData(MonthlyScheduleSelectorType.NTH_WEEKDAY.value)
    )
    editor.nth.setValue(3)
    editor.nth_weekday.setCurrentIndex(editor.nth_weekday.findData("4"))
    editor.parity.setCurrentIndex(editor.parity.findData(MonthParity.EVEN.value))
    editor.single_session.setCurrentIndex(editor.single_session.findData("17:00"))
    editor.single_offset.setText("-2")
    panel.save_button.click()

    expected = EveryTwoMonthsSchedule(
        MonthlyScheduleSelectorType.NTH_WEEKDAY,
        MonthParity.EVEN,
        nth=3,
        nth_weekday=4,
        reading_session=ReadingSession.SESSION_1700,
        day_offset=-2,
    )
    assert (
        settings_services.schedule_defaults.get_schedule(
            ReadingCycle.EVERY_TWO_MONTHS, date(2026, 2, 8)
        )
        == expected
    )
    assert "2026-02-08" in editor.history_text.toPlainText()
    panel.editor.effective_date_edit.setDate(QDate(2026, 2, 9))
    assert editor.schedule() == expected


def test_unsaved_schedule_is_preserved_when_switch_is_cancelled(
    settings_dialog: SettingsDialog, monkeypatch: pytest.MonkeyPatch
) -> None:
    panel = settings_dialog.schedule_panel
    monkeypatch.setattr(QMessageBox, "question", lambda *args: QMessageBox.StandardButton.No)
    panel.editor.daily_table.cellWidget(0, 1).setProperty("text", "-2")

    panel.subject_combo.setCurrentIndex(panel.subject_combo.findData(ReadingCycle.WEEKLY.value))

    assert panel.subject_combo.currentData() == ReadingCycle.DAILY_1X.value
    assert panel.editor.daily_table.cellWidget(0, 1).property("text") == "-2"


def test_offset_history_and_invalid_input(
    settings_dialog: SettingsDialog,
    settings_services: AppContext,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    errors: list[str] = []
    monkeypatch.setattr(QMessageBox, "warning", lambda parent, title, text: errors.append(text))
    settings_dialog.offset_session_combo.setCurrentIndex(
        settings_dialog.offset_session_combo.findData("08:00")
    )
    settings_dialog.offset_date_edit.setDate(QDate(2026, 2, 6))
    settings_dialog.offset_days_edit.setText("invalid")
    settings_dialog.offset_save_button.click()
    assert "整数" in errors[-1]
    assert (
        settings_services.release_offsets.get_release_offset_days(
            ReadingSession.SESSION_0800, date(2026, 2, 6)
        )
        == 0
    )

    settings_dialog.offset_days_edit.setText("999999999")
    settings_dialog.offset_save_button.click()
    assert "日付が対応範囲外" in errors[-1]
    assert (
        settings_services.release_offsets.get_release_offset_days(
            ReadingSession.SESSION_0800, date(2026, 2, 6)
        )
        == 0
    )

    settings_dialog.offset_days_edit.setText("-2")
    settings_dialog.offset_save_button.click()
    assert (
        settings_services.release_offsets.get_release_offset_days(
            ReadingSession.SESSION_0800, date(2026, 2, 6)
        )
        == -2
    )
    assert "2026-02-06  -2 日" in settings_dialog.offset_history.toPlainText()


def test_display_settings_save_and_apply_immediately(
    settings_dialog: SettingsDialog,
    tmp_path: Path,
    qapp: QApplication,
    restore_app_font: QFont,
) -> None:
    before = qapp.font().pointSizeF()
    settings_dialog.scale_combo.setCurrentIndex(settings_dialog.scale_combo.findData("125"))
    settings_dialog.display_save_button.click()

    assert load_display_settings(tmp_path / "ui_settings.json") == DisplaySettings(None, 125)
    assert qapp.font().pointSizeF() == pytest.approx(before * 1.25)


def test_unsaved_release_offset_is_preserved_when_switch_is_cancelled(
    settings_dialog: SettingsDialog, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(QMessageBox, "question", lambda *args: QMessageBox.StandardButton.No)
    settings_dialog.offset_session_combo.setCurrentIndex(
        settings_dialog.offset_session_combo.findData("08:00")
    )
    original_date = settings_dialog.offset_date_edit.date()
    settings_dialog.offset_days_edit.setText("-3")

    settings_dialog.offset_session_combo.setCurrentIndex(
        settings_dialog.offset_session_combo.findData("14:00")
    )
    assert settings_dialog.offset_session_combo.currentData() == "08:00"
    assert settings_dialog.offset_days_edit.text() == "-3"

    settings_dialog.offset_date_edit.setDate(original_date.addDays(1))
    assert settings_dialog.offset_date_edit.date() == original_date
    assert settings_dialog.offset_days_edit.text() == "-3"


def test_display_save_failure_is_reported_without_applying(
    settings_dialog: SettingsDialog,
    qapp: QApplication,
    restore_app_font: QFont,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    errors: list[str] = []
    monkeypatch.setattr(QMessageBox, "critical", lambda parent, title, text: errors.append(text))

    def fail_save(path: Path, settings: DisplaySettings) -> None:
        raise PermissionError("書き込めません")

    monkeypatch.setattr(settings_module, "save_display_settings", fail_save)
    before = qapp.font().pointSizeF()
    settings_dialog.scale_combo.setCurrentIndex(settings_dialog.scale_combo.findData("150"))
    settings_dialog.display_save_button.click()
    assert errors and "ログを確認" in errors[-1]
    assert "書き込めません" in caplog.text
    assert qapp.font().pointSizeF() == before
    assert not (tmp_path / "ui_settings.json").exists()


def test_corrupt_schedule_history_does_not_replace_the_visible_selection(
    settings_dialog: SettingsDialog,
    file_database_repository: tuple[sqlite3.Connection, SQLiteRepository],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    connection, _ = file_database_repository
    connection.execute(
        "UPDATE reading_cycle_schedule_defaults SET schedule_json = ? WHERE reading_cycle = ?",
        ("{invalid", ReadingCycle.WEEKLY.value),
    )
    errors: list[str] = []
    monkeypatch.setattr(QMessageBox, "warning", lambda parent, title, text: errors.append(text))
    panel = settings_dialog.schedule_panel
    before = panel.editor.schedule()
    panel.select_subject(ReadingCycle.WEEKLY.value)
    assert panel.subject_combo.currentData() == ReadingCycle.DAILY_1X.value
    assert panel.editor.schedule() == before
    assert errors and "スケジュール履歴が不正" in errors[-1]
