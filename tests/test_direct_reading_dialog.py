from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from zoneinfo import ZoneInfo

import pytest
from PySide6.QtCore import QDate, QTime
from PySide6.QtWidgets import QMessageBox
from pytestqt.qtbot import QtBot
from reading_service_helpers import (
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
from meter_reading_mini.core.errors import InvalidTimezoneError
from meter_reading_mini.core.meter_reading_sync_repositories import (
    ManualReadingInput,
    MeasurementDefinitionCreateRequest,
    ReadingOverviewRecord,
)
from meter_reading_mini.core.reading_write_orchestration import ReadingWriteOrchestrationService
from meter_reading_mini.db.repositories import SQLiteRepository
from meter_reading_mini.ui.dialogs.direct_reading_dialog import DirectReadingDialog
from meter_reading_mini.ui.staff_selection_memory import StaffSelectionMemory
from meter_reading_mini.ui.widgets.wall_datetime_edit import WallDateTimeEdit

NOW = datetime(2026, 2, 5, 0, 0, tzinfo=UTC)
DAY = date(2026, 2, 4)


def _at(hour: int) -> datetime:
    return datetime(2026, 2, 4, hour, tzinfo=UTC)


@dataclass
class DirectData:
    connection: sqlite3.Connection
    repository: SQLiteRepository
    writer: ReadingWriteOrchestrationService
    services: AppContext

    def reading(self, planned_reading_id: str) -> ReadingOverviewRecord:
        return next(
            item
            for item in self.services.reading_overview.get_overview(DAY).readings
            if item.planned_reading_id == planned_reading_id
        )


@pytest.fixture
def direct_data(
    file_database_repository: tuple[sqlite3.Connection, SQLiteRepository],
) -> DirectData:
    connection, repository = file_database_repository
    writer, _ = build_services(
        connection, repository, multiplier=Decimal("10"), rollover_value=Decimal("1000")
    )
    for name, hour in (("before", 4), ("target", 5), ("following", 6)):
        create_plan(repository, name, _at(hour))
    for name, value, hour in (("before", "100", 4), ("following", "130", 6)):
        writer.save_manual_reading(
            ManualReadingInput(name, ResultStatus.MEASURED, _at(hour), Decimal(value), None),
            staff_id=STAFF_ID,
        )
    connection.execute(
        "UPDATE planned_readings SET task_state = 'auto_closed', close_reason = 'cutoff_elapsed' "
        "WHERE planned_reading_id = 'target'"
    )
    services = create_app_context(connection, clock=lambda: NOW)
    return DirectData(connection, repository, writer, services)


@pytest.fixture
def confirmed_messages(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    messages: list[str] = []
    monkeypatch.setattr(QMessageBox, "question", lambda *args: QMessageBox.StandardButton.Yes)
    monkeypatch.setattr(QMessageBox, "warning", lambda parent, title, text: messages.append(text))
    monkeypatch.setattr(QMessageBox, "critical", lambda parent, title, text: messages.append(text))
    return messages


def _dialog(data: DirectData, qtbot: QtBot, name: str = "target") -> DirectReadingDialog:
    dialog = DirectReadingDialog(data.services, data.reading(name), StaffSelectionMemory())
    qtbot.addWidget(dialog)
    dialog.staff_combo.setCurrentIndex(dialog.staff_combo.findData(STAFF_ID))
    return dialog


def test_direct_input_previews_without_writes_and_recalculates_following_readings(
    direct_data: DirectData, qtbot: QtBot, confirmed_messages: list[str]
) -> None:
    dialog = _dialog(direct_data, qtbot)
    assert dialog.recorded_at.to_utc(now=NOW) == _at(5)
    before = direct_data.connection.total_changes
    dialog.value_edit.setText("110")
    assert direct_data.connection.total_changes == before
    assert dialog.save_button.isEnabled()
    assert "前回値: 100" in dialog.preview_text.toPlainText()
    assert "使用量: 100" in dialog.preview_text.toPlainText()

    dialog.save_button.click()

    assert confirmed_messages == []
    assert direct_data.connection.execute(
        "SELECT p.task_state, r.raw_reading, r.last_reading, r.usage_delta, r.updated_by, "
        "r.result_source FROM planned_readings p "
        "JOIN reading_transactions r USING (planned_reading_id) "
        "WHERE p.planned_reading_id = 'target'"
    ).fetchone() == ("completed", "110", "100", "100", "試験担当", "manual")
    assert direct_data.connection.execute(
        "SELECT last_reading, usage_delta FROM reading_transactions "
        "WHERE planned_reading_id = 'following'"
    ).fetchone() == ("110", "200")


def test_direct_correction_invalidates_bad_input_and_recalculates_later_results(
    direct_data: DirectData, qtbot: QtBot, confirmed_messages: list[str]
) -> None:
    dialog = _dialog(direct_data, qtbot, "before")
    assert dialog.recorded_at.to_utc(now=NOW) == _at(4)
    assert dialog.value_edit.text() == "100"
    assert dialog.save_button.isEnabled()
    changes = direct_data.connection.total_changes
    dialog.value_edit.setText("1e2")
    assert not dialog.save_button.isEnabled()
    assert "10進数文字列" in dialog.preview_text.toPlainText()
    assert direct_data.connection.total_changes == changes
    dialog.value_edit.setText("105")
    assert dialog.save_button.isEnabled()
    dialog.note_edit.setPlainText("訂正")
    dialog.save_button.click()
    assert confirmed_messages == []
    assert direct_data.connection.execute(
        "SELECT raw_reading, note FROM reading_transactions WHERE planned_reading_id = 'before'"
    ).fetchone() == ("105", "訂正")
    assert direct_data.connection.execute(
        "SELECT last_reading, usage_delta FROM reading_transactions "
        "WHERE planned_reading_id = 'following'"
    ).fetchone() == ("105", "250")


def test_direct_save_rejects_changed_database_and_discards_old_preview(
    direct_data: DirectData, qtbot: QtBot, confirmed_messages: list[str]
) -> None:
    dialog = _dialog(direct_data, qtbot)
    dialog.value_edit.setText("110")
    assert dialog.save_button.isEnabled()
    direct_data.writer.save_manual_reading(
        ManualReadingInput("before", ResultStatus.MEASURED, _at(4), Decimal("105"), None),
        staff_id=STAFF_ID,
    )
    before = direct_data.connection.total_changes

    dialog.save_button.click()

    assert "再度プレビュー" in confirmed_messages[-1]
    assert not dialog.save_button.isEnabled()
    assert not dialog.preview_text.toPlainText()
    assert direct_data.connection.total_changes == before
    assert (
        direct_data.connection.execute(
            "SELECT 1 FROM reading_transactions WHERE planned_reading_id = 'target'"
        ).fetchone()
        is None
    )
    dialog.preview_button.click()
    assert "前回値: 105" in dialog.preview_text.toPlainText()
    assert dialog.save_button.isEnabled()


def test_direct_input_rejects_future_dates_and_downstream_errors_without_writes(
    direct_data: DirectData, qtbot: QtBot
) -> None:
    direct_data.connection.execute(
        "UPDATE meter_assignments SET rollover_value = NULL WHERE meter_id = ?", (METER_ID,)
    )
    direct_data.connection.execute(
        "UPDATE measurement_definitions SET warning_threshold = '50' "
        "WHERE measurement_definition_id = 'test-definition'"
    )
    dialog = _dialog(direct_data, qtbot)
    before = direct_data.connection.total_changes
    dialog.value_edit.setText("131")
    assert not dialog.save_button.isEnabled()
    assert "検証エラー" in dialog.preview_text.toPlainText()
    dialog.value_edit.setText("110")
    assert dialog.save_button.isEnabled()
    assert "警告閾値超過: あり" in dialog.preview_text.toPlainText()
    dialog.recorded_at.set_datetime(NOW + timedelta(seconds=1))
    assert not dialog.save_button.isEnabled()
    assert "未来" in dialog.preview_text.toPlainText()
    assert direct_data.connection.total_changes == before
    dialog.recorded_at.set_datetime(_at(5))
    assert dialog.save_button.isEnabled()

    create_plan(direct_data.repository, "future", NOW + timedelta(hours=1))
    tomorrow = direct_data.services.reading_overview.get_overview(DAY + timedelta(days=1))
    reading = next(item for item in tomorrow.readings if item.planned_reading_id == "future")
    future = DirectReadingDialog(direct_data.services, reading, StaffSelectionMemory())
    qtbot.addWidget(future)
    assert future.recorded_at.to_utc(now=NOW) == NOW


def test_direct_confirmation_rejects_input_changed_during_confirmation(
    direct_data: DirectData, qtbot: QtBot, monkeypatch: pytest.MonkeyPatch
) -> None:
    messages: list[str] = []
    dialog = _dialog(direct_data, qtbot)
    dialog.value_edit.setText("110")
    before = direct_data.connection.total_changes

    def change_during_confirmation(*_args: object) -> QMessageBox.StandardButton:
        dialog.value_edit.setText("111")
        return QMessageBox.StandardButton.Yes

    monkeypatch.setattr(QMessageBox, "question", change_during_confirmation)
    monkeypatch.setattr(QMessageBox, "warning", lambda parent, title, text: messages.append(text))
    dialog.save_button.click()
    assert "再度プレビュー" in messages[-1]
    assert not dialog.save_button.isEnabled()
    assert not dialog.preview_text.toPlainText()
    assert direct_data.connection.total_changes == before


def test_power_factor_unity_is_fixed_and_nonmeasured_drops_values(
    direct_data: DirectData, qtbot: QtBot, confirmed_messages: list[str]
) -> None:
    direct_data.repository.create_measurement_definition(
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
    create_plan(direct_data.repository, "power", _at(7), measurement_definition_id="power-factor")
    direct_data.writer.save_manual_reading(
        ManualReadingInput(
            "power",
            ResultStatus.MEASURED,
            _at(7),
            None,
            {"percent": "95.50", "phase": "lag"},
        ),
        staff_id=STAFF_ID,
    )
    dialog = _dialog(direct_data, qtbot, "power")
    assert dialog.value_edit.text() == "95.50"
    assert dialog.phase_combo.currentData() == "lag"
    dialog.value_edit.setText("100")
    assert dialog.phase_combo.currentData() == "unity"
    assert not dialog.phase_combo.isEnabled()
    dialog.value_edit.setText("99")
    assert dialog.phase_combo.currentData() == "lead"
    assert dialog.phase_combo.isEnabled()
    dialog.value_edit.setText("100")
    dialog.save_button.click()
    row = direct_data.connection.execute(
        "SELECT raw_reading, structured_payload_json FROM reading_transactions "
        "WHERE planned_reading_id = 'power'"
    ).fetchone()
    assert row is not None and row[0] is None
    assert json.loads(row[1]) == {"percent": "100", "phase": "unity"}

    correction = _dialog(direct_data, qtbot, "power")
    correction.value_edit.setText("101")
    assert not correction.save_button.isEnabled()
    assert "力率は 100% 以下" in correction.preview_text.toPlainText()
    correction.status_combo.setCurrentIndex(correction.status_combo.findData("meter_fault"))
    assert not correction.value_edit.isEnabled()
    assert correction.save_button.isEnabled()
    correction.save_button.click()
    assert confirmed_messages == []
    assert direct_data.connection.execute(
        "SELECT result_status, raw_reading, structured_payload_json FROM reading_transactions "
        "WHERE planned_reading_id = 'power'"
    ).fetchone() == ("meter_fault", None, None)


def test_wall_datetime_rejects_dst_gaps_overlaps_and_future(qtbot: QtBot) -> None:
    zone = ZoneInfo("America/New_York")
    edit = WallDateTimeEdit(datetime(2026, 3, 8, 6, 30, tzinfo=UTC), zone)
    qtbot.addWidget(edit)
    now = datetime(2027, 1, 1, tzinfo=UTC)
    edit.time_edit.setTime(QTime(2, 30))
    with pytest.raises(InvalidTimezoneError, match="存在しません"):
        edit.to_utc(now=now)
    edit.date_edit.setDate(QDate(2026, 11, 1))
    edit.time_edit.setTime(QTime(1, 30))
    with pytest.raises(InvalidTimezoneError, match="重複"):
        edit.to_utc(now=now)
    edit.date_edit.setDate(QDate(2027, 1, 2))
    with pytest.raises(InvalidTimezoneError, match="未来"):
        edit.to_utc(now=now)
