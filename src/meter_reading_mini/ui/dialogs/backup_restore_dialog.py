"""バックアップの保存と復元候補の確認。"""

from __future__ import annotations

from pathlib import Path

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QApplication,
    QDialog,
    QFileDialog,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMessageBox,
    QPlainTextEdit,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from meter_reading_mini.core.backup_restore_contracts import RestorePreview
from meter_reading_mini.core.backup_restore_service import BackupRestoreService
from meter_reading_mini.core.errors import ValidationError
from meter_reading_mini.ui.error_handling import run_guarded


class BackupRestoreDialog(QDialog):
    def __init__(
        self,
        service: BackupRestoreService,
        backup_directory: Path,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self._service = service
        self._backup_directory = backup_directory
        self._preview: RestorePreview | None = None
        self.setWindowTitle("バックアップ・復元")
        self.resize(760, 580)

        layout = QVBoxLayout(self)
        layout.addWidget(QLabel("現在のデータをバックアップするか、復元元を検査してください。"))
        self.backup_button = QPushButton("バックアップを保存")
        layout.addWidget(self.backup_button)

        layout.addWidget(QLabel("復元元の DB"))
        source_row = QHBoxLayout()
        self.source_edit = QLineEdit()
        self.source_edit.setObjectName("restoreSource")
        self.source_edit.setPlaceholderText("バックアップファイルのパス")
        self.browse_button = QPushButton("参照")
        source_row.addWidget(self.source_edit, stretch=1)
        source_row.addWidget(self.browse_button)
        layout.addLayout(source_row)

        self.inspect_button = QPushButton("復元候補を検査")
        layout.addWidget(self.inspect_button)
        layout.addWidget(QLabel("検査結果と警告"))
        self.preview_text = QPlainTextEdit()
        self.preview_text.setObjectName("restorePreview")
        self.preview_text.setReadOnly(True)
        layout.addWidget(self.preview_text, stretch=1)

        buttons = QHBoxLayout()
        buttons.addStretch()
        self.restore_button = QPushButton("内容を確認して復元")
        self.restore_button.setEnabled(False)
        self.close_button = QPushButton("閉じる")
        buttons.addWidget(self.restore_button)
        buttons.addWidget(self.close_button)
        layout.addLayout(buttons)

        self.backup_button.clicked.connect(lambda: run_guarded(self, self._save_backup))
        self.browse_button.clicked.connect(lambda: run_guarded(self, self._choose_source))
        self.inspect_button.clicked.connect(lambda: run_guarded(self, self._inspect_source))
        self.source_edit.textChanged.connect(lambda _text: run_guarded(self, self._discard_preview))
        self.restore_button.clicked.connect(lambda: run_guarded(self, self._confirm_restore))
        self.close_button.clicked.connect(self.reject)

    def _save_backup(self) -> None:
        default_path = self._backup_directory / self._service.default_backup_filename()
        selected, _ = QFileDialog.getSaveFileName(
            self,
            "バックアップを保存",
            str(default_path),
            "データベース (*.db);;すべてのファイル (*)",
            options=QFileDialog.Option.DontConfirmOverwrite,
        )
        if not selected:
            return
        path = Path(selected)
        if not path.suffix:
            path = path.with_suffix(".db")
        QApplication.setOverrideCursor(Qt.CursorShape.WaitCursor)
        try:
            self._service.create_backup(path)
        finally:
            QApplication.restoreOverrideCursor()
        QMessageBox.information(self, "バックアップ", f"バックアップを保存しました。\n{path}")

    def _discard_preview(self) -> None:
        had_preview = self._preview is not None
        self._preview = None
        self.preview_text.clear()
        self.restore_button.setEnabled(False)
        if had_preview:
            self._service.discard_preview()

    def _choose_source(self) -> None:
        self._discard_preview()
        selected, _ = QFileDialog.getOpenFileName(
            self,
            "復元元を選択",
            self.source_edit.text() or str(self._backup_directory),
            "データベース (*.db);;すべてのファイル (*)",
        )
        if selected:
            self.source_edit.setText(selected)

    def _inspect_source(self) -> None:
        self._discard_preview()
        source = self.source_edit.text().strip()
        if not source:
            raise ValidationError("復元元の DB を選択してください。")
        QApplication.setOverrideCursor(Qt.CursorShape.WaitCursor)
        try:
            preview = self._service.preview_restore(Path(source))
        finally:
            QApplication.restoreOverrideCursor()
        self._preview = preview
        summary = preview.summary
        warnings = "\n".join(f"・{warning}" for warning in preview.warnings) or "なし"
        self.preview_text.setPlainText(
            f"復元元: {preview.source_path}\n"
            f"元のスキーマ版: {preview.source_schema_version} "
            "(必要な場合は最新版へ移行)\n"
            f"担当者: {summary.staff_count} 件\n"
            f"メーター: {summary.meter_count} 件\n"
            f"測定定義: {summary.definition_count} 件\n"
            f"予定枠: {summary.planned_reading_count} 件\n"
            f"検針結果: {summary.result_count} 件\n"
            f"交換記録: {summary.replacement_count} 件\n"
            f"集計式: {summary.formula_count} 件\n\n"
            f"警告 ({len(preview.warnings)} 件):\n{warnings}"
        )
        self.restore_button.setEnabled(True)

    def _confirm_restore(self) -> None:
        preview = self._preview
        if preview is None:
            raise ValidationError("復元候補を検査してください。")
        answer = QMessageBox.question(
            self,
            "復元の最終確認",
            f"復元元: {preview.source_path}\n"
            f"警告: {len(preview.warnings)} 件\n\n"
            "現在の DB 全体をこの候補で上書きします。現在の DB は自動保全され、"
            "アプリを終了して再起動します。表示設定は復元されません。\n"
            "検査結果と警告を確認して復元しますか？",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No,
        )
        if answer != QMessageBox.StandardButton.Yes:
            self._discard_preview()
            return
        self.accept()

    def reject(self) -> None:
        succeeded, _ = run_guarded(self, self._discard_preview)
        if succeeded:
            super().reject()
