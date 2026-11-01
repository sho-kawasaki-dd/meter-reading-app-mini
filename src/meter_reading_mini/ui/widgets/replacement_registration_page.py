"""メーター交換の入力、影響プレビュー、確定。"""

from __future__ import annotations

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
from meter_reading_mini.core.contract_models import Category
from meter_reading_mini.core.errors import DomainError, ValidationError
from meter_reading_mini.core.meter_reading_sync_repositories import (
    MeterMasterCreateInput,
    MeterMasterRecord,
)
from meter_reading_mini.core.meter_replacement_service import (
    MeterReplacementInput,
    ReplacementPreview,
    ReplacementTarget,
)
from meter_reading_mini.ui.error_handling import run_guarded
from meter_reading_mini.ui.widgets.form_helpers import selected_value
from meter_reading_mini.ui.widgets.master_common import category_combo, confirm
from meter_reading_mini.ui.widgets.replacement_definition_editor import (
    ReplacementDefinitionEditor,
    decimal_text,
)
from meter_reading_mini.ui.widgets.wall_datetime_edit import WallDateTimeEdit


class ReplacementRegistrationPage(QWidget):
    saved = Signal(str)

    def __init__(
        self,
        services: AppContext,
        location_combo: QComboBox,
        staff_combo: QComboBox,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self._services = services
        self._location_combo = location_combo
        self._staff_combo = staff_combo
        self._meters: dict[str, MeterMasterRecord] = {}
        self._targets: tuple[ReplacementTarget, ...] = ()
        self._targets_valid = False
        self.definition_editors: dict[str, ReplacementDefinitionEditor] = {}
        self._previewed: tuple[MeterReplacementInput, ReplacementPreview] | None = None

        layout = QVBoxLayout(self)
        form = QFormLayout()
        self.old_meter_combo = QComboBox()
        self.old_meter_combo.setObjectName("replacementOldMeter")
        self.replaced_at = WallDateTimeEdit(
            services.current_datetime(), services.timezone_settings.get_timezone()
        )
        self.new_meter_mode = QComboBox()
        self.new_meter_mode.addItem("登録済みメーターを選択", "existing")
        self.new_meter_mode.addItem("この画面で新規登録", "create")
        self.new_meter_combo = QComboBox()
        self.new_meter_combo.setObjectName("replacementNewMeter")
        self.new_name_edit = QLineEdit()
        self.new_category_combo = category_combo()
        self.new_serial_edit = QLineEdit()
        form.addRow("交換元メーター", self.old_meter_combo)
        form.addRow(f"交換日時（{services.timezone_settings.get_timezone()}）", self.replaced_at)
        form.addRow("交換先", self.new_meter_mode)
        form.addRow("登録済みメーター", self.new_meter_combo)
        form.addRow("新規メーター名", self.new_name_edit)
        form.addRow("新規メーターのカテゴリ", self.new_category_combo)
        form.addRow("新規メーターの製造番号", self.new_serial_edit)
        layout.addLayout(form)
        self.target_status = QLabel()
        self.target_status.setWordWrap(True)
        layout.addWidget(self.target_status)
        self.reload_targets_button = QPushButton("交換対象の測定定義を更新")
        layout.addWidget(self.reload_targets_button)
        self.targets_container = QWidget()
        self.targets_layout = QVBoxLayout(self.targets_container)
        self.targets_layout.setContentsMargins(0, 0, 0, 0)
        layout.addWidget(self.targets_container)
        self.note_edit = QPlainTextEdit()
        self.note_edit.setMaximumHeight(90)
        layout.addWidget(QLabel("備考"))
        layout.addWidget(self.note_edit)
        buttons = QHBoxLayout()
        self.preview_button = QPushButton("交換の影響をプレビュー")
        self.apply_button = QPushButton("確認した交換を確定")
        self.apply_button.setEnabled(False)
        buttons.addWidget(self.preview_button)
        buttons.addWidget(self.apply_button)
        buttons.addStretch()
        layout.addLayout(buttons)
        self.preview_text = QPlainTextEdit()
        self.preview_text.setObjectName("replacementPreview")
        self.preview_text.setReadOnly(True)
        self.preview_text.setMinimumHeight(150)
        layout.addWidget(self.preview_text)

        self.old_meter_combo.currentIndexChanged.connect(self._old_meter_changed)
        self.replaced_at.value_changed.connect(self._target_context_changed)
        self.new_meter_mode.currentIndexChanged.connect(self._mode_changed)
        self.new_meter_combo.currentIndexChanged.connect(self._invalidate_preview)
        self.new_name_edit.textChanged.connect(self._invalidate_preview)
        self.new_category_combo.currentIndexChanged.connect(self._invalidate_preview)
        self.new_serial_edit.textChanged.connect(self._invalidate_preview)
        self.note_edit.textChanged.connect(self._invalidate_preview)
        staff_combo.currentIndexChanged.connect(self._invalidate_preview)
        self.reload_targets_button.clicked.connect(self._target_context_changed)
        self.preview_button.clicked.connect(lambda: run_guarded(self, self._preview))
        self.apply_button.clicked.connect(lambda: run_guarded(self, self._apply))
        self._mode_changed()

    def refresh_location(self, *, old_meter_id: str | None = None) -> None:
        self.invalidate_location()
        selected = old_meter_id or self.old_meter_combo.currentData()
        location_id = self._location_combo.currentData()
        meters = self._services.meter_master.list_meters(show_inactive=True)
        self._meters = {
            meter.meter_id: meter for meter in meters if meter.location_id == location_id
        }
        with QSignalBlocker(self.old_meter_combo):
            self.old_meter_combo.clear()
            self.old_meter_combo.addItem("交換元を選択してください", "")
            for meter in self._meters.values():
                inactive = "（無効）" if not meter.is_active else ""
                self.old_meter_combo.addItem(
                    f"{meter.meter_name}{inactive} [ID: {meter.meter_id}]", meter.meter_id
                )
            self.old_meter_combo.setCurrentIndex(max(self.old_meter_combo.findData(selected), 0))
        self._old_meter_changed()

    def invalidate_location(self) -> None:
        self._invalidate_preview()
        self._targets_valid = False
        self.preview_button.setEnabled(False)
        self.targets_container.setEnabled(False)
        self.target_status.setText("場所と交換元を確認してください。")

    def _old_meter_changed(self, *_args: object) -> None:
        old_id = self.old_meter_combo.currentData()
        selected_new = self.new_meter_combo.currentData()
        with QSignalBlocker(self.new_meter_combo):
            self.new_meter_combo.clear()
            self.new_meter_combo.addItem("交換先を選択してください", "")
            for meter in self._meters.values():
                if meter.is_active and meter.meter_id != old_id:
                    self.new_meter_combo.addItem(
                        f"{meter.meter_name} [ID: {meter.meter_id}]", meter.meter_id
                    )
            self.new_meter_combo.setCurrentIndex(
                max(self.new_meter_combo.findData(selected_new), 0)
            )
        old_meter = self._meters.get(old_id) if isinstance(old_id, str) else None
        if old_meter is not None:
            with QSignalBlocker(self.new_category_combo):
                self.new_category_combo.setCurrentIndex(
                    self.new_category_combo.findData(old_meter.category.value)
                )
        self._target_context_changed()

    def _mode_changed(self, *_args: object) -> None:
        creating = self.new_meter_mode.currentData() == "create"
        self.new_meter_combo.setEnabled(not creating)
        self.new_name_edit.setEnabled(creating)
        self.new_category_combo.setEnabled(creating)
        self.new_serial_edit.setEnabled(creating)
        self._invalidate_preview()

    def _invalidate_preview(self, *_args: object) -> None:
        self._previewed = None
        self.apply_button.setEnabled(False)
        self.preview_text.clear()

    def _target_context_changed(self, *_args: object) -> None:
        self._invalidate_preview()
        run_guarded(self, self._load_targets)

    def _load_targets(self) -> None:
        self._targets_valid = False
        self.preview_button.setEnabled(False)
        self.targets_container.setEnabled(False)
        try:
            meter_id = selected_value(self.old_meter_combo, "交換元メーター")
            at = self.replaced_at.to_utc(now=self._services.current_datetime())
            targets = self._services.meter_replacement.list_replacement_targets(meter_id, at)
        except DomainError as exc:
            self.target_status.setText(f"交換対象を確認できません: {exc.message}")
            return
        if targets != self._targets:
            previous_editors = self.definition_editors.copy()
            while self.targets_layout.count():
                self.targets_layout.takeAt(0)
            self.definition_editors.clear()
            for target in targets:
                definition = target.definition
                editor = previous_editors.pop(definition.measurement_definition_id, None)
                if (
                    editor is None
                    or editor.old_assignment != target.assignment
                    or editor.definition.value_schema != definition.value_schema
                ):
                    if editor is not None:
                        editor.deleteLater()
                    editor = ReplacementDefinitionEditor(definition, target.assignment)
                    editor.changed.connect(self._invalidate_preview)
                else:
                    editor.definition = definition
                    editor.setTitle(
                        f"{definition.display_name or definition.measurement_definition_id} "
                        f"[{definition.measurement_definition_id}]"
                    )
                self.targets_layout.addWidget(editor)
                self.definition_editors[definition.measurement_definition_id] = editor
            for editor in previous_editors.values():
                editor.deleteLater()
            self._targets = targets
        self._targets_valid = True
        self.targets_container.setEnabled(True)
        self.preview_button.setEnabled(True)
        self.target_status.setText(
            f"交換対象の測定定義: {len(targets)} 件。読値と新条件をそれぞれ確認してください。"
        )

    def _request(self) -> MeterReplacementInput:
        if not self._targets_valid:
            raise ValidationError("交換日時と交換元の測定定義を確認してください。")
        location_id = selected_value(self._location_combo, "場所")
        old_id = selected_value(self.old_meter_combo, "交換元メーター")
        new_id: str | None = None
        new_meter: MeterMasterCreateInput | None = None
        if self.new_meter_mode.currentData() == "create":
            new_meter = MeterMasterCreateInput(
                meter_name=self.new_name_edit.text(),
                category=Category(selected_value(self.new_category_combo, "カテゴリ")),
                location_id=location_id,
                meter_serial=self.new_serial_edit.text(),
            )
        else:
            new_id = selected_value(self.new_meter_combo, "交換先メーター")
        return MeterReplacementInput(
            old_meter_id=old_id,
            new_meter_id=new_id,
            new_meter=new_meter,
            replaced_at=self.replaced_at.to_utc(now=self._services.current_datetime()),
            old_final_reading=None,
            new_initial_reading=None,
            staff_id=selected_value(self._staff_combo, "担当者"),
            note=self.note_edit.toPlainText(),
            new_terms={key: editor.new_terms() for key, editor in self.definition_editors.items()},
            definition_readings=tuple(
                editor.definition_reading() for editor in self.definition_editors.values()
            ),
        )

    def _preview(self) -> None:
        self._invalidate_preview()
        request = self._request()
        preview = self._services.meter_replacement.preview_replacement(request)
        lines = [
            f"未完了枠の差し替え: {preview.incomplete_count} 件",
            f"非実測結果を残して差し替え: {preview.nonmeasured_count} 件",
        ]
        for item in preview.definitions:
            editor = self.definition_editors[item.measurement_definition_id]
            old = item.old_assignment
            new = item.new_terms
            lines.extend(
                (
                    f"{editor.title()}",
                    f"  倍率: {decimal_text(old.multiplier)} → {decimal_text(new.multiplier)}",
                    f"  ロールオーバー: {decimal_text(old.rollover_value)} → "
                    f"{decimal_text(new.rollover_value)}",
                    f"  旧メーター境界使用量: {decimal_text(item.old_usage_delta)}"
                    f" / ロールオーバー補正: {'あり' if item.rollover_applied else 'なし'}",
                )
            )
        lines.extend(f"検証エラー: {message}" for message in preview.errors)
        self.preview_text.setPlainText("\n".join(lines))
        if preview.can_register:
            self._previewed = (request, preview)
            self.apply_button.setEnabled(True)

    def _apply(self) -> None:
        if self._previewed is None:
            raise ValidationError("交換の影響をプレビューしてください。")
        request, preview = self._previewed
        try:
            current = self._request()
        except DomainError:
            self._invalidate_preview()
            raise
        if current != request:
            self._invalidate_preview()
            raise ValidationError("入力が変更されました。再度プレビューしてください。")
        if not confirm(
            self,
            "メーター交換",
            "表示した差し替え件数・各測定定義の旧条件と新条件を確認して確定しますか？",
        ):
            return
        try:
            unchanged = self._request() == request
        except DomainError:
            self._invalidate_preview()
            raise
        if not unchanged:
            self._invalidate_preview()
            raise ValidationError("入力が変更されました。再度プレビューしてください。")
        self._invalidate_preview()
        event = self._services.meter_replacement.register_replacement(
            request, expected_preview=preview
        )
        self.saved.emit(event.replacement_event_id)
