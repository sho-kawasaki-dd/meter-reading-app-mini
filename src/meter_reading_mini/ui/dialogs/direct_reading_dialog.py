"""予定枠への直接入力・訂正と使用量のプレビュー。"""

from __future__ import annotations

from decimal import Decimal

from pydantic import ValidationError as PydanticValidationError
from PySide6.QtCore import QSignalBlocker, Signal
from PySide6.QtWidgets import (
    QComboBox,
    QDialog,
    QFormLayout,
    QHBoxLayout,
    QLabel,
    QPlainTextEdit,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from meter_reading_mini.app_context import AppContext
from meter_reading_mini.core.contract_models import (
    PowerFactorPhase,
    PowerFactorReading,
    ResultStatus,
    ValueSchema,
)
from meter_reading_mini.core.errors import DomainError, ValidationError
from meter_reading_mini.core.meter_reading_sync_repositories import (
    ManualReadingInput,
    PlannedReadingTaskState,
    ReadingOverviewRecord,
)
from meter_reading_mini.core.reading_preview import ManualReadingPreview
from meter_reading_mini.ui.error_handling import run_guarded
from meter_reading_mini.ui.models.reading_overview_model import (
    CYCLE_LABELS,
    PHASE_LABELS,
    PREVIOUS_SOURCE_LABELS,
    RESULT_STATUS_LABELS,
    TASK_STATE_LABELS,
)
from meter_reading_mini.ui.staff_selection_memory import StaffSelectionMemory
from meter_reading_mini.ui.widgets.decimal_line_edit import DecimalLineEdit
from meter_reading_mini.ui.widgets.form_helpers import selected_value
from meter_reading_mini.ui.widgets.master_common import confirm, scroll_area
from meter_reading_mini.ui.widgets.staff_combo import StaffComboBox
from meter_reading_mini.ui.widgets.wall_datetime_edit import WallDateTimeEdit


def _decimal_text(value: Decimal | None) -> str:
    return "なし" if value is None else format(value, "f")


class DirectReadingDialog(QDialog):
    saved = Signal()

    def __init__(
        self,
        services: AppContext,
        reading: ReadingOverviewRecord,
        staff_memory: StaffSelectionMemory,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        if reading.task_state is PlannedReadingTaskState.SUPERSEDED:
            raise ValidationError("差し替え済みの枠は入力できません。後継枠を選択してください。")
        definition = services.measurement_definitions.get_measurement_definition(
            reading.measurement_definition_id
        )
        if definition is None:
            raise ValidationError("予定枠の測定定義が見つかりません。")
        if (
            reading.structured_payload_json is not None
            and definition.value_schema is not ValueSchema.POWER_FACTOR_PERCENT_PHASE
        ) or (
            reading.raw_reading is not None
            and definition.value_schema is not ValueSchema.DECIMAL_STRING
        ):
            raise ValidationError("保存済みの読値と測定定義の形式が一致しません。")
        if reading.raw_reading is not None and reading.structured_payload_json is not None:
            raise ValidationError("保存済みの読値の形式が重複しています。")
        self._services = services
        self._reading = reading
        self._value_schema = definition.value_schema
        self._previewed: tuple[ManualReadingInput, str, ManualReadingPreview] | None = None
        self.setWindowTitle("PC 直接入力・訂正")
        self.resize(760, 650)

        layout = QVBoxLayout(self)
        content = QWidget()
        form = QFormLayout(content)
        local_scheduled = reading.scheduled_at.astimezone(
            services.timezone_settings.get_timezone()
        ).strftime("%Y/%m/%d %H:%M:%S")
        information = QLabel(
            f"{reading.location_name} / {reading.meter_name} / {reading.display_name}\n"
            f"予定: {local_scheduled} / 枠: {TASK_STATE_LABELS[reading.task_state]}\n"
            f"業務日: {reading.business_date:%Y/%m/%d} / 検針時間: {reading.reading_session} / "
            f"周期: {CYCLE_LABELS[reading.reading_cycle]}\n"
            f"予定枠 ID: {reading.planned_reading_id}"
        )
        information.setWordWrap(True)
        form.addRow("予定枠", information)
        self.status_combo = QComboBox()
        self.status_combo.setObjectName("directReadingStatus")
        if reading.result_status is ResultStatus.REPLACED:
            self.status_combo.addItem("既存の交換結果（変更する状態を選択）", "")
        for status in (
            ResultStatus.MEASURED,
            ResultStatus.SKIPPED,
            ResultStatus.UNREACHABLE,
            ResultStatus.METER_FAULT,
        ):
            self.status_combo.addItem(RESULT_STATUS_LABELS[status], status.value)
        if reading.result_status not in (None, ResultStatus.REPLACED):
            self.status_combo.setCurrentIndex(
                self.status_combo.findData(reading.result_status.value)
            )
        self.value_edit = DecimalLineEdit(
            "力率" if self._value_schema is ValueSchema.POWER_FACTOR_PERCENT_PHASE else "測定値"
        )
        self.value_edit.setObjectName("directReadingValue")
        self.phase_combo = QComboBox()
        self.phase_combo.setObjectName("directReadingPhase")
        for phase in (PowerFactorPhase.LEAD, PowerFactorPhase.LAG, PowerFactorPhase.UNITY):
            self.phase_combo.addItem(PHASE_LABELS[phase], phase.value)
        if reading.structured_payload_json is not None:
            try:
                payload = PowerFactorReading.model_validate_json(reading.structured_payload_json)
            except PydanticValidationError as exc:
                raise ValidationError("保存済みの力率を読み取れません。") from exc
            self.value_edit.set_decimal_value(payload.percent)
            self.phase_combo.setCurrentIndex(self.phase_combo.findData(payload.phase.value))
        else:
            self.value_edit.set_decimal_value(reading.raw_reading)
        timezone = services.timezone_settings.get_timezone()
        initial = reading.recorded_at or min(reading.scheduled_at, services.current_datetime())
        self.recorded_at = WallDateTimeEdit(initial, timezone)
        self.staff_combo = StaffComboBox(services.staff, staff_memory)
        self.staff_combo.setObjectName("directReadingStaff")
        self.note_edit = QPlainTextEdit()
        self.note_edit.setObjectName("directReadingNote")
        self.note_edit.setMaximumHeight(90)
        self.note_edit.setPlainText(reading.note or "")
        form.addRow("結果状態", self.status_combo)
        form.addRow(
            "力率（%）"
            if self._value_schema is ValueSchema.POWER_FACTOR_PERCENT_PHASE
            else f"測定値（{reading.unit}）",
            self.value_edit,
        )
        if self._value_schema is ValueSchema.POWER_FACTOR_PERCENT_PHASE:
            form.addRow("位相", self.phase_combo)
        form.addRow(f"記録日時（{timezone}）", self.recorded_at)
        form.addRow("担当者", self.staff_combo)
        form.addRow("備考", self.note_edit)
        self.preview_text = QPlainTextEdit()
        self.preview_text.setObjectName("directReadingPreview")
        self.preview_text.setReadOnly(True)
        self.preview_text.setMinimumHeight(140)
        form.addRow("使用量・検証結果", self.preview_text)
        layout.addWidget(scroll_area(content))

        buttons = QHBoxLayout()
        self.preview_button = QPushButton("プレビューを更新")
        self.save_button = QPushButton("表示した内容を保存")
        self.save_button.setEnabled(False)
        close_button = QPushButton("閉じる")
        buttons.addWidget(self.preview_button)
        buttons.addStretch()
        buttons.addWidget(self.save_button)
        buttons.addWidget(close_button)
        layout.addLayout(buttons)
        self.status_combo.currentIndexChanged.connect(self._input_changed)
        self.value_edit.textChanged.connect(self._input_changed)
        self.phase_combo.currentIndexChanged.connect(self._input_changed)
        self.recorded_at.value_changed.connect(self._input_changed)
        self.staff_combo.currentIndexChanged.connect(self._input_changed)
        self.note_edit.textChanged.connect(self._input_changed)
        self.preview_button.clicked.connect(self._input_changed)
        self.save_button.clicked.connect(lambda: run_guarded(self, self._save))
        close_button.clicked.connect(self.reject)
        self._input_changed()

    def _update_value_controls(self) -> None:
        measured = self.status_combo.currentData() == ResultStatus.MEASURED.value
        self.value_edit.setEnabled(measured)
        if self._value_schema is not ValueSchema.POWER_FACTOR_PERCENT_PHASE:
            return
        try:
            percent = self.value_edit.decimal_value()
        except ValidationError:
            percent = None
        if percent == Decimal("100"):
            with QSignalBlocker(self.phase_combo):
                self.phase_combo.setCurrentIndex(
                    self.phase_combo.findData(PowerFactorPhase.UNITY.value)
                )
            self.phase_combo.setEnabled(False)
        else:
            if self.phase_combo.currentData() == PowerFactorPhase.UNITY.value:
                with QSignalBlocker(self.phase_combo):
                    self.phase_combo.setCurrentIndex(
                        self.phase_combo.findData(PowerFactorPhase.LEAD.value)
                    )
            self.phase_combo.setEnabled(measured)

    def _request(self) -> ManualReadingInput:
        status = ResultStatus(selected_value(self.status_combo, "結果状態"))
        raw: Decimal | None = None
        payload: dict[str, str] | None = None
        if status is ResultStatus.MEASURED:
            value = self.value_edit.decimal_value(required=True)
            if value is None:
                raise ValidationError("測定値を入力してください。")
            if self._value_schema is ValueSchema.POWER_FACTOR_PERCENT_PHASE:
                payload = {
                    "percent": format(value, "f"),
                    "phase": selected_value(self.phase_combo, "位相"),
                }
            else:
                raw = value
        return ManualReadingInput(
            planned_reading_id=self._reading.planned_reading_id,
            result_status=status,
            recorded_at=self.recorded_at.to_utc(now=self._services.current_datetime()),
            raw_reading=raw,
            structured_payload_json=payload,
            note=self.note_edit.toPlainText(),
        )

    def _input_changed(self, *_args: object) -> None:
        self._invalidate_preview()
        run_guarded(self, self._refresh_preview)

    def _invalidate_preview(self) -> None:
        self._previewed = None
        self.save_button.setEnabled(False)
        self.preview_text.clear()

    def _refresh_preview(self) -> None:
        self._update_value_controls()
        try:
            request = self._request()
            preview = self._services.reading_preview.preview_manual_reading(request)
            lines = [
                f"前回値: {_decimal_text(preview.previous_reading)} "
                f"（{PREVIOUS_SOURCE_LABELS[preview.previous_source]}）",
                f"使用量: {_decimal_text(preview.usage_delta)} {self._reading.unit}",
                f"ロールオーバー補正: {'あり' if preview.rollover_applied else 'なし'}",
                f"警告閾値超過: {'あり' if preview.warning_threshold_exceeded else 'なし'}",
            ]
            if preview.error:
                lines.append(f"検証エラー: {preview.error}")
            else:
                try:
                    staff_id = selected_value(self.staff_combo, "担当者")
                except ValidationError as exc:
                    lines.append(exc.message)
                else:
                    self._previewed = (request, staff_id, preview)
                    self.save_button.setEnabled(True)
            self.preview_text.setPlainText("\n".join(lines))
        except DomainError as exc:
            self.preview_text.setPlainText(f"入力・検証エラー: {exc.message}")

    def _save(self) -> None:
        if self._previewed is None:
            raise ValidationError("入力内容をプレビューしてください。")
        request, staff_id, preview = self._previewed
        try:
            unchanged = (
                self._request() == request
                and selected_value(self.staff_combo, "担当者") == staff_id
            )
        except DomainError:
            self._invalidate_preview()
            raise
        if not unchanged:
            self._invalidate_preview()
            raise ValidationError("入力が変更されました。プレビューを確認してください。")
        if not confirm(self, "直接入力・訂正", "表示した内容を保存しますか？"):
            return
        try:
            unchanged = (
                self._request() == request
                and selected_value(self.staff_combo, "担当者") == staff_id
            )
        except DomainError:
            self._invalidate_preview()
            raise
        self._invalidate_preview()
        if not unchanged:
            raise ValidationError("入力が変更されました。再度プレビューしてください。")
        outcome = self._services.reading_writer.save_manual_reading(
            request, staff_id=staff_id, expected_preview=preview
        )
        if not outcome.accepted:
            raise ValidationError("検針結果を保存できませんでした。再度プレビューしてください。")
        self.saved.emit()
        self.accept()
