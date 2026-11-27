from __future__ import annotations

import csv
import io
import sqlite3
from datetime import UTC, date, datetime
from decimal import Decimal
from pathlib import Path

import pytest
from PySide6.QtCore import QDate
from PySide6.QtGui import QImage
from PySide6.QtWidgets import QComboBox, QFileDialog, QLineEdit, QMessageBox
from pytestqt.qtbot import QtBot
from reading_service_helpers import (
    DEFINITION_ID,
    STAFF_ID,
    build_services,
    create_plan,
)

from meter_reading_mini.app_context import create_app_context
from meter_reading_mini.core.aggregation_contracts import AggregationTargetType
from meter_reading_mini.core.contract_models import Category, ResultStatus
from meter_reading_mini.core.meter_reading_sync_repositories import ManualReadingInput
from meter_reading_mini.db.connection import connect
from meter_reading_mini.db.repositories import SQLiteRepository
from meter_reading_mini.ui.dialogs.master_dialog import MasterDialog
from meter_reading_mini.ui.staff_selection_memory import StaffSelectionMemory
from meter_reading_mini.ui.widgets.dashboard import DashboardWindow


def test_file_database_formula_to_dashboard_and_exports(
    file_database_repository: tuple[sqlite3.Connection, SQLiteRepository],
    fixed_clock: datetime,
    tmp_path: Path,
    qtbot: QtBot,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    connection, repository = file_database_repository
    writer, _ = build_services(connection, repository)
    for day, reading in ((1, "10.00"), (2, "12.35")):
        scheduled = datetime(2026, 2, day, 3, tzinfo=UTC)
        planned_id = f"planned-{day}"
        create_plan(repository, planned_id, scheduled)
        writer.save_manual_reading(
            ManualReadingInput(
                planned_id, ResultStatus.MEASURED, scheduled, Decimal(reading), None
            ),
            staff_id=STAFF_ID,
        )
    services = create_app_context(connection, clock=lambda: fixed_clock)
    monkeypatch.setattr(QMessageBox, "question", lambda *_args: QMessageBox.StandardButton.Yes)
    monkeypatch.setattr(QMessageBox, "information", lambda *_args: QMessageBox.StandardButton.Ok)
    master = MasterDialog(services, StaffSelectionMemory())
    qtbot.addWidget(master)
    master.show()
    page = master.formula_page
    page.name_edit.setText("試験集計")
    page.unit_edit.setText("m3")
    page.category_combo.setCurrentIndex(page.category_combo.findData(Category.WATER.value))
    page.places_combo.setCurrentIndex(page.places_combo.findData("2"))
    page.effective_date.setDate(QDate(2026, 2, 1))
    page.add_term_button.click()
    definition = page.terms_table.cellWidget(0, 0)
    coefficient = page.terms_table.cellWidget(0, 1)
    assert isinstance(definition, QComboBox) and isinstance(coefficient, QLineEdit)
    definition.setCurrentIndex(definition.findData(DEFINITION_ID))
    coefficient.setText("-1.5")
    page.save_formula_button.click()
    formula = services.aggregation_formulas.list_formulas()[0]

    dashboard = DashboardWindow(services, StaffSelectionMemory())
    qtbot.addWidget(dashboard)
    dashboard.show()
    dashboard.start_date.setDate(QDate(2026, 2, 2))
    dashboard.end_date.setDate(QDate(2026, 2, 2))
    dashboard.refresh()
    dashboard.target_combo.setCurrentIndex(
        dashboard.target_combo.findData(f"formula:{formula.formula_id}")
    )
    display = dashboard.period_table.item(0, 3)
    raw = dashboard.period_table.item(0, 4)
    assert display is not None and display.text() == "-3.53"
    assert raw is not None and raw.text() == "-3.525"

    csv_path = tmp_path / "workflow.csv"
    png_path = tmp_path / "workflow.png"
    monkeypatch.setattr(QFileDialog, "getSaveFileName", lambda *_args: (str(csv_path), ""))
    dashboard.csv_button.click()
    monkeypatch.setattr(QFileDialog, "getSaveFileName", lambda *_args: (str(png_path), ""))
    dashboard.png_button.click()
    rows = list(csv.DictReader(io.StringIO(csv_path.read_text(encoding="utf-8-sig"), newline="")))
    assert rows[0]["表示値"] == "-3.53"
    assert rows[0]["無丸め値"] == "-3.525"
    assert not QImage(str(png_path)).isNull()

    second = connect(tmp_path / "meter-reading.db")
    try:
        restored = create_app_context(second, clock=lambda: fixed_clock)
        report = restored.aggregation_reports.get_report(date(2026, 2, 2), date(2026, 2, 3))
        persisted = next(
            result
            for result in report.periods[0].results
            if result.target_type is AggregationTargetType.FORMULA
            and result.target_id == formula.formula_id
        )
        assert persisted.value == Decimal("-3.525")
        assert persisted.applications[0].version.is_retroactive
    finally:
        second.close()
