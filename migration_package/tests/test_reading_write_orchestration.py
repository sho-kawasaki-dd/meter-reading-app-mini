from __future__ import annotations

from datetime import UTC, date, datetime
from decimal import Decimal
from functools import partial

import pytest
from reading_service_helpers import (
    DEFINITION_ID,
    METER_ID,
    STAFF_ID,
    TEST_NOW,
    build_services,
    create_plan,
)

from meter_reading_mini.core.contract_models import ResultStatus
from meter_reading_mini.core.errors import InactiveMasterError, ValidationError
from meter_reading_mini.core.measurement_definition_service import (
    MeasurementDefinitionService,
)
from meter_reading_mini.core.meter_master_service import MeterMasterService
from meter_reading_mini.core.meter_reading_sync_repositories import (
    CalculatedUsageUpdate,
    ManualReadingInput,
    PlannedReadingCloseReason,
)
from meter_reading_mini.core.reading_signature import verify_meter_reading_signature
from meter_reading_mini.core.reading_write_orchestration import (
    ReadingWriteOrchestrationService,
)
from meter_reading_mini.core.staff_service import StaffService
from meter_reading_mini.core.timeutil import parse_utc
from meter_reading_mini.db.connection import transaction


def _recorded_at(hour: int) -> datetime:
    return datetime(2026, 2, 4, hour, 0, tzinfo=UTC)


def test_common_write_path_recalculates_all_following_measured_values(
    file_database_repository: tuple,
) -> None:
    conn, repository = file_database_repository
    writer, _ = build_services(conn, repository)
    for index, hour in enumerate((4, 5, 6), start=1):
        create_plan(repository, f"plan-{index}", _recorded_at(hour))

    writer.save_manual_reading(
        ManualReadingInput("plan-1", ResultStatus.MEASURED, _recorded_at(4), Decimal("100"), None),
        staff_id=STAFF_ID,
    )
    writer.save_manual_reading(
        ManualReadingInput("plan-2", ResultStatus.MEASURED, _recorded_at(5), Decimal("120"), None),
        staff_id=STAFF_ID,
    )
    writer.save_manual_reading(
        ManualReadingInput("plan-3", ResultStatus.MEASURED, _recorded_at(6), Decimal("150"), None),
        staff_id=STAFF_ID,
    )

    writer.save_manual_reading(
        ManualReadingInput("plan-2", ResultStatus.SKIPPED, _recorded_at(5), None, None),
        staff_id=STAFF_ID,
    )

    rows = conn.execute(
        "SELECT planned_reading_id, result_status, last_reading, usage_delta "
        "FROM reading_transactions ORDER BY planned_reading_id"
    ).fetchall()
    assert rows == [
        ("plan-1", "measured", None, "0"),
        ("plan-2", "skipped", None, None),
        ("plan-3", "measured", "100", "50"),
    ]
    writer.save_manual_reading(
        ManualReadingInput(
            "plan-2",
            ResultStatus.MEASURED,
            _recorded_at(5),
            Decimal("120"),
            None,
        ),
        staff_id=STAFF_ID,
    )
    assert conn.execute(
        "SELECT last_reading, usage_delta FROM reading_transactions "
        "WHERE planned_reading_id = 'plan-3'"
    ).fetchone() == ("120", "30")


def test_manual_write_generates_pc_signature_and_monotonic_update_time(
    file_database_repository: tuple,
) -> None:
    conn, repository = file_database_repository
    writer, sync_service = build_services(conn, repository)
    create_plan(repository, "plan-manual", _recorded_at(4))
    from reading_service_helpers import (
        confirmation_context,
        mobile_submission,
        sync_request,
    )

    submission = mobile_submission(
        "plan-manual",
        "12.5",
        recorded_at="2026-02-04T08:00:00.987654+09:00",
        client_updated_at="2030-01-01T00:00:00+09:00",
    )
    response = sync_service.sync(
        sync_request(submission),
        confirmation_context(),
    )
    assert response.accepted_reading_items == 1

    writer.save_manual_reading(
        ManualReadingInput(
            "plan-manual",
            ResultStatus.MEASURED,
            _recorded_at(4),
            Decimal("13.5"),
            None,
        ),
        staff_id=STAFF_ID,
    )
    stale_response = sync_service.sync(
        sync_request(submission),
        confirmation_context(),
    )
    assert stale_response.ignored_reading_items[0].code.value == "stale_client_update"

    stored = conn.execute(
        "SELECT result_status, recorded_at, client_updated_at, raw_reading, device_id, "
        "updated_by, signature, result_source, arrival_sequence FROM reading_transactions "
        "WHERE planned_reading_id = 'plan-manual'"
    ).fetchone()
    assert stored == (
        "measured",
        "2026-02-04 04:00:00",
        "2029-12-31 15:00:01",
        "13.5",
        "pc-direct",
        "試験担当",
        stored[6],
        "manual",
        2,
    )
    verify_meter_reading_signature(
        planned_reading_id="plan-manual",
        result_status=ResultStatus.MEASURED,
        raw_reading=Decimal("13.5"),
        structured_payload_json=None,
        recorded_at=parse_utc(stored[1]),
        device_id="pc-direct",
        signature=stored[6],
    )


def test_downstream_recalculation_failure_rolls_back_manual_write(
    file_database_repository: tuple,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    conn, repository = file_database_repository
    writer, _ = build_services(conn, repository)
    create_plan(repository, "plan-before", _recorded_at(4))
    create_plan(repository, "plan-edited", _recorded_at(5))
    create_plan(repository, "plan-after", _recorded_at(6))

    for planned_id, hour, raw in (
        ("plan-before", 4, "100"),
        ("plan-edited", 5, "120"),
        ("plan-after", 6, "150"),
    ):
        writer.save_manual_reading(
            ManualReadingInput(
                planned_id,
                ResultStatus.MEASURED,
                _recorded_at(hour),
                Decimal(raw),
                None,
            ),
            staff_id=STAFF_ID,
        )

    original_save = repository.save_calculated_usage

    def fail_recalculation(update: CalculatedUsageUpdate) -> None:
        if update.planned_reading_id == "plan-after":
            raise RuntimeError("後続再計算の障害")
        original_save(update)

    monkeypatch.setattr(repository, "save_calculated_usage", fail_recalculation)
    with pytest.raises(RuntimeError, match="後続再計算の障害"):
        writer.save_manual_reading(
            ManualReadingInput(
                "plan-edited",
                ResultStatus.SKIPPED,
                _recorded_at(5),
                None,
                None,
            ),
            staff_id=STAFF_ID,
        )

    state = conn.execute(
        "SELECT result_status, raw_reading FROM reading_transactions "
        "WHERE planned_reading_id = 'plan-edited'"
    ).fetchone()
    assert state == ("measured", "120")
    assert conn.execute(
        "SELECT usage_delta FROM reading_transactions WHERE planned_reading_id = 'plan-after'"
    ).fetchone() == ("30",)


def test_auto_closed_slot_on_deactivated_meter_can_be_completed_later(
    file_database_repository: tuple,
) -> None:
    conn, repository = file_database_repository
    writer, _ = build_services(conn, repository)
    create_plan(repository, "plan-auto-closed", TEST_NOW)
    repository.auto_close_open_planned_readings(
        date(2026, 2, 4),
        closed_at=TEST_NOW,
        close_reason=PlannedReadingCloseReason.CUTOFF_ELAPSED,
    )
    transaction_factory = partial(transaction, conn)
    definition_service = MeasurementDefinitionService(
        repository,
        repository,
        repository,
        transaction_factory=transaction_factory,
    )
    definition_service.deactivate_measurement_definition(DEFINITION_ID)
    meter_service = MeterMasterService(
        repository,
        transaction_factory=transaction_factory,
    )
    meter_service.deactivate_meter(METER_ID)

    writer.save_manual_reading(
        ManualReadingInput(
            "plan-auto-closed",
            ResultStatus.MEASURED,
            TEST_NOW,
            Decimal("10"),
            None,
        ),
        staff_id=STAFF_ID,
    )

    slot = repository.get_planned_reading("plan-auto-closed")
    assert slot is not None
    assert slot.task_state.value == "completed"
    assert conn.execute(
        "SELECT raw_reading FROM reading_transactions WHERE planned_reading_id = 'plan-auto-closed'"
    ).fetchone() == ("10",)


def test_manual_write_requires_configured_transaction_and_active_staff(
    file_database_repository: tuple,
) -> None:
    conn, repository = file_database_repository
    writer, _ = build_services(conn, repository)
    create_plan(repository, "plan-staff-check", _recorded_at(4))
    unconfigured = ReadingWriteOrchestrationService(
        repository,
        repository,
        repository,
        planned_reading_repository=repository,
        boundary_repository=repository,
        boundary_maintenance_repository=repository,
    )
    reading = ManualReadingInput(
        "plan-staff-check",
        ResultStatus.MEASURED,
        _recorded_at(4),
        Decimal("1"),
        None,
    )
    with pytest.raises(RuntimeError, match="トランザクション"):
        unconfigured.save_manual_reading(reading, staff_id=STAFF_ID)

    staff_service = StaffService(
        repository,
        transaction_factory=partial(transaction, conn),
    )
    staff_service.deactivate_staff(STAFF_ID)
    with pytest.raises(InactiveMasterError):
        writer.save_manual_reading(reading, staff_id=STAFF_ID)
    assert conn.execute(
        "SELECT COUNT(*) FROM reading_transactions WHERE planned_reading_id = ?",
        ("plan-staff-check",),
    ).fetchone() == (0,)


def test_manual_write_validates_payload_and_note(
    file_database_repository: tuple,
) -> None:
    conn, repository = file_database_repository
    writer, _ = build_services(conn, repository)
    create_plan(repository, "plan-invalid-manual", _recorded_at(4))

    with pytest.raises(ValidationError):
        writer.save_manual_reading(
            ManualReadingInput(
                "plan-invalid-manual",
                ResultStatus.MEASURED,
                _recorded_at(4),
                None,
                None,
            ),
            staff_id=STAFF_ID,
        )
    with pytest.raises(ValidationError, match="500"):
        writer.save_manual_reading(
            ManualReadingInput(
                "plan-invalid-manual",
                ResultStatus.MEASURED,
                _recorded_at(4),
                Decimal("1"),
                None,
                note="x" * 501,
            ),
            staff_id=STAFF_ID,
        )
