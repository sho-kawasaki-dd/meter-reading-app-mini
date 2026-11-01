from __future__ import annotations

import sqlite3
from dataclasses import replace
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
    Category,
    MeterReadingSyncRequest,
    MeterReplacementSubmission,
    ResultStatus,
)
from meter_reading_mini.core.errors import InactiveMasterError, ValidationError
from meter_reading_mini.core.meter_reading_sync import MeterReadingSyncService
from meter_reading_mini.core.meter_reading_sync_repositories import (
    CalculatedUsageUpdate,
    ManualReadingInput,
    MeterMasterCreateRequest,
)
from meter_reading_mini.db.connection import transaction
from meter_reading_mini.db.migrations import create_schema
from meter_reading_mini.db.repositories import SQLiteRepository

NOW = datetime(2026, 2, 5, tzinfo=UTC)


def _at(hour: int) -> datetime:
    return datetime(2026, 2, 4, hour, tzinfo=UTC)


def _service(
    conn: sqlite3.Connection, repository: SQLiteRepository
) -> AssignmentCalculationSettingsService:
    ids = iter(("correction-1", "correction-2", "correction-3"))
    return AssignmentCalculationSettingsService(
        repository,
        repository,
        repository,
        repository,
        repository,
        repository,
        repository,
        repository,
        transaction_factory=partial(transaction, conn),
        now_provider=lambda: NOW,
        id_provider=lambda: next(ids),
    )


def _meter(repository: SQLiteRepository, meter_id: str) -> None:
    repository.create_meter_master(
        MeterMasterCreateRequest(
            meter_id=meter_id,
            location_id=LOCATION_ID,
            location_name="試験場所",
            meter_name=meter_id,
            category=Category.WATER,
        )
    )


def _replacement(
    sync: MeterReadingSyncService,
    *,
    event: str,
    old: str,
    new: str,
    hour: int,
    old_final: str,
    new_initial: str,
) -> None:
    replacement = MeterReplacementSubmission.model_validate(
        {
            "replacement_event_id": event,
            "location_id": LOCATION_ID,
            "old_meter_id": old,
            "new_meter_id": new,
            "replaced_at": _at(hour),
            "client_updated_at": _at(hour),
            "old_final_reading": old_final,
            "new_initial_reading": new_initial,
            "updated_by": "試験担当",
        }
    )
    response = sync.sync(
        MeterReadingSyncRequest(reading_items=[], replacement_items=[replacement]),
        confirmation_context(),
    )
    assert response.accepted_replacement_items == 1


def _setup_replacement(conn: sqlite3.Connection, repository: SQLiteRepository):
    writer, sync = build_services(
        conn, repository, multiplier=Decimal("10"), rollover_value=Decimal("1000")
    )
    _meter(repository, "new-meter")
    create_plan(repository, "old-reading", _at(4))
    create_plan(repository, "old-slot", _at(6))
    writer.save_manual_reading(
        ManualReadingInput("old-reading", ResultStatus.MEASURED, _at(4), Decimal("100"), None),
        staff_id=STAFF_ID,
    )
    _replacement(
        sync,
        event="replace-1",
        old=METER_ID,
        new="new-meter",
        hour=5,
        old_final="120",
        new_initial="5",
    )
    old_slot = repository.get_planned_reading("old-slot")
    assert old_slot is not None and old_slot.superseded_by_planned_reading_id is not None
    successor = old_slot.superseded_by_planned_reading_id
    conn.execute(
        "UPDATE meter_assignments SET multiplier = '20', rollover_value = '50' WHERE meter_id = ?",
        ("new-meter",),
    )
    writer.save_manual_reading(
        ManualReadingInput(successor, ResultStatus.MEASURED, _at(6), Decimal("8"), None),
        staff_id=STAFF_ID,
    )
    return writer, sync, successor


def _request(
    repository: SQLiteRepository,
    meter_id: str,
    multiplier: str,
    rollover: str | None,
) -> AssignmentCalculationCorrectionInput:
    assignment = next(
        item
        for item in repository.list_meter_assignments(DEFINITION_ID)
        if item.meter_id == meter_id
    )
    return AssignmentCalculationCorrectionInput(
        measurement_definition_id=DEFINITION_ID,
        meter_id=meter_id,
        effective_from=assignment.effective_from,
        multiplier=Decimal(multiplier),
        rollover_value=Decimal(rollover) if rollover is not None else None,
        staff_id=STAFF_ID,
        reason="倍率設定の訂正",
    )


def _state(conn: sqlite3.Connection) -> tuple[list[tuple], ...]:
    return tuple(
        conn.execute(f"SELECT * FROM {table} ORDER BY rowid").fetchall()
        for table in (
            "meter_assignments",
            "assignment_calculation_corrections",
            "reading_transactions",
            "replacement_boundaries",
        )
    )


def test_preview_and_correction_preserve_audit_and_use_each_period(
    file_database_repository: tuple[sqlite3.Connection, SQLiteRepository],
) -> None:
    conn, repository = file_database_repository
    _, _, successor = _setup_replacement(conn, repository)
    service = _service(conn, repository)
    old_request = _request(repository, METER_ID, "12", "1000")
    before = _state(conn)
    changes = conn.total_changes

    preview = service.preview_correction(old_request)

    assert preview.can_apply
    assert conn.total_changes == changes and _state(conn) == before
    assert [
        (item.old_usage_delta, item.update.old_meter_usage_delta) for item in preview.boundaries
    ] == [(Decimal("200"), Decimal("240"))]
    service.correct_assignment(old_request)
    assert conn.execute(
        "SELECT old_meter_usage_delta FROM replacement_boundaries "
        "WHERE replacement_event_id = 'replace-1'"
    ).fetchone() == ("240",)
    assert conn.execute(
        "SELECT usage_delta FROM reading_transactions WHERE planned_reading_id = ?", (successor,)
    ).fetchone() == ("60",)
    after_old = conn.execute(
        "SELECT raw_reading, recorded_at, updated_by, signature, device_id, note "
        "FROM reading_transactions ORDER BY planned_reading_id"
    ).fetchall()
    before_old = [tuple(row[i] for i in (4, 2, 9, 10, 8, 11)) for row in before[2]]
    assert after_old == before_old
    history = service.list_corrections(DEFINITION_ID, METER_ID, old_request.effective_from)
    assert len(history) == 1
    assert (history[0].old_multiplier, history[0].new_multiplier) == (Decimal("10"), Decimal("12"))
    assert (history[0].staff_id, history[0].staff_name, history[0].reason) == (
        STAFF_ID,
        "試験担当",
        "倍率設定の訂正",
    )
    assert history[0].corrected_at == NOW

    new_request = _request(repository, "new-meter", "30", "50")
    new_preview = service.preview_correction(new_request)
    assert [(item.old_usage_delta, item.new_usage_delta) for item in new_preview.readings] == [
        (Decimal("60"), Decimal("90"))
    ]
    service.correct_assignment(new_request)
    assert conn.execute(
        "SELECT usage_delta FROM reading_transactions WHERE planned_reading_id = ?", (successor,)
    ).fetchone() == ("90",)
    assert conn.execute(
        "SELECT old_meter_usage_delta FROM replacement_boundaries "
        "WHERE replacement_event_id = 'replace-1'"
    ).fetchone() == ("240",)


def test_assignment_correction_clears_legacy_review_flag(
    file_database_repository: tuple[sqlite3.Connection, SQLiteRepository],
) -> None:
    conn, repository = file_database_repository
    _setup_replacement(conn, repository)
    service = _service(conn, repository)
    request = _request(repository, METER_ID, "10", "1000")
    conn.execute(
        "INSERT INTO assignment_review_flags "
        "(measurement_definition_id, meter_id, assignment_effective_from, reason, created_at) "
        "VALUES (?, ?, ?, ?, ?)",
        (
            DEFINITION_ID,
            METER_ID,
            request.effective_from.strftime("%Y-%m-%d %H:%M:%S"),
            "旧条件の確認",
            "2026-02-05 00:00:00",
        ),
    )

    preview = service.preview_correction(request)
    assert preview.can_apply
    assert service.list_review_flags()

    service.correct_assignment(request)

    assert service.list_review_flags() == []


def test_correction_of_a_middle_period_with_a_later_replacement(
    file_database_repository: tuple[sqlite3.Connection, SQLiteRepository],
) -> None:
    conn, repository = file_database_repository
    writer, sync, successor = _setup_replacement(conn, repository)
    _meter(repository, "third-meter")
    create_plan(repository, "second-source", _at(8), meter_id="new-meter")
    _replacement(
        sync,
        event="replace-2",
        old="new-meter",
        new="third-meter",
        hour=7,
        old_final="9",
        new_initial="2",
    )
    third_source = repository.get_planned_reading("second-source")
    assert third_source is not None and third_source.superseded_by_planned_reading_id is not None
    third_slot = third_source.superseded_by_planned_reading_id
    conn.execute("UPDATE meter_assignments SET multiplier = '50' WHERE meter_id = 'third-meter'")
    writer.save_manual_reading(
        ManualReadingInput(third_slot, ResultStatus.MEASURED, _at(8), Decimal("4"), None),
        staff_id=STAFF_ID,
    )
    service = _service(conn, repository)
    request = _request(repository, "new-meter", "25", "50")

    preview = service.preview_correction(request)
    assert preview.can_apply
    assert [item.replacement_event_id for item in preview.boundaries] == ["replace-1", "replace-2"]
    service.correct_assignment(request)

    assert conn.execute(
        "SELECT planned_reading_id, usage_delta FROM reading_transactions "
        "WHERE planned_reading_id IN (?, ?) ORDER BY planned_reading_id",
        (successor, third_slot),
    ).fetchall() == sorted([(successor, "75"), (third_slot, "100")])
    assert conn.execute(
        "SELECT replacement_event_id, old_meter_usage_delta FROM replacement_boundaries "
        "ORDER BY replacement_event_id"
    ).fetchall() == [("replace-1", "200"), ("replace-2", "25")]


def test_preview_identifies_invalid_values_and_commit_rechecks(
    file_database_repository: tuple[sqlite3.Connection, SQLiteRepository],
) -> None:
    conn, repository = file_database_repository
    _setup_replacement(conn, repository)
    service = _service(conn, repository)
    request = _request(repository, METER_ID, "12", "100")
    before = _state(conn)

    preview = service.preview_correction(request)

    assert not preview.can_apply
    assert {issue.target for issue in preview.issues} == {"old-reading", "replace-1"}
    with pytest.raises(ValidationError, match="old-reading"):
        service.correct_assignment(request)
    assert _state(conn) == before
    for invalid in (
        replace(request, reason=" "),
        replace(request, multiplier=Decimal("NaN")),
        replace(request, rollover_value=Decimal("0")),
    ):
        with pytest.raises(ValidationError):
            service.preview_correction(invalid)
    with pytest.raises(InactiveMasterError):
        service.preview_correction(replace(request, staff_id="999"))


def test_removing_rollover_is_rejected_if_a_difference_would_be_negative(
    file_database_repository: tuple[sqlite3.Connection, SQLiteRepository],
) -> None:
    conn, repository = file_database_repository
    writer, _ = build_services(conn, repository, rollover_value=Decimal("1000"))
    create_plan(repository, "before", _at(4))
    create_plan(repository, "after", _at(5))
    for slot, hour, value in (("before", 4, "990"), ("after", 5, "10")):
        writer.save_manual_reading(
            ManualReadingInput(slot, ResultStatus.MEASURED, _at(hour), Decimal(value), None),
            staff_id=STAFF_ID,
        )
    service = _service(conn, repository)
    request = _request(repository, METER_ID, "1", None)
    before = _state(conn)

    preview = service.preview_correction(request)
    assert not preview.can_apply and any(issue.target == "after" for issue in preview.issues)
    with pytest.raises(ValidationError, match="after"):
        service.correct_assignment(request)
    assert _state(conn) == before


def test_correction_rechecks_values_changed_after_preview(
    file_database_repository: tuple[sqlite3.Connection, SQLiteRepository],
) -> None:
    conn, repository = file_database_repository
    writer, _, successor = _setup_replacement(conn, repository)
    service = _service(conn, repository)
    request = _request(repository, "new-meter", "30", "20")
    assert service.preview_correction(request).can_apply
    writer.save_manual_reading(
        ManualReadingInput(successor, ResultStatus.MEASURED, _at(6), Decimal("25"), None),
        staff_id=STAFF_ID,
    )
    before = _state(conn)

    with pytest.raises(ValidationError, match=successor):
        service.correct_assignment(request)

    assert _state(conn) == before


def test_recalculation_failure_rolls_back_conditions_history_and_results(
    file_database_repository: tuple[sqlite3.Connection, SQLiteRepository],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    conn, repository = file_database_repository
    _setup_replacement(conn, repository)
    service = _service(conn, repository)
    request = _request(repository, "new-meter", "30", "50")
    before = _state(conn)
    save = repository.save_calculated_usage

    def fail_after_write(update: CalculatedUsageUpdate) -> None:
        save(update)
        raise RuntimeError("再計算中の障害")

    monkeypatch.setattr(repository, "save_calculated_usage", fail_after_write)
    with pytest.raises(RuntimeError, match="再計算中"):
        service.correct_assignment(request)
    assert _state(conn) == before
    assert not conn.in_transaction


def test_commit_failure_rolls_back_the_complete_correction(tmp_path: Path) -> None:
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
        _setup_replacement(conn, repository)
        service = _service(conn, repository)
        request = _request(repository, METER_ID, "12", "1000")
        before = _state(conn)
        conn.fail_commit = True
        with pytest.raises(sqlite3.OperationalError, match="simulated commit failure"):
            service.correct_assignment(request)
        assert _state(conn) == before
        assert not conn.in_transaction
    finally:
        conn.close()
