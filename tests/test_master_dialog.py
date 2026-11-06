from __future__ import annotations

import sqlite3
from collections.abc import Iterator
from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal
from pathlib import Path

import pytest
from PySide6.QtCore import QDate, Qt
from PySide6.QtGui import QFont
from PySide6.QtWidgets import (
    QApplication,
    QComboBox,
    QLineEdit,
    QMessageBox,
    QScrollArea,
    QTableWidget,
    QWidget,
)
from pytestqt.qtbot import QtBot
from reading_service_helpers import DEFINITION_ID, LOCATION_ID, METER_ID, STAFF_ID, build_services
from test_assignment_calculation_settings import _setup_replacement

from meter_reading_mini.app_context import AppContext, create_app_context
from meter_reading_mini.core.aggregation_contracts import (
    AggregationCycleGroup,
    AggregationFormulaCreateInput,
    AggregationFormulaTerm,
    AggregationFormulaVersionInput,
)
from meter_reading_mini.core.contract_models import Category, Purpose, ReadingCycle
from meter_reading_mini.core.meter_reading_sync_repositories import (
    DailySchedule,
    MeasurementDefinitionRecord,
    MeterMasterCreateInput,
    MeterMasterRecord,
    StaffRecord,
)
from meter_reading_mini.db.repositories import SQLiteRepository
from meter_reading_mini.ui.dialogs.master_dialog import MasterDialog
from meter_reading_mini.ui.dialogs.settings_dialog import SettingsDialog
from meter_reading_mini.ui.display_settings import DisplaySettings, apply_display_settings
from meter_reading_mini.ui.main_window import MainWindow
from meter_reading_mini.ui.staff_selection_memory import StaffSelectionMemory
from meter_reading_mini.ui.widgets.aggregation_formula_page import AggregationFormulaPage


@dataclass
class MasterData:
    connection: sqlite3.Connection
    repository: SQLiteRepository
    services: AppContext
    dialog: MasterDialog
    messages: list[str]


@pytest.fixture
def master_data(
    file_database_repository: tuple[sqlite3.Connection, SQLiteRepository],
    fixed_clock: datetime,
    qtbot: QtBot,
    monkeypatch: pytest.MonkeyPatch,
) -> Iterator[MasterData]:
    connection, repository = file_database_repository
    build_services(connection, repository)
    services = create_app_context(connection, clock=lambda: fixed_clock)
    messages: list[str] = []
    monkeypatch.setattr(QMessageBox, "question", lambda *args: QMessageBox.StandardButton.Yes)
    monkeypatch.setattr(QMessageBox, "warning", lambda parent, title, text: messages.append(text))
    monkeypatch.setattr(QMessageBox, "critical", lambda parent, title, text: messages.append(text))
    dialog = MasterDialog(services, StaffSelectionMemory())
    qtbot.addWidget(dialog)
    dialog.show()
    yield MasterData(connection, repository, services, dialog, messages)
    monkeypatch.setattr(QMessageBox, "question", lambda *_args: QMessageBox.StandardButton.Yes)
    dialog.close()


def _select_row(table: QTableWidget, key: str) -> None:
    for row in range(table.rowCount()):
        item = table.item(row, 0)
        if item is not None and item.data(Qt.ItemDataRole.UserRole) == key:
            table.selectRow(row)
            return
    pytest.fail(f"一覧に {key} がありません")


def _meter(data: MasterData, meter_id: str) -> MeterMasterRecord:
    record = data.services.meter_master.get_meter(meter_id)
    assert record is not None
    return record


def _staff(data: MasterData, staff_id: str) -> StaffRecord:
    record = data.services.staff.get_staff(staff_id)
    assert record is not None
    return record


def _definition(data: MasterData, definition_id: str) -> MeasurementDefinitionRecord:
    record = data.services.measurement_definitions.get_measurement_definition(definition_id)
    assert record is not None
    return record


def test_meter_creation_location_rename_and_active_states(master_data: MasterData) -> None:
    page = master_data.dialog.meter_page
    page.name_edit.setText("新規メーター A")
    page.new_location_edit.setText("新しい場所")
    page.save_button.click()
    first = next(
        meter
        for meter in master_data.services.meter_master.list_meters()
        if meter.meter_name == "新規メーター A"
    )
    assert not page.location_combo.isEnabled()
    assert not page.new_location_edit.isEnabled()

    page.new_button.click()
    page.location_combo.setCurrentIndex(page.location_combo.findData(first.location_id))
    page.name_edit.setText("新規メーター B")
    page.save_button.click()
    second = next(
        meter
        for meter in master_data.services.meter_master.list_meters()
        if meter.meter_name == "新規メーター B"
    )
    page.active_button.click()
    assert not _meter(master_data, second.meter_id).is_active

    page.rename_location_combo.setCurrentIndex(
        page.rename_location_combo.findData(first.location_id)
    )
    page.rename_name_combo.setEditText("統一した場所")
    page.rename_button.click()
    assert _meter(master_data, first.meter_id).location_name == "統一した場所"
    assert _meter(master_data, second.meter_id).location_name == "統一した場所"

    page.show_inactive.setChecked(True)
    _select_row(page.table, second.meter_id)
    page.active_button.click()
    assert _meter(master_data, second.meter_id).is_active
    _select_row(page.table, METER_ID)
    page.active_button.click()
    assert "使用中" in master_data.messages[-1]
    assert _meter(master_data, METER_ID).is_active


def test_conflicting_location_candidates_must_be_unified(master_data: MasterData) -> None:
    page = master_data.dialog.meter_page
    page.location_combo.setCurrentIndex(page.location_combo.findData(LOCATION_ID))
    page.name_edit.setText("同じ場所の別メーター")
    page.save_button.click()
    other = next(
        meter
        for meter in master_data.services.meter_master.list_meters()
        if meter.meter_name == "同じ場所の別メーター"
    )
    master_data.connection.execute(
        "UPDATE physical_meters SET location_name = '旧名称' WHERE meter_id = ?",
        (other.meter_id,),
    )
    page.reload()
    page.new_button.click()
    page.location_combo.setCurrentIndex(page.location_combo.findData(LOCATION_ID))
    assert "旧名称" in page.location_help.text()
    assert "試験場所" in page.location_help.text()
    page.name_edit.setText("拒否される登録")
    page.save_button.click()
    assert "場所名が不一致" in master_data.messages[-1]
    assert all(
        meter.meter_name != "拒否される登録"
        for meter in master_data.services.meter_master.list_meters()
    )

    page.rename_location_combo.setCurrentIndex(page.rename_location_combo.findData(LOCATION_ID))
    candidates = [page.rename_name_combo.itemText(i) for i in range(page.rename_name_combo.count())]
    assert set(candidates) == {"旧名称", "試験場所"}
    page.rename_name_combo.setEditText("統一名称")
    page.rename_button.click()
    assert _meter(master_data, other.meter_id).location_name == "統一名称"
    assert _meter(master_data, METER_ID).location_name == "統一名称"


def test_staff_create_rename_duplicate_and_reactivation(master_data: MasterData) -> None:
    page = master_data.dialog.staff_page
    page.name_edit.setText("新担当")
    page.save_button.click()
    created = next(
        item for item in master_data.services.staff.list_staff() if item.display_name == "新担当"
    )
    page.name_edit.setText("変更後")
    page.save_button.click()
    assert _staff(master_data, created.staff_id).display_name == "変更後"
    page.new_button.click()
    page.name_edit.setText("変更後")
    page.save_button.click()
    assert "既に登録" in master_data.messages[-1]
    _select_row(page.table, created.staff_id)
    page.active_button.click()
    assert not _staff(master_data, created.staff_id).is_active
    page.show_inactive.setChecked(True)
    _select_row(page.table, created.staff_id)
    page.active_button.click()
    assert _staff(master_data, created.staff_id).is_active
    assert master_data.dialog.correction_page.staff_combo.findData(created.staff_id) >= 0


def test_formula_reference_blocks_definition_change_with_visible_error(
    master_data: MasterData,
) -> None:
    master_data.services.aggregation_formulas.create_formula(
        AggregationFormulaCreateInput(
            display_name="集計式",
            unit="m3",
            category=Category.WATER,
            cycle_group=AggregationCycleGroup.DAILY,
            purpose=Purpose.OPERATIONAL,
            decimal_places=2,
            initial_version=AggregationFormulaVersionInput(
                effective_from_date=date(2026, 2, 1),
                is_active=True,
                terms=(AggregationFormulaTerm(DEFINITION_ID, Decimal("1"), 0),),
            ),
        )
    )
    page = master_data.dialog.definition_page
    _select_row(page.table, DEFINITION_ID)
    page.unit_edit.setText("L")
    page.save_button.click()
    assert "集計式の履歴に参照" in master_data.messages[-1]
    assert "新しい測定定義" in master_data.messages[-1]
    assert _definition(master_data, DEFINITION_ID).unit == "m3"
    assert page.unit_edit.text() == "L"


def test_definition_create_backfill_edit_precision_and_reactivation(
    master_data: MasterData, qtbot: QtBot, tmp_path: Path
) -> None:
    window = MainWindow(master_data.services, display_settings_path=tmp_path / "ui_settings.json")
    qtbot.addWidget(window)
    master_data.dialog.saved.connect(window.notify_dialog_saved)
    page = master_data.dialog.definition_page
    assert page.start_date_edit.date() == QDate(2026, 2, 4)
    page.location_combo.setCurrentIndex(page.location_combo.findData(LOCATION_ID))
    page.meter_combo.setCurrentIndex(page.meter_combo.findData(METER_ID))
    page.name_edit.setText("過去からの測定")
    page.unit_edit.setText("m3")
    page.multiplier_edit.setText("1.25")
    page.rollover_edit.setText("1000.25")
    page.warning_edit.setText("0.50")
    page.places_combo.setCurrentIndex(page.places_combo.findData("2"))
    page.start_date_edit.setDate(QDate(2026, 2, 2))
    page.save_button.click()

    definition = next(
        item
        for item in master_data.services.measurement_definitions.list_measurement_definitions()
        if item.display_name == "過去からの測定"
    )
    assert definition.multiplier == Decimal("1.25")
    assert definition.rollover_value == Decimal("1000.25")
    assert definition.warning_threshold == Decimal("0.50")
    assignments = master_data.services.assignment_calculation_settings.list_assignments(
        definition.measurement_definition_id
    )
    assert len(assignments) == 1
    assert assignments[0].meter_id == METER_ID
    assert assignments[0].multiplier == Decimal("1.25")
    assert assignments[0].rollover_value == Decimal("1000.25")
    assert master_data.connection.execute(
        "SELECT business_date, task_state FROM planned_readings "
        "WHERE measurement_definition_id = ? ORDER BY business_date",
        (definition.measurement_definition_id,),
    ).fetchall() == [
        ("2026-02-02", "auto_closed"),
        ("2026-02-03", "auto_closed"),
        ("2026-02-04", "open"),
    ]
    assert not page.location_combo.isEnabled()
    assert not page.meter_combo.isEnabled()
    assert not page.cycle_combo.isEnabled()
    assert not page.value_combo.isEnabled()
    assert not page.method_combo.isEnabled()
    assert page.multiplier_edit.isReadOnly()
    assert page.rollover_edit.isReadOnly()
    assert "過去の読値・使用量は変更されず" in page.precision_notice.text()

    page.places_combo.setCurrentIndex(page.places_combo.findData("1"))
    page.save_button.click()
    assert "ロールオーバー値を表せない" in master_data.messages[-1]
    assert _definition(master_data, definition.measurement_definition_id).decimal_places == 2
    page.places_combo.setCurrentIndex(page.places_combo.findData("3"))
    page.warning_edit.setText("0.75")
    page.save_button.click()
    assert _definition(
        master_data, definition.measurement_definition_id
    ).warning_threshold == Decimal("0.75")

    page.active_button.click()
    assert not _definition(master_data, definition.measurement_definition_id).is_active
    page.show_inactive.setChecked(True)
    _select_row(page.table, definition.measurement_definition_id)
    page.active_button.click()
    assert _definition(master_data, definition.measurement_definition_id).is_active


def test_definition_activation_confirms_effects_and_respects_cancellation(
    master_data: MasterData, monkeypatch: pytest.MonkeyPatch
) -> None:
    page = master_data.dialog.definition_page
    _select_row(page.table, DEFINITION_ID)
    prompts: list[str] = []

    def decline(
        parent: QWidget, title: str, message: str, *args: object
    ) -> QMessageBox.StandardButton:
        prompts.append(message)
        return QMessageBox.StandardButton.No

    monkeypatch.setattr(QMessageBox, "question", decline)
    page.active_button.click()
    assert "今日以降の未消化枠" in prompts[-1]
    assert _definition(master_data, DEFINITION_ID).is_active

    monkeypatch.setattr(QMessageBox, "question", lambda *args: QMessageBox.StandardButton.Yes)
    page.active_button.click()
    assert not _definition(master_data, DEFINITION_ID).is_active
    page.show_inactive.setChecked(True)
    _select_row(page.table, DEFINITION_ID)
    monkeypatch.setattr(QMessageBox, "question", decline)
    page.active_button.click()
    assert "無効期間の枠は補完しません" in prompts[-1]
    assert not _definition(master_data, DEFINITION_ID).is_active


def test_definition_schedule_edit_and_reset_to_cycle_default(master_data: MasterData) -> None:
    panel = master_data.dialog.definition_page.schedule_panel
    panel.select_subject(DEFINITION_ID)
    editor = panel.editor
    editor.effective_date_edit.setDate(QDate(2026, 2, 6))
    editor.daily_table.cellWidget(0, 1).setProperty("text", "-1")
    panel.save_button.click()
    saved = master_data.services.measurement_schedules.get_schedule(
        DEFINITION_ID, ReadingCycle.DAILY_1X, date(2026, 2, 6)
    )
    assert isinstance(saved, DailySchedule)
    assert saved.reading_items[0].day_offset == -1
    assert "2026-02-06" in editor.history_text.toPlainText()
    panel.reset_button.click()
    history = master_data.services.measurement_schedules.list_schedule_history(DEFINITION_ID)
    assert any(
        item.effective_from_date == date(2026, 2, 6) and item.schedule_json is None
        for item in history
    )
    assert "周期既定" in editor.history_text.toPlainText()


def test_initial_meter_is_required_and_only_active_meters_at_selected_location_are_offered(
    master_data: MasterData,
) -> None:
    other = master_data.services.meter_master.create_meter(
        MeterMasterCreateInput("別の場所のメーター", Category.WATER, new_location_name="別の場所")
    )
    inactive = master_data.services.meter_master.create_meter(
        MeterMasterCreateInput("無効メーター", Category.WATER, location_id=LOCATION_ID)
    )
    master_data.services.meter_master.deactivate_meter(inactive.meter_id)
    page = master_data.dialog.definition_page
    page.refresh_references()
    page.location_combo.setCurrentIndex(page.location_combo.findData(LOCATION_ID))
    assert page.meter_combo.findData(METER_ID) >= 0
    assert page.meter_combo.findData(other.meter_id) < 0
    assert page.meter_combo.findData(inactive.meter_id) < 0
    page.name_edit.setText("メーター未指定")
    page.unit_edit.setText("m3")
    page.save_button.click()
    assert "初期メーター" in master_data.messages[-1]
    definitions = master_data.services.measurement_definitions.list_measurement_definitions()
    assert all(definition.display_name != "メーター未指定" for definition in definitions)


def test_out_of_range_legacy_start_date_is_reported_without_changing_it(
    master_data: MasterData,
) -> None:
    master_data.connection.execute(
        "UPDATE measurement_definitions SET generation_start_date = '1700-01-01' "
        "WHERE measurement_definition_id = ?",
        (DEFINITION_ID,),
    )
    page = master_data.dialog.definition_page
    page.reload()
    _select_row(page.table, DEFINITION_ID)
    assert "表示できる範囲外" in master_data.messages[-1]
    assert _definition(master_data, DEFINITION_ID).generation_start_date == date(1700, 1, 1)


def test_last_staff_selection_is_shared_only_in_memory(
    master_data: MasterData, qtbot: QtBot
) -> None:
    memory = StaffSelectionMemory()
    first = MasterDialog(master_data.services, memory)
    qtbot.addWidget(first)
    first.correction_page.staff_combo.setCurrentIndex(
        first.correction_page.staff_combo.findData(STAFF_ID)
    )
    assert memory.staff_id == STAFF_ID
    second = MasterDialog(master_data.services, memory)
    qtbot.addWidget(second)
    assert second.correction_page.staff_combo.currentData() == STAFF_ID


def _correction_dialog(
    file_database_repository: tuple[sqlite3.Connection, SQLiteRepository],
    fixed_clock: datetime,
    qtbot: QtBot,
    monkeypatch: pytest.MonkeyPatch,
) -> tuple[sqlite3.Connection, AppContext, MasterDialog, str, list[str]]:
    connection, repository = file_database_repository
    _, _, successor = _setup_replacement(connection, repository)
    services = create_app_context(connection, clock=lambda: fixed_clock)
    messages: list[str] = []
    monkeypatch.setattr(QMessageBox, "question", lambda *args: QMessageBox.StandardButton.Yes)
    monkeypatch.setattr(QMessageBox, "warning", lambda parent, title, text: messages.append(text))
    monkeypatch.setattr(QMessageBox, "critical", lambda parent, title, text: messages.append(text))
    dialog = MasterDialog(services, StaffSelectionMemory())
    qtbot.addWidget(dialog)
    assignment = next(
        item
        for item in services.assignment_calculation_settings.list_assignments(DEFINITION_ID)
        if item.meter_id == METER_ID
    )
    page = dialog.correction_page
    page.select_assignment(DEFINITION_ID, METER_ID, assignment.effective_from)
    page.staff_combo.setCurrentIndex(page.staff_combo.findData(STAFF_ID))
    page.reason_edit.setText("倍率設定の訂正")
    return connection, services, dialog, successor, messages


def test_ui_correction_previews_and_preserves_other_period_and_audit(
    file_database_repository: tuple[sqlite3.Connection, SQLiteRepository],
    fixed_clock: datetime,
    qtbot: QtBot,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    connection, services, dialog, successor, _ = _correction_dialog(
        file_database_repository, fixed_clock, qtbot, monkeypatch
    )
    page = dialog.correction_page
    audit_before = connection.execute(
        "SELECT raw_reading, recorded_at, updated_by, signature, device_id, note "
        "FROM reading_transactions ORDER BY planned_reading_id"
    ).fetchall()
    page.multiplier_edit.setText("12")
    before_changes = connection.total_changes
    page.preview_button.click()
    assert connection.total_changes == before_changes
    assert page.apply_button.isEnabled()
    assert "200 → 240" in page.preview_text.toPlainText()

    page.rollover_edit.setText("50")
    assert not page.apply_button.isEnabled()
    assert not page.preview_text.toPlainText()
    page.preview_button.click()
    assert "検証エラー" in page.preview_text.toPlainText()
    assert not page.apply_button.isEnabled()
    assert connection.total_changes == before_changes

    page.rollover_edit.setText("1000")
    page.preview_button.click()
    page.apply_button.click()
    assignments = services.assignment_calculation_settings.list_assignments(DEFINITION_ID)
    assert {item.meter_id: item.multiplier for item in assignments} == {
        METER_ID: Decimal("12"),
        "new-meter": Decimal("20"),
    }
    assert connection.execute(
        "SELECT old_meter_usage_delta FROM replacement_boundaries "
        "WHERE replacement_event_id = 'replace-1'"
    ).fetchone() == ("240",)
    assert connection.execute(
        "SELECT usage_delta FROM reading_transactions WHERE planned_reading_id = ?", (successor,)
    ).fetchone() == ("60",)
    assert (
        connection.execute(
            "SELECT raw_reading, recorded_at, updated_by, signature, device_id, note "
            "FROM reading_transactions ORDER BY planned_reading_id"
        ).fetchall()
        == audit_before
    )
    assert "倍率設定の訂正" in page.history_text.toPlainText()


def test_correction_rejects_stale_preview_inside_transaction(
    file_database_repository: tuple[sqlite3.Connection, SQLiteRepository],
    fixed_clock: datetime,
    qtbot: QtBot,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    connection, _, dialog, _, messages = _correction_dialog(
        file_database_repository, fixed_clock, qtbot, monkeypatch
    )
    page = dialog.correction_page
    page.multiplier_edit.setText("12")
    page.preview_button.click()
    assert page.apply_button.isEnabled()
    connection.execute(
        "UPDATE meter_assignments SET multiplier = '11' WHERE meter_id = ?", (METER_ID,)
    )
    page.apply_button.click()
    assert "再度プレビュー" in messages[-1]
    assert not page.apply_button.isEnabled()
    assert connection.execute(
        "SELECT COUNT(*) FROM assignment_calculation_corrections"
    ).fetchone() == (0,)
    assert connection.execute(
        "SELECT multiplier FROM meter_assignments WHERE meter_id = ?", (METER_ID,)
    ).fetchone() == ("11",)


def test_correction_input_errors_discard_preview_and_do_not_save(
    file_database_repository: tuple[sqlite3.Connection, SQLiteRepository],
    fixed_clock: datetime,
    qtbot: QtBot,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    connection, _, dialog, _, messages = _correction_dialog(
        file_database_repository, fixed_clock, qtbot, monkeypatch
    )
    page = dialog.correction_page
    page.multiplier_edit.setText("12")
    page.preview_button.click()
    assert page.apply_button.isEnabled()
    page.reason_edit.clear()
    assert not page.apply_button.isEnabled()
    page.preview_button.click()
    assert "訂正理由" in messages[-1]
    assert not page.preview_text.toPlainText()
    page.reason_edit.setText("設定誤り")
    page.multiplier_edit.setText("1e2")
    page.preview_button.click()
    assert "10進数文字列" in messages[-1]
    assert not page.apply_button.isEnabled()
    assert connection.execute(
        "SELECT COUNT(*) FROM assignment_calculation_corrections"
    ).fetchone() == (0,)


def _assert_reachable(area: QScrollArea, target: QWidget, dialog: QWidget) -> None:
    QApplication.processEvents()
    area.ensureWidgetVisible(target, 0, 0)
    QApplication.processEvents()
    center = target.mapToGlobal(target.rect().center())
    assert area.viewport().rect().contains(area.viewport().mapFromGlobal(center))
    assert dialog.rect().contains(dialog.mapFromGlobal(center))


def _set_formula_term(page: AggregationFormulaPage, coefficient: str = "-1.5") -> None:
    page.add_term_button.click()
    combo = page.terms_table.cellWidget(0, 0)
    edit = page.terms_table.cellWidget(0, 1)
    assert isinstance(combo, QComboBox)
    assert isinstance(edit, QLineEdit)
    combo.setCurrentIndex(combo.findData(DEFINITION_ID))
    edit.setText(coefficient)


def _prepare_formula(page: AggregationFormulaPage) -> None:
    page.name_edit.setText("後付け式")
    page.unit_edit.setText("m3")
    page.category_combo.setCurrentIndex(page.category_combo.findData(Category.WATER.value))
    page.effective_date.setDate(QDate(2026, 2, 1))
    _set_formula_term(page)


def test_formula_editor_retroactive_versions_and_inactive_period(
    master_data: MasterData, monkeypatch: pytest.MonkeyPatch
) -> None:
    confirmations: list[str] = []
    monkeypatch.setattr(
        QMessageBox,
        "question",
        lambda _parent, _title, text, *_args: (
            confirmations.append(text) or QMessageBox.StandardButton.Yes
        ),
    )
    page = master_data.dialog.formula_page
    _prepare_formula(page)
    page.save_formula_button.click()
    formulas = master_data.services.aggregation_formulas.list_formulas()
    assert len(formulas) == 1
    formula = formulas[0]
    versions = master_data.services.aggregation_formulas.list_versions(formula.formula_id)
    assert versions[0].is_retroactive
    assert versions[0].terms[0].coefficient == Decimal("-1.5")
    assert "現在業務日: 2026-02-04" in confirmations[0]
    assert "当時その式が存在した証明ではありません" in confirmations[0]
    assert not page.unit_edit.isEnabled()

    page.new_version_button.click()
    page.effective_date.setDate(QDate(2026, 2, 10))
    edit = page.terms_table.cellWidget(0, 1)
    assert isinstance(edit, QLineEdit)
    edit.setText("0.25")
    page.save_version_button.click()
    assert len(master_data.services.aggregation_formulas.list_versions(formula.formula_id)) == 2
    assert "2026-02-01" in confirmations[-1]
    assert "将来版は削除・変更しません" in confirmations[-1]

    page.new_version_button.click()
    page.effective_date.setDate(QDate(2026, 2, 6))
    page.active_check.setChecked(False)
    page.save_version_button.click()
    versions = master_data.services.aggregation_formulas.list_versions(formula.formula_id)
    assert [version.effective_from_date for version in versions] == [
        date(2026, 2, 1),
        date(2026, 2, 6),
        date(2026, 2, 10),
    ]
    assert not versions[1].is_active and not versions[1].terms
    assert versions[2].terms[0].coefficient == Decimal("0.25")
    assert page.effective_date.date() == QDate(2026, 2, 6)
    _select_row(page.history_table, "2026-02-01")
    assert not page.save_version_button.isEnabled()
    assert "2 版以上の初版は変更できません" in page.version_notice.text()


def test_formula_editor_resaves_only_initial_version_and_cancel_preserves_input(
    master_data: MasterData, monkeypatch: pytest.MonkeyPatch
) -> None:
    page = master_data.dialog.formula_page
    _prepare_formula(page)
    monkeypatch.setattr(QMessageBox, "question", lambda *_args: QMessageBox.StandardButton.No)
    page.save_formula_button.click()
    assert master_data.services.aggregation_formulas.list_formulas() == []
    assert page.name_edit.text() == "後付け式"

    monkeypatch.setattr(QMessageBox, "question", lambda *_args: QMessageBox.StandardButton.Yes)
    page.save_formula_button.click()
    formula = master_data.services.aggregation_formulas.list_formulas()[0]
    page.effective_date.setDate(QDate(2026, 1, 25))
    coefficient = page.terms_table.cellWidget(0, 1)
    assert isinstance(coefficient, QLineEdit)
    coefficient.setText("2.50")
    page.save_version_button.click()
    versions = master_data.services.aggregation_formulas.list_versions(formula.formula_id)
    assert len(versions) == 1
    assert versions[0].effective_from_date == date(2026, 1, 25)
    assert versions[0].terms[0].coefficient == Decimal("2.50")


def test_formula_editor_preserves_early_initial_effective_date(master_data: MasterData) -> None:
    formula = master_data.services.aggregation_formulas.create_formula(
        AggregationFormulaCreateInput(
            display_name="古い集計式",
            unit="m3",
            category=Category.WATER,
            cycle_group=AggregationCycleGroup.DAILY,
            purpose=Purpose.OPERATIONAL,
            decimal_places=None,
            initial_version=AggregationFormulaVersionInput(
                date.min,
                True,
                (AggregationFormulaTerm(DEFINITION_ID, Decimal("1"), 0),),
            ),
        )
    )
    page = master_data.dialog.formula_page
    page.reload()
    _select_row(page.table, formula.formula_id)
    assert page.effective_date.date() == QDate(1, 1, 1)
    coefficient = page.terms_table.cellWidget(0, 1)
    assert isinstance(coefficient, QLineEdit)
    coefficient.setText("2")

    page.save_version_button.click()

    version = master_data.services.aggregation_formulas.list_versions(formula.formula_id)[0]
    assert version.effective_from_date == date.min
    assert version.terms[0].coefficient == Decimal("2")


def test_formula_editor_preserves_but_cannot_reuse_inactive_definition(
    master_data: MasterData,
) -> None:
    page = master_data.dialog.formula_page
    _prepare_formula(page)
    page.save_formula_button.click()
    formula = master_data.services.aggregation_formulas.list_formulas()[0]
    master_data.services.measurement_definitions.deactivate_measurement_definition(DEFINITION_ID)
    page.refresh_definitions()
    page.reload()
    _select_row(page.table, formula.formula_id)
    combo = page.terms_table.cellWidget(0, 0)
    assert isinstance(combo, QComboBox)
    assert combo.currentData() == DEFINITION_ID
    assert "現在無効" in combo.currentText()
    page.new_version_button.click()
    page.effective_date.setDate(QDate(2026, 2, 5))
    page.save_version_button.click()
    assert "無効な測定定義を新たに使用できません" in master_data.messages[-1]
    assert len(master_data.services.aggregation_formulas.list_versions(formula.formula_id)) == 1


def test_formula_editor_does_not_discard_unsaved_metadata_or_version(
    master_data: MasterData,
) -> None:
    page = master_data.dialog.formula_page
    _prepare_formula(page)
    page.save_formula_button.click()
    formula = master_data.services.aggregation_formulas.list_formulas()[0]
    page.name_edit.setText("変更した名前")
    page.effective_date.setDate(QDate(2026, 1, 25))
    coefficient = page.terms_table.cellWidget(0, 1)
    assert isinstance(coefficient, QLineEdit)
    coefficient.setText("2.50")
    page.save_version_button.click()
    assert "名称・表示桁数の変更を先に保存" in master_data.messages[-1]
    assert page.name_edit.text() == "変更した名前"
    assert coefficient.text() == "2.50"
    page.save_formula_button.click()
    assert page.effective_date.date() == QDate(2026, 1, 25)
    assert coefficient.text() == "2.50"
    page.save_version_button.click()
    updated = master_data.services.aggregation_formulas.get_formula(formula.formula_id)
    assert updated is not None
    assert updated.display_name == "変更した名前"
    versions = master_data.services.aggregation_formulas.list_versions(formula.formula_id)
    assert versions[0].effective_from_date == date(2026, 1, 25)
    assert versions[0].terms[0].coefficient == Decimal("2.50")


def test_unsaved_formula_is_preserved_when_switching_or_starting_new(
    master_data: MasterData, monkeypatch: pytest.MonkeyPatch
) -> None:
    page = master_data.dialog.formula_page
    formula = master_data.services.aggregation_formulas.create_formula(
        AggregationFormulaCreateInput(
            display_name="別の式",
            unit="m3",
            category=Category.WATER,
            cycle_group=AggregationCycleGroup.DAILY,
            purpose=Purpose.OPERATIONAL,
            decimal_places=None,
            initial_version=AggregationFormulaVersionInput(
                date(2026, 2, 1),
                True,
                (AggregationFormulaTerm(DEFINITION_ID, Decimal("1"), 0),),
            ),
        )
    )
    page.reload()
    _prepare_formula(page)
    coefficient = page.terms_table.cellWidget(0, 1)
    assert isinstance(coefficient, QLineEdit)
    coefficient.setText("未完成")
    prompts: list[str] = []
    monkeypatch.setattr(
        QMessageBox,
        "question",
        lambda _parent, _title, text, *_args: prompts.append(text) or QMessageBox.StandardButton.No,
    )

    page.new_button.click()
    _select_row(page.table, formula.formula_id)
    assert len(prompts) == 2
    assert page.table.currentRow() == -1
    assert page.name_edit.text() == "後付け式"
    assert coefficient.text() == "未完成"

    monkeypatch.setattr(QMessageBox, "question", lambda *_args: QMessageBox.StandardButton.Yes)
    _select_row(page.table, formula.formula_id)
    assert page.name_edit.text() == "別の式"
    assert page._editing_id == formula.formula_id


def test_unsaved_version_is_preserved_when_switching_history_or_adding_version(
    master_data: MasterData, monkeypatch: pytest.MonkeyPatch
) -> None:
    page = master_data.dialog.formula_page
    _prepare_formula(page)
    page.save_formula_button.click()
    page.new_version_button.click()
    page.effective_date.setDate(QDate(2026, 2, 10))
    page.save_version_button.click()
    coefficient = page.terms_table.cellWidget(0, 1)
    assert isinstance(coefficient, QLineEdit)
    coefficient.setText("-")
    page.name_edit.setText("未保存の名称")
    prompts: list[str] = []
    monkeypatch.setattr(
        QMessageBox,
        "question",
        lambda _parent, _title, text, *_args: prompts.append(text) or QMessageBox.StandardButton.No,
    )

    _select_row(page.history_table, "2026-02-01")
    assert page.effective_date.date() == QDate(2026, 2, 10)
    assert page.history_table.currentRow() == 1
    assert coefficient.text() == "-"
    page.new_version_button.click()
    assert page.effective_date.date() == QDate(2026, 2, 10)
    assert coefficient.text() == "-"
    assert len(prompts) == 2

    monkeypatch.setattr(QMessageBox, "question", lambda *_args: QMessageBox.StandardButton.Yes)
    page.new_version_button.click()
    assert page.effective_date.date() == QDate(2026, 2, 4)
    assert coefficient.text() == "-"
    assert page._version_changed()
    _select_row(page.history_table, "2026-02-01")
    assert page.name_edit.text() == "未保存の名称"
    assert page.effective_date.date() == QDate(2026, 2, 1)


def test_master_dialog_confirms_before_discarding_formula_draft(
    master_data: MasterData, monkeypatch: pytest.MonkeyPatch
) -> None:
    dialog = master_data.dialog
    page = dialog.formula_page
    page.name_edit.setText("入力途中")
    prompts: list[str] = []
    monkeypatch.setattr(
        QMessageBox,
        "question",
        lambda _parent, _title, text, *_args: prompts.append(text) or QMessageBox.StandardButton.No,
    )

    dialog.accept()
    dialog.reject()
    assert not dialog.close()
    assert dialog.isVisible()
    assert page.name_edit.text() == "入力途中"
    assert len(prompts) == 3

    monkeypatch.setattr(QMessageBox, "question", lambda *_args: QMessageBox.StandardButton.Yes)
    assert dialog.close()
    assert not dialog.isVisible()


def test_definition_attribute_change_warns_and_can_be_cancelled(
    master_data: MasterData, monkeypatch: pytest.MonkeyPatch
) -> None:
    confirmations: list[str] = []
    monkeypatch.setattr(
        QMessageBox,
        "question",
        lambda _parent, _title, text, *_args: (
            confirmations.append(text) or QMessageBox.StandardButton.No
        ),
    )
    page = master_data.dialog.definition_page
    _select_row(page.table, DEFINITION_ID)
    original = _definition(master_data, DEFINITION_ID)
    page.purpose_combo.setCurrentIndex(page.purpose_combo.findData(Purpose.BILLING.value))
    page.save_button.click()
    assert _definition(master_data, DEFINITION_ID).purpose == original.purpose
    assert "過去の直接集計" in confirmations[-1]
    assert "運用 → 請求" in confirmations[-1]


def test_dialog_controls_fit_and_are_reachable_at_150_percent(
    master_data: MasterData,
    tmp_path: Path,
    qtbot: QtBot,
    restore_app_font: QFont,
) -> None:
    apply_display_settings(DisplaySettings(None, 150))
    master = master_data.dialog
    master.resize(1280, 680)
    QApplication.processEvents()
    assert master.width() <= 1280 and master.height() <= 680
    for index, target in (
        (0, master.meter_page.rename_button),
        (2, master.formula_page.save_version_button),
        (3, master.correction_page.apply_button),
        (4, master.staff_page.save_button),
    ):
        master.tabs.setCurrentIndex(index)
        area = master.tabs.widget(index)
        assert isinstance(area, QScrollArea)
        _assert_reachable(area, target, master)
    master.tabs.setCurrentIndex(1)
    definition = master.definition_page
    definition.detail_tabs.setCurrentIndex(0)
    form_area = definition.detail_tabs.widget(0)
    assert isinstance(form_area, QScrollArea)
    _assert_reachable(form_area, definition.save_button, master)
    definition.detail_tabs.setCurrentIndex(1)
    schedule_area = definition.detail_tabs.widget(1)
    assert isinstance(schedule_area, QScrollArea)
    _assert_reachable(schedule_area, definition.schedule_panel.save_button, master)

    settings = SettingsDialog(master_data.services, tmp_path / "ui_settings.json")
    qtbot.addWidget(settings)
    settings.resize(1280, 680)
    settings.show()
    QApplication.processEvents()
    assert settings.width() <= 1280 and settings.height() <= 680
    for index, target in (
        (0, settings.cutoff_save_button),
        (1, settings.schedule_panel.save_button),
        (2, settings.offset_save_button),
        (3, settings.display_save_button),
    ):
        settings.tabs.setCurrentIndex(index)
        area = settings.tabs.widget(index)
        assert isinstance(area, QScrollArea)
        _assert_reachable(area, target, settings)
