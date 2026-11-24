"""測定定義ごとの検針履歴を閲覧・出力する。"""

from __future__ import annotations

from datetime import UTC, datetime, tzinfo
from pathlib import Path

from PySide6.QtCore import QDate, QSignalBlocker, Signal
from PySide6.QtWidgets import (
    QAbstractItemView,
    QCheckBox,
    QComboBox,
    QDateEdit,
    QDialog,
    QGridLayout,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QMessageBox,
    QPushButton,
    QTableView,
    QVBoxLayout,
    QWidget,
)

from meter_reading_mini.app_context import AppContext
from meter_reading_mini.core.contract_models import CalculationMethod, Category, ValueSchema
from meter_reading_mini.core.errors import ValidationError
from meter_reading_mini.core.reading_history import default_history_start_date
from meter_reading_mini.core.reading_history_contracts import (
    ReadingHistory,
    ReadingHistoryCode,
    ReadingHistoryDefinitionOption,
    ReadingHistoryIssue,
    ReadingHistoryQuery,
)
from meter_reading_mini.ui.csv_export import export_csv_with_dialog
from meter_reading_mini.ui.dialogs.master_dialog import MasterDialog
from meter_reading_mini.ui.error_handling import run_guarded
from meter_reading_mini.ui.models.reading_history_model import (
    ReadingHistoryTableModel,
    decimal_text,
    local_datetime,
)
from meter_reading_mini.ui.models.reading_overview_model import CATEGORY_LABELS
from meter_reading_mini.ui.reading_history_export import (
    history_filename_prefix,
    save_reading_history_csv,
)
from meter_reading_mini.ui.staff_selection_memory import StaffSelectionMemory
from meter_reading_mini.ui.widgets.form_helpers import python_date, qdate, selected_value


class ReadingHistoryDialog(QDialog):
    data_changed = Signal()

    def __init__(
        self,
        services: AppContext,
        staff_memory: StaffSelectionMemory,
        parent: QWidget | None = None,
        *,
        definition_id: str | None = None,
    ) -> None:
        super().__init__(parent)
        self._services = services
        self._staff_memory = staff_memory
        self._history: ReadingHistory | None = None
        self._timezone: tzinfo = UTC
        self._definitions: tuple[ReadingHistoryDefinitionOption, ...] = ()
        self._pending: list[ReadingHistoryIssue] = []
        self.setWindowTitle("検針履歴")
        self.resize(1200, 700)
        layout = QVBoxLayout(self)
        filters = QGridLayout()
        self.location_combo = QComboBox()
        self.category_combo = QComboBox()
        self.category_combo.addItem("すべて", "")
        for category in Category:
            self.category_combo.addItem(CATEGORY_LABELS[category], category.value)
        self.definition_combo = QComboBox()
        self.inactive_check = QCheckBox("無効化済みを含む")
        self.superseded_check = QCheckBox("差替え済みの枠を表示")
        today = services.current_datetime().date()
        self.start_date = QDateEdit(qdate(default_history_start_date(today)))
        self.end_date = QDateEdit(qdate(today))
        for edit in (self.start_date, self.end_date):
            edit.setCalendarPopup(True)
            edit.setDisplayFormat("yyyy/MM/dd")
            edit.setDateRange(QDate(1, 1, 1), qdate(today))
        for column, (label, widget) in enumerate(
            (
                ("場所", self.location_combo),
                ("カテゴリ", self.category_combo),
                ("測定定義", self.definition_combo),
            )
        ):
            filters.addWidget(QLabel(label), 0, column * 2)
            filters.addWidget(widget, 0, column * 2 + 1)
        filters.addWidget(self.inactive_check, 1, 0, 1, 2)
        filters.addWidget(QLabel("開始業務日"), 1, 2)
        filters.addWidget(self.start_date, 1, 3)
        filters.addWidget(QLabel("終了業務日（含む）"), 1, 4)
        filters.addWidget(self.end_date, 1, 5)
        filters.addWidget(self.superseded_check, 2, 0, 1, 2)
        self.refresh_button = QPushButton("表示")
        filters.addWidget(self.refresh_button, 2, 2)
        layout.addLayout(filters)
        self.status_label = QLabel()
        self.status_label.setWordWrap(True)
        layout.addWidget(self.status_label)
        self.table = QTableView()
        self.table.setObjectName("readingHistoryTable")
        self.table_model = ReadingHistoryTableModel()
        self.table.setModel(self.table_model)
        self.table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.table.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.ResizeToContents)
        layout.addWidget(self.table, 1)
        correction = QHBoxLayout()
        self.pending_combo = QComboBox()
        self.correction_button = QPushButton("計算条件の訂正")
        correction.addWidget(QLabel("確認待ちの割当"))
        correction.addWidget(self.pending_combo, 1)
        correction.addWidget(self.correction_button)
        layout.addLayout(correction)
        self.pending_reason = QLabel()
        self.pending_reason.setWordWrap(True)
        layout.addWidget(self.pending_reason)
        self.csv_button = QPushButton("表示中の履歴を CSV 出力")
        layout.addWidget(self.csv_button)
        notice = QLabel(
            "使用量は保存済み使用量の単純合計です。CSV の数値を表計算ソフトで開くと"
            "精度が失われる場合があります。正確に確認するには文字列として取り込んでください。"
        )
        notice.setWordWrap(True)
        layout.addWidget(notice)
        close_button = QPushButton("閉じる")
        close_button.clicked.connect(self.accept)
        layout.addWidget(close_button)

        self._reload_definitions(definition_id)
        for combo in (self.location_combo, self.category_combo):
            combo.currentIndexChanged.connect(self._filters_changed)
        self.inactive_check.toggled.connect(self._inactive_changed)
        self.definition_combo.currentIndexChanged.connect(self.invalidate)
        self.superseded_check.toggled.connect(self.invalidate)
        self.start_date.dateChanged.connect(self.invalidate)
        self.end_date.dateChanged.connect(self.invalidate)
        self.refresh_button.clicked.connect(self.refresh)
        self.pending_combo.currentIndexChanged.connect(self._pending_changed)
        self.correction_button.clicked.connect(lambda: run_guarded(self, self._open_correction))
        self.csv_button.clicked.connect(lambda: run_guarded(self, self._export_csv))
        self.invalidate()

    def _reload_definitions(self, initial_id: str | None = None) -> None:
        selected = initial_id if initial_id is not None else self.definition_combo.currentData()
        if initial_id:
            all_definitions = self._services.reading_history.list_definitions(include_inactive=True)
            if any(
                d.measurement_definition_id == initial_id and not d.is_active
                for d in all_definitions
            ):
                with QSignalBlocker(self.inactive_check):
                    self.inactive_check.setChecked(True)
        self._definitions = self._services.reading_history.list_definitions(
            include_inactive=self.inactive_check.isChecked()
        )
        current_location = self.location_combo.currentData()
        locations = {item.location_id: item.location_name for item in self._definitions}
        with QSignalBlocker(self.location_combo):
            self.location_combo.clear()
            self.location_combo.addItem("すべて", "")
            for key, name in sorted(locations.items(), key=lambda item: (item[1], item[0])):
                self.location_combo.addItem(f"{name} [{key}]", key)
            self.location_combo.setCurrentIndex(
                max(self.location_combo.findData(current_location), 0)
            )
        self._populate_definitions(selected)

    def _populate_definitions(self, selected: object) -> None:
        location, category = self.location_combo.currentData(), self.category_combo.currentData()
        with QSignalBlocker(self.definition_combo):
            self.definition_combo.clear()
            self.definition_combo.addItem("測定定義を選択してください", "")
            for item in self._definitions:
                if location and item.location_id != location:
                    continue
                if category and item.category.value != category:
                    continue
                inactive = "（無効）" if not item.is_active else ""
                self.definition_combo.addItem(
                    f"{item.display_name}{inactive} / {item.location_name} "
                    f"[{item.measurement_definition_id}]",
                    item.measurement_definition_id,
                )
            self.definition_combo.setCurrentIndex(max(self.definition_combo.findData(selected), 0))

    def _filters_changed(self, *_args: object) -> None:
        self.invalidate()
        self._populate_definitions(self.definition_combo.currentData())

    def _inactive_changed(self, *_args: object) -> None:
        self.invalidate()
        run_guarded(self, self._reload_definitions)

    def invalidate(self, *_args: object) -> None:
        self._history = None
        self.table_model.set_rows((), UTC)
        self._pending.clear()
        self.pending_combo.clear()
        self.pending_reason.clear()
        self.correction_button.setEnabled(False)
        self.csv_button.setEnabled(False)
        self.status_label.setText("履歴は未取得です。条件を確認して「表示」を押してください。")

    def invalidate_for_reload(self) -> None:
        self.invalidate()
        today = qdate(self._services.current_datetime().date())
        with QSignalBlocker(self.start_date), QSignalBlocker(self.end_date):
            self.start_date.setMaximumDate(today)
            self.end_date.setMaximumDate(today)
        self._reload_definitions()

    def refresh(self) -> None:
        self.invalidate()
        succeeded, _ = run_guarded(self, self._load_history)
        if not succeeded:
            self.invalidate()
            self.status_label.setText("履歴の取得に失敗しました。条件やデータを確認してください。")

    def _load_history(self) -> None:
        query = ReadingHistoryQuery(
            selected_value(self.definition_combo, "測定定義"),
            python_date(self.start_date.date()),
            python_date(self.end_date.date()),
            self.superseded_check.isChecked(),
        )
        history = self._services.reading_history.get_history(query)
        timezone = self._services.timezone_settings.get_timezone()
        self.table_model.set_rows(history.rows, timezone)
        self._history, self._timezone = history, timezone
        definition = history.definition
        eligible = (
            definition.calculation_method is CalculationMethod.DELTA
            and definition.value_schema is ValueSchema.DECIMAL_STRING
        )
        total = (decimal_text(history.total_usage) or "値なし") if eligible else "集計対象外"
        self.status_label.setText(
            f"{len(history.rows)} 件 / 保存済み使用量の単純合計: {total} "
            f"{definition.unit if eligible else ''} / 警告 {history.warning_count} 件"
        )
        seen: set[tuple[str | None, datetime | None]] = set()
        for row in history.rows:
            for issue in row.issues:
                key = (issue.meter_id, issue.assignment_effective_from)
                if issue.code is ReadingHistoryCode.ASSIGNMENT_PENDING and key not in seen:
                    seen.add(key)
                    self._pending.append(issue)
                    self.pending_combo.addItem(
                        f"{issue.meter_id} / "
                        f"{local_datetime(issue.assignment_effective_from, timezone)}"
                    )
        self._pending_changed()
        self.csv_button.setEnabled(True)

    def _pending_changed(self, *_args: object) -> None:
        index = self.pending_combo.currentIndex()
        issue = self._pending[index] if 0 <= index < len(self._pending) else None
        self.pending_reason.setText(issue.message if issue else "")
        self.correction_button.setEnabled(
            issue is not None
            and issue.meter_id is not None
            and issue.assignment_effective_from is not None
        )

    def _open_correction(self) -> None:
        index = self.pending_combo.currentIndex()
        if self._history is None or not 0 <= index < len(self._pending):
            raise ValidationError("訂正する確認待ちの割当を選択してください。")
        issue = self._pending[index]
        if issue.meter_id is None or issue.assignment_effective_from is None:
            raise ValidationError("確認待ちの割当を特定できません。")
        definition_id = self._history.definition.measurement_definition_id
        dialog = MasterDialog(self._services, self._staff_memory, self)
        dialog.show_assignment_correction()
        dialog.correction_page.select_assignment(
            definition_id, issue.meter_id, issue.assignment_effective_from
        )
        dialog.saved.connect(self._correction_saved)
        dialog.exec()

    def _correction_saved(self) -> None:
        self.invalidate()
        run_guarded(self, self.invalidate_for_reload)
        self.data_changed.emit()

    def _export_csv(self) -> None:
        history, timezone = self._history, self._timezone
        if history is None:
            raise ValidationError("履歴を再取得してください。")

        def export(path: Path) -> None:
            if self._history is not history:
                raise ValidationError("条件またはデータが変わりました。履歴を再取得してください。")
            save_reading_history_csv(path, history, timezone)

        path = export_csv_with_dialog(
            self,
            "検針履歴",
            history_filename_prefix(history),
            export,
        )
        if path is not None:
            QMessageBox.information(self, "CSV 出力", f"CSV を保存しました。\n{path}")
