"""メーター交換と交換履歴の訂正。"""

from __future__ import annotations

from PySide6.QtCore import QSignalBlocker, Signal
from PySide6.QtWidgets import (
    QComboBox,
    QDialog,
    QFormLayout,
    QPushButton,
    QTabWidget,
    QVBoxLayout,
    QWidget,
)

from meter_reading_mini.app_context import AppContext
from meter_reading_mini.ui.error_handling import run_guarded
from meter_reading_mini.ui.staff_selection_memory import StaffSelectionMemory
from meter_reading_mini.ui.widgets.master_common import scroll_area
from meter_reading_mini.ui.widgets.replacement_history_page import ReplacementHistoryPage
from meter_reading_mini.ui.widgets.replacement_registration_page import ReplacementRegistrationPage
from meter_reading_mini.ui.widgets.staff_combo import StaffComboBox


class MeterReplacementDialog(QDialog):
    saved = Signal()

    def __init__(
        self,
        services: AppContext,
        staff_memory: StaffSelectionMemory,
        parent: QWidget | None = None,
        *,
        location_id: str | None = None,
        old_meter_id: str | None = None,
    ) -> None:
        super().__init__(parent)
        self._services = services
        self.setWindowTitle("メーター交換")
        self.resize(1000, 680)
        layout = QVBoxLayout(self)
        shared = QFormLayout()
        self.location_combo = QComboBox()
        self.location_combo.setObjectName("replacementLocation")
        self.staff_combo = StaffComboBox(services.staff, staff_memory)
        self.staff_combo.setObjectName("replacementStaff")
        shared.addRow("場所", self.location_combo)
        shared.addRow("担当者", self.staff_combo)
        layout.addLayout(shared)
        self.tabs = QTabWidget()
        self.registration_page = ReplacementRegistrationPage(
            services, self.location_combo, self.staff_combo
        )
        self.history_page = ReplacementHistoryPage(services, self.location_combo, self.staff_combo)
        self.registration_tab = scroll_area(self.registration_page)
        self.history_tab = scroll_area(self.history_page)
        self.tabs.addTab(self.registration_tab, "交換の登録")
        self.tabs.addTab(self.history_tab, "交換履歴・訂正")
        layout.addWidget(self.tabs)
        close_button = QPushButton("閉じる")
        close_button.clicked.connect(self.accept)
        layout.addWidget(close_button)
        self.location_combo.currentIndexChanged.connect(self._location_changed)
        self.registration_page.saved.connect(self._registration_saved)
        self.history_page.saved.connect(self.saved.emit)
        self._load_locations(location_id)
        self.registration_page.refresh_location(old_meter_id=old_meter_id)
        self.history_page.reload()

    def _load_locations(self, selected: str | None) -> None:
        with QSignalBlocker(self.location_combo):
            self.location_combo.clear()
            self.location_combo.addItem("場所を選択してください", "")
            for location in self._services.meter_master.list_locations():
                names = " / ".join(location.location_names) or location.location_name
                conflict = "（名称不一致）" if location.has_name_conflict else ""
                self.location_combo.addItem(
                    f"{names}{conflict} [ID: {location.location_id}]", location.location_id
                )
            self.location_combo.setCurrentIndex(
                max(self.location_combo.findData(selected or ""), 0)
            )

    def _location_changed(self, *_args: object) -> None:
        self.registration_page.invalidate_location()
        self.history_page.invalidate_location()
        run_guarded(self, self._refresh_location)

    def _refresh_location(self) -> None:
        self.registration_page.refresh_location()
        self.history_page.reload()

    def _registration_saved(self, event_id: str) -> None:
        self.saved.emit()
        run_guarded(self, lambda: self._after_registration(event_id))

    def _after_registration(self, event_id: str) -> None:
        self.registration_page.refresh_location()
        self.history_page.reload(select_event_id=event_id)
        self.tabs.setCurrentWidget(self.history_tab)
