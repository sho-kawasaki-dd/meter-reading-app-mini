"""割当期間の計算条件を確認・訂正する。"""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal

from PySide6.QtCore import QSignalBlocker, Signal
from PySide6.QtWidgets import (
    QComboBox,
    QFormLayout,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QPlainTextEdit,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from meter_reading_mini.app_context import AppContext
from meter_reading_mini.core.assignment_calculation_settings import (
    AssignmentCalculationCorrectionInput,
    AssignmentCalculationCorrectionPreview,
)
from meter_reading_mini.core.errors import ValidationError
from meter_reading_mini.core.meter_reading_sync_repositories import MeterAssignmentRecord
from meter_reading_mini.ui.error_handling import run_guarded
from meter_reading_mini.ui.staff_selection_memory import StaffSelectionMemory
from meter_reading_mini.ui.widgets.decimal_line_edit import DecimalLineEdit
from meter_reading_mini.ui.widgets.form_helpers import selected_value
from meter_reading_mini.ui.widgets.master_common import confirm


def _decimal_text(value: Decimal | None) -> str:
    return "なし" if value is None else format(value, "f")


class AssignmentCorrectionPage(QWidget):
    saved = Signal()

    def __init__(
        self,
        services: AppContext,
        staff_memory: StaffSelectionMemory,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self._services = services
        self._staff_memory = staff_memory
        self._assignments: list[MeterAssignmentRecord] = []
        self._loaded_assignment: MeterAssignmentRecord | None = None
        self._previewed: (
            tuple[AssignmentCalculationCorrectionInput, AssignmentCalculationCorrectionPreview]
            | None
        ) = None

        layout = QVBoxLayout(self)
        form = QFormLayout()
        self.definition_combo = QComboBox()
        self.definition_combo.setObjectName("correctionDefinition")
        self.assignment_combo = QComboBox()
        self.assignment_combo.setObjectName("correctionAssignment")
        self.current_conditions = QLabel("割当期間を選択してください。")
        self.current_conditions.setWordWrap(True)
        self.review_reason = QLabel()
        self.review_reason.setWordWrap(True)
        self.multiplier_edit = DecimalLineEdit("倍率")
        self.rollover_edit = DecimalLineEdit("ロールオーバー値")
        self.staff_combo = QComboBox()
        self.reason_edit = QLineEdit()
        for label, widget in (
            ("測定定義", self.definition_combo),
            ("割当期間", self.assignment_combo),
            ("現在の計算条件", self.current_conditions),
            ("確認待ちの理由", self.review_reason),
            ("訂正後の倍率", self.multiplier_edit),
            ("訂正後のロールオーバー値", self.rollover_edit),
            ("訂正担当者", self.staff_combo),
            ("訂正理由（必須）", self.reason_edit),
        ):
            form.addRow(label, widget)
        layout.addLayout(form)
        buttons = QHBoxLayout()
        self.preview_button = QPushButton("影響をプレビュー")
        self.apply_button = QPushButton("確認した内容を確定")
        self.apply_button.setEnabled(False)
        buttons.addWidget(self.preview_button)
        buttons.addWidget(self.apply_button)
        buttons.addStretch()
        layout.addLayout(buttons)
        layout.addWidget(QLabel("影響・検証結果"))
        self.preview_text = QPlainTextEdit()
        self.preview_text.setObjectName("assignmentCorrectionPreview")
        self.preview_text.setReadOnly(True)
        self.preview_text.setMinimumHeight(130)
        layout.addWidget(self.preview_text)
        layout.addWidget(QLabel("訂正履歴"))
        self.history_text = QPlainTextEdit()
        self.history_text.setObjectName("assignmentCorrectionHistory")
        self.history_text.setReadOnly(True)
        self.history_text.setMinimumHeight(110)
        layout.addWidget(self.history_text)

        self.definition_combo.currentIndexChanged.connect(
            lambda *_: run_guarded(self, self._load_definition)
        )
        self.assignment_combo.currentIndexChanged.connect(
            lambda *_: run_guarded(self, self._load_assignment)
        )
        self.multiplier_edit.textChanged.connect(self._invalidate_preview)
        self.rollover_edit.textChanged.connect(self._invalidate_preview)
        self.reason_edit.textChanged.connect(self._invalidate_preview)
        self.staff_combo.currentIndexChanged.connect(self._staff_changed)
        self.preview_button.clicked.connect(lambda: run_guarded(self, self._preview))
        self.apply_button.clicked.connect(lambda: run_guarded(self, self._apply))
        self.refresh_staff()
        self.refresh_definitions()

    def _local_datetime(self, value: datetime | None) -> str:
        if value is None:
            return "現在まで"
        return value.astimezone(self._services.timezone_settings.get_timezone()).strftime(
            "%Y/%m/%d %H:%M:%S"
        )

    def _period(self, assignment: MeterAssignmentRecord) -> str:
        return (
            f"{self._local_datetime(assignment.effective_from)} 〜 "
            f"{self._local_datetime(assignment.effective_to)}"
        )

    def refresh_definitions(self) -> None:
        selected = self.definition_combo.currentData()
        definitions = self._services.measurement_definitions.list_measurement_definitions(
            show_inactive=True
        )
        with QSignalBlocker(self.definition_combo):
            self.definition_combo.clear()
            self.definition_combo.addItem("測定定義を選択してください", "")
            for definition in definitions:
                status = "" if definition.is_active else "（無効）"
                self.definition_combo.addItem(
                    f"{definition.display_name or definition.measurement_definition_id}{status} "
                    f"[{definition.measurement_definition_id}]",
                    definition.measurement_definition_id,
                )
            index = self.definition_combo.findData(selected)
            self.definition_combo.setCurrentIndex(max(index, 0))
        self._invalidate_preview()
        if selected != self.definition_combo.currentData() or not self._assignments:
            self._load_definition()

    def refresh_staff(self) -> None:
        selected = self.staff_combo.currentData()
        if not isinstance(selected, str) or not selected:
            selected = self._staff_memory.staff_id or ""
        staff = self._services.staff.list_staff()
        with QSignalBlocker(self.staff_combo):
            self.staff_combo.clear()
            self.staff_combo.addItem("担当者を選択してください", "")
            for item in staff:
                self.staff_combo.addItem(
                    f"{item.display_name} [ID: {item.staff_id}]", item.staff_id
                )
            self.staff_combo.setCurrentIndex(max(self.staff_combo.findData(selected), 0))
        self._invalidate_preview()

    def refresh_meters(self) -> None:
        meters = {
            meter.meter_id: meter.meter_name
            for meter in self._services.meter_master.list_meters(show_inactive=True)
        }
        for index, assignment in enumerate(self._assignments):
            review = "（確認待ち）" if assignment.needs_review else ""
            self.assignment_combo.setItemText(
                index + 1,
                f"{meters.get(assignment.meter_id, assignment.meter_id)} "
                f"[{assignment.meter_id}] / {self._period(assignment)}{review}",
            )
        self._invalidate_preview()

    def _staff_changed(self, *_args: object) -> None:
        value = self.staff_combo.currentData()
        if isinstance(value, str) and value:
            self._staff_memory.staff_id = value
        self._invalidate_preview()

    def _invalidate_preview(self, *_args: object) -> None:
        self._previewed = None
        self.preview_text.clear()
        self.apply_button.setEnabled(False)

    def _load_definition(self) -> None:
        self._invalidate_preview()
        self._loaded_assignment = None
        self._assignments = []
        self.preview_button.setEnabled(False)
        self.history_text.clear()
        with QSignalBlocker(self.assignment_combo):
            self.assignment_combo.clear()
        key = self.definition_combo.currentData()
        self._assignments = (
            self._services.assignment_calculation_settings.list_assignments(key)
            if isinstance(key, str) and key
            else []
        )
        meters = {
            meter.meter_id: meter.meter_name
            for meter in self._services.meter_master.list_meters(show_inactive=True)
        }
        with QSignalBlocker(self.assignment_combo):
            self.assignment_combo.clear()
            self.assignment_combo.addItem("割当期間を選択してください", "")
            for index, assignment in enumerate(self._assignments):
                review = "（確認待ち）" if assignment.needs_review else ""
                self.assignment_combo.addItem(
                    f"{meters.get(assignment.meter_id, assignment.meter_id)} "
                    f"[{assignment.meter_id}] / {self._period(assignment)}{review}",
                    str(index),
                )
            self.assignment_combo.setCurrentIndex(1 if self._assignments else 0)
        self._load_assignment()

    def _selected_assignment(self) -> MeterAssignmentRecord:
        value = selected_value(self.assignment_combo, "割当期間")
        try:
            index = int(value)
            return self._assignments[index]
        except (ValueError, IndexError) as error:
            raise ValidationError("選択した割当期間を読み取れません。") from error

    def _load_assignment(self) -> None:
        self._invalidate_preview()
        self._loaded_assignment = None
        self.preview_button.setEnabled(False)
        self.history_text.clear()
        if not self._assignments or not self.assignment_combo.currentData():
            self.current_conditions.setText("割当期間を選択してください。")
            self.review_reason.clear()
            self.multiplier_edit.clear()
            self.rollover_edit.clear()
            self.reason_edit.clear()
            self.history_text.clear()
            return
        assignment = self._selected_assignment()
        self.current_conditions.setText(
            f"{self._period(assignment)} / 倍率 {_decimal_text(assignment.multiplier)} / "
            f"ロールオーバー {_decimal_text(assignment.rollover_value)}"
        )
        flags = self._services.assignment_calculation_settings.list_review_flags()
        reasons = [
            flag.reason
            for flag in flags
            if flag.measurement_definition_id == assignment.measurement_definition_id
            and flag.meter_id == assignment.meter_id
            and flag.assignment_effective_from == assignment.effective_from
        ]
        self.review_reason.setText(" / ".join(reasons) if reasons else "確認待ちなし")
        self.multiplier_edit.set_decimal_value(assignment.multiplier)
        self.rollover_edit.set_decimal_value(assignment.rollover_value)
        self.reason_edit.clear()
        records = self._services.assignment_calculation_settings.list_corrections(
            assignment.measurement_definition_id, assignment.meter_id, assignment.effective_from
        )
        self.history_text.setPlainText(
            "\n".join(
                f"{self._local_datetime(item.corrected_at)} / {item.staff_name} / "
                f"倍率 {_decimal_text(item.old_multiplier)} → "
                f"{_decimal_text(item.new_multiplier)} / "
                f"ロールオーバー {_decimal_text(item.old_rollover_value)} → "
                f"{_decimal_text(item.new_rollover_value)} / 理由: {item.reason}"
                for item in records
            )
        )
        self._loaded_assignment = assignment
        self.preview_button.setEnabled(True)

    def select_assignment(
        self, definition_id: str, meter_id: str, effective_from: datetime
    ) -> None:
        definition_index = self.definition_combo.findData(definition_id)
        if definition_index < 0:
            raise ValidationError(f"測定定義が見つかりません: {definition_id}")
        if self.definition_combo.currentIndex() == definition_index:
            self._load_definition()
        else:
            self.definition_combo.setCurrentIndex(definition_index)
        for index, assignment in enumerate(self._assignments):
            if assignment.meter_id == meter_id and assignment.effective_from == effective_from:
                self.assignment_combo.setCurrentIndex(index + 1)
                return
        raise ValidationError("確認待ちの割当期間が見つかりません。")

    def _request(self) -> AssignmentCalculationCorrectionInput:
        assignment = self._selected_assignment()
        if assignment != self._loaded_assignment:
            raise ValidationError("割当期間を再読み込みしてください。")
        multiplier = self.multiplier_edit.decimal_value(required=True)
        if multiplier is None:
            raise ValidationError("倍率を入力してください。")
        return AssignmentCalculationCorrectionInput(
            measurement_definition_id=assignment.measurement_definition_id,
            meter_id=assignment.meter_id,
            effective_from=assignment.effective_from,
            multiplier=multiplier,
            rollover_value=self.rollover_edit.decimal_value(),
            staff_id=selected_value(self.staff_combo, "訂正担当者"),
            reason=self.reason_edit.text(),
        )

    def _preview(self) -> None:
        self._invalidate_preview()
        request = self._request()
        preview = self._services.assignment_calculation_settings.preview_correction(request)
        lines = [
            f"対象期間: {self._period(preview.current)}",
            f"倍率: {_decimal_text(preview.current.multiplier)} → "
            f"{_decimal_text(preview.proposed.multiplier)}",
            f"ロールオーバー: {_decimal_text(preview.current.rollover_value)} → "
            f"{_decimal_text(preview.proposed.rollover_value)}",
            f"再計算: 実測 {len(preview.readings)} 件 / 交換境界 {len(preview.boundaries)} 件",
        ]
        lines.extend(
            f"実測 {item.planned_reading_id}: 使用量 {_decimal_text(item.old_usage_delta)} → "
            f"{_decimal_text(item.new_usage_delta)}"
            for item in preview.readings
        )
        lines.extend(
            f"交換 {item.replacement_event_id}: 使用量 {_decimal_text(item.old_usage_delta)} → "
            f"{_decimal_text(item.update.old_meter_usage_delta)}"
            for item in preview.boundaries
        )
        lines.extend(f"検証エラー {item.target}: {item.message}" for item in preview.issues)
        self.preview_text.setPlainText("\n".join(lines))
        if preview.can_apply:
            self._previewed = (request, preview)
            self.apply_button.setEnabled(True)

    def _apply(self) -> None:
        if self._previewed is None:
            raise ValidationError("入力内容をプレビューしてください。")
        request, preview = self._previewed
        if self._request() != request:
            self._invalidate_preview()
            raise ValidationError("入力が変更されました。再度プレビューしてください。")
        if not confirm(
            self,
            "計算条件の訂正",
            "表示した割当期間と再計算結果を確認しましたか？ 訂正履歴を保存します。",
        ):
            return
        self._invalidate_preview()
        self._services.assignment_calculation_settings.correct_assignment(
            request, expected_preview=preview
        )
        self.saved.emit()
        self._load_definition()
        self.select_assignment(
            request.measurement_definition_id, request.meter_id, request.effective_from
        )
