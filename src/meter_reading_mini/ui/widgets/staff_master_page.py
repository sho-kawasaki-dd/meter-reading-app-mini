"""担当者の登録・変更・有効状態管理。"""

from __future__ import annotations

from PySide6.QtCore import QSignalBlocker, Signal
from PySide6.QtWidgets import (
    QCheckBox,
    QFormLayout,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from meter_reading_mini.app_context import AppContext
from meter_reading_mini.core.errors import ValidationError
from meter_reading_mini.core.meter_reading_sync_repositories import (
    StaffCreateRequest,
    StaffUpdateRequest,
)
from meter_reading_mini.ui.error_handling import run_guarded
from meter_reading_mini.ui.widgets.master_common import (
    confirm,
    make_table,
    populate_table,
    selected_id,
)


class StaffMasterPage(QWidget):
    saved = Signal()

    def __init__(self, services: AppContext, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._services = services
        self._editing_id: str | None = None
        layout = QVBoxLayout(self)
        self.show_inactive = QCheckBox("無効な担当者も表示")
        layout.addWidget(self.show_inactive)
        self.table = make_table(("担当者名", "状態", "ID"))
        self.table.setObjectName("staffMasterTable")
        layout.addWidget(self.table)
        actions = QHBoxLayout()
        self.new_button = QPushButton("新規担当者")
        self.active_button = QPushButton("無効化")
        actions.addWidget(self.new_button)
        actions.addWidget(self.active_button)
        actions.addStretch()
        layout.addLayout(actions)
        form = QFormLayout()
        self.name_edit = QLineEdit()
        self.id_label = QLabel("新規登録")
        self.save_button = QPushButton("担当者を保存")
        form.addRow("担当者名", self.name_edit)
        form.addRow("担当者 ID", self.id_label)
        form.addRow(self.save_button)
        layout.addLayout(form)
        layout.addStretch()
        self.show_inactive.toggled.connect(lambda: run_guarded(self, self.reload))
        self.table.currentCellChanged.connect(self._selection_changed)
        self.new_button.clicked.connect(self._new_staff)
        self.active_button.clicked.connect(lambda: run_guarded(self, self._toggle_active))
        self.save_button.clicked.connect(lambda: run_guarded(self, self._save_staff))
        self.reload()
        self._new_staff()

    def reload(self) -> None:
        staff = self._services.staff.list_staff(include_inactive=self.show_inactive.isChecked())
        rows = [
            (
                item.staff_id,
                (item.display_name, "有効" if item.is_active else "無効", item.staff_id),
            )
            for item in staff
        ]
        found = populate_table(self.table, rows, self._editing_id)
        if self._editing_id is not None and not found:
            self._new_staff()

    def _new_staff(self) -> None:
        self._editing_id = None
        with QSignalBlocker(self.table):
            self.table.clearSelection()
            self.table.setCurrentCell(-1, -1)
        self.name_edit.clear()
        self.id_label.setText("新規登録")
        self.active_button.setEnabled(False)

    def _load_selected(self) -> None:
        staff_id = selected_id(self.table)
        if staff_id is None:
            return
        staff = self._services.staff.get_staff(staff_id)
        if staff is None:
            raise ValidationError(f"担当者が見つかりません: {staff_id}")
        self._editing_id = staff_id
        self.name_edit.setText(staff.display_name)
        self.id_label.setText(staff_id)
        self.active_button.setText("無効化" if staff.is_active else "再有効化")
        self.active_button.setEnabled(True)

    def _selection_changed(self, *_args: object) -> None:
        succeeded, _ = run_guarded(self, self._load_selected)
        if not succeeded:
            self._new_staff()

    def _save_staff(self) -> None:
        if self._editing_id is None:
            staff = self._services.staff.create_staff(StaffCreateRequest(self.name_edit.text()))
        else:
            staff = self._services.staff.update_staff(
                self._editing_id, StaffUpdateRequest(self.name_edit.text())
            )
        self._editing_id = staff.staff_id
        self.saved.emit()
        self.reload()
        self._load_selected()

    def _toggle_active(self) -> None:
        if self._editing_id is None:
            raise ValidationError("担当者を選択してください。")
        staff = self._services.staff.get_staff(self._editing_id)
        if staff is None:
            raise ValidationError("選択した担当者が見つかりません。")
        action = "無効化" if staff.is_active else "再有効化"
        if not confirm(self, action, f"{staff.display_name}を{action}しますか？"):
            return
        if staff.is_active:
            self._services.staff.deactivate_staff(staff.staff_id)
        else:
            self._services.staff.activate_staff(staff.staff_id)
        self.saved.emit()
        self.reload()
        self._load_selected()
