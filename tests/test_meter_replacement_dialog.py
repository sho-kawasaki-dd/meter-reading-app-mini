from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal

import pytest
from PySide6.QtCore import Qt
from PySide6.QtGui import QFont
from PySide6.QtWidgets import QApplication, QMessageBox, QScrollArea, QWidget
from pytestqt.qtbot import QtBot
from reading_service_helpers import (
    DEFINITION_ID,
    LOCATION_ID,
    METER_ID,
    STAFF_ID,
    build_services,
    create_plan,
)

from meter_reading_mini.app_context import AppContext, create_app_context
from meter_reading_mini.core.contract_models import (
    CalculationMethod,
    Category,
    Purpose,
    ReadingCycle,
    ResultStatus,
    ValueSchema,
)
from meter_reading_mini.core.meter_reading_sync_repositories import (
    ManualReadingInput,
    MeasurementDefinitionCreateRequest,
    MeterMasterCreateRequest,
    ReadingOverviewRecord,
)
from meter_reading_mini.core.meter_replacement_service import MeterReplacementCorrectionInput
from meter_reading_mini.core.reading_write_orchestration import ReadingWriteOrchestrationService
from meter_reading_mini.db.repositories import SQLiteRepository
from meter_reading_mini.ui.dialogs.direct_reading_dialog import DirectReadingDialog
from meter_reading_mini.ui.dialogs.master_dialog import MasterDialog
from meter_reading_mini.ui.dialogs.meter_replacement_dialog import MeterReplacementDialog
from meter_reading_mini.ui.display_settings import DisplaySettings, apply_display_settings
from meter_reading_mini.ui.staff_selection_memory import StaffSelectionMemory

NOW = datetime(2026, 2, 5, tzinfo=UTC)
DAY = date(2026, 2, 4)


def _at(hour: int) -> datetime:
    return datetime(2026, 2, 4, hour, tzinfo=UTC)


@dataclass
class ReplacementData:
    connection: sqlite3.Connection
    repository: SQLiteRepository
    writer: ReadingWriteOrchestrationService
    services: AppContext
    memory: StaffSelectionMemory
    dialog: MeterReplacementDialog

    def reading(self, planned_reading_id: str) -> ReadingOverviewRecord:
        return next(
            item
            for item in self.services.reading_overview.get_overview(DAY).readings
            if item.planned_reading_id == planned_reading_id
        )

    def successor(self, planned_reading_id: str) -> str:
        old = self.repository.get_planned_reading(planned_reading_id)
        assert old is not None and old.superseded_by_planned_reading_id is not None
        return old.superseded_by_planned_reading_id


@pytest.fixture
def messages(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    result: list[str] = []
    monkeypatch.setattr(QMessageBox, "question", lambda *args: QMessageBox.StandardButton.Yes)
    monkeypatch.setattr(QMessageBox, "warning", lambda parent, title, text: result.append(text))
    monkeypatch.setattr(QMessageBox, "critical", lambda parent, title, text: result.append(text))
    return result


def _meter(repository: SQLiteRepository, meter_id: str) -> None:
    repository.create_meter_master(
        MeterMasterCreateRequest(
            meter_id=meter_id,
            location_id=LOCATION_ID,
            location_name="試験場所",
            meter_name=meter_id,
            category=Category.WATER,
        )
    )


@pytest.fixture
def replacement_data(
    file_database_repository: tuple[sqlite3.Connection, SQLiteRepository],
    qtbot: QtBot,
    messages: list[str],
) -> ReplacementData:
    connection, repository = file_database_repository
    writer, _ = build_services(
        connection, repository, multiplier=Decimal("10"), rollover_value=Decimal("1000")
    )
    for name, hour in (("before", 4), ("later", 6), ("open", 7)):
        create_plan(repository, name, _at(hour))
    writer.save_manual_reading(
        ManualReadingInput("before", ResultStatus.MEASURED, _at(4), Decimal("100"), None),
        staff_id=STAFF_ID,
    )
    _meter(repository, "new-meter")
    services = create_app_context(connection, clock=lambda: NOW)
    memory = StaffSelectionMemory()
    dialog = MeterReplacementDialog(
        services, memory, location_id=LOCATION_ID, old_meter_id=METER_ID
    )
    qtbot.addWidget(dialog)
    dialog.staff_combo.setCurrentIndex(dialog.staff_combo.findData(STAFF_ID))
    page = dialog.registration_page
    page.replaced_at.set_datetime(_at(5))
    page.new_meter_combo.setCurrentIndex(page.new_meter_combo.findData("new-meter"))
    editor = page.definition_editors[DEFINITION_ID]
    editor.old_final_edit.setText("120")
    editor.new_initial_edit.setText("5")
    assert editor.multiplier_edit is not None and editor.rollover_edit is not None
    editor.multiplier_edit.setText("20")
    editor.rollover_edit.setText("50")
    return ReplacementData(connection, repository, writer, services, memory, dialog)


def _read_via_ui(data: ReplacementData, qtbot: QtBot, slot: str, value: str) -> None:
    dialog = DirectReadingDialog(data.services, data.reading(slot), data.memory)
    qtbot.addWidget(dialog)
    assert dialog.staff_combo.currentData() == STAFF_ID
    dialog.value_edit.setText(value)
    assert dialog.save_button.isEnabled(), dialog.preview_text.toPlainText()
    dialog.save_button.click()


def _select_history(dialog: MeterReplacementDialog, event_id: str) -> None:
    table = dialog.history_page.table
    for row in range(table.rowCount()):
        item = table.item(row, 0)
        if item is not None and item.data(Qt.ItemDataRole.UserRole) == event_id:
            table.selectRow(row)
            return
    raise AssertionError(f"交換履歴が見つかりません: {event_id}")


def test_nonmeasured_correction_replacement_and_history_correction_are_integrated(
    replacement_data: ReplacementData, qtbot: QtBot, messages: list[str]
) -> None:
    data = replacement_data
    data.writer.save_manual_reading(
        ManualReadingInput("later", ResultStatus.MEASURED, _at(6), Decimal("110"), None),
        staff_id=STAFF_ID,
    )
    page = data.dialog.registration_page
    page.preview_button.click()
    assert "非実測へ訂正" in messages[-1]
    assert not page.apply_button.isEnabled()

    correction = DirectReadingDialog(data.services, data.reading("later"), data.memory)
    qtbot.addWidget(correction)
    correction.status_combo.setCurrentIndex(correction.status_combo.findData("meter_fault"))
    correction.note_edit.setPlainText("故障を確認")
    correction.save_button.click()
    old_audit = data.connection.execute(
        "SELECT * FROM reading_transactions WHERE planned_reading_id = 'later'"
    ).fetchone()
    before = data.connection.total_changes
    page.preview_button.click()
    assert page.apply_button.isEnabled()
    assert data.connection.total_changes == before
    preview = page.preview_text.toPlainText()
    assert "未完了枠の差し替え: 1 件" in preview
    assert "非実測結果を残して差し替え: 1 件" in preview
    assert "倍率: 10 → 20" in preview
    assert "ロールオーバー: 1000 → 50" in preview
    assert "境界使用量: 200" in preview

    page.apply_button.click()
    assert data.dialog.tabs.currentWidget() is data.dialog.history_tab
    assert (
        data.connection.execute(
            "SELECT * FROM reading_transactions WHERE planned_reading_id = 'later'"
        ).fetchone()
        == old_audit
    )
    successor = data.successor("later")
    old = data.repository.get_planned_reading("later")
    assert old is not None and old.task_state == "superseded"
    assert (
        data.connection.execute(
            "SELECT 1 FROM reading_transactions WHERE planned_reading_id = ?", (successor,)
        ).fetchone()
        is None
    )
    _read_via_ui(data, qtbot, successor, "8")
    assert data.connection.execute(
        "SELECT last_reading, usage_delta FROM reading_transactions WHERE planned_reading_id = ?",
        (successor,),
    ).fetchone() == ("5", "60")

    history = data.dialog.history_page
    assert not history.replaced_at_edit.isEnabled()
    assert not history.old_meter_edit.isEnabled()
    assert not history.new_meter_edit.isEnabled()
    editor = history.definition_editors[DEFINITION_ID]
    editor.old_final_edit.setText("130")
    editor.new_initial_edit.setText("6")
    history.note_edit.setPlainText("交換読値を訂正")
    history.correct_button.click()
    assert data.connection.execute(
        "SELECT old_final_reading, new_initial_reading, old_meter_usage_delta "
        "FROM replacement_boundaries"
    ).fetchone() == ("130", "6", "300")
    assert data.connection.execute(
        "SELECT last_reading, usage_delta FROM reading_transactions WHERE planned_reading_id = ?",
        (successor,),
    ).fetchone() == ("6", "40")
    assert (
        data.connection.execute(
            "SELECT * FROM reading_transactions WHERE planned_reading_id = 'later'"
        ).fetchone()
        == old_audit
    )
    assert data.memory.staff_id == STAFF_ID


def test_replacement_preview_invalidates_and_rechecks_before_creating_meter(
    replacement_data: ReplacementData, messages: list[str]
) -> None:
    data = replacement_data
    page = data.dialog.registration_page
    page.new_meter_mode.setCurrentIndex(page.new_meter_mode.findData("create"))
    page.new_name_edit.setText("画面から新規登録")
    page.new_serial_edit.setText("SERIAL-1")
    before = data.connection.total_changes
    page.preview_button.click()
    assert page.apply_button.isEnabled()
    assert data.connection.total_changes == before
    page.note_edit.setPlainText("更新")
    assert not page.apply_button.isEnabled()
    assert not page.preview_text.toPlainText()
    page.preview_button.click()
    assert page.apply_button.isEnabled()
    data.writer.save_manual_reading(
        ManualReadingInput("later", ResultStatus.SKIPPED, _at(6), None, None),
        staff_id=STAFF_ID,
    )
    before_save = data.connection.total_changes
    page.apply_button.click()
    assert "再度プレビュー" in messages[-1]
    assert not page.apply_button.isEnabled()
    assert data.connection.total_changes == before_save
    assert data.connection.execute("SELECT COUNT(*) FROM replacement_events").fetchone() == (0,)
    assert data.connection.execute(
        "SELECT COUNT(*) FROM physical_meters WHERE meter_name = '画面から新規登録'"
    ).fetchone() == (0,)
    page.preview_button.click()
    page.apply_button.click()
    assert data.connection.execute(
        "SELECT location_id, meter_serial FROM physical_meters "
        "WHERE meter_name = '画面から新規登録'"
    ).fetchone() == (LOCATION_ID, "SERIAL-1")
    assert data.connection.execute("SELECT COUNT(*) FROM replacement_events").fetchone() == (1,)


def test_replacement_input_and_history_changes_invalidate_confirmation(
    replacement_data: ReplacementData, messages: list[str]
) -> None:
    data = replacement_data
    page = data.dialog.registration_page
    page.preview_button.click()
    assert page.apply_button.isEnabled()
    editor = page.definition_editors[DEFINITION_ID]
    editor.old_final_edit.setText("1000")
    assert not page.apply_button.isEnabled()
    assert not page.preview_text.toPlainText()
    page.preview_button.click()
    assert "検証エラー" in page.preview_text.toPlainText()
    assert not page.apply_button.isEnabled()
    editor.old_final_edit.setText("120")
    page.preview_button.click()
    page.apply_button.click()
    history = data.dialog.history_page
    assert history._loaded is not None
    event_id = history._loaded.event.replacement_event_id
    editor = history.definition_editors[DEFINITION_ID]
    editor.old_final_edit.setText("130")
    data.services.meter_replacement.correct_replacement(
        MeterReplacementCorrectionInput(
            event_id,
            Decimal("125"),
            Decimal("5"),
            STAFF_ID,
            "別の訂正",
        )
    )
    before = data.connection.total_changes
    history.correct_button.click()
    assert "再読み込み" in messages[-1]
    assert data.connection.total_changes == before
    history.reload_button.click()
    assert history.definition_editors[DEFINITION_ID].old_final_edit.text() == "125"
    assert history.note_edit.toPlainText() == "別の訂正"


def test_location_switch_clears_old_previews_and_history(
    replacement_data: ReplacementData,
) -> None:
    data = replacement_data
    page = data.dialog.registration_page
    page.preview_button.click()
    assert page.apply_button.isEnabled()
    data.dialog.location_combo.setCurrentIndex(0)
    assert not page.apply_button.isEnabled()
    assert not page.preview_button.isEnabled()
    assert not data.dialog.history_page.correct_button.isEnabled()
    assert "交換元" in page.target_status.text()
    data.dialog.location_combo.setCurrentIndex(data.dialog.location_combo.findData(LOCATION_ID))
    assert not page.apply_button.isEnabled()


def test_replacement_future_time_keeps_input_and_requires_valid_targets(
    replacement_data: ReplacementData,
) -> None:
    page = replacement_data.dialog.registration_page
    page.preview_button.click()
    editor = page.definition_editors[DEFINITION_ID]
    assert page.apply_button.isEnabled()
    page.replaced_at.set_datetime(NOW + timedelta(seconds=1))
    assert "未来" in page.target_status.text()
    assert not page.preview_button.isEnabled()
    assert not page.apply_button.isEnabled()
    page.replaced_at.set_datetime(_at(5))
    assert page.definition_editors[DEFINITION_ID] is editor
    assert editor.old_final_edit.text() == "120"
    assert page.preview_button.isEnabled()
    assert not page.apply_button.isEnabled()


def test_replacement_confirmation_rechecks_changed_inputs(
    replacement_data: ReplacementData, monkeypatch: pytest.MonkeyPatch, messages: list[str]
) -> None:
    data = replacement_data
    page = data.dialog.registration_page
    page.preview_button.click()
    editor = page.definition_editors[DEFINITION_ID]
    assert editor.multiplier_edit is not None

    def change_during_confirmation(*_args: object) -> QMessageBox.StandardButton:
        assert editor.multiplier_edit is not None
        editor.multiplier_edit.setText("21")
        return QMessageBox.StandardButton.Yes

    monkeypatch.setattr(QMessageBox, "question", change_during_confirmation)
    before = data.connection.total_changes
    page.apply_button.click()
    assert "再度プレビュー" in messages[-1]
    assert not page.apply_button.isEnabled()
    assert data.connection.total_changes == before


def test_power_factor_replacement_keeps_boundary_readings_empty(
    replacement_data: ReplacementData,
) -> None:
    data = replacement_data
    data.repository.create_measurement_definition(
        MeasurementDefinitionCreateRequest(
            measurement_definition_id="power-factor",
            location_id=LOCATION_ID,
            display_name="力率",
            category=Category.ELECTRICITY,
            unit="%",
            value_schema=ValueSchema.POWER_FACTOR_PERCENT_PHASE,
            multiplier=Decimal("1"),
            decimal_places=None,
            warning_threshold=None,
            rollover_value=None,
            calculation_method=CalculationMethod.RAW_ONLY,
            reading_cycle=ReadingCycle.MONTHLY,
            purpose=Purpose.OPERATIONAL,
            generation_start_date=DAY,
            initial_schedule_json=None,
            initial_meter_id=METER_ID,
        )
    )
    page = data.dialog.registration_page
    page.reload_targets_button.click()
    assert not page.definition_editors["power-factor"].old_final_edit.isEnabled()
    assert not page.definition_editors["power-factor"].new_initial_edit.isEnabled()
    page.preview_button.click()
    assert page.apply_button.isEnabled(), page.preview_text.toPlainText()
    page.apply_button.click()
    history = data.dialog.history_page
    assert not history.definition_editors["power-factor"].old_final_edit.isEnabled()
    history.correct_button.click()
    assert data.connection.execute(
        "SELECT old_final_reading, new_initial_reading, old_meter_usage_delta "
        "FROM replacement_boundaries WHERE measurement_definition_id = 'power-factor'"
    ).fetchone() == (None, None, None)


def test_multiple_definitions_use_distinct_values_terms_and_rollover(
    replacement_data: ReplacementData, qtbot: QtBot
) -> None:
    data = replacement_data
    data.repository.create_measurement_definition(
        MeasurementDefinitionCreateRequest(
            measurement_definition_id="second",
            location_id=LOCATION_ID,
            display_name="別の測定対象",
            category=Category.WATER,
            unit="m3",
            value_schema=ValueSchema.DECIMAL_STRING,
            multiplier=Decimal("3"),
            decimal_places=2,
            warning_threshold=None,
            rollover_value=Decimal("100"),
            calculation_method=CalculationMethod.DELTA,
            reading_cycle=ReadingCycle.DAILY_1X,
            purpose=Purpose.OPERATIONAL,
            generation_start_date=DAY,
            initial_schedule_json=None,
            initial_meter_id=METER_ID,
        )
    )
    create_plan(data.repository, "second-before", _at(4), measurement_definition_id="second")
    create_plan(data.repository, "second-later", _at(6), measurement_definition_id="second")
    data.writer.save_manual_reading(
        ManualReadingInput("second-before", ResultStatus.MEASURED, _at(4), Decimal("90"), None),
        staff_id=STAFF_ID,
    )
    page = data.dialog.registration_page
    page.reload_targets_button.click()
    first = page.definition_editors[DEFINITION_ID]
    assert first.old_final_edit.text() == "120"
    assert first.new_initial_edit.text() == "5"
    assert first.multiplier_edit is not None and first.rollover_edit is not None
    assert first.multiplier_edit.text() == "20"
    assert first.rollover_edit.text() == "50"
    second = page.definition_editors["second"]
    second.old_final_edit.setText("10")
    second.new_initial_edit.setText("2")
    assert second.multiplier_edit is not None and second.rollover_edit is not None
    second.multiplier_edit.setText("4")
    second.rollover_edit.setText("40")
    page.preview_button.click()
    text = page.preview_text.toPlainText()
    assert "境界使用量: 60 / ロールオーバー補正: あり" in text
    assert "倍率: 3 → 4" in text
    assert page.apply_button.isEnabled()
    page.apply_button.click()
    assert data.connection.execute(
        "SELECT measurement_definition_id, old_final_reading, new_initial_reading, "
        "old_meter_usage_delta FROM replacement_boundaries ORDER BY measurement_definition_id"
    ).fetchall() == [
        ("second", "10", "2", "60"),
        (DEFINITION_ID, "120", "5", "200"),
    ]
    _read_via_ui(data, qtbot, data.successor("second-later"), "5")
    assert data.connection.execute(
        "SELECT last_reading, usage_delta FROM reading_transactions WHERE planned_reading_id = ?",
        (data.successor("second-later"),),
    ).fetchone() == ("2", "12")


def test_later_replacement_rejects_past_registration_and_allows_earlier_corrections(
    replacement_data: ReplacementData, qtbot: QtBot, messages: list[str]
) -> None:
    data = replacement_data
    page = data.dialog.registration_page
    page.preview_button.click()
    page.apply_button.click()
    first_event = data.connection.execute(
        "SELECT replacement_event_id FROM replacement_events"
    ).fetchone()[0]
    first_successor = data.successor("later")
    _read_via_ui(data, qtbot, first_successor, "8")
    _meter(data.repository, "third-meter")
    _meter(data.repository, "spare-meter")
    page.refresh_location(old_meter_id="new-meter")
    page.replaced_at.set_datetime(_at(7))
    page.new_meter_combo.setCurrentIndex(page.new_meter_combo.findData("third-meter"))
    editor = page.definition_editors[DEFINITION_ID]
    editor.old_final_edit.setText("9")
    editor.new_initial_edit.setText("2")
    assert editor.multiplier_edit is not None and editor.rollover_edit is not None
    editor.multiplier_edit.setText("30")
    editor.rollover_edit.setText("100")
    page.preview_button.click()
    assert page.apply_button.isEnabled(), page.preview_text.toPlainText()
    page.apply_button.click()
    next_successor = data.successor(data.successor("open"))
    _read_via_ui(data, qtbot, next_successor, "4")

    page.refresh_location(old_meter_id=METER_ID)
    page.replaced_at.set_datetime(_at(4) + timedelta(minutes=30))
    page.new_meter_combo.setCurrentIndex(page.new_meter_combo.findData("spare-meter"))
    editor = page.definition_editors[DEFINITION_ID]
    editor.old_final_edit.setText("110")
    editor.new_initial_edit.setText("1")
    before = data.connection.total_changes
    page.preview_button.click()
    assert "後に割当切替または交換" in messages[-1]
    assert not page.apply_button.isEnabled()
    assert data.connection.total_changes == before

    _select_history(data.dialog, first_event)
    history = data.dialog.history_page
    editor = history.definition_editors[DEFINITION_ID]
    editor.old_final_edit.setText("130")
    editor.new_initial_edit.setText("6")
    history.correct_button.click()
    assert data.connection.execute(
        "SELECT old_meter_usage_delta FROM replacement_boundaries WHERE replacement_event_id = ?",
        (first_event,),
    ).fetchone() == ("300",)
    assert data.connection.execute(
        "SELECT last_reading, usage_delta FROM reading_transactions WHERE planned_reading_id = ?",
        (first_successor,),
    ).fetchone() == ("6", "40")
    assert data.connection.execute(
        "SELECT last_reading, usage_delta FROM reading_transactions WHERE planned_reading_id = ?",
        (next_successor,),
    ).fetchone() == ("2", "60")

    master = MasterDialog(data.services, data.memory)
    qtbot.addWidget(master)
    old_assignment = next(
        assignment
        for assignment in data.services.assignment_calculation_settings.list_assignments(
            DEFINITION_ID
        )
        if assignment.meter_id == METER_ID
    )
    correction = master.correction_page
    correction.select_assignment(DEFINITION_ID, METER_ID, old_assignment.effective_from)
    correction.reason_edit.setText("旧期間の倍率を訂正")
    correction.multiplier_edit.setText("12")
    correction.preview_button.click()
    assert correction.apply_button.isEnabled()
    correction.apply_button.click()
    assert data.connection.execute(
        "SELECT old_meter_usage_delta FROM replacement_boundaries WHERE replacement_event_id = ?",
        (first_event,),
    ).fetchone() == ("360",)
    assert data.connection.execute(
        "SELECT meter_id, multiplier FROM meter_assignments "
        "WHERE measurement_definition_id = ? ORDER BY effective_from",
        (DEFINITION_ID,),
    ).fetchall() == [(METER_ID, "12"), ("new-meter", "20"), ("third-meter", "30")]
    assert data.connection.execute(
        "SELECT usage_delta FROM reading_transactions WHERE planned_reading_id = ?",
        (next_successor,),
    ).fetchone() == ("60",)

    new_period = DirectReadingDialog(data.services, data.reading(first_successor), data.memory)
    qtbot.addWidget(new_period)
    new_period.value_edit.setText("50")
    assert not new_period.save_button.isEnabled()
    assert "ロールオーバー" in new_period.preview_text.toPlainText()
    _read_via_ui(data, qtbot, "before", "150")
    assert data.connection.execute(
        "SELECT old_meter_usage_delta FROM replacement_boundaries WHERE replacement_event_id = ?",
        (first_event,),
    ).fetchone() == ("11760",)
    assert data.connection.execute(
        "SELECT usage_delta FROM reading_transactions WHERE planned_reading_id = ?",
        (first_successor,),
    ).fetchone() == ("40",)


def _assert_reachable(area: QScrollArea, target: QWidget, dialog: QWidget) -> None:
    QApplication.processEvents()
    area.ensureWidgetVisible(target, 0, 0)
    QApplication.processEvents()
    center = target.mapToGlobal(target.rect().center())
    assert area.viewport().rect().contains(area.viewport().mapFromGlobal(center))
    assert dialog.rect().contains(dialog.mapFromGlobal(center))


def test_direct_and_replacement_controls_are_reachable_at_150_percent(
    replacement_data: ReplacementData, qtbot: QtBot, restore_app_font: QFont
) -> None:
    apply_display_settings(DisplaySettings(None, 150))
    data = replacement_data
    dialog = data.dialog
    dialog.resize(1280, 680)
    dialog.show()
    QApplication.processEvents()
    assert dialog.width() <= 1280 and dialog.height() <= 680
    _assert_reachable(dialog.registration_tab, dialog.registration_page.apply_button, dialog)
    dialog.tabs.setCurrentWidget(dialog.history_tab)
    _assert_reachable(dialog.history_tab, dialog.history_page.correct_button, dialog)

    direct = DirectReadingDialog(data.services, data.reading("later"), data.memory)
    qtbot.addWidget(direct)
    direct.resize(1000, 680)
    direct.show()
    QApplication.processEvents()
    area = direct.findChild(QScrollArea)
    assert area is not None
    _assert_reachable(area, direct.preview_text, direct)
    save_center = direct.save_button.mapTo(direct, direct.save_button.rect().center())
    assert direct.rect().contains(save_center)
