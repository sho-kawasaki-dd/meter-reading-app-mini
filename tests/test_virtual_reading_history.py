from __future__ import annotations

import sqlite3
from dataclasses import replace
from datetime import UTC, date, datetime, timedelta, timezone
from decimal import Decimal

import pytest
from reading_service_helpers import (
    DEFINITION_ID,
    LOCATION_ID,
    METER_ID,
    SHEET_ID,
    STAFF_ID,
    build_services,
    create_plan,
)

from meter_reading_mini.core.contract_models import (
    CalculationMethod,
    Category,
    Purpose,
    ReadingCycle,
    ResultStatus,
    ValueSchema,
)
from meter_reading_mini.core.errors import NegativeUsageDeltaError, ValidationError
from meter_reading_mini.core.meter_reading_sync_repositories import (
    HistoricalMeasuredReadingRecord,
    ManualReadingInput,
    MeasurementDefinitionCreateRequest,
    MeasurementDefinitionRecord,
    MeterMasterCreateRequest,
    ReadingUpsertRequest,
    ReplacementBoundaryRecord,
    ReplacementDefinitionReading,
    ReplacementUpsertRequest,
    ResultSource,
    UpsertDisposition,
)
from meter_reading_mini.core.reading_write_orchestration import ReadingWriteOrchestrationService
from meter_reading_mini.core.usage_delta_calculation import (
    UsageDeltaCalculationResult,
    UsageDeltaCalculationService,
)
from meter_reading_mini.core.virtual_reading_history import (
    VirtualReadingHistory,
    VirtualRecalculationError,
    VirtualRecalculationStage,
)
from meter_reading_mini.db.connection import transaction
from meter_reading_mini.db.repositories import SQLiteRepository


def _at(hour: int) -> datetime:
    return datetime(2026, 2, 4, hour, tzinfo=UTC)


def _request(
    slot: str,
    raw: str | None,
    hour: int,
    *,
    status: ResultStatus = ResultStatus.MEASURED,
) -> ReadingUpsertRequest:
    return ReadingUpsertRequest(
        planned_reading_id=slot,
        measurement_definition_id=DEFINITION_ID,
        result_status=status,
        recorded_at=_at(hour),
        client_updated_at=datetime(2026, 2, 6, tzinfo=UTC) + timedelta(hours=hour),
        raw_reading=Decimal(raw) if raw is not None else None,
        structured_payload_json=None,
        updated_by="試験担当",
        signature=f"署名-{slot}",
        note=None,
        device_id=SHEET_ID,
        result_source=ResultSource.MOBILE_HTML,
    )


def _manual(slot: str, raw: str, hour: int) -> ManualReadingInput:
    return ManualReadingInput(slot, ResultStatus.MEASURED, _at(hour), Decimal(raw), None)


def _create_meter(repository: SQLiteRepository, meter_id: str) -> None:
    repository.create_meter_master(
        MeterMasterCreateRequest(
            meter_id=meter_id,
            location_id=LOCATION_ID,
            location_name="試験場所",
            meter_name="交換用メーター",
            category=Category.WATER,
        )
    )


def _replace_meter(
    conn: sqlite3.Connection,
    repository: SQLiteRepository,
    writer: ReadingWriteOrchestrationService,
    *,
    event_id: str,
    old_meter: str,
    new_meter: str,
    hour: int,
    final: str,
    initial: str | None,
) -> None:
    request = ReplacementUpsertRequest(
        replacement_event_id=event_id,
        location_id=LOCATION_ID,
        old_meter_id=old_meter,
        new_meter_id=new_meter,
        replaced_at=_at(hour),
        client_updated_at=_at(hour),
        old_final_reading=None,
        new_initial_reading=None,
        updated_by="試験担当",
        note=None,
        definition_readings=(
            ReplacementDefinitionReading(
                DEFINITION_ID, Decimal(final), Decimal(initial) if initial is not None else None
            ),
        ),
    )
    with transaction(conn):
        repository.upsert_replacement(request)
        repository.reflect_replacement(request)
        writer.recalculate_after_replacement(request)


def _create_other_definition(
    repository: SQLiteRepository,
    *,
    structured: bool = False,
) -> None:
    repository.create_measurement_definition(
        MeasurementDefinitionCreateRequest(
            measurement_definition_id="other-definition",
            location_id=LOCATION_ID,
            display_name="別の測定対象",
            category=Category.WATER,
            unit="m3",
            value_schema=(
                ValueSchema.POWER_FACTOR_PERCENT_PHASE if structured else ValueSchema.DECIMAL_STRING
            ),
            multiplier=Decimal("1"),
            decimal_places=None if structured else 2,
            warning_threshold=None,
            rollover_value=None,
            calculation_method=CalculationMethod.RAW_ONLY
            if structured
            else CalculationMethod.DELTA,
            reading_cycle=ReadingCycle.DAILY_1X,
            purpose=Purpose.OPERATIONAL,
            generation_start_date=date(2026, 2, 1),
            initial_schedule_json=None,
            initial_meter_id=METER_ID,
        )
    )


def test_virtual_additions_recalculate_in_schedule_order_and_match_saved_results(
    memory_conn: sqlite3.Connection,
    sqlite_repository: SQLiteRepository,
) -> None:
    writer, _ = build_services(memory_conn, sqlite_repository)
    for slot, hour in (("previous", 3), ("early", 8), ("late", 14)):
        create_plan(sqlite_repository, slot, _at(hour))
    writer.save_manual_reading(_manual("previous", "100", 3), staff_id=STAFF_ID)
    history = VirtualReadingHistory(sqlite_repository)
    late = _request("late", "130", 14)
    early = _request("early", "110", 8)
    before = tuple(memory_conn.iterdump())
    changes = memory_conn.total_changes

    assert history.apply(late).accepted
    late_first = history.get_result("late")
    assert late_first is not None and (late_first.last_reading, late_first.usage_delta) == (
        Decimal("100"),
        Decimal("30"),
    )
    assert history.apply(early).accepted

    early_result = history.get_result("early")
    late_result = history.get_result("late")
    assert early_result is not None and (early_result.last_reading, early_result.usage_delta) == (
        Decimal("100"),
        Decimal("10"),
    )
    assert late_result is not None and (late_result.last_reading, late_result.usage_delta) == (
        Decimal("110"),
        Decimal("20"),
    )
    assert history.snapshot().recalculated_reading_ids == frozenset({"late"})
    assert tuple(memory_conn.iterdump()) == before
    assert memory_conn.total_changes == changes

    with transaction(memory_conn):
        assert writer.upsert_reading(late).accepted
        assert writer.upsert_reading(early).accepted
    for slot in ("early", "late"):
        assert (
            history.get_result(slot)
            == sqlite_repository.get_mobile_reading_import_targets([slot])[slot].existing_result
        )


def test_rejected_virtual_change_does_not_affect_later_inputs(
    memory_conn: sqlite3.Connection,
    sqlite_repository: SQLiteRepository,
) -> None:
    writer, _ = build_services(memory_conn, sqlite_repository)
    for slot, hour in (("previous", 3), ("early", 8), ("late", 14)):
        create_plan(sqlite_repository, slot, _at(hour))
    writer.save_manual_reading(_manual("previous", "100", 3), staff_id=STAFF_ID)
    history = VirtualReadingHistory(sqlite_repository)
    late = _request("late", "110", 14)
    rejected = _request("early", "120", 8)
    assert history.apply(late).accepted
    before = history.snapshot()
    database_before = tuple(memory_conn.iterdump())

    calculated = history.calculate_reading(rejected)
    assert (calculated.request.last_reading, calculated.request.usage_delta) == (
        Decimal("100"),
        Decimal("20"),
    )
    with pytest.raises(VirtualRecalculationError) as failure:
        history.apply(rejected)
    assert failure.value.stage is VirtualRecalculationStage.READING
    assert isinstance(failure.value.original_error, NegativeUsageDeltaError)
    assert history.snapshot() == before
    assert tuple(memory_conn.iterdump()) == database_before

    accepted = _request("early", "105", 8)
    assert history.apply(accepted).accepted
    late_result = history.get_result("late")
    assert late_result is not None and (late_result.last_reading, late_result.usage_delta) == (
        Decimal("105"),
        Decimal("5"),
    )
    with transaction(memory_conn):
        assert writer.upsert_reading(late).accepted
    with pytest.raises(NegativeUsageDeltaError) as saved_failure, transaction(memory_conn):
        writer.upsert_reading(rejected)
    assert str(failure.value.original_error) == str(saved_failure.value)
    with transaction(memory_conn):
        assert writer.upsert_reading(accepted).accepted
    assert (
        history.get_result("late")
        == sqlite_repository.get_mobile_reading_import_targets(["late"])["late"].existing_result
    )


@pytest.mark.parametrize("initial", ["5", None])
@pytest.mark.parametrize("order", [("early", "late"), ("late", "early")])
def test_virtual_additions_use_replacement_initial_and_then_virtual_previous(
    memory_conn: sqlite3.Connection,
    sqlite_repository: SQLiteRepository,
    initial: str | None,
    order: tuple[str, str],
) -> None:
    writer, _ = build_services(memory_conn, sqlite_repository)
    _create_meter(sqlite_repository, "new-meter")
    create_plan(sqlite_repository, "old", _at(3))
    writer.save_manual_reading(_manual("old", "100", 3), staff_id=STAFF_ID)
    _replace_meter(
        memory_conn,
        sqlite_repository,
        writer,
        event_id="replacement",
        old_meter=METER_ID,
        new_meter="new-meter",
        hour=6,
        final="120",
        initial=initial,
    )
    create_plan(sqlite_repository, "early", _at(8), meter_id="new-meter")
    create_plan(sqlite_repository, "late", _at(14), meter_id="new-meter")
    requests = {"early": _request("early", "8", 8), "late": _request("late", "12", 14)}
    history = VirtualReadingHistory(sqlite_repository)
    before = tuple(memory_conn.iterdump())

    for slot in order:
        assert history.apply(requests[slot]).accepted

    early = history.get_result("early")
    late = history.get_result("late")
    assert early is not None and (early.last_reading, early.usage_delta) == (
        Decimal("5") if initial is not None else None,
        Decimal("3") if initial is not None else Decimal("0"),
    )
    assert late is not None and (late.last_reading, late.usage_delta) == (
        Decimal("8"),
        Decimal("4"),
    )
    early_context = history.get_calculation_context("early")
    late_context = history.get_calculation_context("late")
    assert early_context.previous is None and early_context.boundary is not None
    assert early_context.boundary.new_initial_reading == (
        Decimal(initial) if initial is not None else None
    )
    assert late_context.boundary is None and late_context.previous is not None
    assert late_context.previous.planned_reading_id == "early"
    assert tuple(memory_conn.iterdump()) == before

    with transaction(memory_conn):
        for slot in order:
            assert writer.upsert_reading(requests[slot]).accepted
    for slot in order:
        assert (
            history.get_result(slot)
            == sqlite_repository.get_mobile_reading_import_targets([slot])[slot].existing_result
        )


@pytest.mark.parametrize("initial", ["5", None])
def test_virtual_nonmeasured_first_reading_restores_replacement_boundary(
    memory_conn: sqlite3.Connection,
    sqlite_repository: SQLiteRepository,
    initial: str | None,
) -> None:
    writer, _ = build_services(memory_conn, sqlite_repository)
    _create_meter(sqlite_repository, "new-meter")
    _replace_meter(
        memory_conn,
        sqlite_repository,
        writer,
        event_id="replacement",
        old_meter=METER_ID,
        new_meter="new-meter",
        hour=6,
        final="120",
        initial=initial,
    )
    create_plan(sqlite_repository, "early", _at(8), meter_id="new-meter")
    create_plan(sqlite_repository, "late", _at(14), meter_id="new-meter")
    with transaction(memory_conn):
        writer.upsert_reading(_request("early", "8", 8))
        writer.upsert_reading(_request("late", "12", 14))
    before = tuple(memory_conn.iterdump())
    history = VirtualReadingHistory(sqlite_repository)
    skipped = _request("early", None, 8, status=ResultStatus.SKIPPED)
    skipped = replace(skipped, client_updated_at=skipped.client_updated_at + timedelta(seconds=1))

    assert history.apply(skipped).accepted

    late = history.get_result("late")
    assert late is not None and (late.last_reading, late.usage_delta) == (
        Decimal("5") if initial is not None else None,
        Decimal("7") if initial is not None else Decimal("0"),
    )
    context = history.get_calculation_context("late")
    assert context.previous is None and context.boundary is not None
    assert tuple(memory_conn.iterdump()) == before
    with transaction(memory_conn):
        assert writer.upsert_reading(skipped).accepted
    assert (
        history.get_result("late")
        == sqlite_repository.get_mobile_reading_import_targets(["late"])["late"].existing_result
    )


def test_virtual_equal_time_readings_follow_id_order_at_replacement_boundary(
    memory_conn: sqlite3.Connection,
    sqlite_repository: SQLiteRepository,
) -> None:
    writer, _ = build_services(memory_conn, sqlite_repository)
    _create_meter(sqlite_repository, "new-meter")
    _replace_meter(
        memory_conn,
        sqlite_repository,
        writer,
        event_id="replacement",
        old_meter=METER_ID,
        new_meter="new-meter",
        hour=6,
        final="120",
        initial="5",
    )
    for slot in ("a-first", "m-middle", "z-last"):
        create_plan(sqlite_repository, slot, _at(6), meter_id="new-meter")
    requests = (
        _request("z-last", "12", 6),
        _request("a-first", "8", 6),
        _request("m-middle", "10", 6),
    )
    history = VirtualReadingHistory(sqlite_repository)
    for request in requests:
        assert history.apply(request).accepted

    for slot, previous, usage in (
        ("a-first", "5", "3"),
        ("m-middle", "8", "2"),
        ("z-last", "10", "2"),
    ):
        result = history.get_result(slot)
        assert result is not None and (result.last_reading, result.usage_delta) == (
            Decimal(previous),
            Decimal(usage),
        )
    assert history.get_calculation_context("a-first").boundary is not None
    assert history.get_calculation_context("m-middle").previous is not None

    with transaction(memory_conn):
        for request in requests:
            assert writer.upsert_reading(request).accepted
    for request in requests:
        slot = request.planned_reading_id
        assert (
            history.get_result(slot)
            == sqlite_repository.get_mobile_reading_import_targets([slot])[slot].existing_result
        )


def test_virtual_skips_multiple_existing_readings_and_keeps_definitions_separate(
    memory_conn: sqlite3.Connection,
    sqlite_repository: SQLiteRepository,
) -> None:
    writer, _ = build_services(memory_conn, sqlite_repository)
    _create_other_definition(sqlite_repository)
    for slot, hour, raw in (("previous", 3, "100"), ("first", 8, "110"), ("second", 9, "120")):
        create_plan(sqlite_repository, slot, _at(hour))
        writer.save_manual_reading(_manual(slot, raw, hour), staff_id=STAFF_ID)
    create_plan(sqlite_repository, "last", _at(10))
    create_plan(
        sqlite_repository,
        "other",
        _at(9),
        measurement_definition_id="other-definition",
    )
    other = replace(_request("other", "900", 9), measurement_definition_id="other-definition")
    history = VirtualReadingHistory(sqlite_repository)
    requests = (
        other,
        _request("second", None, 9, status=ResultStatus.SKIPPED),
        _request("first", None, 8, status=ResultStatus.SKIPPED),
        _request("last", "130", 10),
        replace(_request("first", "115", 8), client_updated_at=datetime(2026, 2, 7, tzinfo=UTC)),
    )
    for request in requests:
        assert history.apply(request).accepted

    last = history.get_result("last")
    assert last is not None and (last.last_reading, last.usage_delta) == (
        Decimal("115"),
        Decimal("15"),
    )
    other_result = history.get_result("other")
    assert other_result is not None and other_result.usage_delta == Decimal("0")
    previous = history.get_calculation_context("last").previous
    assert previous is not None and previous.planned_reading_id == "first"

    with transaction(memory_conn):
        for request in requests:
            assert writer.upsert_reading(request).accepted
    for slot in ("first", "second", "last", "other"):
        assert (
            history.get_result(slot)
            == sqlite_repository.get_mobile_reading_import_targets([slot])[slot].existing_result
        )


def test_virtual_boundary_failure_rolls_back_trial_and_keeps_prior_accepted_rows(
    memory_conn: sqlite3.Connection,
    sqlite_repository: SQLiteRepository,
) -> None:
    writer, _ = build_services(memory_conn, sqlite_repository)
    _create_meter(sqlite_repository, "new-meter")
    create_plan(sqlite_repository, "previous", _at(3))
    create_plan(sqlite_repository, "old-target", _at(4))
    writer.save_manual_reading(_manual("previous", "100", 3), staff_id=STAFF_ID)
    _replace_meter(
        memory_conn,
        sqlite_repository,
        writer,
        event_id="replacement",
        old_meter=METER_ID,
        new_meter="new-meter",
        hour=6,
        final="120",
        initial="5",
    )
    create_plan(sqlite_repository, "new-first", _at(8), meter_id="new-meter")
    new_first = _request("new-first", "8", 8)
    rejected = _request("old-target", "130", 4)
    history = VirtualReadingHistory(sqlite_repository)
    assert history.apply(new_first).accepted
    before = history.snapshot()
    database_before = tuple(memory_conn.iterdump())

    with pytest.raises(VirtualRecalculationError) as failure:
        history.apply(rejected)
    assert failure.value.stage is VirtualRecalculationStage.BOUNDARY
    assert failure.value.replacement_event_id == "replacement"
    assert isinstance(failure.value.original_error, NegativeUsageDeltaError)
    assert history.snapshot() == before
    assert tuple(memory_conn.iterdump()) == database_before

    accepted = _request("old-target", "110", 4)
    assert history.apply(accepted).accepted
    snapshot = history.snapshot()
    assert snapshot.recalculated_reading_ids == frozenset({"new-first"})
    assert len(snapshot.boundary_updates) == 1
    boundary = snapshot.boundary_updates[0]
    assert (boundary.previous_measured_reading, boundary.old_meter_usage_delta) == (
        Decimal("110"),
        Decimal("10"),
    )
    with transaction(memory_conn):
        writer.upsert_reading(new_first)
    with pytest.raises(NegativeUsageDeltaError) as saved_failure, transaction(memory_conn):
        writer.upsert_reading(rejected)
    assert str(failure.value.original_error) == str(saved_failure.value)
    with transaction(memory_conn):
        writer.upsert_reading(accepted)
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


def test_virtual_meter_reuse_preserves_boundary_before_following_error_order(
    memory_conn: sqlite3.Connection,
    sqlite_repository: SQLiteRepository,
) -> None:
    writer, _ = build_services(memory_conn, sqlite_repository)
    _create_meter(sqlite_repository, "new-meter")
    create_plan(sqlite_repository, "old", _at(3))
    writer.save_manual_reading(_manual("old", "100", 3), staff_id=STAFF_ID)
    _replace_meter(
        memory_conn,
        sqlite_repository,
        writer,
        event_id="first",
        old_meter=METER_ID,
        new_meter="new-meter",
        hour=5,
        final="120",
        initial="5",
    )
    create_plan(sqlite_repository, "new", _at(6), meter_id="new-meter")
    writer.save_manual_reading(_manual("new", "8", 6), staff_id=STAFF_ID)
    _replace_meter(
        memory_conn,
        sqlite_repository,
        writer,
        event_id="second",
        old_meter="new-meter",
        new_meter=METER_ID,
        hour=7,
        final="12",
        initial="10",
    )
    create_plan(sqlite_repository, "target", _at(8))
    create_plan(sqlite_repository, "following", _at(9))
    sqlite_repository.upsert_reading(_request("following", "9", 9))
    memory_conn.execute(
        "UPDATE replacement_boundaries SET old_final_reading = ? WHERE replacement_event_id = ?",
        ("90", "first"),
    )
    request = _request("target", "11", 8)

    for expected_stage, expected_message in (
        (VirtualRecalculationStage.BOUNDARY, "前回値 100 より小さい今回値 90"),
        (VirtualRecalculationStage.READING, "前回値 11 より小さい今回値 9"),
    ):
        history = VirtualReadingHistory(sqlite_repository)
        before = history.snapshot()
        database_before = tuple(memory_conn.iterdump())
        with pytest.raises(VirtualRecalculationError) as failure:
            history.apply(request)
        assert failure.value.stage is expected_stage
        assert expected_message in str(failure.value.original_error)
        assert history.snapshot() == before
        assert tuple(memory_conn.iterdump()) == database_before
        with pytest.raises(NegativeUsageDeltaError) as saved_failure, transaction(memory_conn):
            writer.upsert_reading(request)
        assert str(failure.value.original_error) == str(saved_failure.value)
        memory_conn.execute(
            "UPDATE replacement_boundaries SET old_final_reading = ? "
            "WHERE replacement_event_id = ?",
            ("120", "first"),
        )
    memory_conn.execute(
        "UPDATE reading_transactions SET raw_reading = ? WHERE planned_reading_id = ?",
        ("12", "following"),
    )
    history = VirtualReadingHistory(sqlite_repository)
    assert history.apply(request).accepted
    snapshot = history.snapshot()
    assert {update.replacement_event_id for update in snapshot.boundary_updates} == {
        "first",
        "second",
    }
    assert snapshot.recalculated_reading_ids == frozenset({"new", "target", "following"})
    with transaction(memory_conn):
        assert writer.upsert_reading(request).accepted
    for slot in ("new", "target", "following"):
        assert (
            history.get_result(slot)
            == sqlite_repository.get_mobile_reading_import_targets([slot])[slot].existing_result
        )
    for update in snapshot.boundary_updates:
        stored = sqlite_repository.list_replacement_boundaries_for_event(
            update.replacement_event_id
        )[0]
        assert (
            stored.previous_measured_reading,
            stored.previous_measured_planned_reading_id,
            stored.old_meter_usage_delta,
        ) == (
            update.previous_measured_reading,
            update.previous_measured_planned_reading_id,
            update.old_meter_usage_delta,
        )


def test_virtual_downstream_failure_discards_boundary_and_earlier_recalculations(
    memory_conn: sqlite3.Connection,
    sqlite_repository: SQLiteRepository,
) -> None:
    writer, _ = build_services(memory_conn, sqlite_repository)
    _create_meter(sqlite_repository, "new-meter")
    create_plan(sqlite_repository, "old", _at(3))
    create_plan(sqlite_repository, "old-target", _at(4))
    writer.save_manual_reading(_manual("old", "100", 3), staff_id=STAFF_ID)
    _replace_meter(
        memory_conn,
        sqlite_repository,
        writer,
        event_id="replacement",
        old_meter=METER_ID,
        new_meter="new-meter",
        hour=6,
        final="120",
        initial="5",
    )
    create_plan(sqlite_repository, "new-first", _at(8), meter_id="new-meter")
    create_plan(sqlite_repository, "new-late", _at(14), meter_id="new-meter")
    create_plan(sqlite_repository, "other", _at(15), meter_id="new-meter")
    with transaction(memory_conn):
        writer.upsert_reading(_request("new-first", "8", 8))
        writer.upsert_reading(_request("new-late", "12", 14))
    memory_conn.execute(
        "UPDATE reading_transactions SET raw_reading = ? WHERE planned_reading_id = ?",
        ("6", "new-late"),
    )
    history = VirtualReadingHistory(sqlite_repository)
    other = _request("other", "20", 15)
    assert history.apply(other).accepted
    before = history.snapshot()
    database_before = tuple(memory_conn.iterdump())
    target = _request("old-target", "110", 4)

    with pytest.raises(VirtualRecalculationError) as failure:
        history.apply(target)
    assert failure.value.stage is VirtualRecalculationStage.READING
    assert isinstance(failure.value.original_error, NegativeUsageDeltaError)
    assert history.snapshot() == before
    assert tuple(memory_conn.iterdump()) == database_before
    with pytest.raises(NegativeUsageDeltaError) as saved_failure, transaction(memory_conn):
        writer.upsert_reading(target)
    assert str(failure.value.original_error) == str(saved_failure.value)

    memory_conn.execute(
        "UPDATE reading_transactions SET raw_reading = ? WHERE planned_reading_id = ?",
        ("12", "new-late"),
    )
    assert history.apply(target).accepted
    assert history.snapshot().recalculated_reading_ids == frozenset(
        {"new-first", "new-late", "other"}
    )
    assert len(history.snapshot().boundary_updates) == 1
    with transaction(memory_conn):
        writer.upsert_reading(other)
        writer.upsert_reading(target)
    for slot in ("other", "old-target", "new-first", "new-late"):
        assert (
            history.get_result(slot)
            == sqlite_repository.get_mobile_reading_import_targets([slot])[slot].existing_result
        )


def test_virtual_recalculation_ids_are_unique_even_when_values_do_not_change(
    memory_conn: sqlite3.Connection,
    sqlite_repository: SQLiteRepository,
) -> None:
    writer, _ = build_services(memory_conn, sqlite_repository)
    for slot, hour in (("previous", 3), ("first", 8), ("second", 9), ("late", 14)):
        create_plan(sqlite_repository, slot, _at(hour))
    writer.save_manual_reading(_manual("previous", "100", 3), staff_id=STAFF_ID)
    writer.save_manual_reading(_manual("late", "120", 14), staff_id=STAFF_ID)
    history = VirtualReadingHistory(sqlite_repository)
    first = _request("first", "100", 8)
    second = _request("second", "100", 9)

    assert history.apply(first).accepted
    first_snapshot = history.snapshot()
    assert first_snapshot.recalculated_reading_ids == frozenset({"late"})
    result = history.get_result("late")
    assert result is not None and (result.last_reading, result.usage_delta) == (
        Decimal("100"),
        Decimal("20"),
    )
    assert history.apply(second).accepted
    assert history.snapshot().recalculated_reading_ids == frozenset({"late"})
    with transaction(memory_conn):
        writer.upsert_reading(first)
        writer.upsert_reading(second)
    assert (
        history.get_result("late")
        == sqlite_repository.get_mobile_reading_import_targets(["late"])["late"].existing_result
    )


def test_virtual_uses_replacement_assignment_multiplier_and_rollover(
    memory_conn: sqlite3.Connection,
    sqlite_repository: SQLiteRepository,
) -> None:
    writer, _ = build_services(memory_conn, sqlite_repository)
    _create_meter(sqlite_repository, "new-meter")
    _replace_meter(
        memory_conn,
        sqlite_repository,
        writer,
        event_id="replacement",
        old_meter=METER_ID,
        new_meter="new-meter",
        hour=6,
        final="120",
        initial="5",
    )
    memory_conn.execute(
        "UPDATE meter_assignments SET multiplier = ?, rollover_value = ? WHERE meter_id = ?",
        ("2", "10", "new-meter"),
    )
    create_plan(sqlite_repository, "early", _at(8), meter_id="new-meter")
    create_plan(sqlite_repository, "late", _at(14), meter_id="new-meter")
    early = _request("early", "8", 8)
    late = _request("late", "2", 14)
    history = VirtualReadingHistory(sqlite_repository)
    assert history.apply(late).accepted
    assert history.apply(early).accepted
    early_result = history.get_result("early")
    late_result = history.get_result("late")
    assert early_result is not None and (early_result.last_reading, early_result.usage_delta) == (
        Decimal("5"),
        Decimal("6"),
    )
    assert late_result is not None and (late_result.last_reading, late_result.usage_delta) == (
        Decimal("8"),
        Decimal("8"),
    )
    calculated = history.calculate_reading(late)
    assert (calculated.assignment.multiplier, calculated.assignment.rollover_value) == (
        Decimal("2"),
        Decimal("10"),
    )
    with transaction(memory_conn):
        writer.upsert_reading(late)
        writer.upsert_reading(early)
    assert (
        history.get_result("late")
        == sqlite_repository.get_mobile_reading_import_targets(["late"])["late"].existing_result
    )


def test_virtual_unexpected_failure_does_not_publish_partial_changes(
    memory_conn: sqlite3.Connection,
    sqlite_repository: SQLiteRepository,
) -> None:
    writer, _ = build_services(memory_conn, sqlite_repository)
    for slot, hour in (("previous", 3), ("target", 8), ("following", 14)):
        create_plan(sqlite_repository, slot, _at(hour))
    writer.save_manual_reading(_manual("previous", "100", 3), staff_id=STAFF_ID)
    writer.save_manual_reading(_manual("following", "130", 14), staff_id=STAFF_ID)

    class FailingCalculation(UsageDeltaCalculationService):
        def calculate_measured_reading(
            self,
            measurement_definition: MeasurementDefinitionRecord,
            raw_reading: Decimal,
            previous_measured_reading: HistoricalMeasuredReadingRecord | None,
            replacement_boundary: ReplacementBoundaryRecord | None = None,
        ) -> UsageDeltaCalculationResult:
            if raw_reading == Decimal("130"):
                raise RuntimeError("予期しない計算障害")
            return super().calculate_measured_reading(
                measurement_definition, raw_reading, previous_measured_reading, replacement_boundary
            )

    history = VirtualReadingHistory(sqlite_repository, calculation_service=FailingCalculation())
    before = history.snapshot()
    database_before = tuple(memory_conn.iterdump())
    with pytest.raises(RuntimeError, match="予期しない計算障害"):
        history.apply(_request("target", "110", 8))
    assert history.snapshot() == before
    assert tuple(memory_conn.iterdump()) == database_before


def test_virtual_duplicate_then_earlier_input_keeps_audit_and_recalculates_existing(
    memory_conn: sqlite3.Connection,
    sqlite_repository: SQLiteRepository,
) -> None:
    writer, _ = build_services(memory_conn, sqlite_repository)
    for slot, hour in (("previous", 3), ("early", 8), ("late", 14)):
        create_plan(sqlite_repository, slot, _at(hour))
    writer.save_manual_reading(_manual("previous", "100", 3), staff_id=STAFF_ID)
    stored = _request("late", "120.00", 14)
    with transaction(memory_conn):
        writer.upsert_reading(stored)
    original = sqlite_repository.get_mobile_reading_import_targets(["late"])["late"].existing_result
    assert original is not None
    japan = timezone(timedelta(hours=9))
    duplicate = replace(
        stored,
        raw_reading=Decimal("120.0"),
        recorded_at=stored.recorded_at.astimezone(japan).replace(microsecond=999999),
        client_updated_at=stored.client_updated_at.astimezone(japan).replace(microsecond=999999),
        updated_by="別の担当者",
        signature="異なる署名",
    )
    history = VirtualReadingHistory(sqlite_repository)
    before = history.snapshot()
    assert history.apply(duplicate).disposition is UpsertDisposition.IGNORED_DUPLICATE
    assert history.snapshot() == before
    early = _request("early", "110", 8)
    assert history.apply(early).accepted
    late = history.get_result("late")
    assert late is not None and (late.last_reading, late.usage_delta) == (
        Decimal("110"),
        Decimal("10"),
    )
    assert (late.signature, late.updated_by, late.device_id, late.arrival_sequence) == (
        original.signature,
        original.updated_by,
        original.device_id,
        original.arrival_sequence,
    )
    with transaction(memory_conn):
        assert writer.upsert_reading(duplicate).disposition is UpsertDisposition.IGNORED_DUPLICATE
        assert writer.upsert_reading(early).accepted
    assert (
        history.get_result("late")
        == sqlite_repository.get_mobile_reading_import_targets(["late"])["late"].existing_result
    )


def test_virtual_conflict_order_matches_saved_stale_and_same_time_inputs(
    memory_conn: sqlite3.Connection,
    sqlite_repository: SQLiteRepository,
) -> None:
    writer, _ = build_services(memory_conn, sqlite_repository)
    for slot, hour in (("previous", 3), ("target", 8), ("following", 14)):
        create_plan(sqlite_repository, slot, _at(hour))
    writer.save_manual_reading(_manual("previous", "100", 3), staff_id=STAFF_ID)
    current = _request("target", "110", 8)
    with transaction(memory_conn):
        writer.upsert_reading(current)
        writer.upsert_reading(_request("following", "120", 14))
    history = VirtualReadingHistory(sqlite_repository)
    stale = replace(
        current,
        raw_reading=Decimal("130"),
        client_updated_at=current.client_updated_at - timedelta(seconds=1),
    )
    before = history.snapshot()
    assert history.apply(stale).disposition is UpsertDisposition.IGNORED_STALE
    assert history.snapshot() == before
    with pytest.raises(NegativeUsageDeltaError):
        history.apply(replace(stale, raw_reading=Decimal("90")))
    assert history.snapshot() == before

    same_time = replace(current, raw_reading=Decimal("115"))
    assert history.apply(same_time).disposition is UpsertDisposition.REPLACED_ON_TIE
    following = history.get_result("following")
    assert following is not None and (following.last_reading, following.usage_delta) == (
        Decimal("115"),
        Decimal("5"),
    )
    with transaction(memory_conn):
        assert writer.upsert_reading(stale).disposition is UpsertDisposition.IGNORED_STALE
        assert writer.upsert_reading(same_time).disposition is UpsertDisposition.REPLACED_ON_TIE
    assert (
        history.get_result("following")
        == sqlite_repository.get_mobile_reading_import_targets(["following"])[
            "following"
        ].existing_result
    )


def test_virtual_structured_payload_is_immutable_and_compares_normalized_json(
    memory_conn: sqlite3.Connection,
    sqlite_repository: SQLiteRepository,
) -> None:
    writer, _ = build_services(memory_conn, sqlite_repository)
    _create_other_definition(sqlite_repository, structured=True)
    create_plan(
        sqlite_repository, "structured", _at(8), measurement_definition_id="other-definition"
    )
    payload = {"phase": "lag", "nested": {"a": 1, "b": 2}}
    request = replace(
        _request("structured", None, 8),
        measurement_definition_id="other-definition",
        structured_payload_json=payload,
    )
    history = VirtualReadingHistory(sqlite_repository)
    assert history.apply(request).accepted
    snapshot = history.snapshot()
    payload["nested"]["a"] = 99
    assert history.snapshot() == snapshot
    result = history.get_result("structured")
    assert result is not None
    assert result.structured_payload_json == ('{"nested":{"a":1,"b":2},"phase":"lag"}')

    stored = replace(request, structured_payload_json={"nested": {"b": 2, "a": 1}, "phase": "lag"})
    with transaction(memory_conn):
        writer.upsert_reading(stored)
    memory_conn.execute(
        "UPDATE reading_transactions SET structured_payload_json = ? WHERE planned_reading_id = ?",
        ('{ "phase": "lag", "nested": {"b": 2, "a": 1} }', "structured"),
    )
    fresh_history = VirtualReadingHistory(sqlite_repository)
    assert fresh_history.apply(stored).disposition is UpsertDisposition.IGNORED_DUPLICATE
    with transaction(memory_conn):
        assert writer.upsert_reading(stored).disposition is UpsertDisposition.IGNORED_DUPLICATE


@pytest.mark.parametrize(("payload", "message"), [("{", "JSON が不正"), ("[]", "JSON object")])
def test_virtual_and_repository_reject_invalid_stored_structured_payload(
    memory_conn: sqlite3.Connection,
    sqlite_repository: SQLiteRepository,
    payload: str,
    message: str,
) -> None:
    writer, _ = build_services(memory_conn, sqlite_repository)
    _create_other_definition(sqlite_repository, structured=True)
    create_plan(
        sqlite_repository, "structured", _at(8), measurement_definition_id="other-definition"
    )
    request = replace(
        _request("structured", None, 8),
        measurement_definition_id="other-definition",
        structured_payload_json={"phase": "lag"},
    )
    with transaction(memory_conn):
        writer.upsert_reading(request)
    memory_conn.execute("PRAGMA ignore_check_constraints = ON")
    memory_conn.execute(
        "UPDATE reading_transactions SET structured_payload_json = ? WHERE planned_reading_id = ?",
        (payload, "structured"),
    )
    memory_conn.execute("PRAGMA ignore_check_constraints = OFF")
    history = VirtualReadingHistory(sqlite_repository)
    with pytest.raises(ValidationError, match=message):
        history.apply(request)
    with pytest.raises(ValidationError, match=message), transaction(memory_conn):
        writer.upsert_reading(request)


def test_virtual_nonmeasured_update_recalculates_existing_result_without_changing_audit(
    memory_conn: sqlite3.Connection,
    sqlite_repository: SQLiteRepository,
) -> None:
    writer, _ = build_services(memory_conn, sqlite_repository)
    for slot, hour, raw in (("previous", 3, "100"), ("early", 8, "110"), ("late", 14, "130")):
        create_plan(sqlite_repository, slot, _at(hour))
        writer.save_manual_reading(_manual(slot, raw, hour), staff_id=STAFF_ID)
    original = sqlite_repository.get_mobile_reading_import_targets(["late"])["late"].existing_result
    assert original is not None
    history = VirtualReadingHistory(sqlite_repository)
    skipped = _request("early", None, 8, status=ResultStatus.SKIPPED)
    before = tuple(memory_conn.iterdump())

    assert history.apply(skipped).accepted

    early_result = history.get_result("early")
    late_result = history.get_result("late")
    assert early_result is not None and early_result.result_status is ResultStatus.SKIPPED
    assert early_result.raw_reading is None and early_result.usage_delta is None
    assert late_result is not None and (late_result.last_reading, late_result.usage_delta) == (
        Decimal("100"),
        Decimal("30"),
    )
    assert (
        late_result.signature,
        late_result.updated_by,
        late_result.device_id,
        late_result.arrival_sequence,
    ) == (original.signature, original.updated_by, original.device_id, original.arrival_sequence)
    assert tuple(memory_conn.iterdump()) == before

    with transaction(memory_conn):
        assert writer.upsert_reading(skipped).accepted
    assert (
        history.get_result("early")
        == sqlite_repository.get_mobile_reading_import_targets(["early"])["early"].existing_result
    )
    assert (
        history.get_result("late")
        == sqlite_repository.get_mobile_reading_import_targets(["late"])["late"].existing_result
    )
