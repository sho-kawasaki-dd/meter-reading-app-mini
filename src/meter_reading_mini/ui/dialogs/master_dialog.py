"""物理メーター・測定定義・担当者と計算条件を管理する。"""

from __future__ import annotations

from collections.abc import Callable

from PySide6.QtCore import Signal
from PySide6.QtWidgets import QDialog, QPushButton, QTabWidget, QVBoxLayout, QWidget

from meter_reading_mini.app_context import AppContext
from meter_reading_mini.core.meter_reading_sync_repositories import AssignmentReviewFlagRecord
from meter_reading_mini.ui.error_handling import run_guarded
from meter_reading_mini.ui.staff_selection_memory import StaffSelectionMemory
from meter_reading_mini.ui.widgets.aggregation_formula_page import AggregationFormulaPage
from meter_reading_mini.ui.widgets.assignment_correction_page import AssignmentCorrectionPage
from meter_reading_mini.ui.widgets.definition_master_page import DefinitionMasterPage
from meter_reading_mini.ui.widgets.master_common import scroll_area
from meter_reading_mini.ui.widgets.meter_master_page import MeterMasterPage
from meter_reading_mini.ui.widgets.staff_master_page import StaffMasterPage


class MasterDialog(QDialog):
    saved = Signal()

    def __init__(
        self,
        services: AppContext,
        staff_memory: StaffSelectionMemory,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self.setWindowTitle("マスタ管理")
        self.resize(1000, 700)
        layout = QVBoxLayout(self)
        self.tabs = QTabWidget()
        layout.addWidget(self.tabs)
        self.meter_page = MeterMasterPage(services)
        self.definition_page = DefinitionMasterPage(services)
        self.formula_page = AggregationFormulaPage(services)
        self.correction_page = AssignmentCorrectionPage(services, staff_memory)
        self.staff_page = StaffMasterPage(services)
        self.tabs.addTab(scroll_area(self.meter_page), "物理メーター")
        self.tabs.addTab(self.definition_page, "測定定義・スケジュール")
        self.tabs.addTab(scroll_area(self.formula_page), "集計式")
        self.correction_tab = scroll_area(self.correction_page)
        self.tabs.addTab(self.correction_tab, "計算条件の訂正")
        self.tabs.addTab(scroll_area(self.staff_page), "担当者")
        close_button = QPushButton("閉じる")
        close_button.clicked.connect(self.accept)
        layout.addWidget(close_button)
        self.meter_page.saved.connect(lambda: self._saved(self._after_meter))
        self.definition_page.saved.connect(lambda: self._saved(self._after_definition))
        self.formula_page.saved.connect(lambda: self._saved(self.formula_page.refresh_definitions))
        self.correction_page.saved.connect(lambda: self._saved(self._after_correction))
        self.staff_page.saved.connect(lambda: self._saved(self._after_staff))

    def _saved(self, refresh: Callable[[], None]) -> None:
        self.saved.emit()
        run_guarded(self, refresh)

    def _after_meter(self) -> None:
        self.definition_page.refresh_references()
        self.definition_page.reload()
        self.formula_page.refresh_definitions()
        self.correction_page.refresh_meters()

    def _after_definition(self) -> None:
        self.correction_page.refresh_definitions()
        self.formula_page.refresh_definitions()

    def _after_correction(self) -> None:
        self.definition_page.refresh_calculation_conditions()

    def _after_staff(self) -> None:
        self.correction_page.refresh_staff()

    def accept(self) -> None:
        succeeded, discard = run_guarded(self, self.formula_page.confirm_discard_changes)
        if succeeded and discard:
            super().accept()

    def reject(self) -> None:
        succeeded, discard = run_guarded(self, self.formula_page.confirm_discard_changes)
        if succeeded and discard:
            super().reject()

    def show_assignment_correction(self, flag: AssignmentReviewFlagRecord | None = None) -> None:
        self.tabs.setCurrentWidget(self.correction_tab)
        if flag is not None:
            self.correction_page.select_assignment(
                flag.measurement_definition_id, flag.meter_id, flag.assignment_effective_from
            )
