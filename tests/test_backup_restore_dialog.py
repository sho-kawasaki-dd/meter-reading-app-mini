from __future__ import annotations

import sqlite3
from collections.abc import Iterator
from contextlib import closing
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest
from PySide6.QtCore import QPoint
from PySide6.QtGui import QFont
from PySide6.QtWidgets import QApplication, QDialog, QFileDialog, QMessageBox
from pytestqt.qtbot import QtBot

from meter_reading_mini.core.backup_restore_service import BackupRestoreService
from meter_reading_mini.core.errors import FileOutputError, InvalidBackupError
from meter_reading_mini.db.connection import connect, connect_readonly
from meter_reading_mini.db.migrations import create_schema
from meter_reading_mini.db.restore import SQLiteBackupRestoreRepository
from meter_reading_mini.ui.dialogs.backup_restore_dialog import BackupRestoreDialog
from meter_reading_mini.ui.display_settings import DisplaySettings, apply_display_settings


@dataclass
class DialogHarness:
    current: Path
    source: Path
    connection: sqlite3.Connection
    service: BackupRestoreService
    dialog: BackupRestoreDialog
    closed: bool = False

    def close(self) -> None:
        self.connection.close()
        self.closed = True


@pytest.fixture
def harness(tmp_path: Path, fixed_clock: datetime, qtbot: QtBot) -> Iterator[DialogHarness]:
    current = tmp_path / "current.db"
    connection = connect(current)
    create_schema(connection)
    connection.execute("PRAGMA wal_autocheckpoint = 0")
    connection.execute("INSERT INTO staff (name) VALUES ('現行担当')")
    source = tmp_path / "source.db"
    with closing(connect(source)) as candidate:
        create_schema(candidate)
        candidate.execute("INSERT INTO staff (name) VALUES ('復元担当')")
        candidate.executemany(
            "INSERT INTO physical_meters "
            "(meter_id, location_id, location_name, meter_name, category) "
            "VALUES (?, 'location', ?, '試験メーター', 'water')",
            (("meter-a", "新名称"), ("meter-b", "旧名称")),
        )
    repository = SQLiteBackupRestoreRepository(connection, current)
    service = BackupRestoreService(
        repository,
        tmp_path / "backups",
        timezone_provider=lambda: ZoneInfo("Asia/Tokyo"),
        clock=lambda: fixed_clock,
    )
    dialog = BackupRestoreDialog(service, tmp_path / "backups")
    qtbot.addWidget(dialog)
    dialog.show()
    result = DialogHarness(current, source, connection, service, dialog)
    try:
        yield result
    finally:
        dialog.reject()
        service.discard_preview()
        if not result.closed:
            result.close()


@pytest.fixture
def messages(monkeypatch: pytest.MonkeyPatch) -> list[tuple[str, str, str]]:
    result: list[tuple[str, str, str]] = []
    for kind in ("information", "warning", "critical"):
        monkeypatch.setattr(
            QMessageBox,
            kind,
            lambda parent, title, text, *args, kind=kind: (
                result.append((kind, title, text)) or QMessageBox.StandardButton.Ok
            ),
        )
    return result


def _inspect(harness: DialogHarness) -> None:
    harness.dialog.source_edit.setText(str(harness.source))
    harness.dialog.inspect_button.click()
    assert harness.dialog.restore_button.isEnabled()


def test_backup_uses_business_timezone_includes_wal_and_never_overwrites(
    harness: DialogHarness,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    messages: list[tuple[str, str, str]],
) -> None:
    selected = tmp_path / "manual-backup"
    defaults: list[str] = []

    def choose(parent: object, title: str, default: str, file_filter: str, **kwargs: object):
        defaults.append(default)
        assert kwargs["options"] == QFileDialog.Option.DontConfirmOverwrite
        return str(selected), ""

    monkeypatch.setattr(QFileDialog, "getSaveFileName", choose)
    harness.dialog.backup_button.click()

    backup = selected.with_suffix(".db")
    assert Path(defaults[0]).name == "meter_reading_20260204_120000.db"
    with closing(connect_readonly(backup, immutable=True)) as connection:
        assert connection.execute("SELECT name FROM staff").fetchall() == [("現行担当",)]
    assert messages[-1][0] == "information"
    original = backup.read_bytes()

    harness.dialog.backup_button.click()
    assert backup.read_bytes() == original
    assert [message[0] for message in messages] == ["information", "warning"]
    assert "保存できません" in messages[-1][2]


def test_backup_cancel_and_failure_do_not_report_success(
    harness: DialogHarness,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    messages: list[tuple[str, str, str]],
) -> None:
    monkeypatch.setattr(QFileDialog, "getSaveFileName", lambda *args, **kwargs: ("", ""))
    harness.dialog.backup_button.click()
    assert messages == []

    monkeypatch.setattr(
        QFileDialog, "getSaveFileName", lambda *args, **kwargs: (str(tmp_path / "test.db"), "")
    )

    def fail(_path: Path) -> None:
        raise FileOutputError("容量不足")

    monkeypatch.setattr(harness.service, "create_backup", fail)
    harness.dialog.backup_button.click()
    assert len(messages) == 1
    assert messages[0][0] == "warning"
    assert "容量不足" in messages[0][2]


def test_preview_shows_counts_and_warnings_and_edit_invalidates_it(
    harness: DialogHarness, messages: list[tuple[str, str, str]]
) -> None:
    _inspect(harness)
    text = harness.dialog.preview_text.toPlainText()
    assert str(harness.source) in text
    assert "元のスキーマ版: 4" in text
    assert "担当者: 1 件" in text
    assert "メーター: 2 件" in text
    assert "異なる場所名" in text
    assert list(harness.current.parent.glob(".meter-reading-restore-*"))

    harness.dialog.source_edit.setText(str(harness.current.parent / "missing.db"))
    assert not harness.dialog.restore_button.isEnabled()
    assert not harness.dialog.preview_text.toPlainText()
    assert not list(harness.current.parent.glob(".meter-reading-restore-*"))
    harness.dialog.inspect_button.click()
    assert messages[-1][0] == "warning"
    assert not harness.dialog.restore_button.isEnabled()
    with pytest.raises(InvalidBackupError, match="検査済み"):
        harness.service.restore_after_shutdown(harness.close)
    assert not harness.closed


def test_cancelled_selection_and_confirmation_discard_preview(
    harness: DialogHarness,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _inspect(harness)
    monkeypatch.setattr(QFileDialog, "getOpenFileName", lambda *args, **kwargs: ("", ""))
    harness.dialog.browse_button.click()
    assert not harness.dialog.restore_button.isEnabled()
    assert not list(harness.current.parent.glob(".meter-reading-restore-*"))

    harness.dialog.inspect_button.click()
    assert harness.dialog.restore_button.isEnabled()
    monkeypatch.setattr(
        QMessageBox, "question", lambda *args, **kwargs: QMessageBox.StandardButton.No
    )
    harness.dialog.restore_button.click()
    assert not harness.dialog.restore_button.isEnabled()
    assert not list(harness.current.parent.glob(".meter-reading-restore-*"))
    with pytest.raises(InvalidBackupError, match="検査済み"):
        harness.service.restore_after_shutdown(harness.close)


def test_confirmed_candidate_is_kept_for_shutdown_and_replaces_current(
    harness: DialogHarness, monkeypatch: pytest.MonkeyPatch
) -> None:
    _inspect(harness)
    questions: list[str] = []

    def confirm(parent: object, title: str, text: str, *args: object) -> QMessageBox.StandardButton:
        questions.append(text)
        return QMessageBox.StandardButton.Yes

    monkeypatch.setattr(QMessageBox, "question", confirm)
    harness.dialog.restore_button.click()
    assert harness.dialog.result() == QDialog.DialogCode.Accepted
    assert "上書き" in questions[0]
    assert "再起動" in questions[0]
    assert "表示設定は復元されません" in questions[0]
    result = harness.service.restore_after_shutdown(harness.close)
    assert harness.closed
    with closing(connect_readonly(result.backup_path, immutable=True)) as backup:
        assert backup.execute("SELECT name FROM staff").fetchall() == [("現行担当",)]
    with closing(connect_readonly(harness.current, immutable=True)) as restored:
        assert restored.execute("SELECT name FROM staff").fetchall() == [("復元担当",)]


def test_closing_dialog_discards_candidate(harness: DialogHarness) -> None:
    _inspect(harness)
    harness.dialog.close()
    assert not list(harness.current.parent.glob(".meter-reading-restore-*"))
    with pytest.raises(InvalidBackupError, match="検査済み"):
        harness.service.restore_after_shutdown(harness.close)


def test_controls_remain_reachable_at_150_percent(
    harness: DialogHarness, restore_app_font: QFont
) -> None:
    apply_display_settings(DisplaySettings(None, 150))
    harness.dialog.resize(760, 580)
    QApplication.processEvents()
    for widget in (
        harness.dialog.backup_button,
        harness.dialog.source_edit,
        harness.dialog.browse_button,
        harness.dialog.inspect_button,
        harness.dialog.preview_text,
        harness.dialog.restore_button,
        harness.dialog.close_button,
    ):
        assert widget.isVisible()
        top_left = widget.mapTo(harness.dialog, QPoint(0, 0))
        bottom_right = widget.mapTo(harness.dialog, widget.rect().bottomRight())
        assert harness.dialog.rect().contains(top_left)
        assert harness.dialog.rect().contains(bottom_right)
