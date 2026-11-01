from __future__ import annotations

import sqlite3
from dataclasses import replace
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from functools import partial
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest
from reading_service_helpers import (
    DEFINITION_ID,
    LOCATION_ID,
    METER_ID,
    STAFF_ID,
    build_services,
    confirmation_context,
    create_plan,
    mobile_submission,
    sync_request,
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
from meter_reading_mini.core.errors import (
    InactiveMasterError,
    SupersededPlannedReadingError,
    ValidationError,
)
from meter_reading_mini.core.meter_master_service import MeterMasterService
from meter_reading_mini.core.meter_reading_sync_repositories import (
    AssignmentCalculationTerms,
    ManualReadingInput,
    MeasurementDefinitionCreateRequest,
    MeterMasterCreateInput,
    MeterMasterCreateRequest,
    PlannedReadingTaskState,
    ReplacementDefinitionReading,
    ReplacementUpsertRequest,
)
from meter_reading_mini.core.meter_replacement_service import (
    MeterReplacementCorrectionInput,
    MeterReplacementInput,
    MeterReplacementService,
)
from meter_reading_mini.core.reading_write_orchestration import ReadingWriteOrchestrationService
from meter_reading_mini.db.connection import transaction
from meter_reading_mini.db.integrity import check_business_integrity
from meter_reading_mini.db.migrations import create_schema
from meter_reading_mini.db.repositories import SQLiteRepository

NOW = datetime(2026, 2, 5, tzinfo=UTC)


def _at(hour: int) -> datetime:
    return datetime(2026, 2, 4, hour, tzinfo=UTC)


def _meter(repository: SQLiteRepository, meter_id: str, *, location_id: str = LOCATION_ID) -> None:
    repository.create_meter_master(
        MeterMasterCreateRequest(
            meter_id=meter_id,
            location_id=location_id,
            location_name="試験場所" if location_id == LOCATION_ID else "別の場所",
            meter_name=meter_id,
            category=Category.WATER,
        )
    )


def _service(
    conn: sqlite3.Connection,
    repository: SQLiteRepository,
    writer: ReadingWriteOrchestrationService,
    *,
    event_id: str = "replacement-id",
) -> MeterReplacementService:
    meter_service = MeterMasterService(
        repository,
        transaction_factory=partial(transaction, conn),
        id_provider=lambda: "created-meter",
    )
    return MeterReplacementService(
        repository,
        repository,
        repository,
        repository,
        repository,
        repository,
        repository,
        repository,
        meter_service,
        writer,
        transaction_factory=partial(transaction, conn),
        now_provider=lambda: NOW,
        id_provider=lambda: event_id,
        timezone_provider=lambda: ZoneInfo("Asia/Tokyo"),
    )


def _request() -> MeterReplacementInput:
    return MeterReplacementInput(
        old_meter_id=METER_ID,
        new_meter_id="new-meter",
        replaced_at=_at(5),
        old_final_reading=Decimal("120"),
        new_initial_reading=Decimal("5"),
        staff_id=STAFF_ID,
        note="交換",
        new_terms={DEFINITION_ID: AssignmentCalculationTerms(Decimal("20"), Decimal("50"))},
    )


def _setup(conn: sqlite3.Connection, repository: SQLiteRepository):
    writer, sync = build_services(
        conn, repository, multiplier=Decimal("10"), rollover_value=Decimal("1000")
    )
    _meter(repository, "new-meter")
    create_plan(repository, "before", _at(4))
    create_plan(repository, "nonmeasured", _at(6))
    create_plan(repository, "open", _at(7))
    writer.save_manual_reading(
        ManualReadingInput("before", ResultStatus.MEASURED, _at(4), Decimal("100"), None),
        staff_id=STAFF_ID,
    )
    return writer, sync, _service(conn, repository, writer)


def _database_state(conn: sqlite3.Connection) -> tuple[list[tuple], ...]:
    tables = (
        "physical_meters",
        "meter_assignments",
        "planned_readings",
        "reading_transactions",
        "replacement_events",
        "replacement_boundaries",
    )
    return tuple(
        conn.execute(f"SELECT * FROM {table} ORDER BY rowid").fetchall() for table in tables
    )


def test_replacement_retains_nonmeasured_audit_and_correction_updates_both_sides(
    file_database_repository: tuple[sqlite3.Connection, SQLiteRepository],
) -> None:
    conn, repository = file_database_repository
    writer, sync, service = _setup(conn, repository)
    writer.save_manual_reading(
        ManualReadingInput("nonmeasured", ResultStatus.MEASURED, _at(6), Decimal("110"), None),
        staff_id=STAFF_ID,
    )
    with pytest.raises(ValidationError, match="非実測へ訂正"):
        service.preview_replacement(_request())
    writer.save_manual_reading(
        ManualReadingInput("nonmeasured", ResultStatus.METER_FAULT, _at(6), None, None, "故障"),
        staff_id=STAFF_ID,
    )
    old_result = conn.execute(
        "SELECT * FROM reading_transactions WHERE planned_reading_id = 'nonmeasured'"
    ).fetchone()
    before = _database_state(conn)
    changes = conn.total_changes

    preview = service.preview_replacement(_request())

    assert preview.can_register
    assert (preview.incomplete_count, preview.nonmeasured_count) == (1, 1)
    assert preview.definitions[0].old_usage_delta == Decimal("200")
    assert preview.definitions[0].new_terms.multiplier == Decimal("20")
    assert conn.total_changes == changes and _database_state(conn) == before
    event = service.register_replacement(_request())
    assert event.replacement_event_id == "replacement-id"
    assert event.updated_by == "試験担当"
    assert (
        conn.execute(
            "SELECT * FROM reading_transactions WHERE planned_reading_id = 'nonmeasured'"
        ).fetchone()
        == old_result
    )
    old = repository.get_planned_reading("nonmeasured")
    assert old is not None and old.superseded_by_planned_reading_id is not None
    successor = old.superseded_by_planned_reading_id
    assert old.task_state is PlannedReadingTaskState.SUPERSEDED
    assert conn.execute(
        "SELECT task_state FROM planned_readings WHERE planned_reading_id = ?", (successor,)
    ).fetchone() == ("open",)
    assert (
        conn.execute(
            "SELECT 1 FROM reading_transactions WHERE planned_reading_id = ?", (successor,)
        ).fetchone()
        is None
    )
    with pytest.raises(SupersededPlannedReadingError):
        writer.save_manual_reading(
            ManualReadingInput("nonmeasured", ResultStatus.MEASURED, _at(6), Decimal("1"), None),
            staff_id=STAFF_ID,
        )
    mobile = sync.sync(sync_request(mobile_submission("nonmeasured", "1")), confirmation_context())
    assert mobile.accepted_reading_items == 0 and len(mobile.rejected_reading_items) == 1
    writer.save_manual_reading(
        ManualReadingInput(successor, ResultStatus.MEASURED, _at(6), Decimal("8"), None),
        staff_id=STAFF_ID,
    )
    new_audit = conn.execute(
        "SELECT raw_reading, recorded_at, updated_by, signature FROM reading_transactions "
        "WHERE planned_reading_id = ?",
        (successor,),
    ).fetchone()
    corrected = service.correct_replacement(
        MeterReplacementCorrectionInput(
            event.replacement_event_id, Decimal("130"), Decimal("6"), STAFF_ID, "訂正"
        )
    )
    assert corrected.arrival_sequence == 2
    assert corrected.client_updated_at == event.client_updated_at.replace(second=1)
    assert corrected.note == "訂正"
    assert conn.execute(
        "SELECT old_final_reading, new_initial_reading, old_meter_usage_delta "
        "FROM replacement_boundaries WHERE replacement_event_id = ?",
        (event.replacement_event_id,),
    ).fetchone() == ("130", "6", "300")
    assert conn.execute(
        "SELECT last_reading, usage_delta FROM reading_transactions WHERE planned_reading_id = ?",
        (successor,),
    ).fetchone() == ("6", "40")
    assert (
        conn.execute(
            "SELECT raw_reading, recorded_at, updated_by, signature FROM reading_transactions "
            "WHERE planned_reading_id = ?",
            (successor,),
        ).fetchone()
        == new_audit
    )
    history = service.list_replacements(LOCATION_ID)
    assert len(history) == 1 and history[0].event == corrected
    assert history[0].local_replaced_at.hour == 14
    assert check_business_integrity(conn) == []


def test_auto_closed_slot_creates_an_auto_closed_replacement_successor(
    file_database_repository: tuple[sqlite3.Connection, SQLiteRepository],
) -> None:
    conn, repository = file_database_repository
    _, _, service = _setup(conn, repository)
    conn.execute(
        "UPDATE planned_readings SET task_state = 'auto_closed', "
        "close_reason = 'cutoff_elapsed' WHERE planned_reading_id = 'open'"
    )

    service.register_replacement(_request())

    old = repository.get_planned_reading("open")
    assert old is not None and old.superseded_by_planned_reading_id is not None
    successor = repository.get_planned_reading(old.superseded_by_planned_reading_id)
    assert successor is not None
    assert successor.task_state is PlannedReadingTaskState.AUTO_CLOSED


def test_registration_can_create_a_meter_in_the_same_transaction(
    file_database_repository: tuple[sqlite3.Connection, SQLiteRepository],
) -> None:
    conn, repository = file_database_repository
    _, _, service = _setup(conn, repository)
    request = replace(
        _request(),
        new_meter_id=None,
        new_meter=MeterMasterCreateInput(
            meter_name="新規交換先", category=Category.WATER, location_id=LOCATION_ID
        ),
    )
    assert service.preview_replacement(request).can_register
    assert repository.get_meter_master("created-meter") is None
    event = service.register_replacement(request)
    assert event.new_meter_id == "created-meter"
    created = repository.get_meter_master(event.new_meter_id)
    assert created is not None and created.location_name == "試験場所"


def test_definition_lookup_returns_current_assignment_calculation_terms(
    file_database_repository: tuple[sqlite3.Connection, SQLiteRepository],
) -> None:
    conn, repository = file_database_repository
    _, _, service = _setup(conn, repository)
    service.register_replacement(_request())
    old_repository = SQLiteRepository(conn, clock=lambda: _at(4))
    new_repository = SQLiteRepository(conn, clock=lambda: _at(6))
    old = old_repository.get_measurement_definition(DEFINITION_ID)
    new = new_repository.get_measurement_definition(DEFINITION_ID)
    assert old is not None and new is not None
    assert (old.meter_id, old.multiplier, old.rollover_value) == (
        METER_ID,
        Decimal("10"),
        Decimal("1000"),
    )
    assert (new.meter_id, new.multiplier, new.rollover_value) == (
        "new-meter",
        Decimal("20"),
        Decimal("50"),
    )
    assert new_repository.list_measurement_definitions()[0] == new


def test_replacement_values_are_saved_independently_per_definition(
    file_database_repository: tuple[sqlite3.Connection, SQLiteRepository],
) -> None:
    conn, repository = file_database_repository
    _, _, service = _setup(conn, repository)
    repository.create_measurement_definition(
        MeasurementDefinitionCreateRequest(
            measurement_definition_id="second-definition",
            location_id=LOCATION_ID,
            display_name="別測定",
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
            generation_start_date=date(2026, 2, 1),
            initial_schedule_json=None,
            initial_meter_id=METER_ID,
        )
    )
    request = replace(
        _request(),
        old_final_reading=None,
        new_initial_reading=None,
        definition_readings=(
            ReplacementDefinitionReading(DEFINITION_ID, Decimal("120"), Decimal("5")),
            ReplacementDefinitionReading("second-definition", Decimal("35"), Decimal("2")),
        ),
    )

    service.register_replacement(request)

    assert conn.execute(
        "SELECT measurement_definition_id, old_final_reading, new_initial_reading "
        "FROM replacement_boundaries ORDER BY measurement_definition_id"
    ).fetchall() == [
        ("second-definition", "35", "2"),
        (DEFINITION_ID, "120", "5"),
    ]
    assert conn.execute(
        "SELECT old_final_reading, new_initial_reading FROM replacement_events"
    ).fetchone() == (None, None)


def test_mobile_sync_saves_replacement_readings_per_definition(
    file_database_repository: tuple[sqlite3.Connection, SQLiteRepository],
) -> None:
    conn, repository = file_database_repository
    _, sync, _ = _setup(conn, repository)
    repository.create_measurement_definition(
        MeasurementDefinitionCreateRequest(
            measurement_definition_id="second-definition",
            location_id=LOCATION_ID,
            display_name="別測定",
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
            generation_start_date=date(2026, 2, 1),
            initial_schedule_json=None,
            initial_meter_id=METER_ID,
        )
    )
    submission = MeterReplacementSubmission.model_validate(
        {
            "replacement_event_id": "mobile-replacement",
            "location_id": LOCATION_ID,
            "old_meter_id": METER_ID,
            "new_meter_id": "new-meter",
            "replaced_at": _at(5),
            "client_updated_at": _at(5),
            "definition_readings": [
                {
                    "measurement_definition_id": DEFINITION_ID,
                    "old_final_reading": "120",
                    "new_initial_reading": "5",
                },
                {
                    "measurement_definition_id": "second-definition",
                    "old_final_reading": "35",
                    "new_initial_reading": "2",
                },
            ],
            "updated_by": "mobile",
        }
    )

    response = sync.sync(
        MeterReadingSyncRequest(reading_items=[], replacement_items=[submission]),
        confirmation_context(),
    )

    assert response.accepted_replacement_items == 1
    assert conn.execute(
        "SELECT measurement_definition_id, old_final_reading, new_initial_reading "
        "FROM replacement_boundaries ORDER BY measurement_definition_id"
    ).fetchall() == [
        ("second-definition", "35", "2"),
        (DEFINITION_ID, "120", "5"),
    ]
    assert conn.execute(
        "SELECT old_final_reading, new_initial_reading FROM replacement_events"
    ).fetchone() == (None, None)


def test_correction_recalculates_a_later_replacement_without_intermediate_readings(
    file_database_repository: tuple[sqlite3.Connection, SQLiteRepository],
) -> None:
    conn, repository = file_database_repository
    writer, _, first_service = _setup(conn, repository)
    first = first_service.register_replacement(_request())
    _meter(repository, "third-meter")
    second_service = _service(conn, repository, writer, event_id="second-replacement")
    second = second_service.register_replacement(
        MeterReplacementInput(
            old_meter_id="new-meter",
            replaced_at=_at(8),
            old_final_reading=Decimal("9"),
            new_initial_reading=Decimal("1"),
            staff_id=STAFF_ID,
            new_meter_id="third-meter",
        )
    )
    assert conn.execute(
        "SELECT old_meter_usage_delta FROM replacement_boundaries WHERE replacement_event_id = ?",
        (second.replacement_event_id,),
    ).fetchone() == ("80",)

    first_service.correct_replacement(
        MeterReplacementCorrectionInput(
            first.replacement_event_id,
            Decimal("120"),
            Decimal("6"),
            STAFF_ID,
        )
    )

    assert conn.execute(
        "SELECT old_meter_usage_delta FROM replacement_boundaries WHERE replacement_event_id = ?",
        (second.replacement_event_id,),
    ).fetchone() == ("60",)


def test_correction_rolls_back_when_a_later_boundary_becomes_invalid(
    file_database_repository: tuple[sqlite3.Connection, SQLiteRepository],
) -> None:
    conn, repository = file_database_repository
    writer, _, first_service = _setup(conn, repository)
    first = first_service.register_replacement(_request())
    _meter(repository, "third-meter")
    second_service = _service(conn, repository, writer, event_id="second-replacement")
    second = second_service.register_replacement(
        MeterReplacementInput(
            old_meter_id="new-meter",
            replaced_at=_at(8),
            old_final_reading=Decimal("9"),
            new_initial_reading=Decimal("1"),
            staff_id=STAFF_ID,
            new_meter_id="third-meter",
        )
    )
    conn.execute("UPDATE meter_assignments SET rollover_value = NULL WHERE meter_id = 'new-meter'")
    before = _database_state(conn)

    with pytest.raises(ValidationError, match="ロールオーバー"):
        first_service.correct_replacement(
            MeterReplacementCorrectionInput(
                first.replacement_event_id,
                Decimal("120"),
                Decimal("10"),
                STAFF_ID,
                "後続境界エラー",
            )
        )

    assert _database_state(conn) == before
    assert conn.execute(
        "SELECT old_meter_usage_delta FROM replacement_boundaries WHERE replacement_event_id = ?",
        (second.replacement_event_id,),
    ).fetchone() == ("80",)


def test_registration_rechecks_and_rejects_bad_inputs_without_writes(
    file_database_repository: tuple[sqlite3.Connection, SQLiteRepository],
) -> None:
    conn, repository = file_database_repository
    writer, _, service = _setup(conn, repository)
    request = _request()
    preview = service.preview_replacement(request)
    assert preview.can_register
    writer.save_manual_reading(
        ManualReadingInput("open", ResultStatus.MEASURED, _at(7), Decimal("110"), None),
        staff_id=STAFF_ID,
    )
    before = _database_state(conn)
    with pytest.raises(ValidationError, match="非実測へ訂正"):
        service.register_replacement(request)
    assert _database_state(conn) == before
    with pytest.raises(InactiveMasterError):
        service.preview_replacement(replace(request, staff_id="999"))
    with pytest.raises(ValidationError, match="未来"):
        service.preview_replacement(replace(request, replaced_at=datetime(2030, 1, 1, tzinfo=UTC)))
    with pytest.raises(ValidationError, match="同じメーター"):
        service.preview_replacement(replace(request, new_meter_id=METER_ID))
    with pytest.raises(ValidationError, match="いずれか"):
        service.preview_replacement(replace(request, new_meter_id=None))


def test_replacement_reports_negative_boundary_and_invalid_conditions(
    file_database_repository: tuple[sqlite3.Connection, SQLiteRepository],
) -> None:
    conn, repository = file_database_repository
    _, _, service = _setup(conn, repository)
    conn.execute(
        "UPDATE meter_assignments SET rollover_value = NULL WHERE meter_id = ?", (METER_ID,)
    )
    request = replace(_request(), old_final_reading=Decimal("90"))
    preview = service.preview_replacement(request)
    assert not preview.can_register and "ロールオーバー" in preview.errors[0]
    with pytest.raises(ValidationError, match="交換できません"):
        service.register_replacement(request)
    with pytest.raises(ValidationError, match="倍率"):
        service.preview_replacement(
            replace(
                _request(),
                new_terms={DEFINITION_ID: AssignmentCalculationTerms(Decimal("0"), None)},
            )
        )


@pytest.mark.parametrize("problem", ["inactive", "other-location", "assigned", "missing"])
def test_unusable_replacement_meter_is_rejected_without_writes(
    file_database_repository: tuple[sqlite3.Connection, SQLiteRepository], problem: str
) -> None:
    conn, repository = file_database_repository
    _, _, service = _setup(conn, repository)
    if problem == "inactive":
        conn.execute("UPDATE physical_meters SET is_active = 0 WHERE meter_id = 'new-meter'")
    elif problem == "other-location":
        conn.execute(
            "UPDATE physical_meters SET location_id = 'elsewhere' WHERE meter_id = 'new-meter'"
        )
    elif problem == "assigned":
        repository.create_measurement_definition(
            MeasurementDefinitionCreateRequest(
                measurement_definition_id="other-definition",
                location_id=LOCATION_ID,
                display_name="別の測定対象",
                category=Category.WATER,
                unit="m3",
                value_schema=ValueSchema.DECIMAL_STRING,
                multiplier=Decimal("1"),
                decimal_places=2,
                warning_threshold=None,
                rollover_value=None,
                calculation_method=CalculationMethod.DELTA,
                reading_cycle=ReadingCycle.DAILY_1X,
                purpose=Purpose.OPERATIONAL,
                generation_start_date=date(2026, 2, 1),
                initial_schedule_json=None,
                initial_meter_id="new-meter",
            )
        )
    request = replace(_request(), new_meter_id="missing" if problem == "missing" else "new-meter")
    before = _database_state(conn)
    error_type = InactiveMasterError if problem in ("inactive", "missing") else ValidationError
    with pytest.raises(error_type):
        service.register_replacement(request)
    assert _database_state(conn) == before


def test_duplicate_generated_replacement_event_id_is_rejected_atomically(
    file_database_repository: tuple[sqlite3.Connection, SQLiteRepository],
) -> None:
    conn, repository = file_database_repository
    writer, _, service = _setup(conn, repository)
    service.register_replacement(_request())
    _meter(repository, "third-meter")
    duplicated = _service(conn, repository, writer, event_id="replacement-id")
    request = MeterReplacementInput(
        old_meter_id="new-meter",
        new_meter_id="third-meter",
        replaced_at=_at(6),
        old_final_reading=Decimal("6"),
        new_initial_reading=Decimal("1"),
        staff_id=STAFF_ID,
    )
    before = _database_state(conn)
    with pytest.raises(ValidationError, match="交換イベント ID は既に登録"):
        duplicated.register_replacement(request)
    assert _database_state(conn) == before


def test_subsequent_assignment_on_one_definition_rejects_the_whole_replacement(
    file_database_repository: tuple[sqlite3.Connection, SQLiteRepository],
) -> None:
    conn, repository = file_database_repository
    _, _, service = _setup(conn, repository)
    _meter(repository, "later-meter")
    repository.create_measurement_definition(
        MeasurementDefinitionCreateRequest(
            measurement_definition_id="second-definition",
            location_id=LOCATION_ID,
            display_name="別測定",
            category=Category.WATER,
            unit="m3",
            value_schema=ValueSchema.DECIMAL_STRING,
            multiplier=Decimal("1"),
            decimal_places=2,
            warning_threshold=None,
            rollover_value=None,
            calculation_method=CalculationMethod.DELTA,
            reading_cycle=ReadingCycle.DAILY_1X,
            purpose=Purpose.OPERATIONAL,
            generation_start_date=date(2026, 2, 1),
            initial_schedule_json=None,
            initial_meter_id=METER_ID,
        )
    )
    conn.execute(
        "UPDATE meter_assignments SET effective_to = '2026-02-04 08:00:00' "
        "WHERE measurement_definition_id = 'second-definition'"
    )
    conn.execute(
        "INSERT INTO meter_assignments "
        "(measurement_definition_id, meter_id, effective_from, multiplier) VALUES (?, ?, ?, ?)",
        ("second-definition", "later-meter", "2026-02-04 08:00:00", "1"),
    )
    before = _database_state(conn)
    with pytest.raises(ValidationError, match="後に割当切替"):
        service.register_replacement(_request())
    assert _database_state(conn) == before


@pytest.mark.parametrize(
    ("corruption", "message"),
    [
        ("open-result", "未完了枠に結果"),
        ("completed-missing", "完了枠の結果"),
        ("nonmeasured-value", "非実測結果に値"),
        ("replaced-result", "既存の交換結果"),
    ],
)
def test_inconsistent_or_unresolved_old_results_are_rejected(
    file_database_repository: tuple[sqlite3.Connection, SQLiteRepository],
    corruption: str,
    message: str,
) -> None:
    conn, repository = file_database_repository
    writer, _, service = _setup(conn, repository)
    if corruption == "completed-missing":
        conn.execute(
            "UPDATE planned_readings SET task_state = 'completed' WHERE planned_reading_id = 'open'"
        )
    else:
        writer.save_manual_reading(
            ManualReadingInput("open", ResultStatus.SKIPPED, _at(7), None, None),
            staff_id=STAFF_ID,
        )
        if corruption == "open-result":
            conn.execute(
                "UPDATE planned_readings SET task_state = 'open' WHERE planned_reading_id = 'open'"
            )
        elif corruption == "nonmeasured-value":
            conn.execute(
                "UPDATE reading_transactions SET raw_reading = '1' "
                "WHERE planned_reading_id = 'open'"
            )
        else:
            conn.execute(
                "UPDATE reading_transactions SET result_status = 'replaced' "
                "WHERE planned_reading_id = 'open'"
            )
    before = _database_state(conn)
    with pytest.raises(ValidationError, match=message):
        service.register_replacement(_request())
    assert _database_state(conn) == before


def test_correction_of_an_earlier_replacement_preserves_later_assignment_conditions(
    file_database_repository: tuple[sqlite3.Connection, SQLiteRepository],
) -> None:
    conn, repository = file_database_repository
    writer, _, service = _setup(conn, repository)
    first = service.register_replacement(_request())
    first_source = repository.get_planned_reading("nonmeasured")
    second_source = repository.get_planned_reading("open")
    assert first_source is not None and first_source.superseded_by_planned_reading_id is not None
    assert second_source is not None and second_source.superseded_by_planned_reading_id is not None
    first_slot = first_source.superseded_by_planned_reading_id
    next_slot = second_source.superseded_by_planned_reading_id
    writer.save_manual_reading(
        ManualReadingInput(first_slot, ResultStatus.MEASURED, _at(6), Decimal("8"), None),
        staff_id=STAFF_ID,
    )
    _meter(repository, "third-meter")
    second_service = _service(conn, repository, writer, event_id="replacement-2")
    second_service.register_replacement(
        MeterReplacementInput(
            old_meter_id="new-meter",
            new_meter_id="third-meter",
            replaced_at=_at(7),
            old_final_reading=Decimal("9"),
            new_initial_reading=Decimal("2"),
            staff_id=STAFF_ID,
            new_terms={DEFINITION_ID: AssignmentCalculationTerms(Decimal("30"), Decimal("100"))},
        )
    )
    second_old = repository.get_planned_reading(next_slot)
    assert second_old is not None and second_old.superseded_by_planned_reading_id is not None
    third_slot = second_old.superseded_by_planned_reading_id
    writer.save_manual_reading(
        ManualReadingInput(third_slot, ResultStatus.MEASURED, _at(7), Decimal("4"), None),
        staff_id=STAFF_ID,
    )
    service.correct_replacement(
        MeterReplacementCorrectionInput(
            first.replacement_event_id, Decimal("130"), Decimal("6"), STAFF_ID
        )
    )
    assert conn.execute(
        "SELECT replacement_event_id, old_meter_usage_delta FROM replacement_boundaries "
        "ORDER BY replacement_event_id"
    ).fetchall() == [("replacement-2", "20"), ("replacement-id", "300")]
    assert conn.execute(
        "SELECT planned_reading_id, usage_delta FROM reading_transactions "
        "WHERE planned_reading_id IN (?, ?) ORDER BY planned_reading_id",
        (first_slot, third_slot),
    ).fetchall() == sorted([(first_slot, "40"), (third_slot, "60")])


def test_preview_rollover_uses_previous_replacement_initial_when_no_reading_exists(
    file_database_repository: tuple[sqlite3.Connection, SQLiteRepository],
) -> None:
    conn, repository = file_database_repository
    writer, _, service = _setup(conn, repository)
    service.register_replacement(_request())
    _meter(repository, "third-meter")
    next_service = _service(conn, repository, writer, event_id="replacement-2")
    request = MeterReplacementInput(
        old_meter_id="new-meter",
        new_meter_id="third-meter",
        replaced_at=_at(6),
        old_final_reading=Decimal("1"),
        new_initial_reading=Decimal("2"),
        staff_id=STAFF_ID,
    )
    preview = next_service.preview_replacement(request)
    assert preview.can_register
    assert preview.definitions[0].rollover_applied
    assert preview.definitions[0].old_usage_delta == Decimal("920")
    next_service.register_replacement(request)
    assert conn.execute(
        "SELECT old_meter_usage_delta FROM replacement_boundaries "
        "WHERE replacement_event_id = 'replacement-2'"
    ).fetchone() == ("920",)


def test_replacement_normalizes_event_and_assignment_times_to_seconds(
    file_database_repository: tuple[sqlite3.Connection, SQLiteRepository],
) -> None:
    conn, repository = file_database_repository
    _, _, service = _setup(conn, repository)
    event = service.register_replacement(
        replace(_request(), replaced_at=_at(5) + timedelta(microseconds=500000))
    )
    assert event.replaced_at == _at(5)
    assert (
        conn.execute(
            "SELECT e.replaced_at, b.replaced_at, b.new_assignment_effective_from "
            "FROM replacement_events e JOIN replacement_boundaries b USING (replacement_event_id)"
        ).fetchone()
        == ("2026-02-04 05:00:00",) * 3
    )


def test_correction_recalculation_failure_rolls_back_event_and_boundary(
    file_database_repository: tuple[sqlite3.Connection, SQLiteRepository],
) -> None:
    conn, repository = file_database_repository
    writer, _, service = _setup(conn, repository)
    event = service.register_replacement(_request())
    old = repository.get_planned_reading("nonmeasured")
    assert old is not None and old.superseded_by_planned_reading_id is not None
    writer.save_manual_reading(
        ManualReadingInput(
            old.superseded_by_planned_reading_id,
            ResultStatus.MEASURED,
            _at(6),
            Decimal("8"),
            None,
        ),
        staff_id=STAFF_ID,
    )
    conn.execute("UPDATE meter_assignments SET rollover_value = NULL WHERE meter_id = 'new-meter'")
    before = _database_state(conn)
    with pytest.raises(ValidationError, match="ロールオーバー"):
        service.correct_replacement(
            MeterReplacementCorrectionInput(
                event.replacement_event_id, Decimal("130"), Decimal("9"), STAFF_ID
            )
        )
    assert _database_state(conn) == before
    assert not conn.in_transaction


@pytest.mark.parametrize("stage", ["reflection", "recalculation"])
def test_registration_failure_rolls_back_new_meter_and_all_exchange_effects(
    file_database_repository: tuple[sqlite3.Connection, SQLiteRepository],
    monkeypatch: pytest.MonkeyPatch,
    stage: str,
) -> None:
    conn, repository = file_database_repository
    writer, _, service = _setup(conn, repository)
    request = replace(
        _request(),
        new_meter_id=None,
        new_meter=MeterMasterCreateInput(
            meter_name="新規", category=Category.WATER, location_id=LOCATION_ID
        ),
    )
    before = _database_state(conn)
    if stage == "reflection":
        original = repository.reflect_verified_replacement

        def fail_reflection(
            saved: ReplacementUpsertRequest, terms: dict[str, AssignmentCalculationTerms]
        ) -> None:
            original(saved, terms)
            raise RuntimeError("交換反映後の障害")

        monkeypatch.setattr(repository, "reflect_verified_replacement", fail_reflection)
    else:
        original_calculation = writer.recalculate_after_replacement

        def fail_calculation(saved: ReplacementUpsertRequest) -> None:
            original_calculation(saved)
            raise RuntimeError("再計算後の障害")

        monkeypatch.setattr(writer, "recalculate_after_replacement", fail_calculation)
    with pytest.raises(RuntimeError, match="障害"):
        service.register_replacement(request)
    assert _database_state(conn) == before
    assert repository.get_meter_master("created-meter") is None
    assert not conn.in_transaction


def test_replacement_commit_failure_rolls_back_everything(tmp_path: Path) -> None:
    class CommitFailingConnection(sqlite3.Connection):
        fail_commit = False

        def execute(self, sql, parameters=()):
            if self.fail_commit and sql.strip().upper() == "COMMIT":
                raise sqlite3.OperationalError("simulated commit failure")
            return super().execute(sql, parameters)

    conn = sqlite3.connect(
        tmp_path / "replacement.db", autocommit=True, factory=CommitFailingConnection
    )
    try:
        conn.execute("PRAGMA foreign_keys = ON")
        create_schema(conn)
        repository = SQLiteRepository(conn)
        _, _, service = _setup(conn, repository)
        before = _database_state(conn)
        conn.fail_commit = True
        with pytest.raises(sqlite3.OperationalError, match="simulated commit failure"):
            service.register_replacement(_request())
        assert _database_state(conn) == before
        assert not conn.in_transaction
    finally:
        conn.close()


def test_replacement_correction_commit_failure_rolls_back_everything(tmp_path: Path) -> None:
    class CommitFailingConnection(sqlite3.Connection):
        fail_commit = False

        def execute(self, sql, parameters=()):
            if self.fail_commit and sql.strip().upper() == "COMMIT":
                raise sqlite3.OperationalError("simulated commit failure")
            return super().execute(sql, parameters)

    conn = sqlite3.connect(
        tmp_path / "correction.db", autocommit=True, factory=CommitFailingConnection
    )
    try:
        conn.execute("PRAGMA foreign_keys = ON")
        create_schema(conn)
        repository = SQLiteRepository(conn)
        _, _, service = _setup(conn, repository)
        event = service.register_replacement(_request())
        before = _database_state(conn)
        conn.fail_commit = True
        with pytest.raises(sqlite3.OperationalError, match="simulated commit failure"):
            service.correct_replacement(
                MeterReplacementCorrectionInput(
                    event.replacement_event_id, Decimal("130"), Decimal("6"), STAFF_ID
                )
            )
        assert _database_state(conn) == before
        assert not conn.in_transaction
    finally:
        conn.close()
