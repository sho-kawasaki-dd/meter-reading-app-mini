"""交換履歴の表示と測定定義別読値の訂正。"""

from __future__ import annotations

from PySide6.QtCore import Signal
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
from meter_reading_mini.core.errors import ValidationError
from meter_reading_mini.core.meter_reading_sync_repositories import MeterAssignmentRecord
from meter_reading_mini.core.meter_replacement_service import (
    MeterReplacementCorrectionInput,
    ReplacementHistoryEntry,
)
from meter_reading_mini.ui.error_handling import run_guarded
from meter_reading_mini.ui.widgets.form_helpers import selected_value
from meter_reading_mini.ui.widgets.master_common import (
    confirm,
    make_table,
    populate_table,
    selected_id,
)
from meter_reading_mini.ui.widgets.replacement_definition_editor import (
    ReplacementDefinitionEditor,
    decimal_text,
)


class ReplacementHistoryPage(QWidget):
    saved = Signal()

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
        self._entries: dict[str, ReplacementHistoryEntry] = {}
        self._loaded: ReplacementHistoryEntry | None = None
        self.definition_editors: dict[str, ReplacementDefinitionEditor] = {}

        layout = QVBoxLayout(self)
        self.table = make_table(("交換日時", "旧メーター", "新メーター", "担当者", "イベント ID"))
        self.table.setObjectName("replacementHistory")
        layout.addWidget(self.table)
        self.reload_button = QPushButton("履歴を再読み込み")
        layout.addWidget(self.reload_button)
        form = QFormLayout()
        self.replaced_at_edit = QLineEdit()
        self.old_meter_edit = QLineEdit()
        self.new_meter_edit = QLineEdit()
        for field in (self.replaced_at_edit, self.old_meter_edit, self.new_meter_edit):
            field.setEnabled(False)
        form.addRow("交換日時（変更不可）", self.replaced_at_edit)
        form.addRow("旧メーター（変更不可）", self.old_meter_edit)
        form.addRow("新メーター（変更不可）", self.new_meter_edit)
        layout.addLayout(form)
        explanation = QLabel(
            "読値と備考を訂正できます。計算条件はマスタ管理の「計算条件の訂正」で変更します。"
        )
        explanation.setWordWrap(True)
        layout.addWidget(explanation)
        self.definitions_container = QWidget()
        self.definitions_layout = QVBoxLayout(self.definitions_container)
        self.definitions_layout.setContentsMargins(0, 0, 0, 0)
        layout.addWidget(self.definitions_container)
        self.note_edit = QPlainTextEdit()
        self.note_edit.setMaximumHeight(90)
        layout.addWidget(QLabel("備考"))
        layout.addWidget(self.note_edit)
        actions = QHBoxLayout()
        self.correct_button = QPushButton("表示した読値・備考を訂正")
        self.correct_button.setEnabled(False)
        actions.addWidget(self.correct_button)
        actions.addStretch()
        layout.addLayout(actions)
        self.table.currentCellChanged.connect(self._selection_changed)
        self.reload_button.clicked.connect(lambda: run_guarded(self, self.reload))
        self.correct_button.clicked.connect(lambda: run_guarded(self, self._correct))

    def reload(self, *, select_event_id: str | None = None) -> None:
        selected = select_event_id or selected_id(self.table)
        self._loaded = None
        self.correct_button.setEnabled(False)
        location_id = self._location_combo.currentData()
        entries = (
            self._services.meter_replacement.list_replacements(location_id)
            if isinstance(location_id, str) and location_id
            else []
        )
        self._entries = {entry.event.replacement_event_id: entry for entry in entries}
        meters = {
            meter.meter_id: meter.meter_name
            for meter in self._services.meter_master.list_meters(show_inactive=True)
        }
        rows = [
            (
                entry.event.replacement_event_id,
                (
                    entry.local_replaced_at.strftime("%Y/%m/%d %H:%M:%S"),
                    f"{meters.get(entry.event.old_meter_id, '不明')} [{entry.event.old_meter_id}]",
                    f"{meters.get(entry.event.new_meter_id, '不明')} [{entry.event.new_meter_id}]",
                    entry.event.updated_by,
                    entry.event.replacement_event_id,
                ),
            )
            for entry in entries
        ]
        populate_table(self.table, rows, selected)
        self._load_selection()

    def _selection_changed(self, *_args: object) -> None:
        run_guarded(self, self._load_selection)

    def _clear_selection(self) -> None:
        self._loaded = None
        self.correct_button.setEnabled(False)
        self.replaced_at_edit.clear()
        self.old_meter_edit.clear()
        self.new_meter_edit.clear()
        self.note_edit.clear()
        while self.definitions_layout.count():
            item = self.definitions_layout.takeAt(0)
            widget = item.widget() if item is not None else None
            if widget is not None:
                widget.deleteLater()
        self.definition_editors.clear()

    def invalidate_location(self) -> None:
        self._entries.clear()
        populate_table(self.table, [], None)
        self._clear_selection()

    def _assignment(
        self, entry: ReplacementHistoryEntry, definition_id: str, *, old: bool
    ) -> MeterAssignmentRecord:
        event = entry.event
        assignments = self._services.assignment_calculation_settings.list_assignments(definition_id)
        matches = [
            assignment
            for assignment in assignments
            if (
                assignment.meter_id == event.old_meter_id
                and assignment.effective_to == event.replaced_at
                if old
                else assignment.meter_id == event.new_meter_id
                and assignment.effective_from == event.replaced_at
            )
        ]
        if len(matches) != 1:
            raise ValidationError(f"交換履歴の割当条件を特定できません: {definition_id}")
        return matches[0]

    def _load_selection(self) -> None:
        key = selected_id(self.table)
        entry = self._entries.get(key) if key is not None else None
        self._clear_selection()
        if entry is None:
            return
        event = entry.event
        if not entry.definition_readings:
            raise ValidationError("交換履歴に測定定義別の境界がありません。")
        self.replaced_at_edit.setText(entry.local_replaced_at.strftime("%Y/%m/%d %H:%M:%S"))
        old_meter = self._services.meter_master.get_meter(event.old_meter_id)
        new_meter = self._services.meter_master.get_meter(event.new_meter_id)
        self.old_meter_edit.setText(
            f"{old_meter.meter_name if old_meter else '不明'} [ID: {event.old_meter_id}]"
        )
        self.new_meter_edit.setText(
            f"{new_meter.meter_name if new_meter else '不明'} [ID: {event.new_meter_id}]"
        )
        self.note_edit.setPlainText(event.note or "")
        for reading in entry.definition_readings:
            definition = self._services.measurement_definitions.get_measurement_definition(
                reading.measurement_definition_id
            )
            if definition is None:
                raise ValidationError(
                    f"交換履歴の測定定義が見つかりません: {reading.measurement_definition_id}"
                )
            editor = ReplacementDefinitionEditor(
                definition,
                self._assignment(entry, reading.measurement_definition_id, old=True),
                new_assignment=self._assignment(
                    entry, reading.measurement_definition_id, old=False
                ),
                readings=reading,
                edit_terms=False,
            )
            self.definitions_layout.addWidget(editor)
            self.definition_editors[reading.measurement_definition_id] = editor
        self._loaded = entry
        self.correct_button.setEnabled(True)

    def _request(self) -> MeterReplacementCorrectionInput:
        if self._loaded is None:
            raise ValidationError("訂正する交換履歴を選択してください。")
        return MeterReplacementCorrectionInput(
            replacement_event_id=self._loaded.event.replacement_event_id,
            old_final_reading=None,
            new_initial_reading=None,
            staff_id=selected_value(self._staff_combo, "担当者"),
            note=self.note_edit.toPlainText(),
            definition_readings=tuple(
                editor.definition_reading() for editor in self.definition_editors.values()
            ),
        )

    def _correct(self) -> None:
        entry = self._loaded
        if entry is None:
            raise ValidationError("訂正する交換履歴を選択してください。")
        request = self._request()
        original = {item.measurement_definition_id: item for item in entry.definition_readings}
        lines = [
            f"{self.definition_editors[item.measurement_definition_id].title()}: "
            f"旧 {decimal_text(original[item.measurement_definition_id].old_final_reading)} → "
            f"{decimal_text(item.old_final_reading)} / "
            f"新 {decimal_text(original[item.measurement_definition_id].new_initial_reading)} → "
            f"{decimal_text(item.new_initial_reading)}"
            for item in request.definition_readings
        ]
        note = request.note.strip() if request.note else ""
        lines.append("備考: " + (note or "なし"))
        lines.append("境界と後続実測値・交換境界を再計算します。訂正しますか？")
        if not confirm(self, "交換履歴の訂正", "\n".join(lines)):
            return
        unchanged = self._loaded == entry and self._request() == request
        if not unchanged:
            raise ValidationError("入力または対象が変わりました。内容を確認してください。")
        event = self._services.meter_replacement.correct_replacement(
            request, expected_history=entry
        )
        self.saved.emit()
        self.reload(select_event_id=event.replacement_event_id)
