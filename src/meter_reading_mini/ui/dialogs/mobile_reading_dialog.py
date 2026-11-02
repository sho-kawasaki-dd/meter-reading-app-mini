"""スマホ検針シートの出力と結果取込を行う画面。"""

from __future__ import annotations

from pathlib import Path

from PySide6.QtCore import Qt, QTimer, Signal
from PySide6.QtGui import QHideEvent, QShowEvent
from PySide6.QtWidgets import (
    QCheckBox,
    QDateEdit,
    QDialog,
    QFormLayout,
    QGridLayout,
    QGroupBox,
    QLabel,
    QMessageBox,
    QPushButton,
    QTabWidget,
    QVBoxLayout,
    QWidget,
)

from meter_reading_mini.app_context import AppContext
from meter_reading_mini.core.contract_models import ReadingCycle, ReadingSession
from meter_reading_mini.core.errors import FileOutputError
from meter_reading_mini.core.mobile_reading import CYCLE_GROUPS
from meter_reading_mini.core.mobile_reading_sheet import FutureSheetBusinessDateError
from meter_reading_mini.ui.atomic_write import atomic_write_text
from meter_reading_mini.ui.csv_export import export_file_with_dialog
from meter_reading_mini.ui.dialog_placement import center_dialog
from meter_reading_mini.ui.error_handling import run_guarded
from meter_reading_mini.ui.sheet_save_directory_memory import SheetSaveDirectoryMemory
from meter_reading_mini.ui.widgets.form_helpers import python_date, qdate
from meter_reading_mini.ui.widgets.master_common import scroll_area

_CYCLE_GROUP_LABELS = {
    "daily": "日次",
    ReadingCycle.WEEKLY.value: "週次",
    ReadingCycle.MONTHLY.value: "月次",
    ReadingCycle.EVERY_TWO_MONTHS.value: "隔月",
}


def _save_sheet_file(path: Path, html: str) -> None:
    try:
        atomic_write_text(path, html)
    except OSError as error:
        raise FileOutputError(
            f"検針シートを保存できませんでした。\n保存先: {path}\n"
            "保存先やアクセス権を確認してください。"
        ) from error


class MobileReadingDialog(QDialog):
    reload_requested = Signal()

    def __init__(
        self,
        services: AppContext,
        save_directory_memory: SheetSaveDirectoryMemory,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self._services = services
        self._save_directory_memory = save_directory_memory
        self.setWindowTitle("スマホ検針")
        self.setModal(True)
        self.resize(760, 650)

        layout = QVBoxLayout(self)
        self.tabs = QTabWidget()
        self.tabs.setObjectName("mobileReadingTabs")
        self.tabs.addTab(scroll_area(self._build_sheet_page()), "シート出力")
        result_page = QWidget()
        result_layout = QVBoxLayout(result_page)
        result_layout.addWidget(QLabel("結果取込・確定は未実装です。"))
        result_layout.addStretch()
        self.tabs.addTab(scroll_area(result_page), "結果取込・確定")
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
            check = QCheckBox(_CYCLE_GROUP_LABELS[group])
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
        self._date_timer.start()
        center_dialog(self)

    def hideEvent(self, event: QHideEvent) -> None:
        self._date_timer.stop()
        super().hideEvent(event)
