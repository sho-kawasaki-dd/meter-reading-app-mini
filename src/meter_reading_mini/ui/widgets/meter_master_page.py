"""物理メーターと場所名の管理。"""

from __future__ import annotations

from PySide6.QtCore import QSignalBlocker, Signal
from PySide6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QFormLayout,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from meter_reading_mini.app_context import AppContext
from meter_reading_mini.core.contract_models import Category
from meter_reading_mini.core.errors import ValidationError
from meter_reading_mini.core.meter_reading_sync_repositories import (
    MeterLocationRecord,
    MeterMasterCreateInput,
    MeterMasterUpdateRequest,
)
from meter_reading_mini.ui.error_handling import run_guarded
from meter_reading_mini.ui.models.reading_overview_model import CATEGORY_LABELS
from meter_reading_mini.ui.widgets.form_helpers import select_value, selected_value
from meter_reading_mini.ui.widgets.master_common import (
    category_combo,
    confirm,
    make_table,
    populate_table,
    selected_id,
)


class MeterMasterPage(QWidget):
    saved = Signal()

    def __init__(self, services: AppContext, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._services = services
        self._editing_id: str | None = None
        self._locations: dict[str, MeterLocationRecord] = {}
        layout = QVBoxLayout(self)
        self.show_inactive = QCheckBox("無効なメーターも表示")
        layout.addWidget(self.show_inactive)
        self.table = make_table(("メーター名", "場所", "カテゴリ", "状態", "ID"))
        self.table.setObjectName("meterMasterTable")
        layout.addWidget(self.table)
        actions = QHBoxLayout()
        self.new_button = QPushButton("新規メーター")
        self.active_button = QPushButton("無効化")
        actions.addWidget(self.new_button)
        actions.addWidget(self.active_button)
        actions.addStretch()
        layout.addLayout(actions)

        group = QGroupBox("メーターの登録・編集")
        form = QFormLayout(group)
        self.name_edit = QLineEdit()
        self.category = category_combo()
        self.serial_edit = QLineEdit()
        self.location_combo = QComboBox()
        self.new_location_edit = QLineEdit()
        self.location_help = QLabel("場所名は下の「場所名を一括変更」で変更します。")
        self.location_help.setWordWrap(True)
        self.id_label = QLabel("新規登録")
        form.addRow("メーター名", self.name_edit)
        form.addRow("カテゴリ", self.category)
        form.addRow("製造番号", self.serial_edit)
        form.addRow("設置場所", self.location_combo)
        form.addRow("新しい場所名", self.new_location_edit)
        form.addRow("", self.location_help)
        form.addRow("メーター ID", self.id_label)
        self.save_button = QPushButton("メーターを保存")
        form.addRow(self.save_button)
        layout.addWidget(group)

        rename_group = QGroupBox("場所名を一括変更")
        rename_form = QFormLayout(rename_group)
        self.rename_location_combo = QComboBox()
        self.rename_name_combo = QComboBox()
        self.rename_name_combo.setEditable(True)
        self.rename_help = QLabel(
            "同じ場所の無効なメーターにも反映します。不一致の場合は候補名を選ぶか入力してください。"
        )
        self.rename_help.setWordWrap(True)
        self.rename_button = QPushButton("場所名を統一・変更")
        rename_form.addRow("対象の場所", self.rename_location_combo)
        rename_form.addRow("統一する場所名", self.rename_name_combo)
        rename_form.addRow("", self.rename_help)
        rename_form.addRow(self.rename_button)
        layout.addWidget(rename_group)

        self.show_inactive.toggled.connect(lambda: run_guarded(self, self.reload))
        self.table.currentCellChanged.connect(self._selection_changed)
        self.new_button.clicked.connect(self._new_meter)
        self.active_button.clicked.connect(lambda: run_guarded(self, self._toggle_active))
        self.save_button.clicked.connect(lambda: run_guarded(self, self._save_meter))
        self.location_combo.currentIndexChanged.connect(self._location_changed)
        self.rename_location_combo.currentIndexChanged.connect(self._rename_location_changed)
        self.rename_button.clicked.connect(lambda: run_guarded(self, self._rename_location))
        self.reload()
        self._new_meter()

    def reload(self) -> None:
        self._refresh_locations()
        meters = self._services.meter_master.list_meters(
            show_inactive=self.show_inactive.isChecked()
        )
        rows = [
            (
                meter.meter_id,
                (
                    meter.meter_name,
                    meter.location_name,
                    CATEGORY_LABELS[meter.category],
                    "有効" if meter.is_active else "無効",
                    meter.meter_id,
                ),
            )
            for meter in meters
        ]
        if self._editing_id is not None and not populate_table(self.table, rows, self._editing_id):
            self._new_meter()
        elif self._editing_id is None:
            populate_table(self.table, rows, None)

    def _refresh_locations(self) -> None:
        locations = self._services.meter_master.list_locations()
        self._locations = {location.location_id: location for location in locations}
        selected = self.location_combo.currentData()
        rename_selected = self.rename_location_combo.currentData()
        with QSignalBlocker(self.location_combo), QSignalBlocker(self.rename_location_combo):
            self.location_combo.clear()
            self.location_combo.addItem("新しい場所", "")
            self.rename_location_combo.clear()
            for location in locations:
                names = " / ".join(location.location_names) or location.location_name
                label = f"{names}（名称不一致）" if location.has_name_conflict else names
                label += f" [{location.location_id}]"
                self.location_combo.addItem(label, location.location_id)
                self.rename_location_combo.addItem(label, location.location_id)
            index = self.location_combo.findData(selected)
            self.location_combo.setCurrentIndex(max(index, 0))
            rename_index = self.rename_location_combo.findData(rename_selected)
            self.rename_location_combo.setCurrentIndex(max(rename_index, 0))
        self._location_changed()
        self._rename_location_changed()

    def _location_changed(self, *_args: object) -> None:
        location_id = self.location_combo.currentData()
        creating_location = self._editing_id is None and location_id == ""
        self.new_location_edit.setEnabled(creating_location)
        location = self._locations.get(location_id) if isinstance(location_id, str) else None
        if location is not None and location.has_name_conflict:
            self.location_help.setText(
                "場所名が不一致です。候補: "
                + " / ".join(location.location_names)
                + "。先に場所名を一括変更してください。"
            )
        else:
            self.location_help.setText("場所名は「場所名を一括変更」で変更します。")

    def _rename_location_changed(self, *_args: object) -> None:
        location_id = self.rename_location_combo.currentData()
        location = self._locations.get(location_id) if isinstance(location_id, str) else None
        self.rename_name_combo.clear()
        if location is not None:
            self.rename_name_combo.addItems(
                list(location.location_names) or [location.location_name]
            )
        self.rename_button.setEnabled(location is not None)

    def _new_meter(self) -> None:
        self._editing_id = None
        with QSignalBlocker(self.table), QSignalBlocker(self.location_combo):
            self.table.clearSelection()
            self.table.setCurrentCell(-1, -1)
            self.location_combo.setCurrentIndex(0)
        self.location_combo.setEnabled(True)
        self.name_edit.clear()
        self.serial_edit.clear()
        self.new_location_edit.clear()
        self.id_label.setText("新規登録")
        self.active_button.setEnabled(False)
        self._location_changed()

    def _load_selected(self) -> None:
        meter_id = selected_id(self.table)
        if meter_id is None:
            return
        meter = self._services.meter_master.get_meter(meter_id)
        if meter is None:
            raise ValidationError(f"メーターが見つかりません: {meter_id}")
        self._editing_id = meter_id
        self.name_edit.setText(meter.meter_name)
        self.serial_edit.setText(meter.meter_serial or "")
        select_value(self.category, meter.category.value, "カテゴリ")
        select_value(self.location_combo, meter.location_id, "設置場所")
        self.location_combo.setEnabled(False)
        self.new_location_edit.clear()
        self.id_label.setText(meter_id)
        self.active_button.setText("無効化" if meter.is_active else "再有効化")
        self.active_button.setEnabled(True)
        self._location_changed()

    def _selection_changed(self, *_args: object) -> None:
        succeeded, _ = run_guarded(self, self._load_selected)
        if not succeeded:
            self._new_meter()

    def _save_meter(self) -> None:
        category = Category(selected_value(self.category, "カテゴリ"))
        if self._editing_id is None:
            selected = self.location_combo.currentData()
            if not isinstance(selected, str):
                raise ValidationError("設置場所を選択してください。")
            meter = self._services.meter_master.create_meter(
                MeterMasterCreateInput(
                    meter_name=self.name_edit.text(),
                    category=category,
                    location_id=selected or None,
                    new_location_name=self.new_location_edit.text() if not selected else None,
                    meter_serial=self.serial_edit.text(),
                )
            )
        else:
            meter = self._services.meter_master.update_meter(
                self._editing_id,
                MeterMasterUpdateRequest(
                    meter_name=self.name_edit.text(),
                    category=category,
                    meter_serial=self.serial_edit.text(),
                ),
            )
        self._editing_id = meter.meter_id
        self.saved.emit()
        self.reload()
        self._load_selected()

    def _toggle_active(self) -> None:
        if self._editing_id is None:
            raise ValidationError("メーターを選択してください。")
        meter = self._services.meter_master.get_meter(self._editing_id)
        if meter is None:
            raise ValidationError("選択したメーターが見つかりません。")
        action = "無効化" if meter.is_active else "再有効化"
        if not confirm(self, action, f"{meter.meter_name}を{action}しますか？"):
            return
        if meter.is_active:
            self._services.meter_master.deactivate_meter(meter.meter_id)
        else:
            self._services.meter_master.activate_meter(meter.meter_id)
        self.saved.emit()
        self.reload()
        self._load_selected()

    def _rename_location(self) -> None:
        location_id = selected_value(self.rename_location_combo, "対象の場所")
        self._services.meter_master.rename_location(
            location_id, self.rename_name_combo.currentText()
        )
        self.saved.emit()
        self.reload()
        self._load_selected()
