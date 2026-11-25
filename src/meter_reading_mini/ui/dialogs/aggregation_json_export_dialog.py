"""全対象の使用量を指定した期間で JSON 出力する。"""

from __future__ import annotations

from datetime import date, timedelta
from pathlib import Path

from PySide6.QtWidgets import (
    QComboBox,
    QDateEdit,
    QDialog,
    QFormLayout,
    QHBoxLayout,
    QLabel,
    QMessageBox,
    QPushButton,
    QSpinBox,
    QStackedWidget,
    QVBoxLayout,
    QWidget,
)

from meter_reading_mini.app_context import AppContext
from meter_reading_mini.core.aggregation_contracts import AggregationPeriodUnit
from meter_reading_mini.core.aggregation_export_contracts import (
    AggregationExportPeriodSpec,
    AggregationExportSnapshot,
)
from meter_reading_mini.core.aggregation_json import aggregation_period_label
from meter_reading_mini.core.reports import period_boundaries
from meter_reading_mini.ui.aggregation_export import (
    aggregation_json_filename,
    save_aggregation_json,
)
from meter_reading_mini.ui.csv_export import export_file_with_dialog
from meter_reading_mini.ui.error_handling import run_guarded
from meter_reading_mini.ui.widgets.form_helpers import python_date, qdate


class AggregationJsonExportDialog(QDialog):
    def __init__(self, services: AppContext, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._services = services
        self._today = services.current_datetime().date()
        self.setWindowTitle("全対象の使用量を JSON 出力")
        self.resize(540, 240)

        layout = QVBoxLayout(self)
        form = QFormLayout()
        self.unit_combo = QComboBox()
        for unit, label in (
            (AggregationPeriodUnit.DAY, "日"),
            (AggregationPeriodUnit.MONTH, "月"),
            (AggregationPeriodUnit.CALENDAR_YEAR, "暦年"),
            (AggregationPeriodUnit.FISCAL_YEAR, "年度"),
        ):
            self.unit_combo.addItem(label, unit.value)
        form.addRow("集計単位", self.unit_combo)

        self.period_inputs = QStackedWidget()
        self.day_edit = QDateEdit()
        self.day_edit.setCalendarPopup(True)
        self.day_edit.setDisplayFormat("yyyy/MM/dd")
        self.day_edit.setDateRange(qdate(date.min), qdate(min(self._today, date(9999, 12, 30))))
        self.day_edit.setDate(qdate(min(self._today, date(9999, 12, 30))))
        self.month_edit = QDateEdit()
        self.month_edit.setDisplayFormat("yyyy/MM")
        month = min(self._today.replace(day=1), date(9999, 11, 1))
        self.month_edit.setDateRange(qdate(date.min), qdate(month))
        self.month_edit.setDate(qdate(month))
        self.year_edit = QSpinBox()
        self.year_edit.setRange(1, min(self._today.year, 9998))
        self.year_edit.setValue(min(self._today.year, 9998))
        self.year_edit.setSuffix("年")
        self.fiscal_year_edit = QSpinBox()
        fiscal_year = self._today.year if self._today.month >= 4 else self._today.year - 1
        self.fiscal_year_edit.setRange(1, max(1, min(fiscal_year, 9998)))
        self.fiscal_year_edit.setValue(max(1, min(fiscal_year, 9998)))
        self.fiscal_year_edit.setSuffix("年度")
        for widget in (self.day_edit, self.month_edit, self.year_edit, self.fiscal_year_edit):
            self.period_inputs.addWidget(widget)
        form.addRow("指定値", self.period_inputs)
        layout.addLayout(form)

        self.period_label = QLabel()
        self.period_label.setWordWrap(True)
        layout.addWidget(self.period_label)
        self.notice_label = QLabel()
        self.notice_label.setWordWrap(True)
        layout.addWidget(self.notice_label)
        scope = QLabel("全目的・全測定定義・全集計式を出力します。")
        scope.setWordWrap(True)
        layout.addWidget(scope)
        buttons = QHBoxLayout()
        buttons.addStretch()
        self.save_button = QPushButton("保存…")
        close_button = QPushButton("閉じる")
        buttons.addWidget(self.save_button)
        buttons.addWidget(close_button)
        layout.addLayout(buttons)

        self.unit_combo.currentIndexChanged.connect(self._update_period)
        self.day_edit.dateChanged.connect(self._update_period)
        self.month_edit.dateChanged.connect(self._update_period)
        self.year_edit.valueChanged.connect(self._update_period)
        self.fiscal_year_edit.valueChanged.connect(self._update_period)
        self.save_button.clicked.connect(lambda: run_guarded(self, self._save))
        close_button.clicked.connect(self.reject)
        self._update_period()

    def _spec(self) -> AggregationExportPeriodSpec:
        unit = AggregationPeriodUnit(self.unit_combo.currentData())
        if unit is AggregationPeriodUnit.DAY:
            anchor = python_date(self.day_edit.date())
        elif unit is AggregationPeriodUnit.MONTH:
            anchor = python_date(self.month_edit.date()).replace(day=1)
        elif unit is AggregationPeriodUnit.CALENDAR_YEAR:
            anchor = date(self.year_edit.value(), 1, 1)
        else:
            anchor = date(self.fiscal_year_edit.value(), 4, 1)
        return AggregationExportPeriodSpec(unit, anchor)

    def _update_period(self, *_args: object) -> None:
        self.period_inputs.setCurrentIndex(self.unit_combo.currentIndex())
        spec = self._spec()
        start, end = period_boundaries(spec.anchor, spec.unit)
        self.period_label.setText(
            f"{start.isoformat()} 〜 {(end - timedelta(days=1)).isoformat()}"
            f" ({aggregation_period_label(spec.unit, start)})"
        )
        self.save_button.setEnabled(start <= self._today)
        if start > self._today:
            self.notice_label.setText("未来の集計期間は出力できません。")
        elif spec.unit is AggregationPeriodUnit.DAY and start == self._today:
            self.notice_label.setText("当日分は締め前の可能性があります。")
        elif spec.unit is not AggregationPeriodUnit.DAY and start <= self._today < end:
            self.notice_label.setText("進行中の期間は現在業務日までの途中値です。")
        else:
            self.notice_label.clear()

    def _save(self) -> None:
        spec = self._spec()
        default_name = aggregation_json_filename(spec, self._services.current_datetime())
        snapshot: AggregationExportSnapshot | None = None

        def export(path: Path) -> None:
            nonlocal snapshot
            snapshot = self._services.aggregation_reports.get_export_snapshot(spec)
            save_aggregation_json(path, snapshot)

        path = export_file_with_dialog(
            self,
            "全対象の使用量集計",
            "meter-usage",
            ".json",
            "JSON ファイル (*.json)",
            export,
            default_path=Path(default_name),
        )
        if path is not None and snapshot is not None:
            period = snapshot.period
            count = len(snapshot.definitions) + len(snapshot.formulas)
            QMessageBox.information(
                self,
                "JSON 出力",
                f"JSON を保存しました。\n保存先: {path}\n"
                f"集計期間: {period.start.isoformat()} 〜 "
                f"{(period.end - timedelta(days=1)).isoformat()}\n出力件数: {count}",
            )
