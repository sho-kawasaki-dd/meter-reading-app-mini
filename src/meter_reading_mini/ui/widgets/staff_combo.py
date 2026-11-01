"""有効な担当者を選び、直前の選択を起動中だけ共有する。"""

from __future__ import annotations

from PySide6.QtWidgets import QComboBox, QWidget

from meter_reading_mini.core.staff_service import StaffService
from meter_reading_mini.ui.staff_selection_memory import StaffSelectionMemory


class StaffComboBox(QComboBox):
    def __init__(
        self,
        service: StaffService,
        memory: StaffSelectionMemory,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self._memory = memory
        self.addItem("担当者を選択してください", "")
        for staff in service.list_staff():
            self.addItem(f"{staff.display_name} [ID: {staff.staff_id}]", staff.staff_id)
        self.setCurrentIndex(max(self.findData(memory.staff_id or ""), 0))
        self.currentIndexChanged.connect(self._remember_selection)

    def _remember_selection(self, *_args: object) -> None:
        value = self.currentData()
        if isinstance(value, str) and value:
            self._memory.staff_id = value
