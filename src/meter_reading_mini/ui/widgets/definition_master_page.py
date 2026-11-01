"""測定定義と定義別スケジュールの管理。"""

from __future__ import annotations

from datetime import date

from PySide6.QtCore import QSignalBlocker, Signal
from PySide6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QDateEdit,
    QFormLayout,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QPushButton,
    QTabWidget,
    QVBoxLayout,
    QWidget,
)

from meter_reading_mini.app_context import AppContext
from meter_reading_mini.core.contract_models import (
    CalculationMethod,
    Category,
    Purpose,
    ReadingCycle,
    ValueSchema,
)
from meter_reading_mini.core.errors import ValidationError
from meter_reading_mini.core.meter_reading_sync_repositories import (
    MeasurementDefinitionCreateInput,
    MeasurementDefinitionRecord,
    MeasurementDefinitionUpdateRequest,
)
from meter_reading_mini.ui.error_handling import run_guarded
from meter_reading_mini.ui.widgets.decimal_line_edit import DecimalLineEdit
from meter_reading_mini.ui.widgets.form_helpers import (
    python_date,
    qdate,
    select_value,
    selected_value,
)
from meter_reading_mini.ui.widgets.master_common import (
    category_combo,
    confirm,
    make_table,
    populate_table,
    scroll_area,
    selected_id,
)
from meter_reading_mini.ui.widgets.schedule_editor import CYCLE_LABELS
from meter_reading_mini.ui.widgets.schedule_panel import SchedulePanel, ScheduleSubject

VALUE_LABELS = {
    ValueSchema.DECIMAL_STRING: "数値",
    ValueSchema.POWER_FACTOR_PERCENT_PHASE: "力率（%・位相）",
}
METHOD_LABELS = {
    CalculationMethod.DELTA: "前回との差分",
    CalculationMethod.RAW_ONLY: "読値のみ",
}
PURPOSE_LABELS = {
    Purpose.OPERATIONAL: "運用",
    Purpose.BILLING: "請求",
    Purpose.AUDIT: "監査",
}


class DefinitionMasterPage(QWidget):
    saved = Signal()

    def __init__(self, services: AppContext, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._services = services
        self._editing_id: str | None = None
        self._current_meter_id: str | None = None
        layout = QVBoxLayout(self)
        self.show_inactive = QCheckBox("無効な測定定義も表示")
        layout.addWidget(self.show_inactive)
        self.table = make_table(("測定対象", "場所", "周期", "状態", "ID"))
        self.table.setObjectName("definitionMasterTable")
        layout.addWidget(self.table)
        actions = QHBoxLayout()
        self.new_button = QPushButton("新規測定定義")
        self.active_button = QPushButton("無効化")
        actions.addWidget(self.new_button)
        actions.addWidget(self.active_button)
        actions.addStretch()
        layout.addLayout(actions)
        self.detail_tabs = QTabWidget()
        self.detail_tabs.addTab(scroll_area(self._build_form()), "測定定義")
        self.detail_tabs.addTab(scroll_area(self._build_schedule()), "スケジュール")
        layout.addWidget(self.detail_tabs, 1)

        self.show_inactive.toggled.connect(lambda: run_guarded(self, self.reload))
        self.table.currentCellChanged.connect(self._selection_changed)
        self.new_button.clicked.connect(self._new_definition)
        self.active_button.clicked.connect(lambda: run_guarded(self, self._toggle_active))
        self.save_button.clicked.connect(lambda: run_guarded(self, self._save_definition))
        self.location_combo.currentIndexChanged.connect(
            lambda *_: run_guarded(self, self._refresh_meters)
        )
        self.refresh_references()
        self.reload()
        self._new_definition()

    def _today(self) -> date:
        return self._services.current_datetime().date()

    def _build_form(self) -> QWidget:
        page = QWidget()
        form = QFormLayout(page)
        self.name_edit = QLineEdit()
        self.category = category_combo()
        self.unit_edit = QLineEdit()
        self.location_combo = QComboBox()
        self.meter_combo = QComboBox()
        self.cycle_combo = QComboBox()
        for cycle in ReadingCycle:
            self.cycle_combo.addItem(CYCLE_LABELS[cycle], cycle.value)
        self.value_combo = QComboBox()
        for value in ValueSchema:
            self.value_combo.addItem(VALUE_LABELS[value], value.value)
        self.method_combo = QComboBox()
        for method in CalculationMethod:
            self.method_combo.addItem(METHOD_LABELS[method], method.value)
        self.purpose_combo = QComboBox()
        for purpose in Purpose:
            self.purpose_combo.addItem(PURPOSE_LABELS[purpose], purpose.value)
        self.places_combo = QComboBox()
        self.places_combo.addItem("指定なし", "")
        for places in range(11):
            self.places_combo.addItem(f"{places} 桁", str(places))
        self.multiplier_edit = DecimalLineEdit("倍率")
        self.rollover_edit = DecimalLineEdit("ロールオーバー値")
        self.warning_edit = DecimalLineEdit("警告閾値")
        self.start_date_edit = QDateEdit()
        self.start_date_edit.setCalendarPopup(True)
        self.start_date_edit.setDisplayFormat("yyyy/MM/dd")
        self.start_date_edit.setDate(qdate(self._today()))
        self.id_label = QLabel("新規登録")
        self.precision_notice = QLabel(
            "過去の読値・使用量は変更されず、新しい入力から適用されます。"
        )
        self.precision_notice.setWordWrap(True)
        self.assignment_notice = QLabel(
            "現在割当メーターの変更は交換画面で行います（交換画面は今後実装予定です）。"
        )
        self.assignment_notice.setWordWrap(True)
        for label, widget in (
            ("測定対象名", self.name_edit),
            ("カテゴリ", self.category),
            ("単位", self.unit_edit),
            ("集計場所", self.location_combo),
            ("初期／現在割当メーター", self.meter_combo),
            ("検針周期", self.cycle_combo),
            ("値形式", self.value_combo),
            ("計算方式", self.method_combo),
            ("集計目的", self.purpose_combo),
            ("小数桁数", self.places_combo),
            ("", self.precision_notice),
            ("倍率", self.multiplier_edit),
            ("ロールオーバー値", self.rollover_edit),
            ("警告閾値", self.warning_edit),
            ("生成開始日", self.start_date_edit),
            ("", self.assignment_notice),
            ("測定定義 ID", self.id_label),
        ):
            form.addRow(label, widget)
        self.save_button = QPushButton("測定定義を保存")
        form.addRow(self.save_button)
        return page

    def _build_schedule(self) -> QWidget:
        service = self._services.measurement_schedules
        self.schedule_panel = SchedulePanel(
            self._today,
            load_schedule=lambda subject, day: service.get_schedule(
                subject.key, subject.cycle, day
            ),
            load_history=lambda subject: [
                (record.effective_from_date, record.schedule_json)
                for record in service.list_schedule_history(subject.key)
            ],
            save_schedule=lambda subject, schedule, day: service.save_schedule(
                subject.key, subject.cycle, schedule, effective_from_date=day
            ),
            reset_schedule=lambda subject, day: service.reset_to_default(
                subject.key, subject.cycle, effective_from_date=day
            ),
            subject_label="測定定義",
        )
        self.schedule_panel.saved.connect(self.saved.emit)
        return self.schedule_panel

    def refresh_references(self) -> None:
        locations = self._services.meter_master.list_locations()
        current = self.location_combo.currentData()
        with QSignalBlocker(self.location_combo):
            self.location_combo.clear()
            self.location_combo.addItem("場所を選択してください", "")
            for location in locations:
                names = " / ".join(location.location_names) or location.location_name
                if location.has_name_conflict:
                    names += "（名称不一致）"
                self.location_combo.addItem(
                    f"{names} [{location.location_id}]", location.location_id
                )
            index = self.location_combo.findData(current)
            self.location_combo.setCurrentIndex(max(index, 0))
        self._refresh_meters()

    def refresh_calculation_conditions(self) -> None:
        if self._editing_id is None:
            return
        definition = self._services.measurement_definitions.get_measurement_definition(
            self._editing_id
        )
        if definition is None:
            raise ValidationError("選択した測定定義が見つかりません。")
        self.multiplier_edit.set_decimal_value(definition.multiplier)
        self.rollover_edit.set_decimal_value(definition.rollover_value)

    def _refresh_meters(self) -> None:
        location_id = self.location_combo.currentData()
        current = self.meter_combo.currentData()
        meters = self._services.meter_master.list_meters(show_inactive=self._editing_id is not None)
        with QSignalBlocker(self.meter_combo):
            self.meter_combo.clear()
            self.meter_combo.addItem("メーターを選択してください", "")
            for meter in meters:
                if meter.location_id != location_id:
                    continue
                if not meter.is_active and meter.meter_id != self._current_meter_id:
                    continue
                inactive = "（無効）" if not meter.is_active else ""
                self.meter_combo.addItem(
                    f"{meter.meter_name}{inactive} [{meter.meter_id}]", meter.meter_id
                )
            target = self._current_meter_id if self._editing_id is not None else current
            index = self.meter_combo.findData(target)
            self.meter_combo.setCurrentIndex(max(index, 0))

    def _schedule_subjects(self) -> list[ScheduleSubject]:
        subjects: list[ScheduleSubject] = []
        for item in self._services.measurement_definitions.list_measurement_definitions(
            show_inactive=True
        ):
            if item.reading_cycle is None:
                raise ValidationError(
                    f"測定定義の周期がありません: {item.measurement_definition_id}"
                )
            status = "" if item.is_active else "（無効）"
            subjects.append(
                ScheduleSubject(
                    item.measurement_definition_id,
                    f"{item.display_name or item.measurement_definition_id}{status} "
                    f"[{item.measurement_definition_id}]",
                    item.reading_cycle,
                )
            )
        return subjects

    def reload(self) -> None:
        locations = {
            location.location_id: " / ".join(location.location_names) or location.location_name
            for location in self._services.meter_master.list_locations()
        }
        definitions = self._services.measurement_definitions.list_measurement_definitions(
            show_inactive=self.show_inactive.isChecked()
        )
        rows = []
        for item in definitions:
            if item.reading_cycle is None:
                raise ValidationError(
                    f"測定定義の周期がありません: {item.measurement_definition_id}"
                )
            rows.append(
                (
                    item.measurement_definition_id,
                    (
                        item.display_name or "",
                        locations.get(item.location_id or "", item.location_id or ""),
                        CYCLE_LABELS[item.reading_cycle],
                        "有効" if item.is_active else "無効",
                        item.measurement_definition_id,
                    ),
                )
            )
        found = populate_table(self.table, rows, self._editing_id)
        self.schedule_panel.set_subjects(self._schedule_subjects())
        if self._editing_id is not None and not found:
            self._new_definition()

    def _set_immutable(self, immutable: bool) -> None:
        for widget in (
            self.location_combo,
            self.meter_combo,
            self.cycle_combo,
            self.value_combo,
            self.method_combo,
            self.start_date_edit,
        ):
            widget.setEnabled(not immutable)
        self.multiplier_edit.setReadOnly(immutable)
        self.rollover_edit.setReadOnly(immutable)

    def _new_definition(self) -> None:
        self._editing_id = None
        self._current_meter_id = None
        with QSignalBlocker(self.table), QSignalBlocker(self.location_combo):
            self.table.clearSelection()
            self.table.setCurrentCell(-1, -1)
            self.location_combo.setCurrentIndex(0)
        self._set_immutable(False)
        self._refresh_meters()
        self.name_edit.clear()
        self.unit_edit.clear()
        self.warning_edit.clear()
        self.multiplier_edit.setText("1")
        self.rollover_edit.clear()
        self.category.setCurrentIndex(0)
        self.cycle_combo.setCurrentIndex(0)
        self.value_combo.setCurrentIndex(0)
        self.method_combo.setCurrentIndex(0)
        self.purpose_combo.setCurrentIndex(0)
        self.places_combo.setCurrentIndex(0)
        self.start_date_edit.setDate(qdate(self._today()))
        self.id_label.setText("新規登録")
        self.active_button.setEnabled(False)

    def _load_selected(self) -> None:
        definition_id = selected_id(self.table)
        if definition_id is None:
            return
        definition = self._services.measurement_definitions.get_measurement_definition(
            definition_id
        )
        if definition is None:
            raise ValidationError(f"測定定義が見つかりません: {definition_id}")
        self._load_definition(definition)
        self.schedule_panel.select_subject(definition_id)

    def _selection_changed(self, *_args: object) -> None:
        succeeded, _ = run_guarded(self, self._load_selected)
        if not succeeded:
            self._new_definition()

    def _load_definition(self, definition: MeasurementDefinitionRecord) -> None:
        if (
            definition.location_id is None
            or definition.reading_cycle is None
            or definition.generation_start_date is None
            or definition.category is None
            or definition.purpose is None
        ):
            raise ValidationError(
                f"測定定義の必須項目がありません: {definition.measurement_definition_id}"
            )
        self._editing_id = definition.measurement_definition_id
        self._current_meter_id = definition.meter_id
        self.name_edit.setText(definition.display_name or "")
        self.unit_edit.setText(definition.unit or "")
        select_value(self.category, definition.category.value, "カテゴリ")
        select_value(self.location_combo, definition.location_id, "集計場所")
        self._refresh_meters()
        if definition.meter_id is not None:
            select_value(self.meter_combo, definition.meter_id, "現在割当メーター")
        select_value(self.cycle_combo, definition.reading_cycle.value, "検針周期")
        select_value(self.value_combo, definition.value_schema.value, "値形式")
        select_value(self.method_combo, definition.calculation_method.value, "計算方式")
        select_value(self.purpose_combo, definition.purpose.value, "集計目的")
        select_value(
            self.places_combo,
            "" if definition.decimal_places is None else str(definition.decimal_places),
            "小数桁数",
        )
        self.multiplier_edit.set_decimal_value(definition.multiplier)
        self.rollover_edit.set_decimal_value(definition.rollover_value)
        self.warning_edit.set_decimal_value(definition.warning_threshold)
        start = qdate(definition.generation_start_date)
        if start < self.start_date_edit.minimumDate() or start > self.start_date_edit.maximumDate():
            raise ValidationError(
                f"生成開始日が画面で表示できる範囲外です: {definition.generation_start_date}"
            )
        self.start_date_edit.setDate(start)
        self.id_label.setText(definition.measurement_definition_id)
        self.active_button.setText("無効化" if definition.is_active else "再有効化")
        self.active_button.setEnabled(True)
        self._set_immutable(True)

    def _decimal_places(self) -> int | None:
        value = self.places_combo.currentData()
        if not isinstance(value, str):
            raise ValidationError("小数桁数を選択してください。")
        return int(value) if value else None

    def _save_definition(self) -> None:
        category = Category(selected_value(self.category, "カテゴリ"))
        purpose = Purpose(selected_value(self.purpose_combo, "集計目的"))
        warning = self.warning_edit.decimal_value()
        places = self._decimal_places()
        if self._editing_id is None:
            multiplier = self.multiplier_edit.decimal_value(required=True)
            if multiplier is None:
                raise ValidationError("倍率を入力してください。")
            definition = self._services.measurement_definitions.create_measurement_definition(
                MeasurementDefinitionCreateInput(
                    location_id=selected_value(self.location_combo, "集計場所"),
                    display_name=self.name_edit.text(),
                    category=category,
                    unit=self.unit_edit.text(),
                    value_schema=ValueSchema(selected_value(self.value_combo, "値形式")),
                    multiplier=multiplier,
                    decimal_places=places,
                    warning_threshold=warning,
                    rollover_value=self.rollover_edit.decimal_value(),
                    calculation_method=CalculationMethod(
                        selected_value(self.method_combo, "計算方式")
                    ),
                    reading_cycle=ReadingCycle(selected_value(self.cycle_combo, "検針周期")),
                    purpose=purpose,
                    generation_start_date=python_date(self.start_date_edit.date()),
                    initial_schedule_json=None,
                    initial_meter_id=selected_value(self.meter_combo, "初期メーター"),
                )
            )
        else:
            definition = self._services.measurement_definitions.update_measurement_definition(
                self._editing_id,
                MeasurementDefinitionUpdateRequest(
                    display_name=self.name_edit.text(),
                    category=category,
                    unit=self.unit_edit.text(),
                    decimal_places=places,
                    warning_threshold=warning,
                    purpose=purpose,
                ),
            )
        self._editing_id = definition.measurement_definition_id
        self.saved.emit()
        self.reload()
        self._load_selected()

    def _toggle_active(self) -> None:
        if self._editing_id is None:
            raise ValidationError("測定定義を選択してください。")
        definition = self._services.measurement_definitions.get_measurement_definition(
            self._editing_id
        )
        if definition is None:
            raise ValidationError("選択した測定定義が見つかりません。")
        if definition.is_active:
            message = (
                "今日以降の未消化枠を差し替え済みにし、新しい予定枠の生成を停止します。"
                "過去の結果は保持します。無効化しますか？"
            )
            if not confirm(self, "測定定義の無効化", message):
                return
            self._services.measurement_definitions.deactivate_measurement_definition(
                definition.measurement_definition_id
            )
        else:
            message = (
                "今日以降から予定枠を生成します。無効期間の枠は補完しません。再有効化しますか？"
            )
            if not confirm(self, "測定定義の再有効化", message):
                return
            self._services.measurement_definitions.activate_measurement_definition(
                definition.measurement_definition_id
            )
        self.saved.emit()
        self.reload()
        self._load_selected()
