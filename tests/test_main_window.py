from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass, replace
from datetime import UTC, date, datetime
from decimal import Decimal
from pathlib import Path

import pytest
from PySide6.QtCore import QDate, Qt, QTime, QTimer
from PySide6.QtGui import QBrush, QFont
from PySide6.QtWidgets import QApplication, QFileDialog, QMenu, QMessageBox, QToolBar
from pytestqt.qtbot import QtBot
from reading_service_helpers import (
    DEFINITION_ID,
    LOCATION_ID,
    METER_ID,
    SHEET_ID,
    STAFF_ID,
    build_services,
    create_plan,
)

from meter_reading_mini.app_context import AppContext, create_app_context
from meter_reading_mini.core import mobile_reading_sheet
from meter_reading_mini.core.contract_models import (
    CalculationMethod,
    Category,
    Purpose,
    ReadingCycle,
    ReadingSession,
    ResultStatus,
    ValueSchema,
)
from meter_reading_mini.core.errors import InvalidTimezoneError, ValidationError
from meter_reading_mini.core.meter_reading_sync_repositories import (
    ManualReadingInput,
    MeasurementDefinitionCreateRequest,
    MeterMasterCreateRequest,
    PlannedReadingCreateRequest,
    PlannedReadingTaskState,
    ReadingOverviewRecord,
    ResultSource,
    StaffCreateRequest,
)
from meter_reading_mini.core.mobile_reading import RESULT_KIND, encode_mr1_envelope
from meter_reading_mini.db.repositories import SQLiteRepository
from meter_reading_mini.ui.dialogs.direct_reading_dialog import DirectReadingDialog
from meter_reading_mini.ui.dialogs.master_dialog import MasterDialog
from meter_reading_mini.ui.dialogs.meter_replacement_dialog import MeterReplacementDialog
from meter_reading_mini.ui.dialogs.mobile_reading_dialog import MobileReadingDialog
from meter_reading_mini.ui.dialogs.settings_dialog import SettingsDialog
from meter_reading_mini.ui.display_settings import DisplaySettings, apply_display_settings
from meter_reading_mini.ui.main_window import MainWindow, _ReviewAssignment

DAY = date(2026, 2, 4)


@dataclass
class Clock:
    value: datetime

    def __call__(self) -> datetime:
        return self.value


@dataclass
class OverviewData:
    connection: sqlite3.Connection
    repository: SQLiteRepository
    clock: Clock
    services: AppContext


def _at(hour: int) -> datetime:
    return datetime(2026, 2, 4, hour, tzinfo=UTC)


@pytest.fixture
def overview_data(
    file_database_repository: tuple[sqlite3.Connection, SQLiteRepository],
) -> OverviewData:
    connection, repository = file_database_repository
    writer, _ = build_services(connection, repository)
    connection.execute(
        "UPDATE measurement_definitions SET generation_start_date = ?, warning_threshold = '29.99' "
        "WHERE measurement_definition_id = ?",
        (DAY.isoformat(), DEFINITION_ID),
    )
    for index in range(1, 6):
        create_plan(repository, f"plan-{index}", _at(index))
    for index, value in ((1, "100"), (2, "130")):
        writer.save_manual_reading(
            ManualReadingInput(
                f"plan-{index}", ResultStatus.MEASURED, _at(index), Decimal(value), None
            ),
            staff_id=STAFF_ID,
        )
    writer.save_manual_reading(
        ManualReadingInput("plan-3", ResultStatus.METER_FAULT, _at(3), None, None, "故障"),
        staff_id=STAFF_ID,
    )
    connection.execute(
        "UPDATE planned_readings SET task_state = 'superseded', "
        "close_reason = 'meter_replaced', superseded_by_planned_reading_id = 'plan-4' "
        "WHERE planned_reading_id = 'plan-3'"
    )
    connection.execute(
        "UPDATE planned_readings SET task_state = 'auto_closed', close_reason = 'cutoff_elapsed' "
        "WHERE planned_reading_id = 'plan-5'"
    )
    repository.create_measurement_definition(
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
    create_plan(repository, "power-factor-slot", _at(6), measurement_definition_id="power-factor")
    writer.save_manual_reading(
        ManualReadingInput(
            "power-factor-slot",
            ResultStatus.MEASURED,
            _at(6),
            None,
            {"percent": "95.50", "phase": "lag"},
        ),
        staff_id=STAFF_ID,
    )
    repository.create_meter_master(
        MeterMasterCreateRequest(
            meter_id="other-meter",
            location_id="other-location",
            location_name="別の場所",
            meter_name="ガスメーター",
            category=Category.GAS,
        )
    )
    repository.create_measurement_definition(
        MeasurementDefinitionCreateRequest(
            measurement_definition_id="other-definition",
            location_id="other-location",
            display_name="ガス",
            category=Category.GAS,
            unit="m3",
            value_schema=ValueSchema.DECIMAL_STRING,
            multiplier=Decimal("1"),
            decimal_places=2,
            warning_threshold=None,
            rollover_value=None,
            calculation_method=CalculationMethod.RAW_ONLY,
            reading_cycle=ReadingCycle.WEEKLY,
            purpose=Purpose.OPERATIONAL,
            generation_start_date=DAY,
            initial_schedule_json=None,
            initial_meter_id="other-meter",
        )
    )
    repository.create_planned_reading_if_missing(
        PlannedReadingCreateRequest(
            planned_reading_id="other-slot",
            measurement_definition_id="other-definition",
            meter_id="other-meter",
            location_id="other-location",
            business_date=DAY,
            release_date=DAY,
            reading_session=ReadingSession.SESSION_0800,
            scheduled_at=_at(7),
        )
    )
    clock = Clock(_at(3))
    return OverviewData(connection, repository, clock, create_app_context(connection, clock=clock))


@pytest.fixture
def window(overview_data: OverviewData, qtbot: QtBot) -> MainWindow:
    result = MainWindow(overview_data.services)
    qtbot.addWidget(result)
    result.show()
    return result


@pytest.fixture
def messages(monkeypatch: pytest.MonkeyPatch) -> list[tuple[str, str, str]]:
    result: list[tuple[str, str, str]] = []
    for name in ("information", "warning", "critical"):
        monkeypatch.setattr(
            QMessageBox,
            name,
            lambda parent, title, text, *args, kind=name: (
                result.append((kind, title, text)) or QMessageBox.StandardButton.Ok
            ),
        )
    return result


def _select(window: MainWindow, planned_reading_id: str) -> None:
    row = window.table_model.row_for_id(planned_reading_id)
    assert row is not None
    window.table.selectRow(row)


def _insert_review_flag(data: OverviewData, reason: str = "旧倍率が実測値と矛盾") -> None:
    assignment = data.repository.list_meter_assignments(DEFINITION_ID)[0]
    data.connection.execute(
        "INSERT INTO assignment_review_flags "
        "(measurement_definition_id, meter_id, assignment_effective_from, reason, created_at) "
        "VALUES (?, ?, ?, ?, ?)",
        (
            DEFINITION_ID,
            METER_ID,
            assignment.effective_from.strftime("%Y-%m-%d %H:%M:%S"),
            reason,
            "2026-02-04 03:00:00",
        ),
    )


def test_overview_displays_results_states_counts_and_selection(
    window: MainWindow, monkeypatch: pytest.MonkeyPatch
) -> None:
    model = window.table_model
    assert model.rowCount() == 7
    assert window.statusBar().currentMessage() == (
        "全 7 件 / 未消化 2 / 完了 3 / 自動クローズ 1 / 差し替え済み 1 / 警告 1"
    )
    assert not window.manual_reading_action.isEnabled()
    warning_row = model.row_for_id("plan-2")
    power_row = model.row_for_id("power-factor-slot")
    closed_row = model.row_for_id("plan-5")
    old_row = model.row_for_id("plan-3")
    assert None not in (warning_row, power_row, closed_row, old_row)
    assert warning_row is not None and power_row is not None and closed_row is not None
    assert old_row is not None
    assert model.data(model.index(warning_row, 7)) == "130"
    assert model.data(model.index(power_row, 7)) == "95.50%（遅れ）"
    assert model.data(model.index(old_row, 6)) == "旧結果: 計器不良"
    warning_color = model.data(model.index(warning_row, 0), Qt.ItemDataRole.BackgroundRole)
    closed_color = model.data(model.index(closed_row, 0), Qt.ItemDataRole.BackgroundRole)
    assert isinstance(warning_color, QBrush)
    assert isinstance(closed_color, QBrush)

    _select(window, "plan-3")
    assert not window.manual_reading_action.isEnabled()
    assert window.successor_button.isEnabled()
    assert "後継枠 ID: plan-4" in window.selection_details.text()
    _select(window, "plan-4")
    assert window.manual_reading_action.isEnabled()
    assert not window.successor_button.isEnabled()
    opened: list[str] = []
    monkeypatch.setattr(
        DirectReadingDialog,
        "exec",
        lambda dialog: opened.append(dialog._reading.planned_reading_id) or 0,
    )
    window.manual_reading_action.trigger()
    assert opened == ["plan-4"]
    toolbar = window.findChild(QToolBar, "mainToolbar")
    assert toolbar is not None
    assert window.mobile_reading_action in toolbar.actions()
    assert all("ダッシュボード" not in action.text() for action in toolbar.actions())


def test_combined_filters_and_successor_navigation(window: MainWindow, qtbot: QtBot) -> None:
    window.cycle_tabs.setCurrentIndex(3)
    window.state_filter.setCurrentIndex(window.state_filter.findData("completed"))
    window.category_filter.setCurrentIndex(window.category_filter.findData("electricity"))
    window.location_filter.setCurrentIndex(window.location_filter.findData(LOCATION_ID))
    window.keyword_filter.setText("力率")
    assert window.table_model.rowCount() == 1
    selected = window.table_model.reading_at(0)
    assert selected is not None
    assert selected.planned_reading_id == "power-factor-slot"
    assert "完了 1" in window.statusBar().currentMessage()
    window.category_filter.setCurrentIndex(window.category_filter.findData("gas"))
    assert window.table_model.rowCount() == 0

    window.cycle_tabs.setCurrentIndex(0)
    window.state_filter.setCurrentIndex(window.state_filter.findData("superseded"))
    window.category_filter.setCurrentIndex(0)
    window.location_filter.setCurrentIndex(0)
    window.keyword_filter.clear()
    _select(window, "plan-3")
    qtbot.mouseClick(window.successor_button, Qt.MouseButton.LeftButton)
    assert window.state_filter.currentIndex() == 0
    selected = window.table_model.reading_at(window.table.currentIndex().row())
    assert selected is not None
    assert selected.planned_reading_id == "plan-4"
    assert window.manual_reading_action.isEnabled()
    assert "差し替え済み 1" in window.statusBar().currentMessage()


def test_review_warning_blocks_only_affected_assignments_and_clears_after_correction(
    overview_data: OverviewData,
    qtbot: QtBot,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _insert_review_flag(overview_data)
    window = MainWindow(overview_data.services)
    qtbot.addWidget(window)
    window.show()
    assert window.review_group.isVisible()
    assert "旧倍率が実測値と矛盾" in window.review_text.toPlainText()
    assert DEFINITION_ID in window.review_text.toPlainText()
    _select(window, "plan-4")
    assert not window.manual_reading_action.isEnabled()
    assert "計算条件の確認待ち" in window.selection_details.text()
    _select(window, "power-factor-slot")
    assert window.manual_reading_action.isEnabled()
    opened: list[str] = []

    def inspect_dialog(dialog: MasterDialog) -> int:
        assert dialog.tabs.currentWidget() is dialog.correction_tab
        assert dialog.correction_page.definition_combo.currentData() == DEFINITION_ID
        assert "旧倍率が実測値と矛盾" in dialog.correction_page.review_reason.text()
        opened.append("opened")
        return 0

    monkeypatch.setattr(MasterDialog, "exec", inspect_dialog)
    qtbot.mouseClick(window.review_correction_button, Qt.MouseButton.LeftButton)
    assert opened == ["opened"]

    _select(window, "plan-4")
    overview_data.connection.execute("DELETE FROM assignment_review_flags")
    window.notify_dialog_saved()
    assert not window.review_group.isVisible()
    assert window.manual_reading_action.isEnabled()


def test_review_flag_added_after_selection_is_rechecked_before_opening(
    overview_data: OverviewData,
    window: MainWindow,
    messages: list[tuple[str, str, str]],
) -> None:
    _select(window, "plan-4")
    assert window.manual_reading_action.isEnabled()
    _insert_review_flag(overview_data)
    window.manual_reading_action.trigger()
    assert messages[-1][0] == "warning"
    assert "入力できません" in messages[-1][2]
    assert window.review_group.isVisible()
    assert not window.manual_reading_action.isEnabled()
    assert window.table_model.rowCount() == 7


def test_master_action_opens_dialog_and_refreshes_after_save(
    overview_data: OverviewData, window: MainWindow, monkeypatch: pytest.MonkeyPatch
) -> None:
    refresh = overview_data.services.planned_reading_maintenance.refresh
    calls: list[str] = []

    def tracked_refresh() -> None:
        calls.append("refresh")
        refresh()

    def interact(dialog: MasterDialog) -> int:
        assert dialog.parentWidget() is window
        dialog.staff_page.name_edit.setText("画面から追加")
        dialog.staff_page.save_button.click()
        return 0

    monkeypatch.setattr(
        overview_data.services.planned_reading_maintenance, "refresh", tracked_refresh
    )
    monkeypatch.setattr(MasterDialog, "exec", interact)
    window.master_action.trigger()
    assert calls == ["refresh"]
    assert any(
        item.display_name == "画面から追加" for item in overview_data.services.staff.list_staff()
    )


def test_replacement_action_uses_selection_and_refreshes_after_save(
    overview_data: OverviewData, window: MainWindow, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls: list[str] = []
    refresh = overview_data.services.planned_reading_maintenance.refresh

    def tracked_refresh() -> None:
        calls.append("refresh")
        refresh()

    def interact(dialog: MeterReplacementDialog) -> int:
        assert dialog.parentWidget() is window
        assert dialog.location_combo.currentData() == LOCATION_ID
        assert dialog.registration_page.old_meter_combo.currentData() == METER_ID
        dialog.staff_combo.setCurrentIndex(dialog.staff_combo.findData(STAFF_ID))
        assert window._staff_memory.staff_id == STAFF_ID
        dialog.saved.emit()
        return 0

    monkeypatch.setattr(
        overview_data.services.planned_reading_maintenance, "refresh", tracked_refresh
    )
    monkeypatch.setattr(MeterReplacementDialog, "exec", interact)
    _select(window, "plan-4")
    window.replacement_action.trigger()
    assert calls == ["refresh"]


def test_row_context_menu_selects_row_and_opens_direct_reading(
    window: MainWindow,
    messages: list[tuple[str, str, str]],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    assert window.table.contextMenuPolicy() is Qt.ContextMenuPolicy.CustomContextMenu
    opened: list[str] = []
    menus: list[QMenu] = []

    def interact(dialog: DirectReadingDialog) -> int:
        opened.append(dialog._reading.planned_reading_id)
        return 0

    def close_menu() -> None:
        menu = QApplication.activePopupWidget()
        assert isinstance(menu, QMenu)
        menus.append(menu)
        menu.close()

    monkeypatch.setattr(DirectReadingDialog, "exec", interact)
    _select(window, "plan-1")
    row = window.table_model.row_for_id("plan-4")
    assert row is not None
    position = window.table.visualRect(window.table_model.index(row, 0)).center()

    QTimer.singleShot(0, close_menu)
    window.table.customContextMenuRequested.emit(position)
    menus[0].actions()[0].trigger()

    selected = window._selected_reading()
    assert selected is not None
    assert selected.planned_reading_id == "plan-4"
    assert opened == ["plan-4"]
    assert messages == []
    assert len(menus) == 1
    assert [action.text() for action in menus[0].actions()] == [
        "PC 直接入力・訂正",
        "メーター交換",
    ]


def test_row_context_menu_opens_replacement_for_right_clicked_row(
    window: MainWindow,
    messages: list[tuple[str, str, str]],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    opened: list[tuple[str | None, str | None]] = []
    menus: list[QMenu] = []

    def interact(dialog: MeterReplacementDialog) -> int:
        opened.append(
            (
                dialog.location_combo.currentData(),
                dialog.registration_page.old_meter_combo.currentData(),
            )
        )
        return 0

    def close_menu() -> None:
        menu = QApplication.activePopupWidget()
        assert isinstance(menu, QMenu)
        menus.append(menu)
        menu.close()

    monkeypatch.setattr(MeterReplacementDialog, "exec", interact)
    _select(window, "plan-4")
    row = window.table_model.row_for_id("other-slot")
    assert row is not None
    position = window.table.visualRect(window.table_model.index(row, 0)).center()

    QTimer.singleShot(0, close_menu)
    window.table.customContextMenuRequested.emit(position)
    menus[0].actions()[1].trigger()

    assert opened == [("other-location", "other-meter")]
    assert messages == []


@pytest.mark.parametrize("blocked_reading_id", ["plan-3", "plan-4"])
def test_row_context_menu_keeps_direct_action_disabled_for_blocked_readings(
    overview_data: OverviewData,
    qtbot: QtBot,
    blocked_reading_id: str,
) -> None:
    if blocked_reading_id == "plan-4":
        _insert_review_flag(overview_data)
    window = MainWindow(overview_data.services)
    qtbot.addWidget(window)
    window.show()
    menus: list[QMenu] = []

    def close_menu() -> None:
        menu = QApplication.activePopupWidget()
        assert isinstance(menu, QMenu)
        menus.append(menu)
        menu.close()

    row = window.table_model.row_for_id(blocked_reading_id)
    assert row is not None
    position = window.table.visualRect(window.table_model.index(row, 0)).center()

    QTimer.singleShot(0, close_menu)
    window.table.customContextMenuRequested.emit(position)

    assert len(menus) == 1
    assert menus[0].actions()[0] is window.manual_reading_action
    assert not menus[0].actions()[0].isEnabled()


def test_direct_action_saves_and_refreshes_the_selected_row(
    overview_data: OverviewData, window: MainWindow, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(QMessageBox, "question", lambda *args: QMessageBox.StandardButton.Yes)

    def interact(dialog: DirectReadingDialog) -> int:
        dialog.staff_combo.setCurrentIndex(dialog.staff_combo.findData(STAFF_ID))
        dialog.value_edit.setText("140")
        assert dialog.save_button.isEnabled()
        dialog.save_button.click()
        return 0

    monkeypatch.setattr(DirectReadingDialog, "exec", interact)
    _select(window, "plan-4")
    window.manual_reading_action.trigger()
    row = window.table_model.row_for_id("plan-4")
    assert row is not None
    result = window.table_model.reading_at(row)
    assert result is not None
    assert result.result_status is ResultStatus.MEASURED
    assert result.raw_reading == Decimal("140")
    assert result.usage_delta == Decimal("10")
    assert overview_data.services.staff.require_active_staff(STAFF_ID).display_name == "試験担当"


def test_replacement_action_registers_and_refreshes_the_main_window(
    overview_data: OverviewData, window: MainWindow, monkeypatch: pytest.MonkeyPatch
) -> None:
    overview_data.repository.create_meter_master(
        MeterMasterCreateRequest(
            meter_id="replacement-from-window",
            location_id=LOCATION_ID,
            location_name="試験場所",
            meter_name="新メーター",
            category=Category.WATER,
        )
    )
    overview_data.clock.value = _at(8)
    calls: list[str] = []
    refresh = overview_data.services.planned_reading_maintenance.refresh

    def tracked_refresh() -> None:
        calls.append("refresh")
        refresh()

    def interact(dialog: MeterReplacementDialog) -> int:
        dialog.staff_combo.setCurrentIndex(dialog.staff_combo.findData(STAFF_ID))
        page = dialog.registration_page
        page.replaced_at.set_datetime(_at(7))
        page.new_meter_combo.setCurrentIndex(
            page.new_meter_combo.findData("replacement-from-window")
        )
        editor = page.definition_editors[DEFINITION_ID]
        editor.old_final_edit.setText("140")
        editor.new_initial_edit.setText("5")
        page.preview_button.click()
        assert page.apply_button.isEnabled(), page.preview_text.toPlainText()
        page.apply_button.click()
        return 0

    monkeypatch.setattr(QMessageBox, "question", lambda *args: QMessageBox.StandardButton.Yes)
    monkeypatch.setattr(
        overview_data.services.planned_reading_maintenance, "refresh", tracked_refresh
    )
    monkeypatch.setattr(MeterReplacementDialog, "exec", interact)
    _select(window, "plan-4")
    window.replacement_action.trigger()
    events = overview_data.services.meter_replacement.list_replacements(LOCATION_ID)
    assert len(events) == 1
    assert events[0].event.new_meter_id == "replacement-from-window"
    assert calls == ["refresh"]


def test_review_guidance_correction_clears_flag_and_unblocks_input(
    overview_data: OverviewData, qtbot: QtBot, monkeypatch: pytest.MonkeyPatch
) -> None:
    _insert_review_flag(overview_data)
    window = MainWindow(overview_data.services)
    qtbot.addWidget(window)
    window.show()
    _select(window, "plan-4")
    assert not window.manual_reading_action.isEnabled()
    monkeypatch.setattr(QMessageBox, "question", lambda *args: QMessageBox.StandardButton.Yes)

    def correct(dialog: MasterDialog) -> int:
        page = dialog.correction_page
        assert page.definition_combo.currentData() == DEFINITION_ID
        page.staff_combo.setCurrentIndex(page.staff_combo.findData(STAFF_ID))
        page.reason_edit.setText("旧条件を確認")
        page.preview_button.click()
        assert page.apply_button.isEnabled()
        page.apply_button.click()
        return 0

    monkeypatch.setattr(MasterDialog, "exec", correct)
    qtbot.mouseClick(window.review_correction_button, Qt.MouseButton.LeftButton)
    assert not overview_data.services.assignment_calculation_settings.list_review_flags()
    assert not window.review_group.isVisible()
    assert window.manual_reading_action.isEnabled()


def test_dialog_save_reports_refresh_failure_without_losing_committed_change(
    overview_data: OverviewData,
    window: MainWindow,
    messages: list[tuple[str, str, str]],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def fail_refresh() -> None:
        raise ValidationError("予定枠の再更新に失敗しました")

    def interact(dialog: MasterDialog) -> int:
        dialog.staff_page.name_edit.setText("保存された担当者")
        dialog.staff_page.save_button.click()
        return 0

    monkeypatch.setattr(overview_data.services.planned_reading_maintenance, "refresh", fail_refresh)
    monkeypatch.setattr(MasterDialog, "exec", interact)
    window.master_action.trigger()
    assert "再更新に失敗" in messages[-1][2]
    assert window.table_model.rowCount() == 0
    assert any(
        staff.display_name == "保存された担当者"
        for staff in overview_data.services.staff.list_staff()
    )


def test_settings_action_uses_configured_path_and_closes_previous_days(
    overview_data: OverviewData, tmp_path: Path, qtbot: QtBot, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "ui_settings.json"
    window = MainWindow(overview_data.services, display_settings_path=path)
    qtbot.addWidget(window)
    overview_data.clock.value = datetime(2026, 2, 4, 22, 0, tzinfo=UTC)

    def interact(dialog: SettingsDialog) -> int:
        assert dialog.parentWidget() is window
        assert dialog._display_settings_path == path
        dialog.cutoff_edit.setTime(QTime(6, 30))
        dialog.cutoff_save_button.click()
        return 0

    monkeypatch.setattr(SettingsDialog, "exec", interact)
    window.settings_action.trigger()
    assert overview_data.services.cutoff_settings.get_cutoff_time().strftime("%H:%M") == "06:30"
    assert overview_data.connection.execute(
        "SELECT task_state FROM planned_readings WHERE planned_reading_id = 'plan-4'"
    ).fetchone() == ("auto_closed",)


def test_review_assignment_uses_half_open_period(overview_data: OverviewData) -> None:
    _insert_review_flag(overview_data)
    flag = overview_data.services.assignment_calculation_settings.list_review_flags()[0]
    assignment = overview_data.repository.list_meter_assignments(DEFINITION_ID)[0]
    start = _at(2)
    end = _at(4)
    review = _ReviewAssignment(flag, replace(assignment, effective_from=start, effective_to=end))
    readings = overview_data.services.reading_overview.get_overview(DAY).readings
    first = next(reading for reading in readings if reading.planned_reading_id == "plan-1")
    assert not review.contains(replace(first, scheduled_at=_at(1)))
    assert review.contains(replace(first, scheduled_at=start))
    assert review.contains(replace(first, scheduled_at=_at(3)))
    assert not review.contains(replace(first, scheduled_at=end))
    assert not review.contains(replace(first, scheduled_at=_at(3), meter_id="other-meter"))


def test_assignment_review_resolution_failure_clears_the_view(
    overview_data: OverviewData,
    window: MainWindow,
    messages: list[tuple[str, str, str]],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _insert_review_flag(overview_data)
    service = overview_data.services.assignment_calculation_settings
    monkeypatch.setattr(service, "list_assignments", lambda definition_id: [])

    window.refresh_action.trigger()

    assert messages[-1][0] == "warning"
    assert "確認待ちの割当を特定できません" in messages[-1][2]
    assert window.table_model.rowCount() == 0
    assert not window.manual_reading_action.isEnabled()


def test_missing_successor_is_reported_without_selecting_another_row(
    overview_data: OverviewData, qtbot: QtBot, messages: list[tuple[str, str, str]]
) -> None:
    create_plan(
        overview_data.repository,
        "next-day-slot",
        datetime(2026, 2, 5, 1, tzinfo=UTC),
    )
    overview_data.connection.execute(
        "UPDATE planned_readings SET superseded_by_planned_reading_id = 'next-day-slot' "
        "WHERE planned_reading_id = 'plan-3'"
    )
    window = MainWindow(overview_data.services)
    qtbot.addWidget(window)
    window.show()
    _select(window, "plan-3")

    qtbot.mouseClick(window.successor_button, Qt.MouseButton.LeftButton)

    assert messages[-1][0] == "warning"
    assert "この業務日に後継枠が見つかりません" in messages[-1][2]
    assert window.table_model.row_for_id("plan-3") == window.table.currentIndex().row()


def test_date_controls_and_manual_refresh(window: MainWindow, qtbot: QtBot) -> None:
    assert window.date_edit.date() == QDate(2026, 2, 4)
    assert not window.next_day_button.isEnabled()
    window.date_edit.setDate(QDate(2026, 2, 5))
    assert window.date_edit.date() == QDate(2026, 2, 4)
    qtbot.mouseClick(window.previous_day_button, Qt.MouseButton.LeftButton)
    assert window.date_edit.date() == QDate(2026, 2, 3)
    assert window.next_day_button.isEnabled()
    assert window.table_model.rowCount() == 0
    qtbot.mouseClick(window.today_button, Qt.MouseButton.LeftButton)
    assert window.date_edit.date() == QDate(2026, 2, 4)
    assert not window.next_day_button.isEnabled()
    window.refresh_action.trigger()
    assert window.table_model.rowCount() == 7


def test_timer_closes_previous_day_after_cutoff_and_preserves_selected_history(
    overview_data: OverviewData, qtbot: QtBot
) -> None:
    overview_data.clock.value = datetime(2026, 2, 4, 21, 59, tzinfo=UTC)
    overview_data.services.startup_preparation.prepare()
    window = MainWindow(overview_data.services)
    qtbot.addWidget(window)
    window.show()
    assert window.boundary_timer.interval() == 60_000
    window.date_edit.setDate(QDate(2026, 2, 4))
    assert overview_data.connection.execute(
        "SELECT task_state FROM planned_readings WHERE planned_reading_id = 'plan-4'"
    ).fetchone() == ("open",)

    overview_data.clock.value = datetime(2026, 2, 4, 22, 0, tzinfo=UTC)
    window.boundary_timer.timeout.emit()

    assert window.date_edit.date() == QDate(2026, 2, 4)
    assert overview_data.connection.execute(
        "SELECT task_state FROM planned_readings WHERE planned_reading_id = 'plan-4'"
    ).fetchone() == ("auto_closed",)
    assert "自動クローズ 3" in window.statusBar().currentMessage()
    assert window.next_day_button.isEnabled()


def test_window_reconciles_if_cutoff_passes_after_startup_preparation(
    overview_data: OverviewData, qtbot: QtBot
) -> None:
    overview_data.clock.value = datetime(2026, 2, 4, 21, 59, tzinfo=UTC)
    overview_data.services.startup_preparation.prepare()
    overview_data.clock.value = datetime(2026, 2, 4, 22, 0, tzinfo=UTC)

    window = MainWindow(overview_data.services)
    qtbot.addWidget(window)
    window.show()

    assert overview_data.connection.execute(
        "SELECT task_state FROM planned_readings WHERE planned_reading_id = 'plan-4'"
    ).fetchone() == ("auto_closed",)


def test_window_backfills_if_business_day_changes_after_startup_preparation(
    overview_data: OverviewData, qtbot: QtBot
) -> None:
    overview_data.clock.value = datetime(2026, 2, 4, 14, 59, tzinfo=UTC)
    overview_data.services.startup_preparation.prepare()
    overview_data.clock.value = datetime(2026, 2, 4, 15, 0, tzinfo=UTC)

    window = MainWindow(overview_data.services)
    qtbot.addWidget(window)
    window.show()

    assert window.date_edit.date() == QDate(2026, 2, 5)
    assert window.table_model.rowCount() >= 1
    assert (
        overview_data.connection.execute(
            "SELECT COUNT(*) FROM planned_readings WHERE business_date = '2026-02-05'"
        ).fetchone()[0]
        >= 1
    )


def test_window_initial_refresh_failure_is_not_hidden(
    overview_data: OverviewData, qtbot: QtBot, monkeypatch: pytest.MonkeyPatch
) -> None:
    def fail_refresh() -> None:
        raise ValidationError("初期更新に失敗しました")

    monkeypatch.setattr(overview_data.services.planned_reading_maintenance, "refresh", fail_refresh)
    with pytest.raises(ValidationError, match="初期更新に失敗"):
        MainWindow(overview_data.services)


def test_timer_advances_current_day_and_clamps_when_clock_moves_back(
    overview_data: OverviewData, window: MainWindow
) -> None:
    overview_data.clock.value = datetime(2026, 2, 4, 15, 0, tzinfo=UTC)
    window.boundary_timer.timeout.emit()
    assert window.date_edit.date() == QDate(2026, 2, 5)
    assert window.date_edit.maximumDate() == QDate(2026, 2, 5)
    assert not window.next_day_button.isEnabled()
    overview_data.clock.value = _at(3)
    window.boundary_timer.timeout.emit()
    assert window.date_edit.date() == QDate(2026, 2, 4)
    assert window.date_edit.maximumDate() == QDate(2026, 2, 4)


def test_timer_retries_a_failed_boundary_update(
    overview_data: OverviewData,
    window: MainWindow,
    messages: list[tuple[str, str, str]],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service = overview_data.services.planned_reading_maintenance
    refresh = service.refresh
    calls = 0

    def fail_once() -> None:
        nonlocal calls
        calls += 1
        if calls == 1:
            raise ValidationError("締め処理に失敗しました")
        refresh()

    monkeypatch.setattr(service, "refresh", fail_once)
    overview_data.clock.value = datetime(2026, 2, 4, 22, 0, tzinfo=UTC)
    window.boundary_timer.timeout.emit()
    assert messages[-1][0] == "warning"
    assert "締め処理に失敗" in messages[-1][2]
    assert window.table_model.rowCount() == 0
    assert overview_data.connection.execute(
        "SELECT task_state FROM planned_readings WHERE planned_reading_id = 'plan-4'"
    ).fetchone() == ("open",)

    window.boundary_timer.timeout.emit()
    assert calls == 2
    assert window.date_edit.date() == QDate(2026, 2, 5)
    assert overview_data.connection.execute(
        "SELECT task_state FROM planned_readings WHERE planned_reading_id = 'plan-4'"
    ).fetchone() == ("auto_closed",)
    assert "失敗" not in window.statusBar().currentMessage()


def test_timer_uses_business_timezone_through_dst_gap(
    file_database_repository: tuple[sqlite3.Connection, SQLiteRepository], qtbot: QtBot
) -> None:
    connection, repository = file_database_repository
    clock = Clock(datetime(2026, 3, 8, 6, 59, tzinfo=UTC))
    services = create_app_context(connection, clock=clock)
    services.timezone_settings.save_timezone("America/New_York")
    services.cutoff_settings.save_cutoff_time("02:30")
    build_services(connection, repository)
    connection.execute(
        "UPDATE measurement_definitions SET generation_start_date = '2026-03-07' "
        "WHERE measurement_definition_id = ?",
        (DEFINITION_ID,),
    )
    services.startup_preparation.prepare()
    window = MainWindow(services)
    qtbot.addWidget(window)
    window.show()
    assert services.current_datetime().strftime("%Y-%m-%d %H:%M %z") == "2026-03-08 01:59 -0500"
    window.date_edit.setDate(QDate(2026, 3, 7))
    assert connection.execute(
        "SELECT COUNT(*) FROM planned_readings "
        "WHERE business_date = '2026-03-07' AND task_state = 'open'"
    ).fetchone() == (1,)

    clock.value = datetime(2026, 3, 8, 7, 0, tzinfo=UTC)
    window.boundary_timer.timeout.emit()

    assert services.current_datetime().strftime("%Y-%m-%d %H:%M %z") == "2026-03-08 03:00 -0400"
    assert window.date_edit.date() == QDate(2026, 3, 7)
    assert connection.execute(
        "SELECT COUNT(*) FROM planned_readings "
        "WHERE business_date = '2026-03-07' AND task_state = 'auto_closed'"
    ).fetchone() == (1,)


def test_today_button_detects_day_change_before_timer(
    overview_data: OverviewData, window: MainWindow, qtbot: QtBot
) -> None:
    overview_data.clock.value = datetime(2026, 2, 4, 15, 0, tzinfo=UTC)

    qtbot.mouseClick(window.today_button, Qt.MouseButton.LeftButton)

    assert window.date_edit.date() == QDate(2026, 2, 5)
    assert window.date_edit.maximumDate() == QDate(2026, 2, 5)


def test_clock_requires_an_aware_datetime(overview_data: OverviewData) -> None:
    services = create_app_context(overview_data.connection, clock=lambda: datetime(2026, 2, 4, 12))
    with pytest.raises(InvalidTimezoneError):
        services.current_datetime()


def test_refresh_reports_errors_clears_stale_rows_and_recovers(
    overview_data: OverviewData,
    window: MainWindow,
    messages: list[tuple[str, str, str]],
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    service = overview_data.services.planned_reading_maintenance
    refresh = service.refresh

    def fail_domain() -> None:
        raise ValidationError("予定枠を更新できません")

    monkeypatch.setattr(service, "refresh", fail_domain)
    window.refresh_action.trigger()
    assert messages[-1] == ("warning", "入力内容を確認してください", "予定枠を更新できません")
    assert window.table_model.rowCount() == 0
    assert "失敗" in window.statusBar().currentMessage()
    assert not window.manual_reading_action.isEnabled()

    def fail_unexpected() -> None:
        raise RuntimeError("更新中の予期しない障害")

    monkeypatch.setattr(service, "refresh", fail_unexpected)
    window.refresh_action.trigger()
    assert messages[-1][0] == "critical"
    assert "更新中の予期しない障害" in caplog.text
    monkeypatch.setattr(service, "refresh", refresh)
    window.refresh_action.trigger()
    assert window.table_model.rowCount() == 7


def _mobile_result_payload() -> dict[str, object]:
    return {
        "kind": RESULT_KIND,
        "v": 1,
        "sheet_id": SHEET_ID,
        "entries": [
            {
                "planned_reading_id": "plan-4",
                "result_status": "measured",
                "raw_reading": "140",
                "recorded_at": _at(4).isoformat(),
                "client_updated_at": _at(5).isoformat(),
                "note": "現場で確認",
            }
        ],
    }


def _window_reading(window: MainWindow, planned_reading_id: str) -> ReadingOverviewRecord:
    row = window.table_model.row_for_id(planned_reading_id)
    assert row is not None
    reading = window.table_model.reading_at(row)
    assert reading is not None
    return reading


def test_mobile_action_opens_a_real_modal_dialog_with_shared_context(
    overview_data: OverviewData, window: MainWindow
) -> None:
    observed: list[tuple[MobileReadingDialog, bool, bool]] = []

    def close_dialog() -> None:
        dialog = window.findChild(MobileReadingDialog)
        if dialog is not None:
            is_active_modal = QApplication.activeModalWidget() is dialog and dialog.isModal()
            observed.append((dialog, dialog.isVisible(), is_active_modal))
            dialog.accept()

    assert window.mobile_reading_action.text() == "スマホ検針"
    assert window.mobile_reading_action.isEnabled()
    QTimer.singleShot(0, close_dialog)
    window.mobile_reading_action.trigger()

    assert len(observed) == 1
    dialog, was_visible, was_modal = observed[0]
    assert was_visible and was_modal
    assert dialog.parentWidget() is window
    assert dialog._services is overview_data.services
    assert dialog._save_directory_memory is window._sheet_save_directory_memory
    assert dialog.staff_combo._memory is window._staff_memory


def test_mobile_commit_updates_main_window_while_the_dialog_is_modal(
    window: MainWindow,
    messages: list[tuple[str, str, str]],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    observed: list[tuple[bool, Decimal | None, Decimal | None, bool]] = []

    def interact() -> None:
        dialog = window.findChild(MobileReadingDialog)
        if dialog is None:
            return
        try:
            dialog.tabs.setCurrentIndex(1)
            dialog.staff_combo.setCurrentIndex(dialog.staff_combo.findData(STAFF_ID))
            QApplication.clipboard().setText(encode_mr1_envelope(_mobile_result_payload()))
            dialog.paste_button.click()
            assert dialog.commit_button.isEnabled()
            dialog.commit_button.click()
            reading = _window_reading(window, "plan-4")
            observed.append(
                (
                    QApplication.activeModalWidget() is dialog,
                    reading.raw_reading,
                    reading.usage_delta,
                    dialog.commit_button.isEnabled(),
                )
            )
        finally:
            dialog.accept()

    monkeypatch.setattr(QMessageBox, "question", lambda *_args: QMessageBox.StandardButton.Yes)
    QTimer.singleShot(0, interact)
    window.mobile_reading_action.trigger()

    assert observed == [(True, Decimal("140"), Decimal("10"), False)]
    assert len(messages) == 1 and "確定 1 件" in messages[0][2]


def test_mobile_action_is_reachable_at_150_percent(
    overview_data: OverviewData,
    qtbot: QtBot,
    restore_app_font: QFont,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    apply_display_settings(DisplaySettings(None, 150))
    window = MainWindow(overview_data.services)
    qtbot.addWidget(window)
    window.show()
    QApplication.processEvents()
    toolbar = window.findChild(QToolBar, "mainToolbar")
    assert toolbar is not None
    button = toolbar.widgetForAction(window.mobile_reading_action)
    assert button is not None and button.isVisible() and button.isEnabled()
    center = button.mapToGlobal(button.rect().center())
    assert toolbar.rect().contains(toolbar.mapFromGlobal(center))
    assert window.rect().contains(window.mapFromGlobal(center))
    opened: list[bool] = []
    monkeypatch.setattr(MobileReadingDialog, "exec", lambda _dialog: opened.append(True) or 0)

    qtbot.mouseClick(button, Qt.MouseButton.LeftButton)

    assert opened == [True]


def test_mobile_dialog_shares_staff_and_save_directory_during_the_session(
    overview_data: OverviewData,
    window: MainWindow,
    messages: list[tuple[str, str, str]],
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    other_staff = overview_data.services.staff.create_staff(StaffCreateRequest("別の担当者"))
    target = tmp_path / "検針シート.html"
    defaults: list[Path] = []
    direct_selections: list[object] = []
    mobile_selections: list[object] = []

    def choose(_parent: object, _caption: str, initial: str, file_filter: str) -> tuple[str, str]:
        defaults.append(Path(initial))
        return (str(target), file_filter) if len(defaults) == 1 else ("", file_filter)

    def interact_direct(dialog: DirectReadingDialog) -> int:
        if not direct_selections:
            dialog.staff_combo.setCurrentIndex(dialog.staff_combo.findData(STAFF_ID))
        direct_selections.append(dialog.staff_combo.currentData())
        return 0

    def interact_mobile(dialog: MobileReadingDialog) -> int:
        mobile_selections.append(dialog.staff_combo.currentData())
        if len(mobile_selections) == 1:
            dialog.staff_combo.setCurrentIndex(dialog.staff_combo.findData(other_staff.staff_id))
            dialog.export_button.click()
        elif len(mobile_selections) == 2:
            dialog.export_button.click()
        else:
            assert dialog.staff_combo.findData(other_staff.staff_id) == -1
            assert not dialog.commit_button.isEnabled()
        return 0

    monkeypatch.setattr(QFileDialog, "getSaveFileName", choose)
    monkeypatch.setattr(DirectReadingDialog, "exec", interact_direct)
    monkeypatch.setattr(MobileReadingDialog, "exec", interact_mobile)
    _select(window, "plan-4")
    window.manual_reading_action.trigger()
    window.mobile_reading_action.trigger()
    window.mobile_reading_action.trigger()
    window.manual_reading_action.trigger()
    overview_data.services.staff.deactivate_staff(other_staff.staff_id)
    window.mobile_reading_action.trigger()

    assert direct_selections == [STAFF_ID, other_staff.staff_id]
    assert mobile_selections == [STAFF_ID, other_staff.staff_id, ""]
    assert len(defaults) == 2 and defaults[1] == tmp_path / defaults[0].name
    assert window._sheet_save_directory_memory.directory == tmp_path
    assert target.exists()
    assert [kind for kind, _title, _text in messages] == ["information"]


@pytest.mark.parametrize(
    "outcome", ["success", "cancel", "empty", "generation_error", "save_error"]
)
def test_mobile_sheet_generation_refreshes_main_window_for_every_outcome(
    outcome: str,
    overview_data: OverviewData,
    window: MainWindow,
    messages: list[tuple[str, str, str]],
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    assert _window_reading(window, "plan-4").task_state is PlannedReadingTaskState.OPEN
    overview_data.clock.value = datetime(2026, 2, 4, 22, tzinfo=UTC)
    service = overview_data.services.planned_reading_maintenance
    refresh = service.refresh
    events: list[str] = []
    target = (
        tmp_path / "missing" / "sheet.html" if outcome == "save_error" else tmp_path / "sheet.html"
    )

    def tracked_refresh() -> None:
        events.append("refresh")
        refresh()

    def choose(_parent: object, _caption: str, _initial: str, file_filter: str) -> tuple[str, str]:
        events.append("choose")
        return ("", file_filter) if outcome == "cancel" else (str(target), file_filter)

    def fail_render(_data: object) -> str:
        raise RuntimeError("HTML 生成の試験障害")

    def interact(dialog: MobileReadingDialog) -> int:
        if outcome == "empty":
            dialog.cycle_checks["every_two_months"].setChecked(True)
        dialog.export_button.click()
        return 0

    monkeypatch.setattr(service, "refresh", tracked_refresh)
    monkeypatch.setattr(QFileDialog, "getSaveFileName", choose)
    monkeypatch.setattr(MobileReadingDialog, "exec", interact)
    if outcome == "generation_error":
        monkeypatch.setattr(mobile_reading_sheet, "render_sheet_html", fail_render)

    window.mobile_reading_action.trigger()

    assert events == (
        ["refresh"] if outcome in {"empty", "generation_error"} else ["refresh", "choose"]
    )
    assert window.date_edit.date() == QDate(2026, 2, 4)
    assert _window_reading(window, "plan-4").task_state is PlannedReadingTaskState.AUTO_CLOSED
    assert overview_data.connection.execute(
        "SELECT task_state FROM planned_readings WHERE planned_reading_id = 'plan-4'"
    ).fetchone() == ("auto_closed",)
    if outcome == "success":
        assert target.exists()
        assert len(messages) == 1 and messages[0][0] == "information"
        assert "保存しました" in messages[0][1]
    elif outcome == "cancel":
        assert messages == [] and not target.exists()
    elif outcome == "empty":
        assert len(messages) == 1 and messages[0][0] == "warning"
        assert "出力できる検針枠がありません" in messages[0][2]
    elif outcome == "generation_error":
        assert len(messages) == 1 and messages[0][0] == "critical"
        assert "HTML 生成の試験障害" in caplog.text
    else:
        assert len(messages) == 1 and messages[0][0] == "warning"
        assert "保存できませんでした" in messages[0][2]
        assert not target.exists()


@pytest.mark.parametrize("outcome", ["generation_error", "save_error"])
def test_mobile_export_and_reload_failures_are_both_reported(
    outcome: str,
    overview_data: OverviewData,
    window: MainWindow,
    messages: list[tuple[str, str, str]],
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    overview_data.clock.value = datetime(2026, 2, 4, 22, tzinfo=UTC)

    def fail_refresh() -> None:
        raise ValidationError("一覧の再読込に失敗しました")

    def fail_render(_data: object) -> str:
        raise RuntimeError("HTML 生成の試験障害")

    def interact(dialog: MobileReadingDialog) -> int:
        dialog.export_button.click()
        return 0

    monkeypatch.setattr(overview_data.services.planned_reading_maintenance, "refresh", fail_refresh)
    monkeypatch.setattr(MobileReadingDialog, "exec", interact)
    monkeypatch.setattr(
        QFileDialog,
        "getSaveFileName",
        lambda *_args: (str(tmp_path / "missing" / "sheet.html"), ""),
    )
    if outcome == "generation_error":
        monkeypatch.setattr(mobile_reading_sheet, "render_sheet_html", fail_render)

    window.mobile_reading_action.trigger()

    assert len(messages) == 2
    assert messages[0][0] == "warning" and "再読込に失敗" in messages[0][2]
    if outcome == "generation_error":
        assert messages[1][0] == "critical" and "予期しないエラー" in messages[1][2]
        assert "HTML 生成の試験障害" in caplog.text
    else:
        assert messages[1][0] == "warning" and "保存できませんでした" in messages[1][2]
    assert all(kind != "information" for kind, _title, _text in messages)
    assert window.table_model.rowCount() == 0
    assert "失敗" in window.statusBar().currentMessage()
    assert overview_data.connection.execute(
        "SELECT task_state FROM planned_readings WHERE planned_reading_id = 'plan-4'"
    ).fetchone() == ("auto_closed",)


@pytest.mark.parametrize("source", ["clipboard", "json"])
def test_mobile_commit_refreshes_main_window_and_keeps_audit_data(
    source: str,
    overview_data: OverviewData,
    window: MainWindow,
    messages: list[tuple[str, str, str]],
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    overview_data.clock.value = _at(6)
    overview_data.services.reading_writer.save_manual_reading(
        ManualReadingInput("plan-5", ResultStatus.MEASURED, _at(5), Decimal("150"), None),
        staff_id=STAFF_ID,
    )
    window.refresh_planned_readings()
    assert _window_reading(window, "plan-5").usage_delta == Decimal("20")
    later_before = overview_data.repository.get_mobile_reading_import_targets(["plan-5"])[
        "plan-5"
    ].existing_result
    assert later_before is not None
    service = overview_data.services.planned_reading_maintenance
    refresh = service.refresh
    calls: list[str] = []
    payload = _mobile_result_payload()
    result_path = tmp_path / "result.json"
    result_path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8-sig")

    def tracked_refresh() -> None:
        calls.append("refresh")
        refresh()

    def interact(dialog: MobileReadingDialog) -> int:
        dialog.tabs.setCurrentIndex(1)
        dialog.staff_combo.setCurrentIndex(dialog.staff_combo.findData(STAFF_ID))
        before = tuple(overview_data.connection.iterdump())
        if source == "clipboard":
            QApplication.clipboard().setText(encode_mr1_envelope(payload))
            dialog.paste_button.click()
        else:
            dialog.load_json_button.click()
        assert dialog.preview_model.rowCount() == 1 and dialog.commit_button.isEnabled()
        assert tuple(overview_data.connection.iterdump()) == before
        assert _window_reading(window, "plan-4").result_status is None
        assert _window_reading(window, "plan-5").usage_delta == Decimal("20")
        assert "後続実測 1 件" in dialog.summary_label.text()
        dialog.commit_button.click()
        assert dialog.preview_model.rowCount() == 0 and not dialog.commit_button.isEnabled()
        return 0

    monkeypatch.setattr(service, "refresh", tracked_refresh)
    monkeypatch.setattr(MobileReadingDialog, "exec", interact)
    monkeypatch.setattr(QFileDialog, "getOpenFileName", lambda *_args: (str(result_path), ""))
    monkeypatch.setattr(QMessageBox, "question", lambda *_args: QMessageBox.StandardButton.Yes)
    window.cycle_tabs.setCurrentIndex(1)
    _select(window, "plan-4")

    window.mobile_reading_action.trigger()

    assert calls == ["refresh"]
    reading = _window_reading(window, "plan-4")
    assert reading.task_state is PlannedReadingTaskState.COMPLETED
    assert reading.result_status is ResultStatus.MEASURED
    assert reading.raw_reading == Decimal("140")
    assert reading.last_reading == Decimal("130") and reading.usage_delta == Decimal("10")
    later = _window_reading(window, "plan-5")
    assert later.last_reading == Decimal("140") and later.usage_delta == Decimal("10")
    assert window.cycle_tabs.currentIndex() == 1
    assert window._selected_reading() == reading
    existing = overview_data.repository.get_mobile_reading_import_targets(["plan-4"])[
        "plan-4"
    ].existing_result
    assert existing is not None
    assert existing.device_id == SHEET_ID and existing.updated_by == "試験担当"
    assert existing.result_source is ResultSource.MOBILE_HTML and existing.signature
    later_after = overview_data.repository.get_mobile_reading_import_targets(["plan-5"])[
        "plan-5"
    ].existing_result
    assert later_after is not None
    assert later_after.raw_reading == later_before.raw_reading
    assert later_after.signature == later_before.signature
    assert later_after.updated_by == later_before.updated_by
    assert later_after.arrival_sequence == later_before.arrival_sequence
    assert len(messages) == 1 and messages[0][0] == "information"
    assert "確定 1 件" in messages[0][2]


def test_mobile_commit_reload_failure_is_reported_without_losing_the_result(
    overview_data: OverviewData,
    window: MainWindow,
    messages: list[tuple[str, str, str]],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service = overview_data.services.planned_reading_maintenance
    refresh = service.refresh

    def fail_refresh() -> None:
        raise ValidationError("一覧の再読込に失敗しました")

    def interact(dialog: MobileReadingDialog) -> int:
        dialog.staff_combo.setCurrentIndex(dialog.staff_combo.findData(STAFF_ID))
        dialog.result_edit.setPlainText(json.dumps(_mobile_result_payload()))
        dialog.preview_button.click()
        assert dialog.commit_button.isEnabled()
        dialog.commit_button.click()
        return 0

    monkeypatch.setattr(service, "refresh", fail_refresh)
    monkeypatch.setattr(MobileReadingDialog, "exec", interact)
    monkeypatch.setattr(QMessageBox, "question", lambda *_args: QMessageBox.StandardButton.Yes)

    window.mobile_reading_action.trigger()

    assert len(messages) == 2
    assert messages[0][0] == "warning" and "再読込に失敗" in messages[0][2]
    assert messages[1][0] == "information" and "確定 1 件" in messages[1][2]
    assert window.table_model.rowCount() == 0
    assert "失敗" in window.statusBar().currentMessage()
    existing = overview_data.repository.get_mobile_reading_import_targets(["plan-4"])[
        "plan-4"
    ].existing_result
    assert existing is not None and existing.raw_reading == Decimal("140")
    assert existing.signature

    monkeypatch.setattr(service, "refresh", refresh)
    window.refresh_action.trigger()
    assert _window_reading(window, "plan-4").raw_reading == Decimal("140")


@pytest.mark.parametrize("unexpected", [False, True])
def test_mobile_dialog_open_errors_are_reported_by_main_window(
    unexpected: bool,
    overview_data: OverviewData,
    window: MainWindow,
    messages: list[tuple[str, str, str]],
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    def fail(*_args: object, **_kwargs: object) -> None:
        if unexpected:
            raise RuntimeError("担当者読込の予期しない障害")
        raise ValidationError("担当者を読み込めません")

    opened: list[bool] = []
    monkeypatch.setattr(overview_data.services.staff, "list_staff", fail)
    monkeypatch.setattr(MobileReadingDialog, "exec", lambda _dialog: opened.append(True) or 0)

    window.mobile_reading_action.trigger()

    assert opened == [] and len(messages) == 1
    if unexpected:
        assert messages[0][0] == "critical" and "予期しないエラー" in messages[0][2]
        assert "担当者読込の予期しない障害" in caplog.text
    else:
        assert messages[0][0] == "warning" and "担当者を読み込めません" in messages[0][2]
    assert window.table_model.rowCount() == 7


@pytest.mark.parametrize("failure", ["load", "parse", "preview", "commit"])
def test_mobile_import_errors_opened_from_main_window_invalidate_preview(
    failure: str,
    overview_data: OverviewData,
    window: MainWindow,
    messages: list[tuple[str, str, str]],
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    calls: list[str] = []
    before = tuple(overview_data.connection.iterdump())

    def fail_preview(_result: object) -> None:
        raise RuntimeError("プレビューの試験障害")

    def fail_commit(*_args: object, **_kwargs: object) -> None:
        raise ValidationError("確定の試験障害")

    def interact(dialog: MobileReadingDialog) -> int:
        dialog.staff_combo.setCurrentIndex(dialog.staff_combo.findData(STAFF_ID))
        dialog.result_edit.setPlainText(json.dumps(_mobile_result_payload()))
        dialog.preview_button.click()
        assert dialog.commit_button.isEnabled()
        if failure == "load":
            dialog.load_json_button.click()
        elif failure == "parse":
            dialog.result_edit.setPlainText("MR1:abc.00000000:END")
            dialog.preview_button.click()
        elif failure == "preview":
            monkeypatch.setattr(
                overview_data.services.mobile_reading_import, "preview", fail_preview
            )
            dialog.preview_button.click()
        else:
            monkeypatch.setattr(overview_data.services.mobile_reading_import, "commit", fail_commit)
            dialog.commit_button.click()
        assert dialog.preview_model.rowCount() == 0 and not dialog.commit_button.isEnabled()
        return 0

    monkeypatch.setattr(
        overview_data.services.planned_reading_maintenance,
        "refresh",
        lambda: calls.append("refresh"),
    )
    monkeypatch.setattr(MobileReadingDialog, "exec", interact)
    monkeypatch.setattr(
        QFileDialog, "getOpenFileName", lambda *_args: (str(tmp_path / "missing.json"), "")
    )
    monkeypatch.setattr(QMessageBox, "question", lambda *_args: QMessageBox.StandardButton.Yes)

    window.mobile_reading_action.trigger()

    assert calls == [] and tuple(overview_data.connection.iterdump()) == before
    assert window.table_model.rowCount() == 7
    assert len(messages) == 1
    if failure == "preview":
        assert messages[0][0] == "critical" and "予期しないエラー" in messages[0][2]
        assert "プレビューの試験障害" in caplog.text
    else:
        assert messages[0][0] == "warning"
        if failure == "load":
            assert "読み込めませんでした" in messages[0][2]
        elif failure == "parse":
            assert "欠けているか改変されています" in messages[0][2]
        elif failure == "commit":
            assert "確定の試験障害" in messages[0][2]
