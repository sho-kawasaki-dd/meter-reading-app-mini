from __future__ import annotations

import hashlib
import sqlite3
from datetime import UTC, datetime
from decimal import Decimal
from functools import partial
from pathlib import Path

import pytest
from reading_service_helpers import (
    DEFINITION_ID,
    LOCATION_ID,
    METER_ID,
    STAFF_ID,
    build_services,
    confirmation_context,
    create_plan,
)

from meter_reading_mini.core.assignment_calculation_settings import (
    AssignmentCalculationCorrectionInput,
    AssignmentCalculationSettingsService,
)
from meter_reading_mini.core.contract_models import (
    CalculationMethod,
    Category,
    MeterReadingSyncRequest,
    MeterReplacementSubmission,
    Purpose,
    ReadingCycle,
    ResultStatus,
    ValueSchema,
)
from meter_reading_mini.core.meter_reading_sync_repositories import (
    ManualReadingInput,
    MeasurementDefinitionCreateRequest,
    MeterMasterCreateRequest,
)
from meter_reading_mini.db.backup import copy_database
from meter_reading_mini.db.connection import connect, connect_memory, connect_readonly, transaction
from meter_reading_mini.db.integrity import (
    check_business_integrity,
    list_business_integrity_warnings,
)
from meter_reading_mini.db.migrations import create_schema
from meter_reading_mini.db.repositories import SQLiteRepository


def _at(hour: int) -> datetime:
    return datetime(2026, 2, 4, hour, tzinfo=UTC)


@pytest.fixture
def prepared_database(
    file_database_repository: tuple[sqlite3.Connection, SQLiteRepository],
) -> tuple[sqlite3.Connection, str]:
    conn, repository = file_database_repository
    writer, sync = build_services(
        conn, repository, multiplier=Decimal("10"), rollover_value=Decimal("1000")
    )
    repository.create_meter_master(
        MeterMasterCreateRequest(
            meter_id="new-meter",
            location_id=LOCATION_ID,
            location_name="試験場所",
            meter_name="交換先",
            category=Category.WATER,
        )
    )
    create_plan(repository, "old-reading", _at(4))
    create_plan(repository, "old-slot", _at(6))
    writer.save_manual_reading(
        ManualReadingInput("old-reading", ResultStatus.MEASURED, _at(4), Decimal("100"), None),
        staff_id=STAFF_ID,
    )
    replacement = MeterReplacementSubmission.model_validate(
        {
            "replacement_event_id": "replacement",
            "location_id": LOCATION_ID,
            "old_meter_id": METER_ID,
            "new_meter_id": "new-meter",
            "replaced_at": _at(5),
            "client_updated_at": _at(5),
            "old_final_reading": "120",
            "new_initial_reading": "5",
            "updated_by": "試験担当",
        }
    )
    response = sync.sync(
        MeterReadingSyncRequest(reading_items=[], replacement_items=[replacement]),
        confirmation_context(),
    )
    assert response.accepted_replacement_items == 1
    old_slot = repository.get_planned_reading("old-slot")
    assert old_slot is not None and old_slot.superseded_by_planned_reading_id is not None
    successor = old_slot.superseded_by_planned_reading_id
    writer.save_manual_reading(
        ManualReadingInput(successor, ResultStatus.MEASURED, _at(6), Decimal("8"), None),
        staff_id=STAFF_ID,
    )
    old_assignment = next(
        item
        for item in repository.list_meter_assignments(DEFINITION_ID)
        if item.meter_id == METER_ID
    )
    correction = AssignmentCalculationSettingsService(
        repository,
        repository,
        repository,
        repository,
        repository,
        repository,
        repository,
        repository,
        transaction_factory=partial(transaction, conn),
        now_provider=lambda: datetime(2026, 2, 5, tzinfo=UTC),
        id_provider=lambda: "correction",
    )
    correction.correct_assignment(
        AssignmentCalculationCorrectionInput(
            measurement_definition_id=DEFINITION_ID,
            meter_id=METER_ID,
            effective_from=old_assignment.effective_from,
            multiplier=Decimal("12"),
            rollover_value=Decimal("1000"),
            staff_id=STAFF_ID,
            reason="倍率訂正",
        )
    )
    return conn, successor


def test_backup_business_check_accepts_real_history_without_writing(
    prepared_database: tuple[sqlite3.Connection, str], tmp_path: Path
) -> None:
    conn, _ = prepared_database
    original_changes = conn.total_changes
    assert check_business_integrity(conn) == []
    assert conn.total_changes == original_changes
    backup_path = tmp_path / "backup.db"
    copy_database(conn, backup_path)
    original_hash = hashlib.sha256(backup_path.read_bytes()).digest()
    readonly = connect_readonly(backup_path)
    try:
        assert check_business_integrity(readonly) == []
        assert readonly.total_changes == 0
    finally:
        readonly.close()
    assert hashlib.sha256(backup_path.read_bytes()).digest() == original_hash


def test_legacy_location_name_conflict_is_a_warning_not_a_restore_blocker(
    file_database_repository: tuple[sqlite3.Connection, SQLiteRepository],
) -> None:
    conn, repository = file_database_repository
    build_services(conn, repository)
    repository.create_meter_master(
        MeterMasterCreateRequest(
            meter_id="second-meter",
            location_id=LOCATION_ID,
            location_name="試験場所",
            meter_name="別メーター",
            category=Category.WATER,
        )
    )
    conn.execute(
        "UPDATE physical_meters SET location_name = '旧名称' WHERE meter_id = ?",
        (METER_ID,),
    )

    assert check_business_integrity(conn) == []
    assert any(
        "場所 test-location に異なる場所名" in warning
        for warning in list_business_integrity_warnings(conn)
    )


@pytest.mark.parametrize(
    ("table", "column"),
    [
        ("measurement_definitions", "multiplier"),
        ("measurement_definitions", "warning_threshold"),
        ("measurement_definitions", "rollover_value"),
        ("meter_assignments", "multiplier"),
        ("meter_assignments", "rollover_value"),
        ("assignment_calculation_corrections", "old_multiplier"),
        ("assignment_calculation_corrections", "old_rollover_value"),
        ("assignment_calculation_corrections", "new_multiplier"),
        ("assignment_calculation_corrections", "new_rollover_value"),
        ("reading_transactions", "raw_reading"),
        ("reading_transactions", "last_reading"),
        ("reading_transactions", "usage_delta"),
        ("replacement_events", "old_final_reading"),
        ("replacement_events", "new_initial_reading"),
        ("replacement_boundaries", "previous_measured_reading"),
        ("replacement_boundaries", "old_final_reading"),
        ("replacement_boundaries", "old_meter_usage_delta"),
        ("replacement_boundaries", "new_initial_reading"),
    ],
)
def test_every_decimal_column_rejects_nonplain_values(
    prepared_database: tuple[sqlite3.Connection, str], table: str, column: str
) -> None:
    conn, _ = prepared_database
    conn.execute(f"UPDATE {table} SET {column} = ?", ("1e2",))
    assert any(f"{table}.{column}" in reason for reason in check_business_integrity(conn))


@pytest.mark.parametrize("value", ["NaN", "Infinity", "+1", "01", ".5", "-1", "0"])
def test_assignment_multiplier_rejects_invalid_values(
    prepared_database: tuple[sqlite3.Connection, str], value: str
) -> None:
    conn, _ = prepared_database
    conn.execute(
        "UPDATE meter_assignments SET multiplier = ? WHERE meter_id = ?", (value, METER_ID)
    )
    assert any(
        "meter_assignments.multiplier" in reason for reason in check_business_integrity(conn)
    )


@pytest.mark.parametrize(
    ("statements", "message"),
    [
        (
            (
                "UPDATE reading_transactions SET raw_reading = NULL "
                "WHERE planned_reading_id = 'old-reading'",
            ),
            "数値実測結果",
        ),
        (
            (
                "UPDATE reading_transactions SET result_status = 'skipped' "
                "WHERE planned_reading_id = 'old-reading'",
            ),
            "非実測結果",
        ),
        (
            (
                "UPDATE planned_readings SET task_state = 'open' "
                "WHERE planned_reading_id = 'old-reading'",
            ),
            "未完了",
        ),
        (
            (
                "UPDATE planned_readings SET task_state = 'completed' "
                "WHERE planned_reading_id = 'old-slot'",
            ),
            "結果がありません",
        ),
        (("UPDATE measurement_definitions SET calculation_method = 'raw_only'",), "raw_only"),
        (
            (
                "UPDATE measurement_definitions SET value_schema = 'power_factor_percent_phase'",
                "UPDATE reading_transactions SET raw_reading = NULL, last_reading = NULL, "
                "usage_delta = NULL, structured_payload_json = "
                '\'{"percent":"101","phase":"lead"}\'',
            ),
            "力率データ",
        ),
        (("UPDATE replacement_boundaries SET old_final_reading = '1001'",), "ロールオーバー値"),
        (("UPDATE replacement_events SET old_final_reading = '121'",), "重複保存"),
        (
            (
                "UPDATE replacement_boundaries "
                "SET previous_measured_planned_reading_id = 'old-slot'",
            ),
            "前回実測",
        ),
        (
            ("UPDATE replacement_boundaries SET previous_measured_reading = NULL",),
            "前回実測値と参照先",
        ),
        (("UPDATE replacement_boundaries SET old_meter_usage_delta = NULL",), "旧メーター使用量"),
        (
            ("UPDATE assignment_calculation_corrections SET corrected_at = 'not-a-date'",),
            "訂正日時",
        ),
        (
            (
                "UPDATE replacement_events SET new_initial_reading = '1000'",
                "UPDATE replacement_boundaries SET new_initial_reading = '1000'",
            ),
            "ロールオーバー値",
        ),
        (
            (
                "UPDATE replacement_events SET replaced_at = '2026-02-04 05:00:01'",
                "UPDATE replacement_boundaries SET replaced_at = '2026-02-04 05:00:01'",
            ),
            "旧・新割当の期間",
        ),
    ],
)
def test_business_inconsistencies_are_reported(
    prepared_database: tuple[sqlite3.Connection, str], statements: tuple[str, ...], message: str
) -> None:
    conn, _ = prepared_database
    for sql in statements:
        conn.execute(sql)
    assert any(message in reason for reason in check_business_integrity(conn))


def test_overlapping_assignment_periods_are_rejected(
    prepared_database: tuple[sqlite3.Connection, str],
) -> None:
    conn, _ = prepared_database
    conn.execute(
        "INSERT INTO meter_assignments "
        "(measurement_definition_id, meter_id, effective_from, effective_to, multiplier) "
        "VALUES (?, ?, '2026-02-04 04:30:00', '2026-02-04 06:30:00', '1')",
        (DEFINITION_ID, METER_ID),
    )
    assert any("割当期間が重複" in reason for reason in check_business_integrity(conn))


def test_wrong_meter_for_a_live_slot_is_rejected(
    prepared_database: tuple[sqlite3.Connection, str],
) -> None:
    conn, successor = prepared_database
    conn.execute(
        "UPDATE planned_readings SET meter_id = ? WHERE planned_reading_id = ?",
        (METER_ID, successor),
    )
    assert any("割当を一意に解決" in reason for reason in check_business_integrity(conn))


def test_valid_power_factor_and_raw_only_values_are_accepted(
    file_database_repository: tuple[sqlite3.Connection, SQLiteRepository],
) -> None:
    conn, repository = file_database_repository
    writer, _ = build_services(conn, repository)
    conn.execute("UPDATE measurement_definitions SET calculation_method = 'raw_only'")
    create_plan(repository, "raw", _at(4))
    writer.save_manual_reading(
        ManualReadingInput("raw", ResultStatus.MEASURED, _at(4), Decimal("12"), None),
        staff_id=STAFF_ID,
    )
    repository.create_meter_master(
        MeterMasterCreateRequest(
            meter_id="power-meter",
            location_id=LOCATION_ID,
            location_name="試験場所",
            meter_name="力率計",
            category=Category.ELECTRICITY,
        )
    )
    repository.create_measurement_definition(
        MeasurementDefinitionCreateRequest(
            measurement_definition_id="power-definition",
            location_id=LOCATION_ID,
            display_name="力率",
            category=Category.ELECTRICITY,
            unit="%",
            value_schema=ValueSchema.POWER_FACTOR_PERCENT_PHASE,
            multiplier=Decimal("1"),
            decimal_places=2,
            warning_threshold=None,
            rollover_value=None,
            calculation_method=CalculationMethod.RAW_ONLY,
            reading_cycle=ReadingCycle.DAILY_1X,
            purpose=Purpose.OPERATIONAL,
            generation_start_date=_at(4).date(),
            initial_schedule_json=None,
            initial_meter_id="power-meter",
        )
    )
    create_plan(
        repository,
        "power",
        _at(4),
        meter_id="power-meter",
        measurement_definition_id="power-definition",
    )
    writer.save_manual_reading(
        ManualReadingInput(
            "power",
            ResultStatus.MEASURED,
            _at(4),
            None,
            {"percent": "95.5", "phase": "lag"},
        ),
        staff_id=STAFF_ID,
    )
    assert check_business_integrity(conn) == []


def test_foreign_key_corruption_is_reported_before_business_checks(
    prepared_database: tuple[sqlite3.Connection, str],
) -> None:
    conn, _ = prepared_database
    conn.execute("PRAGMA foreign_keys = OFF")
    try:
        conn.execute("DELETE FROM staff")
        assert any("外部キー制約" in reason for reason in check_business_integrity(conn))
    finally:
        conn.execute("PRAGMA foreign_keys = ON")


def test_event_without_boundaries_is_rejected(
    prepared_database: tuple[sqlite3.Connection, str],
) -> None:
    conn, _ = prepared_database
    conn.execute("DELETE FROM replacement_boundaries")
    assert any("交換境界がありません" in reason for reason in check_business_integrity(conn))


def test_corrupt_backup_is_rejected_after_readonly_inspection(
    prepared_database: tuple[sqlite3.Connection, str], tmp_path: Path
) -> None:
    conn, _ = prepared_database
    backup_path = tmp_path / "corrupt.db"
    copy_database(conn, backup_path)
    copy = connect(backup_path)
    try:
        copy.execute("UPDATE replacement_events SET old_final_reading = '121'")
    finally:
        copy.close()
    readonly = connect_readonly(backup_path)
    try:
        assert any("重複保存" in reason for reason in check_business_integrity(readonly))
    finally:
        readonly.close()


def test_wrong_version_and_invalid_schema_are_reported() -> None:
    conn = connect_memory()
    try:
        assert "スキーマ版" in check_business_integrity(conn)[0]
        create_schema(conn)
        conn.execute("DROP INDEX idx_physical_meters_location")
        assert any("スキーマが不正" in reason for reason in check_business_integrity(conn))
    finally:
        conn.close()
