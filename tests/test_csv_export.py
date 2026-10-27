from __future__ import annotations

from pathlib import Path

import pytest
from PySide6.QtWidgets import QFileDialog, QMessageBox, QWidget

from meter_reading_mini.ui.csv_export import export_csv_with_dialog, export_file_with_dialog


def test_export_csv_with_dialog_completes_extension_and_writes(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, qtbot: object
) -> None:
    parent = QWidget()
    target_without_ext = tmp_path / "readings_report"
    monkeypatch.setattr(
        QFileDialog,
        "getSaveFileName",
        lambda *_args, **_kwargs: (str(target_without_ext), "CSV ファイル (*.csv)"),
    )

    written: list[Path] = []
    result = export_csv_with_dialog(
        parent,
        "検針履歴",
        "meter_readings",
        lambda path: written.append(path),
    )

    expected = tmp_path / "readings_report.csv"
    assert result == expected
    assert written == [expected]


def test_export_file_with_dialog_asks_confirmation_when_completed_path_exists(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, qtbot: object
) -> None:
    parent = QWidget()
    existing_html = tmp_path / "sheet.html"
    existing_html.write_text("old", encoding="utf-8")

    monkeypatch.setattr(
        QFileDialog,
        "getSaveFileName",
        lambda *_args, **_kwargs: (str(tmp_path / "sheet"), "HTML ファイル (*.html)"),
    )
    monkeypatch.setattr(
        QMessageBox,
        "question",
        lambda *_args, **_kwargs: QMessageBox.StandardButton.No,
    )

    written: list[Path] = []
    result = export_file_with_dialog(
        parent,
        "検針シート",
        "meter_reading",
        ".html",
        "HTML ファイル (*.html)",
        lambda path: written.append(path),
    )

    assert result is None
    assert written == []
