from __future__ import annotations

import logging
from collections.abc import Callable
from pathlib import Path

from PySide6.QtCore import Qt
from PySide6.QtWidgets import QApplication, QFileDialog, QMessageBox, QWidget

from meter_reading_mini.core.timeutil import local_timestamp_for_filename
from meter_reading_mini.ui.error_handling import run_guarded

logger = logging.getLogger(__name__)


def _choose_export_path(
    parent: QWidget,
    label: str,
    default_prefix: str,
    extension: str,
    file_filter: str,
) -> Path | None:
    suffix = extension if extension.startswith(".") else f".{extension}"
    default_name = f"{default_prefix}_{local_timestamp_for_filename()}{suffix}"
    selected, _selected_filter = QFileDialog.getSaveFileName(
        parent,
        f"{label}を{suffix[1:].upper()}出力",
        default_name,
        file_filter,
    )
    if not selected:
        return None
    path = Path(selected)
    if path.suffix.lower() == suffix.lower():
        return path
    # 保存ダイアログの上書き確認が及ばないため、補完後のパスだけ追加確認する
    completed = path.with_name(path.name + suffix)
    if completed.exists():
        answer = QMessageBox.question(
            parent,
            "上書き確認",
            f"{completed.name} は既に存在します。上書きしますか？",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No,
        )
        if answer != QMessageBox.StandardButton.Yes:
            return None
    return completed


def export_csv_with_dialog(
    parent: QWidget,
    label: str,
    default_prefix: str,
    export: Callable[[Path], None],
) -> Path | None:
    path = _choose_export_path(
        parent,
        label,
        default_prefix,
        ".csv",
        "CSV ファイル (*.csv)",
    )
    return _run_export(parent, label, path, ".csv", export)


def export_file_with_dialog(
    parent: QWidget,
    label: str,
    default_prefix: str,
    extension: str,
    file_filter: str,
    export: Callable[[Path], None],
) -> Path | None:
    path = _choose_export_path(parent, label, default_prefix, extension, file_filter)
    return _run_export(parent, label, path, extension, export)


def _run_export(
    parent: QWidget,
    label: str,
    path: Path | None,
    extension: str,
    export: Callable[[Path], None],
) -> Path | None:
    if path is None:
        return None

    output_type = extension.removeprefix(".").upper()

    def run_export() -> None:
        logger.info("%s出力を開始します: 種別=%s 保存先=%s", output_type, label, path)
        try:
            export(path)
        except Exception:
            logger.error(
                "%s出力に失敗しました: 種別=%s 保存先=%s",
                output_type,
                label,
                path,
                exc_info=True,
            )
            raise
        logger.info("%s出力が完了しました: 種別=%s 保存先=%s", output_type, label, path)

    QApplication.setOverrideCursor(Qt.CursorShape.WaitCursor)
    try:
        succeeded, _result = run_guarded(parent, run_export)
    finally:
        QApplication.restoreOverrideCursor()
    return path if succeeded else None
