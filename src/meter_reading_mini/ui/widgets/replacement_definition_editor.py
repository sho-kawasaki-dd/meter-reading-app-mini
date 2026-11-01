"""交換時の測定定義別読値と割当条件を入力する。"""

from __future__ import annotations

from decimal import Decimal

from PySide6.QtCore import Signal
from PySide6.QtWidgets import QFormLayout, QGroupBox, QLabel, QWidget

from meter_reading_mini.core.contract_models import ValueSchema
from meter_reading_mini.core.errors import ValidationError
from meter_reading_mini.core.meter_reading_sync_repositories import (
    AssignmentCalculationTerms,
    MeasurementDefinitionRecord,
    MeterAssignmentRecord,
    ReplacementDefinitionReading,
)
from meter_reading_mini.ui.widgets.decimal_line_edit import DecimalLineEdit


def decimal_text(value: Decimal | None) -> str:
    return "なし" if value is None else format(value, "f")


def _conditions(assignment: MeterAssignmentRecord) -> str:
    return (
        f"倍率 {decimal_text(assignment.multiplier)} / "
        f"ロールオーバー {decimal_text(assignment.rollover_value)}"
    )


class ReplacementDefinitionEditor(QGroupBox):
    changed = Signal()

    def __init__(
        self,
        definition: MeasurementDefinitionRecord,
        old_assignment: MeterAssignmentRecord,
        *,
        new_assignment: MeterAssignmentRecord | None = None,
        readings: ReplacementDefinitionReading | None = None,
        edit_terms: bool = True,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(
            f"{definition.display_name or definition.measurement_definition_id} "
            f"[{definition.measurement_definition_id}]",
            parent,
        )
        self.definition = definition
        self.old_assignment = old_assignment
        form = QFormLayout(self)
        old_conditions = QLabel(_conditions(old_assignment))
        old_conditions.setWordWrap(True)
        form.addRow("旧条件（変更不可）", old_conditions)
        self.old_final_edit = DecimalLineEdit("旧メーター最終読値")
        self.new_initial_edit = DecimalLineEdit("新メーター初期読値")
        if readings is not None:
            self.old_final_edit.set_decimal_value(readings.old_final_reading)
            self.new_initial_edit.set_decimal_value(readings.new_initial_reading)
        if definition.value_schema is not ValueSchema.DECIMAL_STRING:
            self.old_final_edit.setEnabled(False)
            self.new_initial_edit.setEnabled(False)
            form.addRow("", QLabel("力率の交換境界には数値読値を指定しません。"))
        form.addRow("旧メーター最終読値", self.old_final_edit)
        form.addRow("新メーター初期読値", self.new_initial_edit)
        self.multiplier_edit: DecimalLineEdit | None = None
        self.rollover_edit: DecimalLineEdit | None = None
        if edit_terms:
            self.multiplier_edit = DecimalLineEdit("新しい倍率")
            self.rollover_edit = DecimalLineEdit("新しいロールオーバー値")
            self.multiplier_edit.set_decimal_value(old_assignment.multiplier)
            self.rollover_edit.set_decimal_value(old_assignment.rollover_value)
            form.addRow("新しい倍率", self.multiplier_edit)
            form.addRow("新しいロールオーバー値", self.rollover_edit)
            self.multiplier_edit.textChanged.connect(lambda *_: self.changed.emit())
            self.rollover_edit.textChanged.connect(lambda *_: self.changed.emit())
        elif new_assignment is not None:
            new_conditions = QLabel(_conditions(new_assignment))
            new_conditions.setWordWrap(True)
            form.addRow("新条件（変更不可）", new_conditions)
        self.old_final_edit.textChanged.connect(lambda *_: self.changed.emit())
        self.new_initial_edit.textChanged.connect(lambda *_: self.changed.emit())

    def definition_reading(self) -> ReplacementDefinitionReading:
        return ReplacementDefinitionReading(
            self.definition.measurement_definition_id,
            self.old_final_edit.decimal_value(),
            self.new_initial_edit.decimal_value(),
        )

    def new_terms(self) -> AssignmentCalculationTerms:
        if self.multiplier_edit is None or self.rollover_edit is None:
            raise ValidationError("交換後の計算条件を入力できません。")
        multiplier = self.multiplier_edit.decimal_value(required=True)
        if multiplier is None:
            raise ValidationError("新しい倍率を入力してください。")
        return AssignmentCalculationTerms(multiplier, self.rollover_edit.decimal_value())
