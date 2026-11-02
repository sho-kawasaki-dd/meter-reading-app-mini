"""スマホ検針シートの出力と結果取込を行う画面。"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import NoReturn

from PySide6.QtCore import Qt, QTimer, Signal
from PySide6.QtGui import QHideEvent, QShowEvent
from PySide6.QtWidgets import (
    QAbstractItemView,
    QApplication,
    QCheckBox,
    QDateEdit,
    QDialog,
    QFileDialog,
    QFormLayout,
    QGridLayout,
    QGroupBox,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QMessageBox,
    QPlainTextEdit,
    QPushButton,
    QTableView,
    QTabWidget,
    QVBoxLayout,
    QWidget,
)

from meter_reading_mini.app_context import AppContext
from meter_reading_mini.core.contract_models import ReadingSession
from meter_reading_mini.core.errors import DomainError, FileOutputError, ValidationError
from meter_reading_mini.core.mobile_reading import CYCLE_GROUPS, parse_mobile_reading_result
from meter_reading_mini.core.mobile_reading_import import (
    MobileReadingImportPreview,
    MobileReadingPreviewChangedError,
)
from meter_reading_mini.core.mobile_reading_sheet import FutureSheetBusinessDateError
from meter_reading_mini.ui.atomic_write import atomic_write_text
from meter_reading_mini.ui.csv_export import export_file_with_dialog
from meter_reading_mini.ui.dialog_placement import center_dialog
from meter_reading_mini.ui.error_handling import run_guarded
from meter_reading_mini.ui.models.mobile_reading_preview_model import (
    MobileReadingPreviewTableModel,
    confirmation_text,
    preview_summary_text,
)
from meter_reading_mini.ui.models.reading_overview_model import CYCLE_GROUP_LABELS
from meter_reading_mini.ui.sheet_save_directory_memory import SheetSaveDirectoryMemory
from meter_reading_mini.ui.staff_selection_memory import StaffSelectionMemory
from meter_reading_mini.ui.widgets.form_helpers import python_date, qdate, selected_value
from meter_reading_mini.ui.widgets.master_common import confirm, scroll_area
from meter_reading_mini.ui.widgets.staff_combo import StaffComboBox


def _save_sheet_file(path: Path, html: str) -> None:
    try:
        atomic_write_text(path, html)
    except OSError as error:
        raise FileOutputError(
            f"検針シートを保存できませんでした。\n保存先: {path}\n"
            "保存先やアクセス権を確認してください。"
        ) from error


def _reject_json_constant(value: str) -> NoReturn:
    raise ValueError(f"JSON で許可されない数値です: {value}")


@dataclass(frozen=True, slots=True)
class _DisplayedPreview:
    text: str
    preview: MobileReadingImportPreview


class MobileReadingDialog(QDialog):
    reload_requested = Signal()

    def __init__(
        self,
        services: AppContext,
        save_directory_memory: SheetSaveDirectoryMemory,
        staff_memory: StaffSelectionMemory,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self._services = services
        self._save_directory_memory = save_directory_memory
        self._displayed_preview: _DisplayedPreview | None = None
        self._preview_revision = 0
        self._staff_revision = 0
        self.setWindowTitle("スマホ検針")
        self.setModal(True)
        self.resize(760, 650)

        layout = QVBoxLayout(self)
        self.tabs = QTabWidget()
        self.tabs.setObjectName("mobileReadingTabs")
        self.tabs.addTab(scroll_area(self._build_sheet_page()), "シート出力")
        self.tabs.addTab(scroll_area(self._build_result_page(staff_memory)), "結果取込・確定")
        layout.addWidget(self.tabs)
        self.close_button = QPushButton("閉じる")
        self.close_button.clicked.connect(self.accept)
        layout.addWidget(self.close_button)

        self._date_timer = QTimer(self)
        self._date_timer.setInterval(60_000)
        self._date_timer.timeout.connect(lambda: run_guarded(self, self._refresh_date_limit))

    def _build_sheet_page(self) -> QWidget:
        page = QWidget()
        layout = QVBoxLayout(page)
        form = QFormLayout()
        today = self._services.current_datetime().date()
        self.business_date_edit = QDateEdit()
        self.business_date_edit.setObjectName("mobileSheetBusinessDate")
        self.business_date_edit.setCalendarPopup(True)
        self.business_date_edit.setDisplayFormat("yyyy/MM/dd")
        self.business_date_edit.setMaximumDate(qdate(today))
        self.business_date_edit.setDate(qdate(today))
        form.addRow("対象業務日", self.business_date_edit)
        layout.addLayout(form)

        explanation = QLabel("検針時間・周期区分はそれぞれ複数選択できます。未選択は全件です。")
        explanation.setWordWrap(True)
        layout.addWidget(explanation)

        session_group = QGroupBox("検針時間")
        session_layout = QGridLayout(session_group)
        self.session_checks: dict[ReadingSession, QCheckBox] = {}
        for index, session in enumerate(ReadingSession):
            check = QCheckBox(session.value)
            check.setObjectName(f"mobileSheetSession{session.value.replace(':', '')}")
            self.session_checks[session] = check
            session_layout.addWidget(check, index // 3, index % 3)
        layout.addWidget(session_group)

        cycle_group = QGroupBox("周期区分")
        cycle_layout = QGridLayout(cycle_group)
        self.cycle_checks: dict[str, QCheckBox] = {}
        for index, group in enumerate(CYCLE_GROUPS):
            check = QCheckBox(CYCLE_GROUP_LABELS[group])
            check.setObjectName(f"mobileSheetCycle_{group}")
            self.cycle_checks[group] = check
            cycle_layout.addWidget(check, index // 2, index % 2)
        layout.addWidget(cycle_group)

        self.export_button = QPushButton("HTML シートを出力")
        self.export_button.setObjectName("mobileSheetExport")
        self.export_button.clicked.connect(lambda: run_guarded(self, self._export_sheet))
        layout.addWidget(self.export_button, alignment=Qt.AlignmentFlag.AlignLeft)
        layout.addStretch()
        return page

    def _build_result_page(self, staff_memory: StaffSelectionMemory) -> QWidget:
        page = QWidget()
        layout = QVBoxLayout(page)
        explanation = QLabel(
            "スマートフォンの結果テキストを貼り付けるか、結果 JSON を読み込んでください。"
            "手編集した場合は「プレビュー」を押してください。"
        )
        explanation.setWordWrap(True)
        layout.addWidget(explanation)

        buttons = QHBoxLayout()
        self.paste_button = QPushButton("クリップボードから貼付")
        self.paste_button.setObjectName("mobileResultPaste")
        self.load_json_button = QPushButton("結果 JSON を読込")
        self.load_json_button.setObjectName("mobileResultLoadJson")
        self.preview_button = QPushButton("プレビュー")
        self.preview_button.setObjectName("mobileResultPreview")
        for button in (self.paste_button, self.load_json_button, self.preview_button):
            buttons.addWidget(button)
        buttons.addStretch()
        layout.addLayout(buttons)

        self.result_edit = QPlainTextEdit()
        self.result_edit.setObjectName("mobileResultText")
        self.result_edit.setPlaceholderText("MR1 結果テキストまたは結果 JSON")
        self.result_edit.setMinimumHeight(110)
        layout.addWidget(self.result_edit)

        self.summary_label = QLabel("入力内容をプレビューしてください。")
        self.summary_label.setObjectName("mobileResultSummary")
        self.summary_label.setWordWrap(True)
        self.summary_label.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        layout.addWidget(self.summary_label)

        self.preview_model = MobileReadingPreviewTableModel()
        self.preview_table = QTableView()
        self.preview_table.setObjectName("mobileResultPreviewTable")
        self.preview_table.setModel(self.preview_model)
        self.preview_table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.preview_table.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        self.preview_table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.preview_table.setWordWrap(True)
        self.preview_table.setMinimumHeight(220)
        horizontal_header = self.preview_table.horizontalHeader()
        horizontal_header.setMaximumSectionSize(450)
        horizontal_header.setSectionResizeMode(QHeaderView.ResizeMode.ResizeToContents)
        self.preview_table.verticalHeader().setSectionResizeMode(
            QHeaderView.ResizeMode.ResizeToContents
        )
        layout.addWidget(self.preview_table)

        staff_form = QFormLayout()
        self.staff_combo = StaffComboBox(self._services.staff, staff_memory)
        self.staff_combo.setObjectName("mobileResultStaff")
        staff_form.addRow("確定担当者", self.staff_combo)
        layout.addLayout(staff_form)
        self.commit_button = QPushButton("採用対象を一括確定")
        self.commit_button.setObjectName("mobileResultCommit")
        self.commit_button.setEnabled(False)
        layout.addWidget(self.commit_button, alignment=Qt.AlignmentFlag.AlignLeft)
        layout.addStretch()

        self.result_edit.textChanged.connect(self._invalidate_preview)
        self.staff_combo.currentIndexChanged.connect(self._staff_changed)
        self.paste_button.clicked.connect(lambda: run_guarded(self, self._paste_result))
        self.load_json_button.clicked.connect(lambda: run_guarded(self, self._load_result_json))
        self.preview_button.clicked.connect(lambda: run_guarded(self, self._preview_result))
        self.commit_button.clicked.connect(lambda: run_guarded(self, self._commit_result))
        return page

    def _invalidate_preview(self, message: str = "入力内容をプレビューしてください。") -> None:
        self._preview_revision += 1
        self._displayed_preview = None
        self.commit_button.setEnabled(False)
        self.preview_model.set_preview(None)
        self.summary_label.setText(message)

    def _staff_changed(self) -> None:
        self._staff_revision += 1
        run_guarded(self, self._refresh_commit_enabled)

    def _refresh_commit_enabled(self) -> None:
        self.commit_button.setEnabled(False)
        displayed = self._displayed_preview
        if displayed is None or not displayed.preview.can_commit:
            return
        staff_id = self.staff_combo.currentData()
        if not isinstance(staff_id, str) or not staff_id:
            return
        staff = self._services.staff.get_staff(staff_id)
        self.commit_button.setEnabled(staff is not None and staff.is_active)

    def _paste_result(self) -> None:
        self._invalidate_preview()
        text = QApplication.clipboard().text()
        self.result_edit.setPlainText(text)
        self._preview_result()

    def _load_result_json(self) -> None:
        self._invalidate_preview()
        filename, _ = QFileDialog.getOpenFileName(
            self, "検針結果 JSON を開く", "", "JSON ファイル (*.json)"
        )
        if not filename:
            return
        path = Path(filename)
        try:
            text = path.read_text(encoding="utf-8-sig")
        except (OSError, UnicodeDecodeError) as error:
            raise ValidationError(
                f"結果 JSON を UTF-8 で読み込めませんでした。\nファイル: {path}"
            ) from error
        self.result_edit.setPlainText(text)
        try:
            data = json.loads(text, parse_constant=_reject_json_constant)
        except ValueError as error:
            raise ValidationError("結果ファイルは正しい JSON 形式で指定してください。") from error
        if not isinstance(data, dict):
            raise ValidationError("結果ファイルは検針結果の JSON オブジェクトを指定してください。")
        self._preview_result()

    def _preview_result(self) -> None:
        self._invalidate_preview()
        try:
            revision = self._preview_revision
            text = self.result_edit.toPlainText()
            result = parse_mobile_reading_result(text)
            preview = self._services.mobile_reading_import.preview(result)
            self.preview_model.set_preview(preview)
            self._ensure_preview_input_current(text, revision)
            self.summary_label.setText(preview_summary_text(preview))
            self._displayed_preview = _DisplayedPreview(text, preview)
            self._refresh_commit_enabled()
            self._ensure_preview_input_current(text, revision)
        except Exception:
            self._invalidate_preview()
            raise

    def _ensure_preview_input_current(self, text: str, revision: int) -> None:
        if self._preview_revision != revision or self.result_edit.toPlainText() != text:
            raise ValidationError(
                "プレビュー中に入力内容が変更されました。再度プレビューしてください。"
            )

    def _ensure_preview_current(
        self, displayed: _DisplayedPreview, staff_id: str, staff_revision: int
    ) -> None:
        if (
            self._displayed_preview is not displayed
            or self.result_edit.toPlainText() != displayed.text
            or self.staff_combo.currentData() != staff_id
            or self._staff_revision != staff_revision
        ):
            raise ValidationError("入力内容または担当者が変更されました。")
        self._services.staff.require_active_staff(staff_id)

    def _commit_result(self) -> None:
        try:
            displayed = self._displayed_preview
            if displayed is None or not displayed.preview.can_commit:
                raise ValidationError("確定できるプレビューがありません。")
            staff_id = selected_value(self.staff_combo, "確定担当者")
            staff_revision = self._staff_revision
            self._ensure_preview_current(displayed, staff_id, staff_revision)
            if not confirm(self, "スマホ検針の一括確定", confirmation_text(displayed.preview)):
                return
            self._ensure_preview_current(displayed, staff_id, staff_revision)
            self._invalidate_preview()
            result = parse_mobile_reading_result(displayed.text)
            committed = self._services.mobile_reading_import.commit(
                result, displayed.preview, staff_id=staff_id
            )
        except DomainError as error:
            self._invalidate_preview("確定できませんでした。再度プレビューしてください。")
            if isinstance(error, MobileReadingPreviewChangedError):
                raise
            raise ValidationError(
                f"確定できませんでした: {error.message}\n再度プレビューしてください。"
            ) from error
        except Exception:
            self._invalidate_preview("確定できませんでした。再度プレビューしてください。")
            raise
        self.reload_requested.emit()
        QMessageBox.information(self, "スマホ検針を確定しました", committed.summary.message)

    def _refresh_date_limit(self) -> None:
        self.business_date_edit.setMaximumDate(qdate(self._services.current_datetime().date()))

    def _export_sheet(self) -> None:
        target_date = python_date(self.business_date_edit.date())
        today = self._services.current_datetime().date()
        self.business_date_edit.setMaximumDate(qdate(today))
        if target_date > today:
            raise FutureSheetBusinessDateError(target_date, today)

        sessions = tuple(
            session.value for session, check in self.session_checks.items() if check.isChecked()
        )
        groups = tuple(group for group, check in self.cycle_checks.items() if check.isChecked())
        try:
            sheet = self._services.mobile_reading_sheet.create_sheet(
                target_date, reading_sessions=sessions or None, cycle_groups=groups or None
            )
        finally:
            self.reload_requested.emit()

        directory = self._save_directory_memory.directory
        default_path = directory / sheet.filename if directory is not None else Path(sheet.filename)
        saved_path = export_file_with_dialog(
            self,
            "検針シート",
            "meter_reading",
            ".html",
            "HTML ファイル (*.html)",
            lambda path: _save_sheet_file(path, sheet.html),
            default_path=default_path,
        )
        if saved_path is None:
            return
        self._save_directory_memory.directory = saved_path.parent
        QMessageBox.information(
            self,
            "検針シートを保存しました",
            f"保存先: {saved_path}\n出力枠数: {sheet.slot_count} 件\n\n"
            "Quick Share でスマートフォンに渡し、Chrome で開いてください。",
        )

    def showEvent(self, event: QShowEvent) -> None:
        super().showEvent(event)
        run_guarded(self, self._refresh_date_limit)
        run_guarded(self, self._refresh_commit_enabled)
        self._date_timer.start()
        center_dialog(self)

    def hideEvent(self, event: QHideEvent) -> None:
        self._date_timer.stop()
        super().hideEvent(event)
