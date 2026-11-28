from __future__ import annotations

import sqlite3
from dataclasses import FrozenInstanceError, replace
from datetime import UTC, date, datetime, timedelta, timezone
from decimal import Decimal

import pytest

from meter_reading_mini.core.contract_models import (
    CalculationMethod,
    Category,
    Purpose,
    ReadingCycle,
    ReadingSession,
    ResultStatus,
    ValueSchema,
)
from meter_reading_mini.core.errors import ValidationError
from meter_reading_mini.core.meter_reading_sync_repositories import (
    MeasurementDefinitionCreateRequest,
    MeterMasterCreateRequest,
    MobileReadingImportRepository,
    PlannedReadingCloseReason,
    PlannedReadingCreateRequest,
    PlannedReadingTaskState,
    ReadingUpsertRequest,
    ReplacementBoundaryCalculationUpdate,
    ReplacementDefinitionReading,
    ReplacementUpsertRequest,
    ResultSource,
)
from meter_reading_mini.core.timeutil import utc_now_str
from meter_reading_mini.db.repositories import SQLiteRepository

READ_AT = datetime(2026, 2, 4, 8, tzinfo=UTC)
LOCATION_ID = "location-test-001"


def _create_plan(
    repository: SQLiteRepository,
    planned_reading_id: str,
    definition_id: str,
    meter_id: str,
    scheduled_at: datetime = READ_AT,
) -> None:
    assert repository.create_planned_reading_if_missing(
        PlannedReadingCreateRequest(
            planned_reading_id=planned_reading_id,
            measurement_definition_id=definition_id,
            meter_id=meter_id,
            location_id=LOCATION_ID,
            business_date=scheduled_at.date(),
            release_date=scheduled_at.date(),
            reading_session=ReadingSession.SESSION_0800,
            scheduled_at=scheduled_at,
        )
    )


def _create_definition(
    repository: SQLiteRepository,
    definition_id: str,
    meter_id: str,
    *,
    structured: bool = False,
) -> None:
    repository.create_measurement_definition(
        MeasurementDefinitionCreateRequest(
            measurement_definition_id=definition_id,
            location_id=LOCATION_ID,
            display_name="力率" if structured else "別の測定対象",
            category=Category.ELECTRICITY,
            unit="%" if structured else "kWh",
            value_schema=(
                ValueSchema.POWER_FACTOR_PERCENT_PHASE if structured else ValueSchema.DECIMAL_STRING
            ),
            multiplier=Decimal("1"),
            decimal_places=None if structured else 2,
            warning_threshold=Decimal("0") if structured else None,
            rollover_value=None,
            calculation_method=CalculationMethod.RAW_ONLY
            if structured
            else CalculationMethod.DELTA,
            reading_cycle=ReadingCycle.MONTHLY,
            purpose=Purpose.OPERATIONAL,
            generation_start_date=date(2026, 2, 1),
            initial_schedule_json=None,
            initial_meter_id=meter_id,
        )
    )


def _store_reading(
    repository: SQLiteRepository,
    planned_reading_id: str,
    definition_id: str,
    *,
    recorded_at: datetime = READ_AT,
    raw_reading: Decimal | None = Decimal("100"),
    result_status: ResultStatus = ResultStatus.MEASURED,
) -> None:
    repository.upsert_reading(
        ReadingUpsertRequest(
            planned_reading_id=planned_reading_id,
            measurement_definition_id=definition_id,
            result_status=result_status,
            recorded_at=recorded_at,
            client_updated_at=recorded_at,
            raw_reading=raw_reading,
            structured_payload_json=None,
            updated_by="試験担当",
            signature="署名",
            note=None,
            device_id="pc-direct",
            result_source=ResultSource.MANUAL,
        )
    )


def test_import_targets_include_complete_results_and_inactive_masters(
    memory_conn: sqlite3.Connection,
    sqlite_repository: SQLiteRepository,
    sample_master: tuple[str, str],
) -> None:
    meter_id, definition_id = sample_master
    _create_definition(sqlite_repository, "power-factor", meter_id, structured=True)
    for planned_id, definition in (
        ("slot-measured", definition_id),
        ("slot-empty", definition_id),
        ("slot-skipped", definition_id),
        ("slot-superseded", definition_id),
        ("slot-structured", "power-factor"),
    ):
        _create_plan(sqlite_repository, planned_id, definition, meter_id)

    japan = timezone(timedelta(hours=9))
    recorded_at = datetime(2026, 2, 4, 17, 5, 6, 789123, tzinfo=japan)
    updated_at = datetime(2026, 2, 4, 17, 6, 7, 123456, tzinfo=japan)
    request = ReadingUpsertRequest(
        planned_reading_id="slot-measured",
        measurement_definition_id=definition_id,
        result_status=ResultStatus.MEASURED,
        recorded_at=recorded_at,
        client_updated_at=updated_at,
        raw_reading=Decimal("123.4500"),
        structured_payload_json=None,
        updated_by="確定担当",
        signature="監査署名",
        note="確認済み\n備考",
        device_id="a" * 32,
        result_source=ResultSource.MOBILE_HTML,
        last_reading=Decimal("100.01"),
        usage_delta=Decimal("23.44"),
    )
    sqlite_repository.upsert_reading(request)
    sqlite_repository.upsert_reading(
        replace(
            request,
            raw_reading=Decimal("125.4500"),
            client_updated_at=updated_at + timedelta(seconds=1),
            usage_delta=Decimal("25.44"),
        )
    )
    _store_reading(
        sqlite_repository,
        "slot-skipped",
        definition_id,
        raw_reading=None,
        result_status=ResultStatus.SKIPPED,
    )
    sqlite_repository.upsert_reading(
        replace(
            request,
            planned_reading_id="slot-structured",
            measurement_definition_id="power-factor",
            raw_reading=None,
            structured_payload_json={"phase": "lag", "percent": "95.5"},
            last_reading=None,
            usage_delta=None,
            result_source=ResultSource.MANUAL,
            device_id="pc-direct",
        )
    )
    memory_conn.execute(
        "UPDATE planned_readings SET task_state = 'auto_closed', "
        "closed_at = '2026-02-04 07:00:00', close_reason = 'cutoff_elapsed' "
        "WHERE planned_reading_id = 'slot-empty'"
    )
    memory_conn.execute(
        "UPDATE planned_readings SET task_state = 'superseded', "
        "closed_at = '2026-02-04 07:30:00', close_reason = 'meter_replaced', "
        "superseded_by_planned_reading_id = 'slot-empty' "
        "WHERE planned_reading_id = 'slot-superseded'"
    )
    sqlite_repository.set_measurement_definition_active(definition_id, False)
    sqlite_repository.set_measurement_definition_active("power-factor", False)
    sqlite_repository.set_meter_master_active(meter_id, False)
    changes = memory_conn.total_changes

    targets = sqlite_repository.get_mobile_reading_import_targets(
        [
            "slot-measured",
            "slot-empty",
            "slot-skipped",
            "slot-superseded",
            "slot-structured",
            "missing-slot",
        ]
    )

    assert set(targets) == {
        "slot-measured",
        "slot-empty",
        "slot-skipped",
        "slot-superseded",
        "slot-structured",
    }
    measured = targets["slot-measured"]
    assert (
        measured.planned_reading_id,
        measured.measurement_definition_id,
        measured.meter_id,
        measured.location_id,
        measured.location_name,
        measured.meter_name,
        measured.display_name,
    ) == (
        "slot-measured",
        definition_id,
        meter_id,
        LOCATION_ID,
        "架空の試験棟",
        "試験用電力量計",
        "試験用積算電力量",
    )
    assert (
        measured.category,
        measured.unit,
        measured.value_schema,
        measured.calculation_method,
        measured.decimal_places,
        measured.warning_threshold,
        measured.reading_cycle,
        measured.purpose,
        measured.meter_is_active,
        measured.definition_is_active,
    ) == (
        Category.ELECTRICITY,
        "kWh",
        ValueSchema.DECIMAL_STRING,
        CalculationMethod.DELTA,
        2,
        Decimal("900.00"),
        ReadingCycle.DAILY_1X,
        Purpose.OPERATIONAL,
        False,
        False,
    )
    assert (
        measured.business_date,
        measured.reading_session,
        measured.scheduled_at,
        measured.task_state,
        measured.closed_at,
        measured.close_reason,
        measured.superseded_by_planned_reading_id,
    ) == (
        date(2026, 2, 4),
        ReadingSession.SESSION_0800,
        READ_AT,
        PlannedReadingTaskState.COMPLETED,
        datetime(2026, 2, 4, 8, 5, 6, tzinfo=UTC),
        None,
        None,
    )
    existing = measured.existing_result
    assert existing is not None
    assert (
        existing.result_status,
        existing.raw_reading,
        existing.structured_payload_json,
        existing.note,
        existing.recorded_at,
        existing.client_updated_at,
        existing.result_source,
        existing.device_id,
        existing.updated_by,
        existing.signature,
        existing.last_reading,
        existing.usage_delta,
        existing.arrival_sequence,
    ) == (
        ResultStatus.MEASURED,
        Decimal("125.4500"),
        None,
        "確認済み\n備考",
        datetime(2026, 2, 4, 8, 5, 6, tzinfo=UTC),
        datetime(2026, 2, 4, 8, 6, 8, tzinfo=UTC),
        ResultSource.MOBILE_HTML,
        "a" * 32,
        "確定担当",
        "監査署名",
        Decimal("100.01"),
        Decimal("25.44"),
        2,
    )
    empty = targets["slot-empty"]
    assert empty.existing_result is None
    assert empty.task_state is PlannedReadingTaskState.AUTO_CLOSED
    assert empty.close_reason is PlannedReadingCloseReason.CUTOFF_ELAPSED
    assert empty.closed_at == datetime(2026, 2, 4, 7, tzinfo=UTC)
    skipped = targets["slot-skipped"].existing_result
    assert skipped is not None
    assert skipped.result_status is ResultStatus.SKIPPED and skipped.raw_reading is None
    superseded = targets["slot-superseded"]
    assert superseded.task_state is PlannedReadingTaskState.SUPERSEDED
    assert superseded.close_reason is PlannedReadingCloseReason.METER_REPLACED
    assert superseded.superseded_by_planned_reading_id == "slot-empty"
    structured = targets["slot-structured"]
    assert structured.value_schema is ValueSchema.POWER_FACTOR_PERCENT_PHASE
    assert structured.calculation_method is CalculationMethod.RAW_ONLY
    assert structured.reading_cycle is ReadingCycle.MONTHLY
    assert structured.existing_result is not None
    assert structured.existing_result.raw_reading is None
    assert structured.existing_result.structured_payload_json == '{"percent":"95.5","phase":"lag"}'
    assert structured.existing_result.result_source is ResultSource.MANUAL
    assert memory_conn.total_changes == changes


def test_import_target_lookup_chunks_ids_without_inventing_missing_slots(
    memory_conn: sqlite3.Connection,
    sqlite_repository: SQLiteRepository,
    sample_master: tuple[str, str],
) -> None:
    meter_id, definition_id = sample_master
    _create_plan(sqlite_repository, "a-existing", definition_id, meter_id)
    _create_plan(sqlite_repository, "z-existing", definition_id, meter_id)
    missing = [f"missing-{index:04d}" for index in range(510)]
    changes = memory_conn.total_changes

    assert sqlite_repository.get_mobile_reading_import_targets([]) == {}
    targets = sqlite_repository.get_mobile_reading_import_targets(
        [*missing, "z-existing", "a-existing", "a-existing", "x' OR 1 = 1 --"]
    )

    assert set(targets) == {"a-existing", "z-existing"}
    assert all(record.existing_result is None for record in targets.values())
    assert memory_conn.total_changes == changes


def test_import_target_records_preserve_saved_calculations_as_immutable_values(
    memory_conn: sqlite3.Connection,
    sqlite_repository: SQLiteRepository,
    sample_master: tuple[str, str],
) -> None:
    meter_id, definition_id = sample_master
    for planned_id in ("target", "unrelated"):
        _create_plan(sqlite_repository, planned_id, definition_id, meter_id)
        _store_reading(sqlite_repository, planned_id, definition_id)
    original = sqlite_repository.get_mobile_reading_import_targets(["target"])["target"]
    assert original.existing_result is not None
    with pytest.raises(FrozenInstanceError):
        original.existing_result.__setattr__("last_reading", Decimal("99"))

    memory_conn.execute(
        "UPDATE reading_transactions SET last_reading = ? WHERE planned_reading_id = ?",
        ("12", "unrelated"),
    )
    assert sqlite_repository.get_mobile_reading_import_targets(["target"])["target"] == original
    memory_conn.execute(
        "UPDATE reading_transactions SET last_reading = ? WHERE planned_reading_id = ?",
        ("98.50", "target"),
    )
    changed = sqlite_repository.get_mobile_reading_import_targets(["target"])["target"]
    assert changed.existing_result is not None
    assert changed != original
    assert changed.existing_result.last_reading == Decimal("98.50")
    assert changed.existing_result.arrival_sequence == original.existing_result.arrival_sequence
    assert original.existing_result.last_reading is None


def test_import_repository_exposes_scoped_history_and_assignment_conditions(
    memory_conn: sqlite3.Connection,
    sqlite_repository: SQLiteRepository,
    sample_master: tuple[str, str],
) -> None:
    meter_id, definition_id = sample_master
    read_repository: MobileReadingImportRepository = sqlite_repository
    other_meter = "other-meter"
    sqlite_repository.create_meter_master(
        MeterMasterCreateRequest(
            meter_id=other_meter,
            location_id=LOCATION_ID,
            location_name="架空の試験棟",
            meter_name="別の計器",
            category=Category.ELECTRICITY,
        )
    )
    _create_definition(sqlite_repository, "other-definition", meter_id)
    for planned_id, definition, meter, at in (
        ("a", definition_id, meter_id, READ_AT),
        ("b", definition_id, meter_id, READ_AT),
        ("c", definition_id, meter_id, READ_AT),
        ("later", definition_id, meter_id, READ_AT + timedelta(hours=1)),
        ("skipped", definition_id, meter_id, READ_AT),
        ("other-meter-slot", definition_id, other_meter, READ_AT),
        ("other-definition-slot", "other-definition", meter_id, READ_AT),
    ):
        _create_plan(sqlite_repository, planned_id, definition, meter, at)
        _store_reading(
            sqlite_repository,
            planned_id,
            definition,
            recorded_at=at,
            raw_reading=None if planned_id == "skipped" else Decimal("100"),
            result_status=(
                ResultStatus.SKIPPED if planned_id == "skipped" else ResultStatus.MEASURED
            ),
        )

    changes = memory_conn.total_changes
    previous = read_repository.get_latest_measured_reading(
        definition_id, meter_id, before_scheduled_at=READ_AT, before_planned_reading_id="b"
    )
    following = read_repository.list_following_measured_readings(
        definition_id, meter_id, after_scheduled_at=READ_AT, after_planned_reading_id="b"
    )
    all_since = read_repository.list_measured_readings_on_or_after(
        definition_id, meter_id, from_scheduled_at=READ_AT
    )
    assignment = read_repository.get_meter_assignment(definition_id, READ_AT)

    assert previous is not None and previous.planned_reading_id == "a"
    assert [reading.planned_reading_id for reading in following] == ["c", "later"]
    assert [reading.planned_reading_id for reading in all_since] == ["a", "b", "c", "later"]
    assert all(reading.raw_reading == Decimal("100") for reading in all_since)
    assert assignment is not None
    assert (assignment.meter_id, assignment.multiplier, assignment.rollover_value) == (
        meter_id,
        Decimal("1.00"),
        Decimal("1000"),
    )
    assert (
        read_repository.get_meter_assignment_by_key(
            definition_id, meter_id, assignment.effective_from
        )
        == assignment
    )
    assert read_repository.list_meter_assignments(definition_id) == [assignment]
    assert set(
        read_repository.get_mobile_reading_import_targets(
            [reading.planned_reading_id for reading in all_since]
        )
    ) == {"a", "b", "c", "later"}
    assert memory_conn.total_changes == changes


def test_new_meter_boundary_candidates_ignore_persisted_history_and_track_reuse(
    memory_conn: sqlite3.Connection,
    sqlite_repository: SQLiteRepository,
    sample_master: tuple[str, str],
) -> None:
    old_meter, definition_id = sample_master
    new_meter = "replacement-meter"
    sqlite_repository.create_meter_master(
        MeterMasterCreateRequest(
            meter_id=new_meter,
            location_id=LOCATION_ID,
            location_name="架空の試験棟",
            meter_name="交換後の計器",
            category=Category.ELECTRICITY,
        )
    )
    _create_definition(sqlite_repository, "other-definition", old_meter)

    def replace_meter(
        event_id: str,
        from_meter: str,
        to_meter: str,
        replaced_at: datetime,
        initial: Decimal | None,
    ) -> None:
        request = ReplacementUpsertRequest(
            replacement_event_id=event_id,
            location_id=LOCATION_ID,
            old_meter_id=from_meter,
            new_meter_id=to_meter,
            replaced_at=replaced_at,
            client_updated_at=replaced_at,
            old_final_reading=None,
            new_initial_reading=None,
            updated_by="試験担当",
            note=None,
            definition_readings=(
                ReplacementDefinitionReading(definition_id, Decimal("100"), initial),
                ReplacementDefinitionReading("other-definition", None, None),
            ),
        )
        sqlite_repository.upsert_replacement(request)
        sqlite_repository.reflect_replacement(request)

    first = READ_AT - timedelta(hours=5)
    second = READ_AT - timedelta(hours=3)
    third = READ_AT - timedelta(hours=1)
    _create_plan(
        sqlite_repository, "old-first", definition_id, old_meter, first - timedelta(hours=1)
    )
    _store_reading(
        sqlite_repository,
        "old-first",
        definition_id,
        recorded_at=first - timedelta(hours=1),
        raw_reading=Decimal("90"),
    )
    replace_meter("replacement-1", old_meter, new_meter, first, Decimal("5"))
    _create_plan(
        sqlite_repository, "new-first", definition_id, new_meter, first + timedelta(minutes=30)
    )
    _store_reading(
        sqlite_repository,
        "new-first",
        definition_id,
        recorded_at=first + timedelta(minutes=30),
        raw_reading=Decimal("8"),
    )
    replace_meter("replacement-2", new_meter, old_meter, second, None)
    _create_plan(
        sqlite_repository, "old-reused", definition_id, old_meter, second + timedelta(minutes=30)
    )
    _store_reading(
        sqlite_repository,
        "old-reused",
        definition_id,
        recorded_at=second + timedelta(minutes=30),
        raw_reading=Decimal("12"),
    )
    replace_meter("replacement-3", old_meter, new_meter, third, Decimal("20"))
    before_new_reading = sqlite_repository.list_replacement_boundaries_for_new_meter(
        definition_id, new_meter, before_scheduled_at=READ_AT
    )
    _create_plan(
        sqlite_repository, "new-reused", definition_id, new_meter, third + timedelta(minutes=30)
    )
    _store_reading(
        sqlite_repository,
        "new-reused",
        definition_id,
        recorded_at=third + timedelta(minutes=30),
        raw_reading=Decimal("23"),
    )
    sqlite_repository.update_replacement_boundary_calculation(
        ReplacementBoundaryCalculationUpdate(
            replacement_event_id="replacement-3",
            measurement_definition_id=definition_id,
            previous_measured_reading=Decimal("12"),
            previous_measured_planned_reading_id="old-reused",
            old_meter_usage_delta=Decimal("88"),
        )
    )
    assignment = sqlite_repository.get_meter_assignment(definition_id, third)
    assert assignment is not None
    memory_conn.execute(
        "INSERT INTO assignment_review_flags "
        "(measurement_definition_id, meter_id, assignment_effective_from, reason, created_at) "
        "VALUES (?, ?, ?, ?, ?)",
        (definition_id, new_meter, utc_now_str(third), "条件の確認", utc_now_str(READ_AT)),
    )
    changes = memory_conn.total_changes

    candidates = sqlite_repository.list_replacement_boundaries_for_new_meter(
        definition_id, new_meter, before_scheduled_at=READ_AT
    )

    assert [boundary.replacement_event_id for boundary in before_new_reading] == [
        "replacement-3",
        "replacement-1",
    ]
    assert [boundary.replacement_event_id for boundary in candidates] == [
        "replacement-3",
        "replacement-1",
    ]
    assert candidates[0].new_initial_reading == Decimal("20")
    assert candidates[0].old_final_reading == Decimal("100")
    assert candidates[0].old_meter_id == old_meter
    assert candidates[0].new_meter_id == new_meter
    assert candidates[0].replaced_at == third
    assert candidates[0].old_assignment_effective_from == second
    assert candidates[0].new_assignment_effective_from == third
    assert candidates[0].previous_measured_reading == Decimal("12")
    assert candidates[0].previous_measured_planned_reading_id == "old-reused"
    assert candidates[0].old_meter_usage_delta == Decimal("88")
    assert candidates[1].previous_measured_reading == Decimal("90")
    assert candidates[1].previous_measured_planned_reading_id == "old-first"
    event_boundaries = sqlite_repository.list_replacement_boundaries_for_event("replacement-3")
    assert [item.measurement_definition_id for item in event_boundaries] == [
        definition_id,
        "other-definition",
    ]
    assert event_boundaries[0] == candidates[0]
    assert sqlite_repository.list_replacement_boundaries_for_old_meter(
        definition_id, old_meter
    ) == [
        candidates[1],
        candidates[0],
    ]
    reviewed = sqlite_repository.get_meter_assignment(definition_id, third)
    assert reviewed is not None and reviewed.needs_review
    assert (
        sqlite_repository.get_meter_assignment_by_key(definition_id, new_meter, third) == reviewed
    )
    assert sqlite_repository.list_assignment_boundaries(reviewed) == [candidates[0]]
    assert [item.meter_id for item in sqlite_repository.list_meter_assignments(definition_id)] == [
        old_meter,
        new_meter,
        old_meter,
        new_meter,
    ]
    assert [
        item.planned_reading_id
        for item in sqlite_repository.list_measured_readings_on_or_after(
            definition_id, old_meter, from_scheduled_at=second
        )
    ] == ["old-reused"]
    assert (
        sqlite_repository.get_replacement_boundary_for_new_meter_first_reading(
            definition_id, new_meter, before_scheduled_at=READ_AT
        )
        is None
    )
    assert [
        boundary.replacement_event_id
        for boundary in sqlite_repository.list_replacement_boundaries_for_new_meter(
            definition_id, new_meter, before_scheduled_at=third
        )
    ] == ["replacement-3", "replacement-1"]
    assert [
        boundary.replacement_event_id
        for boundary in sqlite_repository.list_replacement_boundaries_for_new_meter(
            definition_id, new_meter, before_scheduled_at=third - timedelta(seconds=1)
        )
    ] == ["replacement-1"]
    assert (
        sqlite_repository.list_replacement_boundaries_for_new_meter(
            definition_id, new_meter, before_scheduled_at=first - timedelta(seconds=1)
        )
        == []
    )
    old_meter_candidates = sqlite_repository.list_replacement_boundaries_for_new_meter(
        definition_id, old_meter, before_scheduled_at=READ_AT
    )
    assert [boundary.replacement_event_id for boundary in old_meter_candidates] == ["replacement-2"]
    assert old_meter_candidates[0].new_initial_reading is None
    assert old_meter_candidates[0].previous_measured_reading == Decimal("8")
    assert [
        boundary.replacement_event_id
        for boundary in sqlite_repository.list_replacement_boundaries_for_new_meter(
            "other-definition", new_meter, before_scheduled_at=READ_AT
        )
    ] == ["replacement-3", "replacement-1"]
    assert (
        sqlite_repository.list_replacement_boundaries_for_new_meter(
            definition_id,
            new_meter,
            before_scheduled_at=READ_AT.astimezone(timezone(timedelta(hours=9))),
        )
        == candidates
    )
    with pytest.raises(ValueError, match="タイムゾーン"):
        sqlite_repository.list_replacement_boundaries_for_new_meter(
            definition_id, new_meter, before_scheduled_at=READ_AT.replace(tzinfo=None)
        )
    assert (
        sqlite_repository.list_replacement_boundaries_for_new_meter(
            "missing-definition", new_meter, before_scheduled_at=READ_AT
        )
        == []
    )
    assert memory_conn.total_changes == changes

    memory_conn.execute(
        "UPDATE replacement_boundaries SET previous_measured_reading = ? "
        "WHERE replacement_event_id = ? AND measurement_definition_id = ?",
        ("1e3", "replacement-3", definition_id),
    )
    with pytest.raises(ValidationError, match="plain decimal"):
        sqlite_repository.list_replacement_boundaries_for_new_meter(
            definition_id, new_meter, before_scheduled_at=READ_AT
        )


@pytest.mark.parametrize("column", ["raw_reading", "last_reading", "usage_delta"])
def test_import_target_lookup_surfaces_invalid_saved_decimal_values(
    memory_conn: sqlite3.Connection,
    sqlite_repository: SQLiteRepository,
    sample_master: tuple[str, str],
    column: str,
) -> None:
    meter_id, definition_id = sample_master
    _create_plan(sqlite_repository, "invalid-decimal", definition_id, meter_id)
    _store_reading(sqlite_repository, "invalid-decimal", definition_id)
    memory_conn.execute(
        f"UPDATE reading_transactions SET {column} = ? WHERE planned_reading_id = ?",
        ("1e3", "invalid-decimal"),
    )

    with pytest.raises(ValidationError, match="plain decimal"):
        sqlite_repository.get_mobile_reading_import_targets(["invalid-decimal"])


def test_import_target_lookup_surfaces_invalid_threshold(
    memory_conn: sqlite3.Connection,
    sqlite_repository: SQLiteRepository,
    sample_master: tuple[str, str],
) -> None:
    meter_id, definition_id = sample_master
    _create_plan(sqlite_repository, "invalid-threshold", definition_id, meter_id)
    memory_conn.execute(
        "UPDATE measurement_definitions SET warning_threshold = ? "
        "WHERE measurement_definition_id = ?",
        ("1e3", definition_id),
    )

    with pytest.raises(ValidationError, match="plain decimal"):
        sqlite_repository.get_mobile_reading_import_targets(["invalid-threshold"])


@pytest.mark.parametrize(
    ("delete_sql", "key", "message"),
    [
        ("DELETE FROM physical_meters WHERE meter_id = ?", "meter", "物理メーター"),
        (
            "DELETE FROM measurement_definitions WHERE measurement_definition_id = ?",
            "definition",
            "測定定義",
        ),
    ],
)
def test_import_target_lookup_does_not_treat_missing_master_as_missing_slot(
    memory_conn: sqlite3.Connection,
    sqlite_repository: SQLiteRepository,
    sample_master: tuple[str, str],
    delete_sql: str,
    key: str,
    message: str,
) -> None:
    meter_id, definition_id = sample_master
    _create_plan(sqlite_repository, "orphaned-slot", definition_id, meter_id)
    memory_conn.execute("PRAGMA foreign_keys = OFF")
    try:
        memory_conn.execute(delete_sql, (meter_id if key == "meter" else definition_id,))
    finally:
        memory_conn.execute("PRAGMA foreign_keys = ON")

    with pytest.raises(ValidationError, match=message):
        sqlite_repository.get_mobile_reading_import_targets(["orphaned-slot"])
