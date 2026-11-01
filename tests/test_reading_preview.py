from __future__ import annotations

import sqlite3
from datetime import UTC, datetime, timedelta
from decimal import Decimal

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

from meter_reading_mini.core.contract_models import (
    Category,
    MeterReadingSyncRequest,
    MeterReplacementSubmission,
    ResultStatus,
)
from meter_reading_mini.core.errors import SupersededPlannedReadingError, ValidationError
from meter_reading_mini.core.meter_master_service import MeterMasterService
from meter_reading_mini.core.meter_reading_sync_repositories import (
    AssignmentCalculationTerms,
    ManualReadingInput,
    MeterMasterCreateRequest,
)
from meter_reading_mini.core.meter_replacement_service import (
    MeterReplacementInput,
    MeterReplacementService,
)
from meter_reading_mini.core.previous_reading import PreviousReadingSource
from meter_reading_mini.core.reading_preview import ReadingPreviewService
from meter_reading_mini.db.connection import transaction
from meter_reading_mini.db.repositories import SQLiteRepository

NOW = datetime(2026, 2, 5, tzinfo=UTC)


def _at(hour: int) -> datetime:
    return datetime(2026, 2, 4, hour, tzinfo=UTC)


def _preview(repository: SQLiteRepository) -> ReadingPreviewService:
    return ReadingPreviewService(
        repository,
        repository,
        repository,
        repository,
        repository,
        repository,
        now_provider=lambda: NOW,
    )


def _input(slot: str, raw: str, hour: int = 5) -> ManualReadingInput:
    return ManualReadingInput(slot, ResultStatus.MEASURED, _at(hour), Decimal(raw), None)


def test_preview_is_read_only_and_matches_save_for_auto_closed_and_correction(
    file_database_repository: tuple[sqlite3.Connection, SQLiteRepository],
) -> None:
    conn, repository = file_database_repository
    writer, _ = build_services(conn, repository, multiplier=Decimal("10"))
    create_plan(repository, "previous", _at(4))
    create_plan(repository, "target", _at(5))
    writer.save_manual_reading(_input("previous", "100", 4), staff_id=STAFF_ID)
    conn.execute(
        "UPDATE planned_readings SET task_state = 'auto_closed', close_reason = 'cutoff_elapsed' "
        "WHERE planned_reading_id = 'target'"
    )
    conn.execute(
        "UPDATE measurement_definitions SET warning_threshold = '100' "
        "WHERE measurement_definition_id = ?",
        (DEFINITION_ID,),
    )
    service = _preview(repository)
    changes = conn.total_changes

    preview = service.preview_manual_reading(_input("target", "111"))

    assert conn.total_changes == changes
    assert (preview.previous_reading, preview.previous_source) == (
        Decimal("100"),
        PreviousReadingSource.MEASURED,
    )
    assert preview.usage_delta == Decimal("110")
    assert preview.warning_threshold_exceeded and not preview.rollover_applied and preview.can_save
    writer.save_manual_reading(_input("target", "111"), staff_id=STAFF_ID)
    assert conn.execute(
        "SELECT p.task_state, r.last_reading, r.usage_delta FROM planned_readings p "
        "JOIN reading_transactions r USING (planned_reading_id) "
        "WHERE p.planned_reading_id = 'target'"
    ).fetchone() == ("completed", "100", "110")

    correction = service.preview_manual_reading(_input("target", "110"))
    assert correction.previous_reading == Decimal("100")
    assert correction.usage_delta == Decimal("100")
    assert not correction.warning_threshold_exceeded


def test_preview_and_save_reject_the_same_invalid_inputs(
    file_database_repository: tuple[sqlite3.Connection, SQLiteRepository],
) -> None:
    conn, repository = file_database_repository
    writer, _ = build_services(conn, repository)
    create_plan(repository, "previous", _at(4))
    create_plan(repository, "target", _at(5))
    writer.save_manual_reading(_input("previous", "100", 4), staff_id=STAFF_ID)
    service = _preview(repository)

    invalid_delta = _input("target", "99")
    preview = service.preview_manual_reading(invalid_delta)
    assert not preview.can_save and preview.error is not None
    with pytest.raises(ValidationError):
        writer.save_manual_reading(invalid_delta, staff_id=STAFF_ID)
    future = ManualReadingInput(
        "target", ResultStatus.MEASURED, NOW + timedelta(seconds=1), Decimal("101"), None
    )
    for action in (
        lambda: service.preview_manual_reading(future),
        lambda: writer.save_manual_reading(future, staff_id=STAFF_ID),
    ):
        with pytest.raises(ValidationError, match="未来"):
            action()
    invalid_shape = _input("target", "1.234")
    for action in (
        lambda: service.preview_manual_reading(invalid_shape),
        lambda: writer.save_manual_reading(invalid_shape, staff_id=STAFF_ID),
    ):
        with pytest.raises(ValidationError):
            action()
    assert conn.execute("SELECT COUNT(*) FROM reading_transactions").fetchone() == (1,)

    conn.execute(
        "UPDATE planned_readings SET task_state = 'superseded' WHERE planned_reading_id = 'target'"
    )
    with pytest.raises(SupersededPlannedReadingError):
        service.preview_manual_reading(_input("target", "101"))


def test_save_revalidates_current_state_after_preview(
    file_database_repository: tuple[sqlite3.Connection, SQLiteRepository],
) -> None:
    conn, repository = file_database_repository
    writer, _ = build_services(conn, repository)
    create_plan(repository, "previous", _at(4))
    create_plan(repository, "target", _at(5))
    writer.save_manual_reading(_input("previous", "100", 4), staff_id=STAFF_ID)
    reading = _input("target", "105")
    assert _preview(repository).preview_manual_reading(reading).can_save

    writer.save_manual_reading(_input("previous", "110", 4), staff_id=STAFF_ID)
    before = tuple(conn.iterdump())
    with pytest.raises(ValidationError):
        writer.save_manual_reading(reading, staff_id=STAFF_ID)
    assert tuple(conn.iterdump()) == before


def test_preview_rejects_correction_that_makes_a_following_reading_negative(
    file_database_repository: tuple[sqlite3.Connection, SQLiteRepository],
) -> None:
    conn, repository = file_database_repository
    writer, _ = build_services(conn, repository, multiplier=Decimal("1"))
    create_plan(repository, "previous", _at(3))
    create_plan(repository, "target", _at(4))
    create_plan(repository, "following", _at(5))
    writer.save_manual_reading(_input("previous", "100", 3), staff_id=STAFF_ID)
    writer.save_manual_reading(_input("target", "110", 4), staff_id=STAFF_ID)
    writer.save_manual_reading(_input("following", "130", 5), staff_id=STAFF_ID)
    changes = conn.total_changes

    preview = _preview(repository).preview_manual_reading(_input("target", "140", 4))

    assert not preview.can_save
    assert preview.error is not None and "ロールオーバー" in preview.error
    assert conn.total_changes == changes


def test_preview_rejects_correction_that_makes_a_later_replacement_boundary_negative(
    file_database_repository: tuple[sqlite3.Connection, SQLiteRepository],
) -> None:
    conn, repository = file_database_repository
    writer, _ = build_services(conn, repository, multiplier=Decimal("1"))
    create_plan(repository, "source", _at(4))
    writer.save_manual_reading(_input("source", "100", 4), staff_id=STAFF_ID)
    repository.create_meter_master(
        MeterMasterCreateRequest(
            meter_id="replacement-meter",
            location_id=LOCATION_ID,
            location_name="試験場所",
            meter_name="交換先",
            category=Category.WATER,
        )
    )
    replacement = MeterReplacementService(
        repository,
        repository,
        repository,
        repository,
        repository,
        repository,
        repository,
        repository,
        MeterMasterService(repository, transaction_factory=lambda: transaction(conn)),
        writer,
        transaction_factory=lambda: transaction(conn),
        now_provider=lambda: NOW,
    )
    replacement.register_replacement(
        MeterReplacementInput(
            old_meter_id=METER_ID,
            replaced_at=_at(5),
            old_final_reading=Decimal("120"),
            new_initial_reading=Decimal("5"),
            staff_id=STAFF_ID,
            new_meter_id="replacement-meter",
            new_terms={DEFINITION_ID: AssignmentCalculationTerms(Decimal("1"), None)},
        )
    )

    preview = _preview(repository).preview_manual_reading(_input("source", "130", 4))

    assert not preview.can_save
    assert preview.error is not None and "ロールオーバー" in preview.error


@pytest.mark.parametrize("value", ["-1", "-0"])
def test_preview_and_save_reject_negative_decimal_objects(
    file_database_repository: tuple[sqlite3.Connection, SQLiteRepository], value: str
) -> None:
    conn, repository = file_database_repository
    writer, _ = build_services(conn, repository)
    create_plan(repository, "first", _at(4))
    reading = _input("first", value, 4)
    for action in (
        lambda: _preview(repository).preview_manual_reading(reading),
        lambda: writer.save_manual_reading(reading, staff_id=STAFF_ID),
    ):
        with pytest.raises(ValidationError):
            action()
    assert conn.execute("SELECT COUNT(*) FROM reading_transactions").fetchone() == (0,)


def test_preview_reports_rollover_and_first_reading(
    file_database_repository: tuple[sqlite3.Connection, SQLiteRepository],
) -> None:
    conn, repository = file_database_repository
    writer, _ = build_services(
        conn, repository, multiplier=Decimal("2"), rollover_value=Decimal("1000")
    )
    create_plan(repository, "first", _at(4))
    create_plan(repository, "second", _at(5))
    service = _preview(repository)
    first = service.preview_manual_reading(_input("first", "990", 4))
    assert (first.previous_source, first.previous_reading, first.usage_delta) == (
        PreviousReadingSource.NONE,
        None,
        Decimal("0"),
    )
    writer.save_manual_reading(_input("first", "990", 4), staff_id=STAFF_ID)
    second = service.preview_manual_reading(_input("second", "10"))
    assert second.rollover_applied and second.usage_delta == Decimal("40")
    with pytest.raises(ValidationError):
        service.preview_manual_reading(_input("second", "1000"))


@pytest.mark.parametrize("initial", ["5", None])
def test_preview_uses_each_assignment_and_the_replacement_initial_value(
    file_database_repository: tuple[sqlite3.Connection, SQLiteRepository], initial: str | None
) -> None:
    conn, repository = file_database_repository
    writer, sync = build_services(conn, repository, multiplier=Decimal("10"))
    repository.create_meter_master(
        MeterMasterCreateRequest(
            meter_id="new-meter",
            location_id=LOCATION_ID,
            location_name="試験場所",
            meter_name="新メーター",
            category=Category.WATER,
        )
    )
    create_plan(repository, "old", _at(4))
    create_plan(repository, "to-replace", _at(6))
    writer.save_manual_reading(_input("old", "100", 4), staff_id=STAFF_ID)
    replacement = MeterReplacementSubmission.model_validate(
        {
            "replacement_event_id": "replace",
            "location_id": LOCATION_ID,
            "old_meter_id": METER_ID,
            "new_meter_id": "new-meter",
            "replaced_at": "2026-02-04T05:30:00+00:00",
            "client_updated_at": "2026-02-04T05:31:00+00:00",
            "old_final_reading": "120",
            "new_initial_reading": initial,
            "updated_by": "試験担当",
        }
    )
    response = sync.sync(
        MeterReadingSyncRequest(reading_items=[], replacement_items=[replacement]),
        confirmation_context(),
    )
    assert response.accepted_replacement_items == 1
    old_slot = repository.get_planned_reading("to-replace")
    assert old_slot is not None and old_slot.superseded_by_planned_reading_id is not None
    successor = old_slot.superseded_by_planned_reading_id
    conn.execute(
        "UPDATE meter_assignments SET multiplier = '20', rollover_value = '50' WHERE meter_id = ?",
        ("new-meter",),
    )
    preview = _preview(repository).preview_manual_reading(_input(successor, "8", 6))
    assert preview.previous_source is (
        PreviousReadingSource.REPLACEMENT_INITIAL
        if initial is not None
        else PreviousReadingSource.REPLACEMENT_NO_INITIAL
    )
    assert preview.previous_reading == (Decimal("5") if initial is not None else None)
    assert preview.usage_delta == (Decimal("60") if initial is not None else Decimal("0"))
    writer.save_manual_reading(_input(successor, "8", 6), staff_id=STAFF_ID)
    assert conn.execute(
        "SELECT usage_delta FROM reading_transactions WHERE planned_reading_id = ?", (successor,)
    ).fetchone() == ("60" if initial is not None else "0",)
