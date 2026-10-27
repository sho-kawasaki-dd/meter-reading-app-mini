from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal

import pytest
from reading_service_helpers import (
    DEFINITION_ID,
    LOCATION_ID,
    METER_ID,
    SHEET_ID,
    build_services,
    confirmation_context,
    create_plan,
    mobile_submission,
    sync_request,
)

from meter_reading_mini.core.contract_models import (
    Category,
    MeterReadingSubmission,
    MeterReadingSyncRequest,
    MeterReplacementSubmission,
    ResultStatus,
    SyncItemIgnoreCode,
    SyncItemRejectCode,
)
from meter_reading_mini.core.errors import DomainError
from meter_reading_mini.core.meter_reading_sync_repositories import (
    ManualReadingInput,
    MeterMasterCreateRequest,
    ReplacementBoundaryCalculationUpdate,
)
from meter_reading_mini.core.reading_signature import verify_meter_reading_signature
from meter_reading_mini.core.timeutil import parse_utc
from meter_reading_mini.db.connection import transaction


def _scheduled(hour: int) -> datetime:
    return datetime(2026, 2, 4, hour, 0, tzinfo=UTC)


def test_sync_service_rejects_business_rule_per_savepoint_and_continues(
    file_database_repository: tuple,
) -> None:
    conn, repository = file_database_repository
    _, service = build_services(conn, repository)
    create_plan(repository, "plan-first", _scheduled(4))
    create_plan(repository, "plan-negative", _scheduled(5))
    create_plan(repository, "plan-last", _scheduled(6))

    response = service.sync(
        sync_request(
            mobile_submission("plan-first", "100"),
            mobile_submission("plan-negative", "90"),
            mobile_submission("plan-last", "120"),
        ),
        confirmation_context(),
    )

    assert response.accepted_reading_items == 2
    assert response.rejected_reading_items[0].code is SyncItemRejectCode.INVALID_READING_SUBMISSION
    assert response.rejected_reading_items[0].planned_reading_id == "plan-negative"
    assert conn.execute(
        "SELECT COUNT(*) FROM reading_transactions WHERE planned_reading_id = 'plan-negative'"
    ).fetchone() == (0,)
    assert conn.execute(
        "SELECT last_reading, usage_delta FROM reading_transactions "
        "WHERE planned_reading_id = 'plan-last'"
    ).fetchone() == ("100", "20")


def test_sync_builds_pc_audit_signature_and_normalizes_timestamp(
    file_database_repository: tuple,
) -> None:
    conn, repository = file_database_repository
    _, service = build_services(conn, repository)
    create_plan(repository, "plan-signature", _scheduled(4))
    submission = mobile_submission(
        "plan-signature",
        "12.5",
        recorded_at="2026-02-04T08:00:00.987654+09:00",
    )

    response = service.sync(
        sync_request(submission),
        confirmation_context(),
    )

    assert response.accepted_reading_items == 1
    stored = conn.execute(
        "SELECT result_status, recorded_at, client_updated_at, raw_reading, device_id, "
        "updated_by, signature, result_source FROM reading_transactions "
        "WHERE planned_reading_id = 'plan-signature'"
    ).fetchone()
    assert stored[:6] == (
        "measured",
        "2026-02-03 23:00:00",
        "2026-02-03 23:05:00",
        "12.5",
        SHEET_ID,
        "試験担当",
    )
    assert stored[7] == "mobile_html"
    verify_meter_reading_signature(
        planned_reading_id="plan-signature",
        result_status=submission.result_status,
        raw_reading=Decimal("12.5"),
        structured_payload_json=None,
        recorded_at=parse_utc(stored[1]),
        device_id=SHEET_ID,
        signature=stored[6],
    )


def test_identical_mobile_reimport_is_ignored_without_incrementing_arrival_sequence(
    file_database_repository: tuple,
) -> None:
    conn, repository = file_database_repository
    _, service = build_services(conn, repository)
    create_plan(repository, "plan-duplicate", _scheduled(4))
    submission = mobile_submission("plan-duplicate", "42")

    response = service.sync(
        sync_request(submission, submission),
        confirmation_context(),
    )

    assert response.accepted_reading_items == 1
    assert len(response.ignored_reading_items) == 1
    assert response.ignored_reading_items[0].code is SyncItemIgnoreCode.DUPLICATE_SUBMISSION
    assert response.detail is not None
    assert "duplicate" in response.detail
    assert conn.execute(
        "SELECT arrival_sequence FROM reading_transactions "
        "WHERE planned_reading_id = 'plan-duplicate'"
    ).fetchone() == (1,)


def test_sync_rejects_invalid_staff_or_sheet_before_writing(
    file_database_repository: tuple,
) -> None:
    conn, repository = file_database_repository
    _, service = build_services(conn, repository)
    create_plan(repository, "plan-invalid-context", _scheduled(4))
    request = sync_request(mobile_submission("plan-invalid-context", "10"))

    with pytest.raises(DomainError):
        service.sync(request, confirmation_context(sheet_id="invalid"))
    with pytest.raises(DomainError):
        service.sync(request, confirmation_context(staff_id="999"))

    assert conn.execute(
        "SELECT COUNT(*) FROM reading_transactions WHERE planned_reading_id = ?",
        ("plan-invalid-context",),
    ).fetchone() == (0,)


def test_sync_reports_duplicate_and_stale_items_separately(
    file_database_repository: tuple,
) -> None:
    conn, repository = file_database_repository
    _, service = build_services(conn, repository)
    create_plan(repository, "plan-stale", _scheduled(4))
    first = mobile_submission(
        "plan-stale",
        "10",
        client_updated_at="2026-02-04T08:05:00+09:00",
    )
    stale = mobile_submission(
        "plan-stale",
        "11",
        client_updated_at="2026-02-04T08:04:59+09:00",
    )

    response = service.sync(sync_request(first, stale), confirmation_context())

    assert response.accepted_reading_items == 1
    assert response.ignored_reading_items[0].code is SyncItemIgnoreCode.STALE_CLIENT_UPDATE
    assert response.detail is not None
    assert "stale" in response.detail
    assert conn.execute(
        "SELECT raw_reading, arrival_sequence FROM reading_transactions "
        "WHERE planned_reading_id = 'plan-stale'"
    ).fetchone() == ("10", 1)


def test_sync_rejects_unknown_slots_and_measurement_value_overflow(
    file_database_repository: tuple,
) -> None:
    conn, repository = file_database_repository
    _, service = build_services(conn, repository)
    create_plan(repository, "plan-invalid-decimal", _scheduled(4))
    create_plan(repository, "plan-long-note", _scheduled(5))
    long_note = MeterReadingSubmission.model_validate(
        {
            "planned_reading_id": "plan-long-note",
            "result_status": "measured",
            "raw_reading": "10",
            "recorded_at": "2026-02-04T08:00:00+09:00",
            "client_updated_at": "2026-02-04T08:05:00+09:00",
            "note": "x" * 501,
        }
    )

    response = service.sync(
        sync_request(
            mobile_submission("missing-planned-reading", "10"),
            mobile_submission("plan-invalid-decimal", "10.123"),
            long_note,
        ),
        confirmation_context(),
    )

    assert response.rejected_reading_items[0].code is SyncItemRejectCode.UNKNOWN_PLANNED_READING
    assert response.rejected_reading_items[1].code is SyncItemRejectCode.INVALID_READING_SUBMISSION
    assert response.rejected_reading_items[2].code is SyncItemRejectCode.INVALID_READING_SUBMISSION
    assert conn.execute(
        "SELECT COUNT(*) FROM reading_transactions WHERE planned_reading_id = ?",
        ("plan-invalid-decimal",),
    ).fetchone() == (0,)


def test_sync_rejects_a_planned_reading_with_missing_definition(
    file_database_repository: tuple,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    conn, repository = file_database_repository
    _, service = build_services(conn, repository)
    create_plan(repository, "plan-definition-missing", _scheduled(4))
    monkeypatch.setattr(repository, "get_measurement_definition", lambda definition_id: None)

    response = service.sync(
        sync_request(mobile_submission("plan-definition-missing", "10")),
        confirmation_context(),
    )

    assert (
        response.rejected_reading_items[0].code is SyncItemRejectCode.UNKNOWN_MEASUREMENT_DEFINITION
    )


def test_sync_submission_model_does_not_accept_mobile_audit_claims() -> None:
    with pytest.raises(ValueError):
        MeterReadingSubmission.model_validate(
            {
                "planned_reading_id": "plan-1",
                "result_status": "measured",
                "raw_reading": "1",
                "recorded_at": "2026-02-04T08:00:00+09:00",
                "client_updated_at": "2026-02-04T08:00:00+09:00",
                "device_id": "forged-device",
                "updated_by": "forged-name",
                "signature": "forged-signature",
            }
        )


def test_replacement_is_atomic_and_recalculates_delayed_new_meter_results(
    file_database_repository: tuple,
) -> None:
    conn, repository = file_database_repository
    writer, service = build_services(
        conn,
        repository,
        multiplier=Decimal("2"),
        rollover_value=Decimal("100"),
    )
    new_meter_id = "replacement-meter"
    with transaction(conn):
        repository.create_meter_master(
            MeterMasterCreateRequest(
                meter_id=new_meter_id,
                location_id=LOCATION_ID,
                location_name="試験場所",
                meter_name="交換後メーター",
                category=Category.WATER,
            )
        )
    create_plan(repository, "plan-old-before", _scheduled(4))
    create_plan(repository, "plan-old-slot", _scheduled(6))
    create_plan(repository, "plan-new-late", _scheduled(7), meter_id=new_meter_id)
    create_plan(repository, "plan-new-following", _scheduled(8), meter_id=new_meter_id)
    writer.save_manual_reading(
        ManualReadingInput(
            "plan-old-before",
            ResultStatus.MEASURED,
            _scheduled(4),
            Decimal("90"),
            None,
        ),
        staff_id="1",
    )
    writer.save_manual_reading(
        ManualReadingInput(
            "plan-new-late",
            ResultStatus.MEASURED,
            _scheduled(7),
            Decimal("7"),
            None,
        ),
        staff_id="1",
    )
    writer.save_manual_reading(
        ManualReadingInput(
            "plan-new-following",
            ResultStatus.MEASURED,
            _scheduled(8),
            Decimal("9"),
            None,
        ),
        staff_id="1",
    )
    replacement = MeterReplacementSubmission.model_validate(
        {
            "replacement_event_id": "replace-event",
            "location_id": LOCATION_ID,
            "old_meter_id": METER_ID,
            "new_meter_id": new_meter_id,
            "replaced_at": "2026-02-04T05:30:00+00:00",
            "client_updated_at": "2026-02-04T05:31:00+00:00",
            "old_final_reading": "5",
            "new_initial_reading": "95",
            "updated_by": "入力側の担当者",
        }
    )

    response = service.sync(
        MeterReadingSyncRequest(reading_items=[], replacement_items=[replacement]),
        confirmation_context(),
    )

    assert response.accepted_replacement_items == 1
    old_slot = repository.get_planned_reading("plan-old-slot")
    assert old_slot is not None
    assert old_slot.task_state.value == "superseded"
    assert old_slot.close_reason.value == "meter_replaced"
    successor_id = old_slot.superseded_by_planned_reading_id
    assert successor_id is not None
    new_slot = repository.get_planned_reading(successor_id)
    assert new_slot is not None
    assert new_slot.meter_id == new_meter_id
    assert new_slot.scheduled_at == _scheduled(6)
    assert (
        conn.execute(
            "SELECT release_date FROM planned_readings WHERE planned_reading_id = ?",
            (successor_id,),
        ).fetchone()
        == conn.execute(
            "SELECT release_date FROM planned_readings WHERE planned_reading_id = 'plan-old-slot'"
        ).fetchone()
    )
    assert conn.execute(
        "SELECT previous_measured_reading, old_meter_usage_delta "
        "FROM replacement_boundaries WHERE replacement_event_id = 'replace-event'"
    ).fetchone() == ("90", "30")
    writer.save_manual_reading(
        ManualReadingInput(
            "plan-old-before",
            ResultStatus.MEASURED,
            _scheduled(4),
            Decimal("95"),
            None,
        ),
        staff_id="1",
    )
    assert conn.execute(
        "SELECT previous_measured_reading, old_meter_usage_delta "
        "FROM replacement_boundaries WHERE replacement_event_id = 'replace-event'"
    ).fetchone() == ("95", "20")
    assert conn.execute(
        "SELECT last_reading, usage_delta FROM reading_transactions "
        "WHERE planned_reading_id = 'plan-new-late'"
    ).fetchone() == ("95", "24")
    assert conn.execute(
        "SELECT last_reading, usage_delta FROM reading_transactions "
        "WHERE planned_reading_id = 'plan-new-following'"
    ).fetchone() == ("7", "4")
    writer.save_manual_reading(
        ManualReadingInput(
            "plan-new-late",
            ResultStatus.SKIPPED,
            _scheduled(7),
            None,
            None,
        ),
        staff_id="1",
    )
    assert conn.execute(
        "SELECT last_reading, usage_delta FROM reading_transactions "
        "WHERE planned_reading_id = 'plan-new-following'"
    ).fetchone() == ("95", "28")
    assert conn.execute(
        "SELECT result_source, device_id, updated_by FROM reading_transactions "
        "WHERE planned_reading_id = 'plan-new-following'"
    ).fetchone() == ("manual", "pc-direct", "試験担当")
    assert repository.get_meter_master(METER_ID).is_active is True


def test_missing_replacement_initial_value_starts_new_meter_usage_at_zero(
    file_database_repository: tuple,
) -> None:
    conn, repository = file_database_repository
    writer, service = build_services(conn, repository)
    new_meter_id = "replacement-without-initial"
    with transaction(conn):
        repository.create_meter_master(
            MeterMasterCreateRequest(
                meter_id=new_meter_id,
                location_id=LOCATION_ID,
                location_name="試験場所",
                meter_name="交換後メーター",
                category=Category.WATER,
            )
        )
    create_plan(repository, "plan-old-history-no-initial", _scheduled(4))
    create_plan(repository, "plan-old-open-no-initial", _scheduled(6))
    create_plan(
        repository,
        "plan-new-first-no-initial",
        _scheduled(7),
        meter_id=new_meter_id,
    )
    writer.save_manual_reading(
        ManualReadingInput(
            "plan-old-history-no-initial",
            ResultStatus.MEASURED,
            _scheduled(4),
            Decimal("90"),
            None,
        ),
        staff_id="1",
    )
    writer.save_manual_reading(
        ManualReadingInput(
            "plan-new-first-no-initial",
            ResultStatus.MEASURED,
            _scheduled(7),
            Decimal("7"),
            None,
        ),
        staff_id="1",
    )
    replacement = MeterReplacementSubmission.model_validate(
        {
            "replacement_event_id": "replace-no-initial",
            "location_id": LOCATION_ID,
            "old_meter_id": METER_ID,
            "new_meter_id": new_meter_id,
            "replaced_at": "2026-02-04T05:30:00+00:00",
            "client_updated_at": "2026-02-04T05:31:00+00:00",
            "old_final_reading": None,
            "new_initial_reading": None,
            "updated_by": "入力側の担当者",
        }
    )

    response = service.sync(
        MeterReadingSyncRequest(reading_items=[], replacement_items=[replacement]),
        confirmation_context(),
    )

    assert response.accepted_replacement_items == 1
    assert conn.execute(
        "SELECT last_reading, usage_delta FROM reading_transactions "
        "WHERE planned_reading_id = 'plan-new-first-no-initial'"
    ).fetchone() == (None, "0")
    assert conn.execute(
        "SELECT previous_measured_reading, old_meter_usage_delta "
        "FROM replacement_boundaries WHERE replacement_event_id = 'replace-no-initial'"
    ).fetchone() == ("90", None)


def test_invalid_replacement_boundary_rolls_back_exchange_side_effects(
    file_database_repository: tuple,
) -> None:
    conn, repository = file_database_repository
    writer, service = build_services(conn, repository)
    new_meter_id = "invalid-replacement-meter"
    with transaction(conn):
        repository.create_meter_master(
            MeterMasterCreateRequest(
                meter_id=new_meter_id,
                location_id=LOCATION_ID,
                location_name="試験場所",
                meter_name="交換後メーター",
                category=Category.WATER,
            )
        )
    create_plan(repository, "plan-invalid-boundary-prev", _scheduled(4))
    create_plan(repository, "plan-invalid-boundary-slot", _scheduled(6))
    writer.save_manual_reading(
        ManualReadingInput(
            "plan-invalid-boundary-prev",
            ResultStatus.MEASURED,
            _scheduled(4),
            Decimal("90"),
            None,
        ),
        staff_id="1",
    )
    replacement = MeterReplacementSubmission.model_validate(
        {
            "replacement_event_id": "replace-invalid",
            "location_id": LOCATION_ID,
            "old_meter_id": METER_ID,
            "new_meter_id": new_meter_id,
            "replaced_at": "2026-02-04T05:30:00+00:00",
            "client_updated_at": "2026-02-04T05:31:00+00:00",
            "old_final_reading": "80",
            "new_initial_reading": "5",
            "updated_by": "入力側の担当者",
        }
    )

    response = service.sync(
        MeterReadingSyncRequest(reading_items=[], replacement_items=[replacement]),
        confirmation_context(),
    )

    assert len(response.rejected_replacement_items) == 1
    assert response.rejected_replacement_items[0].replacement_event_id == "replace-invalid"
    assert conn.execute(
        "SELECT COUNT(*) FROM replacement_events WHERE replacement_event_id = 'replace-invalid'"
    ).fetchone() == (0,)
    assert conn.execute(
        "SELECT COUNT(*) FROM replacement_boundaries WHERE replacement_event_id = 'replace-invalid'"
    ).fetchone() == (0,)
    slot = repository.get_planned_reading("plan-invalid-boundary-slot")
    assert slot is not None
    assert slot.task_state.value == "open"
    assignment = repository.get_meter_assignment(DEFINITION_ID, _scheduled(6))
    assert assignment is not None
    assert assignment.meter_id == METER_ID


def test_sync_rejects_inactive_replacement_target_without_effects(
    file_database_repository: tuple,
) -> None:
    conn, repository = file_database_repository
    _, service = build_services(conn, repository)
    new_meter_id = "inactive-replacement-target"
    with transaction(conn):
        repository.create_meter_master(
            MeterMasterCreateRequest(
                meter_id=new_meter_id,
                location_id=LOCATION_ID,
                location_name="試験場所",
                meter_name="無効化済み交換先",
                category=Category.WATER,
            )
        )
        repository.set_meter_master_active(new_meter_id, False)
    replacement = MeterReplacementSubmission.model_validate(
        {
            "replacement_event_id": "replace-inactive-target",
            "location_id": LOCATION_ID,
            "old_meter_id": METER_ID,
            "new_meter_id": new_meter_id,
            "replaced_at": "2026-02-04T05:30:00+00:00",
            "client_updated_at": "2026-02-04T05:31:00+00:00",
            "old_final_reading": None,
            "new_initial_reading": None,
            "updated_by": "入力側の担当者",
        }
    )

    response = service.sync(
        MeterReadingSyncRequest(reading_items=[], replacement_items=[replacement]),
        confirmation_context(),
    )

    assert (
        response.rejected_replacement_items[0].code
        is SyncItemRejectCode.INVALID_REPLACEMENT_SUBMISSION
    )
    assert conn.execute(
        "SELECT COUNT(*) FROM replacement_events "
        "WHERE replacement_event_id = 'replace-inactive-target'"
    ).fetchone() == (0,)
    assignment = repository.get_meter_assignment(DEFINITION_ID, _scheduled(6))
    assert assignment is not None
    assert assignment.meter_id == METER_ID


def test_unexpected_boundary_update_error_rolls_back_whole_exchange_batch(
    file_database_repository: tuple,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    conn, repository = file_database_repository
    writer, service = build_services(conn, repository)
    new_meter_id = "boundary-update-failure-meter"
    with transaction(conn):
        repository.create_meter_master(
            MeterMasterCreateRequest(
                meter_id=new_meter_id,
                location_id=LOCATION_ID,
                location_name="試験場所",
                meter_name="交換後メーター",
                category=Category.WATER,
            )
        )
    create_plan(repository, "plan-boundary-failure-prev", _scheduled(4))
    create_plan(repository, "plan-boundary-failure-open", _scheduled(6))
    writer.save_manual_reading(
        ManualReadingInput(
            "plan-boundary-failure-prev",
            ResultStatus.MEASURED,
            _scheduled(4),
            Decimal("90"),
            None,
        ),
        staff_id="1",
    )
    original_update = repository.update_replacement_boundary_calculation

    def update_then_fail(update: ReplacementBoundaryCalculationUpdate) -> None:
        original_update(update)
        raise RuntimeError("交換境界保存中の予期しない障害")

    monkeypatch.setattr(repository, "update_replacement_boundary_calculation", update_then_fail)
    replacement = MeterReplacementSubmission.model_validate(
        {
            "replacement_event_id": "replace-boundary-failure",
            "location_id": LOCATION_ID,
            "old_meter_id": METER_ID,
            "new_meter_id": new_meter_id,
            "replaced_at": "2026-02-04T05:30:00+00:00",
            "client_updated_at": "2026-02-04T05:31:00+00:00",
            "old_final_reading": "100",
            "new_initial_reading": "5",
            "updated_by": "入力側の担当者",
        }
    )

    with pytest.raises(RuntimeError, match="交換境界保存中"):
        service.sync(
            MeterReadingSyncRequest(reading_items=[], replacement_items=[replacement]),
            confirmation_context(),
        )

    assert conn.execute(
        "SELECT COUNT(*) FROM replacement_events "
        "WHERE replacement_event_id = 'replace-boundary-failure'"
    ).fetchone() == (0,)
    assert conn.execute(
        "SELECT COUNT(*) FROM replacement_boundaries "
        "WHERE replacement_event_id = 'replace-boundary-failure'"
    ).fetchone() == (0,)
    old_slot = repository.get_planned_reading("plan-boundary-failure-open")
    assert old_slot is not None
    assert old_slot.task_state.value == "open"
    assignment = repository.get_meter_assignment(DEFINITION_ID, _scheduled(6))
    assert assignment is not None
    assert assignment.meter_id == METER_ID
