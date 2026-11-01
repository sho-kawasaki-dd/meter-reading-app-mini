from __future__ import annotations

import sqlite3
from dataclasses import FrozenInstanceError, replace
from datetime import UTC, date, datetime, timedelta, timezone
from decimal import Decimal
from unittest.mock import patch

import pytest
from reading_service_helpers import (
    DEFINITION_ID,
    LOCATION_ID,
    METER_ID,
    SHEET_ID,
    STAFF_ID,
    build_services,
    confirmation_context,
    create_plan,
)

from meter_reading_mini.core.contract_models import (
    CalculationMethod,
    Category,
    MeterReadingSubmission,
    Purpose,
    ReadingCycle,
    ResultStatus,
    ValueSchema,
)
from meter_reading_mini.core.errors import ValidationError
from meter_reading_mini.core.meter_reading_sync import MeterReadingSyncService
from meter_reading_mini.core.meter_reading_sync_repositories import (
    ManualReadingInput,
    MeasurementDefinitionCreateRequest,
    MeterMasterCreateRequest,
    PlannedReadingTaskState,
    ReplacementDefinitionReading,
    ReplacementUpsertRequest,
)
from meter_reading_mini.core.mobile_reading import MobileReadingResult, SheetFilter
from meter_reading_mini.core.mobile_reading_import import (
    MobileReadingDisposition,
    MobileReadingImportService,
    MobileReadingPreviewRow,
    MobileReadingRejectCode,
    MobileReadingWarningCode,
)
from meter_reading_mini.core.previous_reading import PreviousReadingSource
from meter_reading_mini.core.reading_write_orchestration import ReadingWriteOrchestrationService
from meter_reading_mini.core.usage_delta_calculation import UsageDeltaCalculationService
from meter_reading_mini.db.connection import transaction
from meter_reading_mini.db.repositories import SQLiteRepository


def _at(hour: int) -> datetime:
    return datetime(2026, 2, 4, hour, tzinfo=UTC)


def _submission(
    slot: str,
    raw: str | None,
    hour: int,
    *,
    updated_at: datetime | None = None,
    status: ResultStatus = ResultStatus.MEASURED,
    note: str | None = None,
    structured: dict[str, object] | None = None,
) -> MeterReadingSubmission:
    return MeterReadingSubmission.model_validate(
        {
            "planned_reading_id": slot,
            "result_status": status,
            "raw_reading": raw,
            "structured_payload": structured,
            "recorded_at": _at(hour),
            "client_updated_at": updated_at or datetime(2026, 2, 6, hour, tzinfo=UTC),
            "note": note,
        }
    )


def _result(
    *entries: MeterReadingSubmission,
    business_date: date | None = None,
    generated_at: str | None = None,
    filter: SheetFilter | None = None,
) -> MobileReadingResult:
    return MobileReadingResult(
        sheet_id=SHEET_ID,
        entries=tuple(entries),
        business_date=business_date,
        generated_at=generated_at,
        filter=filter,
    )


def _manual(slot: str, raw: str, hour: int) -> ManualReadingInput:
    return ManualReadingInput(slot, ResultStatus.MEASURED, _at(hour), Decimal(raw), None)


def _stored_equals_projection(repository: SQLiteRepository, row: MobileReadingPreviewRow) -> None:
    assert row.final is not None
    expected = row.final.result
    assert expected is not None
    target = repository.get_mobile_reading_import_targets([row.input.planned_reading_id])[
        row.input.planned_reading_id
    ]
    actual = target.existing_result
    assert actual is not None
    assert (
        actual.result_status,
        actual.raw_reading,
        actual.structured_payload_json,
        actual.note,
        actual.recorded_at,
        actual.client_updated_at,
        actual.result_source,
        actual.device_id,
        actual.signature,
        actual.last_reading,
        actual.usage_delta,
        actual.arrival_sequence,
    ) == (
        expected.result_status,
        expected.raw_reading,
        expected.structured_payload_json,
        expected.note,
        expected.recorded_at,
        expected.client_updated_at,
        expected.result_source,
        expected.device_id,
        expected.signature,
        expected.last_reading,
        expected.usage_delta,
        expected.arrival_sequence,
    )
    if expected.updated_by is not None:
        assert actual.updated_by == expected.updated_by
    assert target.task_state is row.final.task_state
    assert target.closed_at == row.final.closed_at
    assert target.close_reason == row.final.close_reason


def _replace_meter(
    conn: sqlite3.Connection,
    repository: SQLiteRepository,
    writer: ReadingWriteOrchestrationService,
    *,
    initial: str | None,
    include_structured: bool = False,
) -> None:
    repository.create_meter_master(
        MeterMasterCreateRequest(
            meter_id="new-meter",
            location_id=LOCATION_ID,
            location_name="試験場所",
            meter_name="交換後メーター",
            category=Category.WATER,
        )
    )
    request = ReplacementUpsertRequest(
        replacement_event_id="replacement",
        location_id=LOCATION_ID,
        old_meter_id=METER_ID,
        new_meter_id="new-meter",
        replaced_at=_at(6),
        client_updated_at=_at(6),
        old_final_reading=None,
        new_initial_reading=None,
        updated_by="試験担当",
        note=None,
        definition_readings=(
            ReplacementDefinitionReading(
                DEFINITION_ID, Decimal("120"), Decimal(initial) if initial is not None else None
            ),
        )
        + ((ReplacementDefinitionReading("structured", None, None),) if include_structured else ()),
    )
    with transaction(conn):
        repository.upsert_replacement(request)
        repository.reflect_replacement(request)
        writer.recalculate_after_replacement(request)


def _structured_definition(repository: SQLiteRepository) -> None:
    repository.create_measurement_definition(
        MeasurementDefinitionCreateRequest(
            measurement_definition_id="structured",
            location_id=LOCATION_ID,
            display_name="力率",
            category=Category.WATER,
            unit="%",
            value_schema=ValueSchema.POWER_FACTOR_PERCENT_PHASE,
            multiplier=Decimal("1"),
            decimal_places=None,
            warning_threshold=None,
            rollover_value=None,
            calculation_method=CalculationMethod.RAW_ONLY,
            reading_cycle=ReadingCycle.MONTHLY,
            purpose=Purpose.OPERATIONAL,
            generation_start_date=date(2026, 2, 1),
            initial_schedule_json=None,
            initial_meter_id=METER_ID,
        )
    )


def _sync(service: MeterReadingSyncService, result: MobileReadingResult) -> None:
    service.sync(result.to_sync_request(), confirmation_context())


def test_preview_is_read_only_and_returns_final_values_in_payload_order(
    memory_conn: sqlite3.Connection,
    sqlite_repository: SQLiteRepository,
) -> None:
    writer, sync = build_services(memory_conn, sqlite_repository)
    for slot, hour in (("previous", 3), ("early", 8), ("late", 14)):
        create_plan(sqlite_repository, slot, _at(hour))
    writer.save_manual_reading(_manual("previous", "100", 3), staff_id=STAFF_ID)
    result = _result(_submission("late", "130", 14), _submission("early", "110", 8))
    before = tuple(memory_conn.iterdump())
    changes = memory_conn.total_changes

    with (
        patch.object(
            sqlite_repository, "upsert_reading", wraps=sqlite_repository.upsert_reading
        ) as upsert,
        patch.object(
            sqlite_repository,
            "save_calculated_usage",
            wraps=sqlite_repository.save_calculated_usage,
        ) as save_usage,
        patch.object(
            sqlite_repository,
            "update_replacement_boundary_calculation",
            wraps=sqlite_repository.update_replacement_boundary_calculation,
        ) as update_boundary,
    ):
        preview = MobileReadingImportService(sqlite_repository).preview(result)
    upsert.assert_not_called()
    save_usage.assert_not_called()
    update_boundary.assert_not_called()
    assert tuple(memory_conn.iterdump()) == before
    assert memory_conn.total_changes == changes
    assert [row.input.planned_reading_id for row in preview.rows] == ["late", "early"]
    assert [row.row_number for row in preview.rows] == [1, 2]
    assert all(row.disposition is MobileReadingDisposition.NEW for row in preview.rows)
    assert preview.summary.accepted == 2 and preview.summary.recalculated_existing == 0
    assert preview.can_commit
    late, early = preview.rows
    assert late.input_calculation is not None
    assert late.input_calculation.usage_delta == Decimal("30")
    assert late.final is not None and late.final.result is not None
    assert (late.final.result.last_reading, late.final.result.usage_delta) == (
        Decimal("110"),
        Decimal("20"),
    )
    assert late.final.previous is not None
    assert late.final.previous.planned_reading_id == "early"
    assert early.final is not None and early.final.result is not None
    assert early.final.result.usage_delta == Decimal("10")
    assert early.final.result.updated_by is None

    _sync(sync, result)
    for row in preview.rows:
        _stored_equals_projection(sqlite_repository, row)


def test_preview_calculates_each_input_once_before_conflict_resolution(
    memory_conn: sqlite3.Connection,
    sqlite_repository: SQLiteRepository,
) -> None:
    _, sync = build_services(memory_conn, sqlite_repository)
    create_plan(sqlite_repository, "target", _at(8))
    entry = _submission("target", "1", 8)
    _sync(sync, _result(entry))
    calculator = UsageDeltaCalculationService()
    with patch.object(calculator, "calculate", wraps=calculator.calculate) as calculate:
        preview = MobileReadingImportService(
            sqlite_repository, calculation_service=calculator
        ).preview(_result(entry))
    assert preview.rows[0].disposition is MobileReadingDisposition.DUPLICATE
    assert calculate.call_count == 1


def test_equal_schedule_times_use_planned_id_order_without_reordering_payload(
    memory_conn: sqlite3.Connection,
    sqlite_repository: SQLiteRepository,
) -> None:
    writer, sync = build_services(memory_conn, sqlite_repository)
    create_plan(sqlite_repository, "previous", _at(3))
    for slot in ("a-first", "z-last"):
        create_plan(sqlite_repository, slot, _at(8))
    writer.save_manual_reading(_manual("previous", "100", 3), staff_id=STAFF_ID)
    result = _result(_submission("z-last", "130", 8), _submission("a-first", "110", 8))
    preview = MobileReadingImportService(sqlite_repository).preview(result)
    late, early = preview.rows
    assert late.input.planned_reading_id == "z-last"
    assert early.input.planned_reading_id == "a-first"
    assert late.final is not None and late.final.result is not None
    assert late.final.previous is not None
    assert late.final.previous.planned_reading_id == "a-first"
    assert late.final.result.usage_delta == Decimal("20")
    _sync(sync, result)
    for row in preview.rows:
        _stored_equals_projection(sqlite_repository, row)


def test_downstream_rejection_rolls_back_trial_before_next_row(
    memory_conn: sqlite3.Connection,
    sqlite_repository: SQLiteRepository,
) -> None:
    writer, sync = build_services(memory_conn, sqlite_repository)
    for slot, hour in (("previous", 3), ("early", 8), ("middle", 9), ("late", 14)):
        create_plan(sqlite_repository, slot, _at(hour))
    writer.save_manual_reading(_manual("previous", "100", 3), staff_id=STAFF_ID)
    result = _result(
        _submission("late", "110", 14),
        _submission("early", "120", 8),
        _submission("middle", "105", 9),
    )
    preview = MobileReadingImportService(sqlite_repository).preview(result)
    late, rejected, middle = preview.rows
    assert rejected.disposition is MobileReadingDisposition.REJECTED
    assert rejected.reason is not None
    assert rejected.reason.code is MobileReadingRejectCode.DOWNSTREAM_RECALCULATION
    assert rejected.reason.cause is MobileReadingRejectCode.NEGATIVE_USAGE
    assert (rejected.reason.raw_reading, rejected.reason.last_reading) == (
        Decimal("110"),
        Decimal("120"),
    )
    assert rejected.input_calculation is not None
    assert rejected.input_calculation.usage_delta == Decimal("20")
    assert rejected.final is not None and rejected.final.result is None
    assert rejected.final.warning_threshold_exceeded is None
    assert rejected.final.rollover_applied is None
    assert late.final is not None and late.final.result is not None
    assert (late.final.result.last_reading, late.final.result.usage_delta) == (
        Decimal("105"),
        Decimal("5"),
    )
    assert middle.disposition is MobileReadingDisposition.NEW
    assert preview.summary.rejected == 1
    assert preview.effects.accepted_reading_ids == frozenset({"late", "middle"})
    assert preview.summary.recalculated_existing == 0

    _sync(sync, result)
    for row in (late, middle):
        _stored_equals_projection(sqlite_repository, row)
    assert (
        sqlite_repository.get_mobile_reading_import_targets(["early"])["early"].existing_result
        is None
    )


def test_duplicate_is_preserved_but_indirectly_recalculated(
    memory_conn: sqlite3.Connection,
    sqlite_repository: SQLiteRepository,
) -> None:
    writer, sync = build_services(memory_conn, sqlite_repository)
    for slot, hour in (("previous", 3), ("early", 8), ("late", 14)):
        create_plan(sqlite_repository, slot, _at(hour))
    writer.save_manual_reading(_manual("previous", "100", 3), staff_id=STAFF_ID)
    late = _submission("late", "120.00", 14, note="既存")
    _sync(sync, _result(late))
    before = sqlite_repository.get_mobile_reading_import_targets(["late"])["late"].existing_result
    assert before is not None
    duplicate = _submission(
        "late",
        "120.0",
        14,
        updated_at=late.client_updated_at.astimezone(timezone(timedelta(hours=9))).replace(
            microsecond=999999
        ),
        note="既存",
    )
    result = _result(duplicate, _submission("early", "110", 8))
    preview = MobileReadingImportService(sqlite_repository).preview(result)
    ignored, accepted = preview.rows
    assert ignored.disposition is MobileReadingDisposition.DUPLICATE
    assert ignored.target is not None and ignored.target.existing_result == before
    assert ignored.input.raw_reading == "120.0"
    assert ignored.final is not None and ignored.final.result is not None
    final = ignored.final.result
    assert (final.last_reading, final.usage_delta) == (Decimal("110"), Decimal("10"))
    assert (final.device_id, final.updated_by, final.signature, final.arrival_sequence) == (
        before.device_id,
        before.updated_by,
        before.signature,
        before.arrival_sequence,
    )
    assert preview.effects.recalculated_existing_ids == frozenset({"late"})
    assert preview.summary.duplicate == 1 and preview.summary.recalculated_existing == 1

    _sync(sync, result)
    _stored_equals_projection(sqlite_repository, ignored)
    _stored_equals_projection(sqlite_repository, accepted)


@pytest.mark.parametrize(
    ("raw", "update_offset", "expected"),
    [
        ("90", 0, MobileReadingDisposition.REJECTED),
        ("130", -1, MobileReadingDisposition.STALE),
    ],
)
def test_excluded_existing_row_is_still_indirectly_recalculated(
    memory_conn: sqlite3.Connection,
    sqlite_repository: SQLiteRepository,
    raw: str,
    update_offset: int,
    expected: MobileReadingDisposition,
) -> None:
    writer, sync = build_services(memory_conn, sqlite_repository)
    for slot, hour in (("previous", 3), ("early", 8), ("late", 14)):
        create_plan(sqlite_repository, slot, _at(hour))
    writer.save_manual_reading(_manual("previous", "100", 3), staff_id=STAFF_ID)
    existing = _submission("late", "120", 14, note="保持する備考")
    _sync(sync, _result(existing))
    before = sqlite_repository.get_mobile_reading_import_targets(["late"])["late"].existing_result
    assert before is not None
    excluded = _submission(
        "late",
        raw,
        14,
        updated_at=existing.client_updated_at + timedelta(seconds=update_offset),
        note="採用しない備考",
    )
    result = _result(excluded, _submission("early", "110", 8))
    preview = MobileReadingImportService(sqlite_repository).preview(result)
    row = preview.rows[0]
    assert row.disposition is expected and row.input.raw_reading == raw
    assert row.final is not None and row.final.result is not None
    projected = row.final.result
    assert (
        projected.raw_reading,
        projected.note,
        projected.last_reading,
        projected.usage_delta,
    ) == (
        Decimal("120"),
        "保持する備考",
        Decimal("110"),
        Decimal("10"),
    )
    assert (projected.signature, projected.updated_by, projected.arrival_sequence) == (
        before.signature,
        before.updated_by,
        before.arrival_sequence,
    )
    assert preview.effects.recalculated_existing_ids == frozenset({"late"})
    _sync(sync, result)
    _stored_equals_projection(sqlite_repository, row)


@pytest.mark.parametrize(
    ("raw", "update_offset", "expected", "reason"),
    [
        ("90", -1, MobileReadingDisposition.REJECTED, MobileReadingRejectCode.NEGATIVE_USAGE),
        ("130", -1, MobileReadingDisposition.STALE, None),
        ("110", 0, MobileReadingDisposition.DUPLICATE, None),
        ("115", 0, MobileReadingDisposition.OVERWRITTEN, None),
    ],
)
def test_preview_matches_sync_conflict_and_calculation_order(
    memory_conn: sqlite3.Connection,
    sqlite_repository: SQLiteRepository,
    raw: str,
    update_offset: int,
    expected: MobileReadingDisposition,
    reason: MobileReadingRejectCode | None,
) -> None:
    writer, sync = build_services(memory_conn, sqlite_repository)
    for slot, hour in (("previous", 3), ("target", 8), ("following", 14)):
        create_plan(sqlite_repository, slot, _at(hour))
    writer.save_manual_reading(_manual("previous", "100", 3), staff_id=STAFF_ID)
    current = _submission("target", "110", 8)
    _sync(sync, _result(current, _submission("following", "120", 14)))
    incoming = _submission(
        "target", raw, 8, updated_at=current.client_updated_at + timedelta(seconds=update_offset)
    )
    result = _result(incoming)
    preview = MobileReadingImportService(sqlite_repository).preview(result)
    row = preview.rows[0]
    assert row.disposition is expected
    assert (row.reason.code if row.reason is not None else None) is reason
    assert preview.can_commit is (expected is MobileReadingDisposition.OVERWRITTEN)

    _sync(sync, result)
    _stored_equals_projection(sqlite_repository, row)


def test_missing_superseded_and_valid_rows_are_independent(
    memory_conn: sqlite3.Connection,
    sqlite_repository: SQLiteRepository,
) -> None:
    _, sync = build_services(memory_conn, sqlite_repository)
    create_plan(sqlite_repository, "superseded", _at(8))
    create_plan(sqlite_repository, "valid", _at(14))
    memory_conn.execute(
        "UPDATE planned_readings SET task_state = 'superseded', "
        "close_reason = 'meter_replaced', closed_at = ? WHERE planned_reading_id = ?",
        ("2026-02-04 06:00:00", "superseded"),
    )
    result = _result(
        _submission("missing", "1", 3),
        _submission("superseded", "2", 8),
        _submission("valid", "3", 14),
    )
    preview = MobileReadingImportService(sqlite_repository).preview(result)
    missing, superseded, valid = preview.rows
    assert missing.reason is not None
    assert missing.reason.code is MobileReadingRejectCode.UNKNOWN_PLANNED_READING
    assert missing.target is None and missing.final is None
    assert superseded.reason is not None
    assert superseded.reason.code is MobileReadingRejectCode.SUPERSEDED
    assert valid.disposition is MobileReadingDisposition.NEW
    assert preview.summary.rejected == 2 and preview.summary.accepted == 1
    _sync(sync, result)
    _stored_equals_projection(sqlite_repository, valid)


@pytest.mark.parametrize(
    ("fault", "expected"),
    [
        ("missing_definition", MobileReadingRejectCode.UNKNOWN_MEASUREMENT_DEFINITION),
        ("missing_assignment", MobileReadingRejectCode.MISSING_ASSIGNMENT),
        ("needs_review", MobileReadingRejectCode.ASSIGNMENT_NEEDS_REVIEW),
        ("invalid_value", MobileReadingRejectCode.INVALID_INPUT),
    ],
)
def test_per_row_business_rejections_are_typed(
    memory_conn: sqlite3.Connection,
    sqlite_repository: SQLiteRepository,
    monkeypatch: pytest.MonkeyPatch,
    fault: str,
    expected: MobileReadingRejectCode,
) -> None:
    build_services(memory_conn, sqlite_repository)
    create_plan(sqlite_repository, "target", _at(8))
    if fault == "missing_definition":
        monkeypatch.setattr(sqlite_repository, "get_measurement_definition", lambda _: None)
    elif fault == "missing_assignment":
        monkeypatch.setattr(sqlite_repository, "get_meter_assignment", lambda *_: None)
    elif fault == "needs_review":
        memory_conn.execute(
            "INSERT INTO assignment_review_flags "
            "(measurement_definition_id, meter_id, assignment_effective_from, reason, created_at) "
            "SELECT measurement_definition_id, meter_id, effective_from, '要確認', ? "
            "FROM meter_assignments WHERE measurement_definition_id = ?",
            ("2026-02-04 00:00:00", DEFINITION_ID),
        )
    raw = "1.234" if fault == "invalid_value" else "1"
    preview = MobileReadingImportService(sqlite_repository).preview(
        _result(_submission("target", raw, 8))
    )
    row = preview.rows[0]
    assert row.disposition is MobileReadingDisposition.REJECTED
    assert row.reason is not None and row.reason.code is expected
    assert row.final is not None and row.final.result is None
    assert not preview.can_commit


@pytest.mark.parametrize(
    ("business_date", "sheet_filter", "expected"),
    [
        (None, None, set()),
        (date(2026, 2, 4), SheetFilter.create(["08:00"], ["daily"]), set()),
        (
            date(2026, 2, 3),
            SheetFilter.create(["14:00"], ["monthly"]),
            {
                MobileReadingWarningCode.BUSINESS_DATE_MISMATCH,
                MobileReadingWarningCode.READING_SESSION_MISMATCH,
                MobileReadingWarningCode.CYCLE_GROUP_MISMATCH,
            },
        ),
        (None, SheetFilter.create(), set()),
    ],
)
def test_header_mismatches_warn_without_rejecting(
    memory_conn: sqlite3.Connection,
    sqlite_repository: SQLiteRepository,
    business_date: date | None,
    sheet_filter: SheetFilter | None,
    expected: set[MobileReadingWarningCode],
) -> None:
    build_services(memory_conn, sqlite_repository)
    create_plan(sqlite_repository, "target", _at(8))
    result = _result(
        _submission("target", "1", 8),
        business_date=business_date,
        filter=sheet_filter,
        generated_at="2024-01-01 00:00:00",
    )
    row = MobileReadingImportService(sqlite_repository).preview(result).rows[0]
    assert {warning.code for warning in row.warnings} == expected
    assert row.disposition is MobileReadingDisposition.NEW


def test_duplicate_ids_and_invalid_header_are_rejected_before_repository_reads(
    memory_conn: sqlite3.Connection,
    sqlite_repository: SQLiteRepository,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    build_services(memory_conn, sqlite_repository)
    first = _submission("first", "1", 8)
    second = _submission("second", "2", 14)
    result = _result(first, second)
    second.planned_reading_id = "first"

    def fail_read(*_args: object, **_kwargs: object) -> None:
        raise AssertionError("入力全体の検証前に DB を読み取ってはいけません")

    monkeypatch.setattr(sqlite_repository, "get_mobile_reading_import_targets", fail_read)
    service = MobileReadingImportService(sqlite_repository)
    with pytest.raises(ValidationError, match="重複"):
        service.preview(result)
    invalid_header = _result(_submission("first", "1", 8), generated_at="2026-02-04T08:00:00Z")
    with pytest.raises(ValidationError, match="出力日時"):
        service.preview(invalid_header)
    invalid_entry = _submission("first", "1", 8)
    object.__setattr__(invalid_entry, "raw_reading", 1.5)
    with pytest.raises(ValidationError, match="基本形式"):
        service.preview(_result(invalid_entry))
    invalid_note = _submission("first", "1", 8, note="長" * 501)
    with pytest.raises(ValidationError, match="備考"):
        service.preview(_result(invalid_note))


def test_rollover_threshold_and_auto_closed_revival_use_assignment_conditions(
    memory_conn: sqlite3.Connection,
    sqlite_repository: SQLiteRepository,
) -> None:
    writer, sync = build_services(
        memory_conn, sqlite_repository, multiplier=Decimal("2"), rollover_value=Decimal("100")
    )
    for slot, hour in (("previous", 3), ("target", 8)):
        create_plan(sqlite_repository, slot, _at(hour))
    writer.save_manual_reading(_manual("previous", "98", 3), staff_id=STAFF_ID)
    memory_conn.execute(
        "UPDATE measurement_definitions SET warning_threshold = ? "
        "WHERE measurement_definition_id = ?",
        ("10", DEFINITION_ID),
    )
    memory_conn.execute(
        "UPDATE planned_readings SET task_state = 'auto_closed', close_reason = 'cutoff_elapsed', "
        "closed_at = ? WHERE planned_reading_id = ?",
        ("2026-02-04 07:00:00", "target"),
    )
    result = _result(_submission("target", "5", 8))
    preview = MobileReadingImportService(sqlite_repository).preview(result)
    row = preview.rows[0]
    assert row.disposition is MobileReadingDisposition.REVIVED
    assert row.final is not None and row.final.result is not None
    assert row.final.result.usage_delta == Decimal("14")
    assert row.final.task_state is PlannedReadingTaskState.COMPLETED
    assert row.final.previous is not None
    assert row.final.previous.source is PreviousReadingSource.MEASURED
    assert {warning.code for warning in row.warnings} == {
        MobileReadingWarningCode.ROLLOVER_APPLIED,
        MobileReadingWarningCode.THRESHOLD_EXCEEDED,
    }
    _sync(sync, result)
    _stored_equals_projection(sqlite_repository, row)


def test_final_threshold_warning_reflects_later_input_recalculation(
    memory_conn: sqlite3.Connection,
    sqlite_repository: SQLiteRepository,
) -> None:
    writer, _ = build_services(memory_conn, sqlite_repository)
    for slot, hour in (("previous", 3), ("early", 8), ("late", 14)):
        create_plan(sqlite_repository, slot, _at(hour))
    writer.save_manual_reading(_manual("previous", "100", 3), staff_id=STAFF_ID)
    memory_conn.execute(
        "UPDATE measurement_definitions SET warning_threshold = ? "
        "WHERE measurement_definition_id = ?",
        ("25", DEFINITION_ID),
    )
    result = _result(_submission("late", "130", 14), _submission("early", "110", 8))
    late = MobileReadingImportService(sqlite_repository).preview(result).rows[0]
    assert late.input_calculation is not None
    assert late.input_calculation.warning_threshold_exceeded
    assert late.final is not None and late.final.result is not None
    assert late.final.result.usage_delta == Decimal("20")
    assert not late.final.warning_threshold_exceeded
    assert MobileReadingWarningCode.THRESHOLD_EXCEEDED not in {
        warning.code for warning in late.warnings
    }


@pytest.mark.parametrize("initial", ["5", None])
@pytest.mark.parametrize("order", [("early", "late"), ("late", "early")])
def test_replacement_initial_and_virtual_previous_match_sync(
    memory_conn: sqlite3.Connection,
    sqlite_repository: SQLiteRepository,
    initial: str | None,
    order: tuple[str, str],
) -> None:
    writer, sync = build_services(memory_conn, sqlite_repository)
    create_plan(sqlite_repository, "old", _at(3))
    writer.save_manual_reading(_manual("old", "100", 3), staff_id=STAFF_ID)
    _replace_meter(memory_conn, sqlite_repository, writer, initial=initial)
    create_plan(sqlite_repository, "early", _at(8), meter_id="new-meter")
    create_plan(sqlite_repository, "late", _at(14), meter_id="new-meter")
    entries = {"early": _submission("early", "8", 8), "late": _submission("late", "12", 14)}
    result = _result(*(entries[slot] for slot in order))
    preview = MobileReadingImportService(sqlite_repository).preview(result)
    rows = {row.input.planned_reading_id: row for row in preview.rows}
    early, late = rows["early"], rows["late"]
    assert early.final is not None and early.final.result is not None
    assert early.final.previous is not None
    assert early.final.previous.source is (
        PreviousReadingSource.REPLACEMENT_INITIAL
        if initial is not None
        else PreviousReadingSource.REPLACEMENT_NO_INITIAL
    )
    assert (early.final.result.last_reading, early.final.result.usage_delta) == (
        Decimal("5") if initial is not None else None,
        Decimal("3") if initial is not None else Decimal("0"),
    )
    assert late.final is not None and late.final.result is not None
    assert (late.final.result.last_reading, late.final.result.usage_delta) == (
        Decimal("8"),
        Decimal("4"),
    )
    _sync(sync, result)
    for row in preview.rows:
        _stored_equals_projection(sqlite_repository, row)


def test_boundary_failure_is_typed_and_does_not_publish_trial(
    memory_conn: sqlite3.Connection,
    sqlite_repository: SQLiteRepository,
) -> None:
    writer, _ = build_services(memory_conn, sqlite_repository)
    create_plan(sqlite_repository, "old", _at(3))
    create_plan(sqlite_repository, "old-target", _at(4))
    writer.save_manual_reading(_manual("old", "100", 3), staff_id=STAFF_ID)
    _replace_meter(memory_conn, sqlite_repository, writer, initial="5")
    create_plan(sqlite_repository, "new-first", _at(8), meter_id="new-meter")
    result = _result(_submission("new-first", "8", 8), _submission("old-target", "130", 4))
    before = tuple(memory_conn.iterdump())
    preview = MobileReadingImportService(sqlite_repository).preview(result)
    rejected = preview.rows[1]
    assert rejected.reason is not None
    assert rejected.reason.code is MobileReadingRejectCode.BOUNDARY_RECALCULATION
    assert rejected.reason.cause is MobileReadingRejectCode.NEGATIVE_USAGE
    assert rejected.reason.replacement_event_id == "replacement"
    assert (rejected.reason.raw_reading, rejected.reason.last_reading) == (
        Decimal("120"),
        Decimal("130"),
    )
    assert preview.effects.accepted_reading_ids == frozenset({"new-first"})
    assert preview.effects.boundary_updates == ()
    assert preview.summary.recalculated_boundaries == 0
    assert tuple(memory_conn.iterdump()) == before


@pytest.mark.parametrize("initial", ["5", None])
def test_nonmeasured_first_reading_recalculates_next_from_replacement(
    memory_conn: sqlite3.Connection,
    sqlite_repository: SQLiteRepository,
    initial: str | None,
) -> None:
    writer, sync = build_services(memory_conn, sqlite_repository)
    _replace_meter(memory_conn, sqlite_repository, writer, initial=initial)
    create_plan(sqlite_repository, "early", _at(8), meter_id="new-meter")
    create_plan(sqlite_repository, "late", _at(14), meter_id="new-meter")
    early = _submission("early", "8", 8)
    _sync(sync, _result(early, _submission("late", "12", 14)))
    skipped = _submission(
        "early",
        None,
        8,
        status=ResultStatus.SKIPPED,
        updated_at=early.client_updated_at + timedelta(seconds=1),
    )
    result = _result(skipped)
    preview = MobileReadingImportService(sqlite_repository).preview(result)
    assert preview.rows[0].disposition is MobileReadingDisposition.OVERWRITTEN
    assert preview.effects.recalculated_existing_ids == frozenset({"late"})
    projected = dict(preview.effects.results)["late"]
    assert (projected.last_reading, projected.usage_delta) == (
        Decimal("5") if initial is not None else None,
        Decimal("7") if initial is not None else Decimal("0"),
    )
    _sync(sync, result)
    _stored_equals_projection(sqlite_repository, preview.rows[0])
    stored = sqlite_repository.get_mobile_reading_import_targets(["late"])["late"].existing_result
    assert stored is not None
    assert (stored.last_reading, stored.usage_delta) == (
        projected.last_reading,
        projected.usage_delta,
    )


def test_repeated_boundary_recalculation_is_counted_once_and_matches_sync(
    memory_conn: sqlite3.Connection,
    sqlite_repository: SQLiteRepository,
) -> None:
    writer, sync = build_services(memory_conn, sqlite_repository)
    for slot, hour in (("old", 3), ("first", 4), ("second", 5)):
        create_plan(sqlite_repository, slot, _at(hour))
    writer.save_manual_reading(_manual("old", "100", 3), staff_id=STAFF_ID)
    _replace_meter(memory_conn, sqlite_repository, writer, initial="5")
    create_plan(sqlite_repository, "new", _at(8), meter_id="new-meter")
    _sync(sync, _result(_submission("new", "8", 8)))
    result = _result(_submission("first", "105", 4), _submission("second", "110", 5))
    preview = MobileReadingImportService(sqlite_repository).preview(result)
    assert preview.summary.recalculated_boundaries == 1
    assert preview.summary.recalculated_existing == 1
    assert preview.effects.recalculated_existing_ids == frozenset({"new"})
    boundary = preview.effects.boundary_updates[0]
    assert (boundary.previous_measured_reading, boundary.old_meter_usage_delta) == (
        Decimal("110"),
        Decimal("10"),
    )
    _sync(sync, result)
    stored = sqlite_repository.list_replacement_boundaries_for_event("replacement")[0]
    assert (
        stored.previous_measured_reading,
        stored.previous_measured_planned_reading_id,
        stored.old_meter_usage_delta,
    ) == (
        boundary.previous_measured_reading,
        boundary.previous_measured_planned_reading_id,
        boundary.old_meter_usage_delta,
    )
    for row in preview.rows:
        _stored_equals_projection(sqlite_repository, row)


def test_unique_recalculation_count_includes_unchanged_existing_value_once(
    memory_conn: sqlite3.Connection,
    sqlite_repository: SQLiteRepository,
) -> None:
    writer, sync = build_services(memory_conn, sqlite_repository)
    for slot, hour in (("previous", 3), ("first", 8), ("second", 9), ("late", 14)):
        create_plan(sqlite_repository, slot, _at(hour))
    writer.save_manual_reading(_manual("previous", "100", 3), staff_id=STAFF_ID)
    writer.save_manual_reading(_manual("late", "120", 14), staff_id=STAFF_ID)
    result = _result(_submission("first", "100", 8), _submission("second", "100", 9))
    preview = MobileReadingImportService(sqlite_repository).preview(result)
    assert preview.effects.recalculated_existing_ids == frozenset({"late"})
    assert preview.summary.recalculated_existing == 1
    projected = dict(preview.effects.results)["late"]
    assert (projected.last_reading, projected.usage_delta) == (Decimal("100"), Decimal("20"))
    _sync(sync, result)
    actual = sqlite_repository.get_mobile_reading_import_targets(["late"])["late"].existing_result
    assert actual is not None
    assert (actual.last_reading, actual.usage_delta) == (
        projected.last_reading,
        projected.usage_delta,
    )


def test_structured_input_snapshot_is_deeply_independent_and_reconstructable(
    memory_conn: sqlite3.Connection,
    sqlite_repository: SQLiteRepository,
) -> None:
    build_services(memory_conn, sqlite_repository)
    _structured_definition(sqlite_repository)
    create_plan(sqlite_repository, "target", _at(8), measurement_definition_id="structured")
    entry = _submission(
        "target",
        None,
        8,
        structured={"percent": "95", "phase": "lag", "nested": {"values": [1, {"x": "old"}]}},
    )
    result = _result(entry, business_date=date(2026, 2, 4))
    service = MobileReadingImportService(sqlite_repository)
    preview = service.preview(result)
    frozen = preview.input
    assert preview.rows[0].reason is not None
    assert preview.rows[0].reason.code is MobileReadingRejectCode.INVALID_INPUT
    assert entry.structured_payload is not None
    entry.structured_payload["nested"]["values"][1]["x"] = "new"
    restored = frozen.to_result()
    assert restored.entries[0].structured_payload is not None
    assert restored.entries[0].structured_payload["nested"]["values"][1]["x"] == "old"
    restored.entries[0].structured_payload["nested"]["values"][1]["x"] = "copy"
    restored_again = frozen.to_result().entries[0].structured_payload
    assert restored_again is not None
    assert restored_again["nested"]["values"][1]["x"] == "old"
    assert service.preview(result).input != frozen
    with pytest.raises(FrozenInstanceError):
        frozen.entries[0].__setattr__("note", "変更")


def test_structured_input_is_normalized_and_matches_saved_result(
    memory_conn: sqlite3.Connection,
    sqlite_repository: SQLiteRepository,
) -> None:
    _, sync = build_services(memory_conn, sqlite_repository)
    _structured_definition(sqlite_repository)
    create_plan(sqlite_repository, "target", _at(8), measurement_definition_id="structured")
    result = _result(
        _submission("target", None, 8, structured={"percent": "95.50", "phase": "lag"})
    )
    preview = MobileReadingImportService(sqlite_repository).preview(result)
    row = preview.rows[0]
    assert row.disposition is MobileReadingDisposition.NEW
    assert row.final is not None and row.final.result is not None
    assert row.final.result.structured_payload_json == '{"percent":"95.50","phase":"lag"}'
    assert row.final.result.usage_delta is None
    assert row.final.rollover_applied is None and row.final.warning_threshold_exceeded is None
    assert row.input_calculation is not None
    assert row.input_calculation.rollover_applied is None
    _sync(sync, result)
    _stored_equals_projection(sqlite_repository, row)


def test_snapshot_detects_related_values_without_using_only_arrival_sequence(
    memory_conn: sqlite3.Connection,
    sqlite_repository: SQLiteRepository,
) -> None:
    writer, _ = build_services(memory_conn, sqlite_repository)
    for slot, hour in (("previous", 3), ("target", 8), ("following", 14)):
        create_plan(sqlite_repository, slot, _at(hour))
    writer.save_manual_reading(_manual("previous", "100", 3), staff_id=STAFF_ID)
    writer.save_manual_reading(_manual("following", "120", 14), staff_id=STAFF_ID)
    result = _result(_submission("target", "110", 8))
    service = MobileReadingImportService(sqlite_repository)
    baseline = service.preview(result)
    assert service.preview(result) == baseline
    memory_conn.execute(
        "UPDATE reading_transactions SET last_reading = ? WHERE planned_reading_id = ?",
        ("99", "following"),
    )
    changed = service.preview(result)
    assert changed.database != baseline.database
    assert changed.effects == baseline.effects
    memory_conn.execute(
        "UPDATE meter_assignments SET multiplier = ? WHERE measurement_definition_id = ?",
        ("2", DEFINITION_ID),
    )
    assert service.preview(result).database != changed.database


def test_snapshot_ignores_changes_to_unrelated_measurement_definition(
    memory_conn: sqlite3.Connection,
    sqlite_repository: SQLiteRepository,
) -> None:
    build_services(memory_conn, sqlite_repository)
    _structured_definition(sqlite_repository)
    create_plan(sqlite_repository, "target", _at(8))
    create_plan(sqlite_repository, "other", _at(14), measurement_definition_id="structured")
    service = MobileReadingImportService(sqlite_repository)
    result = _result(_submission("target", "1", 8))
    baseline = service.preview(result)
    memory_conn.execute(
        "UPDATE measurement_definitions SET display_name = ? WHERE measurement_definition_id = ?",
        ("別の名称", "structured"),
    )
    memory_conn.execute(
        "UPDATE meter_assignments SET multiplier = ? WHERE measurement_definition_id = ?",
        ("9", "structured"),
    )
    assert service.preview(result) == baseline


def test_snapshot_tracks_relevant_boundary_fields_but_not_unrelated_event_fields(
    memory_conn: sqlite3.Connection,
    sqlite_repository: SQLiteRepository,
) -> None:
    writer, _ = build_services(memory_conn, sqlite_repository)
    _structured_definition(sqlite_repository)
    create_plan(sqlite_repository, "old", _at(3))
    create_plan(sqlite_repository, "old-target", _at(4))
    writer.save_manual_reading(_manual("old", "100", 3), staff_id=STAFF_ID)
    _replace_meter(memory_conn, sqlite_repository, writer, initial="5", include_structured=True)
    create_plan(
        sqlite_repository,
        "structured-target",
        _at(8),
        meter_id="new-meter",
        measurement_definition_id="structured",
    )
    result = _result(
        _submission("old-target", "110", 4),
        _submission("structured-target", None, 8, structured={"percent": "95", "phase": "lag"}),
    )
    service = MobileReadingImportService(sqlite_repository)
    baseline = service.preview(result)
    assert baseline.summary.accepted == 2 and baseline.summary.recalculated_boundaries == 1
    memory_conn.execute(
        "UPDATE replacement_boundaries SET old_meter_usage_delta = ? "
        "WHERE replacement_event_id = ? AND measurement_definition_id = ?",
        ("7", "replacement", "structured"),
    )
    assert service.preview(result) == baseline
    memory_conn.execute(
        "UPDATE replacement_boundaries SET old_final_reading = ? "
        "WHERE replacement_event_id = ? AND measurement_definition_id = ?",
        ("125", "replacement", DEFINITION_ID),
    )
    assert service.preview(result).database != baseline.database


def test_snapshot_ignores_current_assignment_when_only_new_assignment_is_used(
    memory_conn: sqlite3.Connection,
    sqlite_repository: SQLiteRepository,
) -> None:
    writer, _ = build_services(memory_conn, sqlite_repository)
    _replace_meter(memory_conn, sqlite_repository, writer, initial="5")
    create_plan(sqlite_repository, "new", _at(8), meter_id="new-meter")
    service = MobileReadingImportService(sqlite_repository)
    result = _result(_submission("new", "8", 8))
    baseline = service.preview(result)
    memory_conn.execute(
        "UPDATE meter_assignments SET multiplier = ? WHERE measurement_definition_id = ? "
        "AND meter_id = ?",
        ("9", DEFINITION_ID, METER_ID),
    )
    assert service.preview(result) == baseline
    memory_conn.execute(
        "UPDATE replacement_boundaries SET new_initial_reading = ? "
        "WHERE replacement_event_id = ? AND measurement_definition_id = ?",
        ("6", "replacement", DEFINITION_ID),
    )
    assert service.preview(result).database != baseline.database


def test_snapshot_detects_new_following_measured_row(
    memory_conn: sqlite3.Connection,
    sqlite_repository: SQLiteRepository,
) -> None:
    writer, _ = build_services(memory_conn, sqlite_repository)
    for slot, hour in (("previous", 3), ("target", 8), ("following", 14)):
        create_plan(sqlite_repository, slot, _at(hour))
    writer.save_manual_reading(_manual("previous", "100", 3), staff_id=STAFF_ID)
    service = MobileReadingImportService(sqlite_repository)
    result = _result(_submission("target", "110", 8))
    baseline = service.preview(result)
    writer.save_manual_reading(_manual("following", "120", 14), staff_id=STAFF_ID)
    changed = service.preview(result)
    assert changed.database != baseline.database
    assert changed.summary.recalculated_existing == 1


def test_final_estimate_reports_unavailable_context_for_rejected_existing_result(
    memory_conn: sqlite3.Connection,
    sqlite_repository: SQLiteRepository,
) -> None:
    _, sync = build_services(memory_conn, sqlite_repository)
    create_plan(sqlite_repository, "target", _at(8))
    entry = _submission("target", "1", 8)
    _sync(sync, _result(entry))
    memory_conn.execute(
        "INSERT INTO assignment_review_flags "
        "(measurement_definition_id, meter_id, assignment_effective_from, reason, created_at) "
        "SELECT measurement_definition_id, meter_id, effective_from, '要確認', ? "
        "FROM meter_assignments WHERE measurement_definition_id = ?",
        ("2026-02-04 00:00:00", DEFINITION_ID),
    )
    row = MobileReadingImportService(sqlite_repository).preview(_result(entry)).rows[0]
    assert row.reason is not None
    assert row.reason.code is MobileReadingRejectCode.ASSIGNMENT_NEEDS_REVIEW
    assert row.final is not None and row.final.result is not None
    assert row.final.previous is None and row.final.calculation_error is not None
    assert row.final.calculation_error.code is MobileReadingRejectCode.ASSIGNMENT_NEEDS_REVIEW


def test_snapshot_detects_input_header_and_line_changes(
    memory_conn: sqlite3.Connection,
    sqlite_repository: SQLiteRepository,
) -> None:
    build_services(memory_conn, sqlite_repository)
    create_plan(sqlite_repository, "target", _at(8))
    entry = _submission("target", "1.00", 8, note="元")
    result = _result(entry, business_date=date(2026, 2, 4))
    service = MobileReadingImportService(sqlite_repository)
    baseline = service.preview(result)
    assert service.preview(baseline.input.to_result()) == baseline
    entry.note = "変更"
    assert service.preview(result).input != baseline.input
    entry.note = "元"
    entry.client_updated_at += timedelta(seconds=1)
    assert service.preview(result).input != baseline.input
    entry.client_updated_at -= timedelta(seconds=1)
    entry.raw_reading = Decimal("2.00")
    assert service.preview(result).input != baseline.input
    entry.raw_reading = Decimal("1.00")
    assert service.preview(replace(result, sheet_id="b" * 32)).input != baseline.input
    assert service.preview(replace(result, business_date=date(2026, 2, 3))).input != baseline.input
    assert (
        service.preview(replace(result, filter=SheetFilter.create(["14:00"]))).input
        != baseline.input
    )
    entry.note = ""
    ordered_filter = SheetFilter(reading_sessions=("14:00", "08:00"))
    exact = service.preview(replace(result, filter=ordered_filter)).input
    assert exact.entries[0].note == ""
    assert exact.filter is not None and exact.filter.reading_sessions == ("14:00", "08:00")
    assert service.preview(exact.to_result()).input == exact


def test_unexpected_error_is_not_converted_to_row_rejection(
    memory_conn: sqlite3.Connection,
    sqlite_repository: SQLiteRepository,
) -> None:
    build_services(memory_conn, sqlite_repository)
    create_plan(sqlite_repository, "target", _at(8))

    before = tuple(memory_conn.iterdump())
    with (
        patch.object(
            UsageDeltaCalculationService, "calculate", side_effect=RuntimeError("予期しない障害")
        ),
        pytest.raises(RuntimeError, match="予期しない障害"),
    ):
        MobileReadingImportService(sqlite_repository).preview(
            _result(_submission("target", "1", 8))
        )
    assert tuple(memory_conn.iterdump()) == before
