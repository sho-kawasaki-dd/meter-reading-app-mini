from __future__ import annotations

import sqlite3
from dataclasses import dataclass, replace
from datetime import UTC, date, datetime
from decimal import Decimal
from pathlib import Path

import pytest
from PySide6.QtCore import QDate, Qt, QTime, QTimer
from PySide6.QtGui import QBrush
from PySide6.QtWidgets import QApplication, QMenu, QMessageBox, QToolBar
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
)
from meter_reading_mini.db.repositories import SQLiteRepository
from meter_reading_mini.ui.dialogs.direct_reading_dialog import DirectReadingDialog
from meter_reading_mini.ui.dialogs.master_dialog import MasterDialog
from meter_reading_mini.ui.dialogs.meter_replacement_dialog import MeterReplacementDialog
from meter_reading_mini.ui.dialogs.settings_dialog import SettingsDialog
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
    assert all(
        "スマホ" not in action.text() and "ダッシュボード" not in action.text()
        for action in toolbar.actions()
    )


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
