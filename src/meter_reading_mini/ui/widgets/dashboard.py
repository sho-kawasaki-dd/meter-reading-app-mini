"""集計結果を系列ごとに表示し、CSV / PNG に出力する。"""

from __future__ import annotations

import math
from datetime import datetime, timedelta
from decimal import Decimal
from html import escape
from pathlib import Path

from PySide6.QtCharts import QCategoryAxis, QChart, QChartView, QLineSeries, QValueAxis
from PySide6.QtCore import QDate, QSignalBlocker, Qt, Signal
from PySide6.QtGui import QColor, QPainter, QPen
from PySide6.QtWidgets import (
    QComboBox,
    QDateEdit,
    QDialog,
    QGridLayout,
    QHBoxLayout,
    QLabel,
    QMessageBox,
    QPlainTextEdit,
    QPushButton,
    QScrollArea,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

from meter_reading_mini.app_context import AppContext
from meter_reading_mini.core.aggregation_contracts import (
    AggregationCode,
    AggregationIssue,
    AggregationPeriod,
    AggregationPeriodUnit,
    AggregationReport,
    AggregationResult,
    AggregationTargetType,
)
from meter_reading_mini.core.contract_models import Category, Purpose
from meter_reading_mini.core.errors import ValidationError
from meter_reading_mini.ui.aggregation_display import (
    BASELINE_LABELS,
    PERIOD_LABELS,
    SOURCE_LABELS,
    TARGET_LABELS,
    display_value,
    result_state,
)
from meter_reading_mini.ui.aggregation_export import save_aggregation_csv, save_chart_png
from meter_reading_mini.ui.csv_export import export_csv_with_dialog, export_file_with_dialog
from meter_reading_mini.ui.dialogs.master_dialog import MasterDialog
from meter_reading_mini.ui.error_handling import run_guarded
from meter_reading_mini.ui.models.reading_overview_model import (
    CATEGORY_LABELS,
    PURPOSE_LABELS,
    RESULT_STATUS_LABELS,
    TASK_STATE_LABELS,
)
from meter_reading_mini.ui.staff_selection_memory import StaffSelectionMemory
from meter_reading_mini.ui.widgets.form_helpers import python_date, qdate, selected_value
from meter_reading_mini.ui.widgets.master_common import make_table

_Target = tuple[AggregationTargetType, str]
_PeriodResult = tuple[AggregationPeriod, AggregationResult | None]
_STATUS_LABELS = {
    **{status.value: label for status, label in TASK_STATE_LABELS.items()},
    **{status.value: label for status, label in RESULT_STATUS_LABELS.items()},
    "replacement": "交換境界",
}


class DashboardWindow(QDialog):
    data_changed = Signal()

    def __init__(
        self,
        services: AppContext,
        staff_memory: StaffSelectionMemory,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self._services = services
        self._staff_memory = staff_memory
        self._report: AggregationReport | None = None
        self._targets: dict[str, _Target] = {}
        self._rows: list[_PeriodResult] = []
        self._pending_issues: list[AggregationIssue] = []
        self._chart_available = False
        self.setWindowTitle("使用量の集計")
        self.resize(1150, 700)

        outer = QVBoxLayout(self)
        self.scroll_area = QScrollArea()
        self.scroll_area.setWidgetResizable(True)
        content = QWidget()
        self.scroll_area.setWidget(content)
        outer.addWidget(self.scroll_area)
        layout = QVBoxLayout(content)

        filters = QGridLayout()
        today = services.current_datetime().date()
        self.start_date = QDateEdit(qdate(today.replace(day=1)))
        self.end_date = QDateEdit(qdate(today))
        for edit in (self.start_date, self.end_date):
            edit.setCalendarPopup(True)
            edit.setDisplayFormat("yyyy/MM/dd")
            edit.setDateRange(QDate(1, 1, 1), QDate(9999, 12, 31))
        self.unit_combo = QComboBox()
        for unit in AggregationPeriodUnit:
            self.unit_combo.addItem(PERIOD_LABELS[unit], unit.value)
        self.purpose_combo = QComboBox()
        for purpose in Purpose:
            self.purpose_combo.addItem(PURPOSE_LABELS[purpose], purpose.value)
        self.category_combo = QComboBox()
        self.category_combo.addItem("すべて", "")
        for category in Category:
            self.category_combo.addItem(CATEGORY_LABELS[category], category.value)
        self.location_combo = QComboBox()
        self._load_locations()
        for column, (label, widget) in enumerate(
            (
                ("開始業務日", self.start_date),
                ("終了業務日（含む）", self.end_date),
                ("集計単位", self.unit_combo),
            )
        ):
            filters.addWidget(QLabel(label), 0, column * 2)
            filters.addWidget(widget, 0, column * 2 + 1)
        for column, (label, widget) in enumerate(
            (
                ("目的", self.purpose_combo),
                ("カテゴリ", self.category_combo),
                ("場所", self.location_combo),
            )
        ):
            filters.addWidget(QLabel(label), 1, column * 2)
            filters.addWidget(widget, 1, column * 2 + 1)
        layout.addLayout(filters)
        selection = QHBoxLayout()
        self.refresh_button = QPushButton("集計を取得")
        self.target_combo = QComboBox()
        self.target_combo.setObjectName("aggregationTarget")
        selection.addWidget(self.refresh_button)
        selection.addWidget(QLabel("表示する系列"))
        selection.addWidget(self.target_combo, 1)
        layout.addLayout(selection)
        self.status_label = QLabel()
        self.status_label.setWordWrap(True)
        layout.addWidget(self.status_label)
        self.notice_label = QLabel(
            "目的ごとに別系列で表示します。測定定義と式を合算しません。"
            "直接集計には現在の単位・カテゴリ・目的が過去にも適用されます。"
        )
        self.notice_label.setWordWrap(True)
        layout.addWidget(self.notice_label)

        self.chart = QChart()
        self.chart.legend().hide()
        self.chart_view = QChartView(self.chart)
        self.chart_view.setObjectName("aggregationChart")
        self.chart_view.setRenderHint(QPainter.RenderHint.Antialiasing)
        self.chart_view.setMinimumHeight(260)
        layout.addWidget(self.chart_view)
        self.chart_note = QLabel()
        self.chart_note.setWordWrap(True)
        layout.addWidget(self.chart_note)

        self.period_table = make_table(
            (
                "行",
                "開始業務日",
                "終了日（含まない）",
                "表示値",
                "無丸め値",
                "状態",
                "欠測系",
                "警告",
                "情報",
            )
        )
        self.period_table.setObjectName("aggregationPeriodTable")
        layout.addWidget(self.period_table)
        self.detail_text = QPlainTextEdit()
        self.detail_text.setObjectName("aggregationIssues")
        self.detail_text.setReadOnly(True)
        self.detail_text.setMinimumHeight(130)
        layout.addWidget(self.detail_text)
        correction = QHBoxLayout()
        self.pending_combo = QComboBox()
        self.correction_button = QPushButton("割当条件を訂正")
        correction.addWidget(QLabel("確認待ちの割当"))
        correction.addWidget(self.pending_combo, 1)
        correction.addWidget(self.correction_button)
        layout.addLayout(correction)
        self.details_table = make_table(
            (
                "測定定義",
                "場所",
                "明細元",
                "明細 ID",
                "前回基準業務日",
                "基準元",
                "基準 ID",
                "基準読値",
                "今回業務日",
                "元使用量",
                "係数",
                "寄与表示値",
                "寄与無丸め値",
                "状態",
            )
        )
        self.details_table.setObjectName("aggregationDetails")
        layout.addWidget(self.details_table)
        export_row = QHBoxLayout()
        self.csv_button = QPushButton("表示系列を CSV 出力")
        self.png_button = QPushButton("グラフを PNG 出力")
        export_row.addWidget(self.csv_button)
        export_row.addWidget(self.png_button)
        export_row.addStretch()
        layout.addLayout(export_row)
        self.export_notice = QLabel(
            "CSV の無丸め値は、表計算ソフトに数値として取り込むと精度が失われる場合があります。"
            "正確に確認するには文字列として取り込んでください。"
        )
        self.export_notice.setWordWrap(True)
        layout.addWidget(self.export_notice)
        close_button = QPushButton("閉じる")
        close_button.clicked.connect(self.accept)
        outer.addWidget(close_button)

        for edit in (self.start_date, self.end_date):
            edit.dateChanged.connect(self.invalidate)
        for combo in (
            self.unit_combo,
            self.purpose_combo,
            self.category_combo,
            self.location_combo,
        ):
            combo.currentIndexChanged.connect(self.invalidate)
        self.refresh_button.clicked.connect(self.refresh)
        self.target_combo.currentIndexChanged.connect(self._target_changed)
        self.period_table.currentCellChanged.connect(self._period_changed)
        self.correction_button.clicked.connect(lambda: run_guarded(self, self._open_correction))
        self.csv_button.clicked.connect(lambda: run_guarded(self, self._export_csv))
        self.png_button.clicked.connect(lambda: run_guarded(self, self._export_png))
        self.invalidate()
        self.refresh()

    def _load_locations(self) -> None:
        current = self.location_combo.currentData()
        with QSignalBlocker(self.location_combo):
            self.location_combo.clear()
            self.location_combo.addItem("すべて", "")
            for location in self._services.meter_master.list_locations():
                names = " / ".join(location.location_names) or location.location_name
                self.location_combo.addItem(
                    f"{names} [{location.location_id}]", location.location_id
                )
            index = self.location_combo.findData(current if isinstance(current, str) else "")
            self.location_combo.setCurrentIndex(max(index, 0))

    def _clear_chart(self) -> None:
        self.chart.removeAllSeries()
        for axis in self.chart.axes():
            axis.setVisible(False)
            self.chart.removeAxis(axis)
            axis.deleteLater()
        self.chart.setTitle("")
        self._chart_available = False
        self.png_button.setEnabled(False)

    def invalidate(self, *_args: object) -> None:
        self._report = None
        self._targets.clear()
        self._rows.clear()
        self._pending_issues.clear()
        with QSignalBlocker(self.target_combo), QSignalBlocker(self.period_table):
            self.target_combo.clear()
            self.period_table.setRowCount(0)
        self.details_table.setRowCount(0)
        self.detail_text.clear()
        self.pending_combo.clear()
        self.correction_button.setEnabled(False)
        self.csv_button.setEnabled(False)
        self.png_button.setEnabled(False)
        self._clear_chart()
        self.chart_note.clear()
        self.status_label.setText(
            "集計は未取得です。条件を確認して「集計を取得」を押してください。"
        )

    def refresh(self) -> None:
        self.invalidate()
        succeeded, _ = run_guarded(self, self._load_report)
        if not succeeded:
            self.invalidate()
            self.status_label.setText("集計の取得に失敗しました。条件やデータを確認してください。")

    def _load_report(self) -> None:
        start = python_date(self.start_date.date())
        try:
            end = python_date(self.end_date.date()) + timedelta(days=1)
        except OverflowError as error:
            raise ValidationError("終了業務日の翌日を表現できません。") from error
        category_value = self.category_combo.currentData()
        location_value = self.location_combo.currentData()
        if not isinstance(category_value, str) or not isinstance(location_value, str):
            raise ValidationError("カテゴリ・場所を選択してください。")
        report = self._services.aggregation_reports.get_report(
            start,
            end,
            unit=AggregationPeriodUnit(selected_value(self.unit_combo, "集計単位")),
            purpose=Purpose(selected_value(self.purpose_combo, "目的")),
            category=Category(category_value) if category_value else None,
            location_id=location_value or None,
        )
        self._report = report
        with QSignalBlocker(self.target_combo):
            for period in report.periods:
                for result in period.results:
                    key = f"{result.target_type.value}:{result.target_id}"
                    if key not in self._targets:
                        self._targets[key] = (result.target_type, result.target_id)
                        self.target_combo.addItem(
                            f"{TARGET_LABELS[result.target_type]}: {result.display_name} "
                            f"({result.unit}) [{result.target_id}]",
                            key,
                        )
        self.status_label.setText(
            f"現在業務日: {report.as_of}。終了日は含む指定で、集計の終了境界は翌日です。"
            if self._targets
            else "条件に該当する集計対象がありません。"
        )
        self._display_target()

    def _selected_target(self) -> _Target | None:
        key = self.target_combo.currentData()
        return self._targets.get(key) if isinstance(key, str) else None

    def _target_changed(self, *_args: object) -> None:
        succeeded, _ = run_guarded(self, self._display_target)
        if not succeeded:
            self.invalidate()
            self.status_label.setText("系列の表示に失敗しました。集計を再取得してください。")

    def _display_target(self) -> None:
        self._rows.clear()
        self._pending_issues.clear()
        self._clear_chart()
        self.details_table.setRowCount(0)
        self.detail_text.clear()
        self.pending_combo.clear()
        self.correction_button.setEnabled(False)
        self.csv_button.setEnabled(False)
        self.png_button.setEnabled(False)
        with QSignalBlocker(self.period_table):
            self.period_table.setRowCount(0)
        target = self._selected_target()
        if self._report is None or target is None:
            self.chart_note.setText("表示できる系列がありません。")
            return
        for period in self._report.periods:
            result = next(
                (item for item in period.results if (item.target_type, item.target_id) == target),
                None,
            )
            self._rows.append((period, result))
        with QSignalBlocker(self.period_table):
            self.period_table.setRowCount(len(self._rows))
            for row, (period, result) in enumerate(self._rows):
                if result is None:
                    shown, raw = "", ""
                else:
                    shown = display_value(result.value, result.decimal_places)
                    raw = "" if result.value is None else format(result.value, "f")
                state = result_state(result, is_in_progress=period.is_in_progress)
                flags = (
                    (
                        "あり" if result.missing_codes else "",
                        "あり" if result.warning_codes else "",
                        "あり" if result.information_codes else "",
                    )
                    if result is not None
                    else ("", "", "")
                )
                for column, value in enumerate(
                    (
                        str(row + 1),
                        period.start.isoformat(),
                        period.end.isoformat(),
                        shown,
                        raw,
                        state,
                        *flags,
                    )
                ):
                    self.period_table.setItem(row, column, QTableWidgetItem(value))
            if self._rows:
                self.period_table.selectRow(0)
        self.csv_button.setEnabled(True)
        self._draw_chart()
        self._show_period(0 if self._rows else -1)

    def _draw_chart(self) -> None:
        self._clear_chart()
        points: list[float | None] = []
        unrenderable: list[int] = []
        converted_values: dict[float, dict[Decimal, list[int]]] = {}
        target_result: AggregationResult | None = None
        for index, (_, result) in enumerate(self._rows, start=1):
            if result is not None:
                target_result = result
            if result is None or result.value is None:
                points.append(None)
                continue
            value = float(result.value)
            if not math.isfinite(value) or (value == 0.0 and not result.value.is_zero()):
                unrenderable.append(index)
            else:
                converted_values.setdefault(value, {}).setdefault(result.value, []).append(index)
            points.append(value)
        if target_result is not None:
            statuses = ["参考表示"]
            if any(result is None or result.value is None for _, result in self._rows):
                statuses.append("値なしの期間あり")
            if any(result is not None and result.is_reference for _, result in self._rows):
                statuses.append("参考値あり")
            if any(
                result is not None and result.is_partial_application for _, result in self._rows
            ):
                statuses.append("一部期間のみ適用")
            if any(period.is_in_progress for period, _ in self._rows):
                statuses.append("進行中の期間あり")
            if any(result is not None and result.warning_codes for _, result in self._rows):
                statuses.append("警告あり")
            self.chart.setTitle(
                f"{escape(target_result.display_name)} ({escape(target_result.unit)})"
                f" — {PURPOSE_LABELS[target_result.purpose]} / {'・'.join(statuses)}"
            )
        if unrenderable:
            self.chart_note.setText(
                "描画できない値があります（表の行: "
                + ", ".join(map(str, unrenderable))
                + "）。正確な値は表または CSV で確認してください。"
            )
            return
        indistinguishable = sorted(
            index
            for group in converted_values.values()
            if len(group) > 1
            for indices in group.values()
            for index in indices
        )
        if indistinguishable:
            self.chart_note.setText(
                "描画精度では異なる値を区別できません（表の行: "
                + ", ".join(map(str, indistinguishable))
                + "）。正確な値は表または CSV で確認してください。"
            )
            return
        values = [value for value in points if value is not None]
        if not values:
            self.chart_note.setText("表示できる数値がありません。")
            return
        low, high = min(values), max(values)
        span = high - low
        if not math.isfinite(span):
            self.chart_note.setText(
                "値の範囲が描画可能範囲外です。表または CSV を確認してください。"
            )
            return
        if low == high:
            padding = abs(low) * 0.05 if low else 1.0
            if padding == 0.0:
                padding = math.ulp(low)
        else:
            padding = span * 0.05
        minimum, maximum = low - padding, high + padding
        if (
            not math.isfinite(minimum)
            or not math.isfinite(maximum)
            or minimum >= maximum
            or not math.isfinite(maximum - minimum)
        ):
            self.chart_note.setText(
                "値の範囲が描画可能範囲外です。表または CSV を確認してください。"
            )
            return
        ticks = [minimum + (maximum - minimum) * (index / 4) for index in range(5)]
        if any(left >= right for left, right in zip(ticks, ticks[1:], strict=False)):
            self.chart_note.setText(
                "描画精度では目盛りを区別できません。正確な値は表または CSV で確認してください。"
            )
            return
        precision = next(
            (
                digits
                for digits in range(6, 18)
                if len({format(tick, f".{digits}g") for tick in ticks}) == len(ticks)
            ),
            None,
        )
        if precision is None:
            self.chart_note.setText(
                "描画精度では目盛りを区別できません。正確な値は表または CSV で確認してください。"
            )
            return
        axis_x = QCategoryAxis()
        axis_x.setTitleText("表の行番号 / 期間開始業務日")
        axis_x.setStartValue(0)
        axis_x.setLabelsPosition(QCategoryAxis.AxisLabelsPosition.AxisLabelsPositionOnValue)
        count = len(points)
        indexes = (
            range(count)
            if count <= 6
            else sorted({round(index * (count - 1) / 5) for index in range(6)})
        )
        for index in indexes:
            axis_x.append(f"{index + 1}\n{self._rows[index][0].start}", float(index + 1))
        axis_x.setRange(0.5, len(points) + 0.5)
        axis_y = QValueAxis()
        axis_y.setTitleText(escape(target_result.unit) if target_result is not None else "")
        axis_y.setLabelFormat(f"%.{precision}g")
        axis_y.setTickCount(len(ticks))
        axis_y.setRange(minimum, maximum)
        self.chart.addAxis(axis_x, Qt.AlignmentFlag.AlignBottom)
        self.chart.addAxis(axis_y, Qt.AlignmentFlag.AlignLeft)
        series: QLineSeries | None = None
        for index, value in enumerate(points, start=1):
            if value is None:
                series = None
                continue
            if series is None:
                series = QLineSeries()
                series.setPen(QPen(QColor("#2563eb"), 2))
                series.setPointsVisible(True)
                self.chart.addSeries(series)
                series.attachAxis(axis_x)
                series.attachAxis(axis_y)
            series.append(float(index), value)
        self._chart_available = True
        self.png_button.setEnabled(True)
        self.chart_note.setText(
            "横軸は表の行番号と期間開始業務日です。値のない期間では線をつなぎません。"
            "グラフは参考表示です。正確な値は表または CSV で確認してください。"
        )

    def _period_changed(self, row: int, *_args: object) -> None:
        succeeded, _ = run_guarded(self, lambda: self._show_period(row))
        if not succeeded:
            self.invalidate()
            self.status_label.setText("内訳の表示に失敗しました。集計を再取得してください。")

    def _show_period(self, row: int) -> None:
        self.details_table.setRowCount(0)
        self.pending_combo.clear()
        self._pending_issues.clear()
        self.correction_button.setEnabled(False)
        if row < 0 or row >= len(self._rows):
            self.detail_text.clear()
            return
        period, result = self._rows[row]
        if result is None:
            self.detail_text.setPlainText("この期間には式の有効版が適用されていません。")
            return
        lines: list[str] = []
        if period.is_in_progress:
            lines.append("進行中の期間です。現在業務日までの途中値です。")
        if result.uses_current_attributes:
            lines.append("直接集計には現在の測定定義の単位・カテゴリ・目的を使用しています。")
        if result.is_reference:
            lines.append("欠測系の理由があるため参考値です。")
        if result.is_partial_application:
            lines.append("式が期間の一部だけに適用されています。全期間の完全な値ではありません。")
        if result.exclusion_reason:
            lines.append(result.exclusion_reason)
        elif not result.details and result.value is None:
            lines.append("この有効期間に予定枠・交換境界はありません。")
        if result.missing_definition_ids:
            lines.append("使用量のない式項目: " + "、".join(result.missing_definition_ids))
        for application in result.applications:
            retroactive = " / 後付け設定" if application.version.is_retroactive else ""
            lines.append(
                f"式版 {application.version.effective_from_date}: "
                f"{application.start} 以上 {application.end} 未満{retroactive}"
            )
        seen: set[AggregationIssue] = set()
        for issue in result.issues:
            kind = (
                "欠測"
                if issue.code.startswith("MISSING_")
                else ("警告" if issue.code.startswith("WARN_") else "情報")
            )
            if issue not in seen:
                seen.add(issue)
                lines.append(f"{kind}: {issue.message}")
                if issue.measurement_definition_id:
                    lines.append(f"  測定定義: {issue.measurement_definition_id}")
                if issue.source_id:
                    lines.append(f"  対象 ID: {issue.source_id}")
                if issue.related_ids:
                    lines.append("  関連 ID: " + "、".join(issue.related_ids))
                if issue.meter_id:
                    lines.append(f"  メーター ID: {issue.meter_id}")
                if issue.assignment_effective_from:
                    lines.append(
                        f"  割当開始: {self._local_datetime(issue.assignment_effective_from)}"
                    )
                if issue.resolution:
                    lines.append(f"  対応: {issue.resolution}")
            if issue.code is AggregationCode.MISSING_ASSIGNMENT_PENDING and not any(
                previous.measurement_definition_id == issue.measurement_definition_id
                and previous.meter_id == issue.meter_id
                and previous.assignment_effective_from == issue.assignment_effective_from
                for previous in self._pending_issues
            ):
                self._pending_issues.append(issue)
        self.detail_text.setPlainText("\n".join(lines) or "この期間に警告・情報はありません。")
        for issue in self._pending_issues:
            self.pending_combo.addItem(
                f"{issue.measurement_definition_id} / {issue.meter_id} / "
                f"{self._local_datetime(issue.assignment_effective_from)}"
            )
        self.correction_button.setEnabled(bool(self._pending_issues))
        self.details_table.setRowCount(len(result.details))
        for detail_row, detail in enumerate(result.details):
            values = (
                detail.measurement_definition_id,
                detail.location_id,
                SOURCE_LABELS[detail.source],
                detail.source_id,
                detail.baseline.business_date.isoformat() if detail.baseline else "",
                BASELINE_LABELS[detail.baseline.source] if detail.baseline else "",
                detail.baseline.source_id if detail.baseline else "",
                (
                    format(detail.baseline.reading, "f")
                    if detail.baseline and detail.baseline.reading is not None
                    else ""
                ),
                detail.business_date.isoformat(),
                "" if detail.source_usage is None else format(detail.source_usage, "f"),
                format(detail.coefficient, "f"),
                display_value(detail.contribution, result.decimal_places),
                "" if detail.contribution is None else format(detail.contribution, "f"),
                _STATUS_LABELS.get(detail.status, detail.status),
            )
            for column, value in enumerate(values):
                self.details_table.setItem(detail_row, column, QTableWidgetItem(value))

    def _local_datetime(self, value: datetime | None) -> str:
        if value is None or value.tzinfo is None or value.utcoffset() is None:
            raise ValidationError("確認待ち割当の開始日時が不正です。")
        return value.astimezone(self._services.timezone_settings.get_timezone()).strftime(
            "%Y/%m/%d %H:%M:%S %z"
        )

    def _open_correction(self) -> None:
        index = self.pending_combo.currentIndex()
        if index < 0 or index >= len(self._pending_issues):
            raise ValidationError("訂正する確認待ちの割当を選択してください。")
        issue = self._pending_issues[index]
        flag = next(
            (
                item
                for item in self._services.assignment_calculation_settings.list_review_flags()
                if item.measurement_definition_id == issue.measurement_definition_id
                and item.meter_id == issue.meter_id
                and item.assignment_effective_from == issue.assignment_effective_from
            ),
            None,
        )
        self.invalidate()
        if flag is None:
            raise ValidationError("確認待ちの割当が変更されています。集計を再取得してください。")
        dialog = MasterDialog(self._services, self._staff_memory, self)
        dialog.show_assignment_correction(flag)
        dialog.saved.connect(self._correction_saved)
        dialog.exec()
        self._load_locations()
        self.refresh()

    def _correction_saved(self) -> None:
        self.invalidate()
        self.data_changed.emit()

    def _export_csv(self) -> None:
        report, target = self._report, self._selected_target()
        if report is None or target is None:
            raise ValidationError("集計を再取得して出力対象を選択してください。")

        def export(output: Path) -> None:
            if self._report is not report or self._selected_target() != target:
                raise ValidationError(
                    "集計条件またはデータが変わりました。集計を再取得してください。"
                )
            save_aggregation_csv(output, report, *target)

        path = export_csv_with_dialog(
            self,
            "表示中の集計",
            "meter_aggregation",
            export,
        )
        if path is not None:
            QMessageBox.information(self, "CSV 出力", f"CSV を保存しました。\n{path}")

    def _export_png(self) -> None:
        if self._report is None or not self._chart_available:
            raise ValidationError("描画できる集計グラフを再取得してください。")
        report, target = self._report, self._selected_target()

        def export(output: Path) -> None:
            if (
                self._report is not report
                or self._selected_target() != target
                or not self._chart_available
            ):
                raise ValidationError(
                    "集計条件またはデータが変わりました。集計を再取得してください。"
                )
            save_chart_png(output, self.chart_view)

        path = export_file_with_dialog(
            self,
            "表示中のグラフ",
            "meter_aggregation",
            ".png",
            "PNG ファイル (*.png)",
            export,
        )
        if path is not None:
            QMessageBox.information(self, "PNG 出力", f"PNG を保存しました。\n{path}")
