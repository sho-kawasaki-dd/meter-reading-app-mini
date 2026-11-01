from __future__ import annotations

from collections.abc import Callable
from dataclasses import replace
from datetime import UTC, date, datetime
from decimal import Decimal
from functools import partial
from uuid import uuid4

import pytest
from reading_service_helpers import (
    DEFINITION_ID,
    LOCATION_ID,
    METER_ID,
    TEST_NOW,
    build_services,
    create_plan,
)

from meter_reading_mini.core.contract_models import (
    CalculationMethod,
    Category,
    Purpose,
    ReadingCycle,
    ValueSchema,
)
from meter_reading_mini.core.errors import (
    InactiveMasterError,
    InvalidTimezoneError,
    MasterInUseError,
    MeterAssignmentResolutionError,
    ValidationError,
)
from meter_reading_mini.core.measurement_definition_service import (
    MeasurementDefinitionService,
)
from meter_reading_mini.core.meter_master_service import (
    MeterMasterService,
    MeterNotFoundError,
    MeterValidationError,
)
from meter_reading_mini.core.meter_reading_sync_repositories import (
    MeasurementDefinitionCreateInput,
    MeasurementDefinitionCreateRequest,
    MeasurementDefinitionUpdateRequest,
    MeterMasterCreateInput,
    MeterMasterCreateRequest,
    MeterMasterUpdateRequest,
    PlannedReadingCloseReason,
    StaffCreateRequest,
    StaffUpdateRequest,
)
from meter_reading_mini.core.staff_service import (
    StaffNotFoundError,
    StaffService,
    StaffValidationError,
)
from meter_reading_mini.db.connection import transaction
from meter_reading_mini.db.repositories import SQLiteRepository


def _meter_service(conn, repository: SQLiteRepository) -> MeterMasterService:
    return MeterMasterService(repository, transaction_factory=partial(transaction, conn))


def _definition_service(
    conn, repository: SQLiteRepository, *, id_provider: Callable[[], str] | None = None
) -> MeasurementDefinitionService:
    return MeasurementDefinitionService(
        repository,
        repository,
        repository,
        transaction_factory=partial(transaction, conn),
        id_provider=id_provider or (lambda: uuid4().hex),
    )


def _new_definition_request(
    *,
    multiplier: Decimal = Decimal("1"),
    warning_threshold: Decimal | None = None,
    rollover_value: Decimal | None = None,
    decimal_places: int | None = 2,
    initial_schedule_json: str | None = None,
    initial_meter_id: str | None = None,
    display_name: str = "測定対象",
) -> MeasurementDefinitionCreateInput:
    return MeasurementDefinitionCreateInput(
        location_id=LOCATION_ID,
        display_name=display_name,
        category=Category.WATER,
        unit="m3",
        value_schema=ValueSchema.DECIMAL_STRING,
        multiplier=multiplier,
        decimal_places=decimal_places,
        warning_threshold=warning_threshold,
        rollover_value=rollover_value,
        calculation_method=CalculationMethod.DELTA,
        reading_cycle=ReadingCycle.DAILY_1X,
        purpose=Purpose.OPERATIONAL,
        generation_start_date=date(2026, 2, 1),
        initial_schedule_json=initial_schedule_json,
        initial_meter_id=initial_meter_id,
    )


def test_meter_master_service_uses_repository_and_protects_active_meters(
    file_database_repository: tuple,
) -> None:
    conn, repository = file_database_repository
    build_services(conn, repository)
    service = MeterMasterService(
        repository,
        transaction_factory=partial(transaction, conn),
        id_provider=lambda: "meter-unused",
    )
    assert [meter.meter_id for meter in service.list_meters()] == [METER_ID]
    assert service.get_usage(METER_ID).has_active_assignment is True
    with pytest.raises(MasterInUseError):
        service.deactivate_meter(METER_ID)
    with pytest.raises(MeterNotFoundError):
        service.get_usage("missing-meter")
    with pytest.raises(MeterValidationError):
        service.create_meter(
            MeterMasterCreateInput(
                location_id=LOCATION_ID,
                meter_name=" ",
                category=Category.WATER,
            )
        )

    created = service.create_meter(
        MeterMasterCreateInput(
            location_id=LOCATION_ID,
            meter_name="予備メーター",
            category=Category.WATER,
            meter_serial="S-001",
        )
    )
    updated = service.update_meter(
        created.meter_id,
        MeterMasterUpdateRequest(
            meter_name="更新メーター",
            category=Category.GAS,
            meter_serial=None,
        ),
    )
    assert updated.location_name == "試験場所"
    assert updated.category is Category.GAS

    service.deactivate_meter(created.meter_id)
    assert service.get_meter(created.meter_id) is not None
    assert created.meter_id not in {meter.meter_id for meter in service.list_meters()}
    assert created.meter_id in {meter.meter_id for meter in service.list_meters(show_inactive=True)}
    service.activate_meter(created.meter_id)
    active_meter = service.get_meter(created.meter_id)
    assert active_meter is not None
    assert active_meter.is_active is True
    with pytest.raises(MeterNotFoundError):
        service.deactivate_meter("missing-meter")


def test_meter_master_service_rolls_back_partial_repository_failure(
    file_database_repository: tuple,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    conn, repository = file_database_repository
    ids = iter(("meter-rollback", "new-location"))
    service = MeterMasterService(
        repository, transaction_factory=partial(transaction, conn), id_provider=lambda: next(ids)
    )
    original_create = repository.create_meter_master

    def create_then_fail(request: MeterMasterCreateRequest):
        original_create(request)
        raise RuntimeError("保存後に予期しない障害")

    monkeypatch.setattr(repository, "create_meter_master", create_then_fail)
    with pytest.raises(RuntimeError, match="予期しない障害"):
        service.create_meter(
            MeterMasterCreateInput(
                new_location_name="試験場所",
                meter_name="rollback",
                category=Category.WATER,
            )
        )
    assert repository.get_meter_master("meter-rollback") is None


def test_meter_and_location_ids_are_generated_and_renames_include_inactive_meters(
    file_database_repository: tuple,
) -> None:
    conn, repository = file_database_repository
    ids = iter(("meter-a", "location-a", "meter-b", "meter-c"))
    service = MeterMasterService(
        repository, transaction_factory=partial(transaction, conn), id_provider=lambda: next(ids)
    )
    first = service.create_meter(
        MeterMasterCreateInput(
            meter_name="メーター A", category=Category.WATER, new_location_name="旧名称"
        )
    )
    second = service.create_meter(
        MeterMasterCreateInput(
            meter_name="メーター B", category=Category.WATER, location_id=first.location_id
        )
    )
    assert (first.meter_id, first.location_id, second.meter_id) == (
        "meter-a",
        "location-a",
        "meter-b",
    )
    service.deactivate_meter(second.meter_id)
    service.rename_location(first.location_id, "  新名称  ")
    assert [(item.location_id, item.location_name) for item in service.list_locations()] == [
        ("location-a", "新名称")
    ]
    assert repository.get_meter_master(second.meter_id).location_name == "新名称"
    third = service.create_meter(
        MeterMasterCreateInput(
            meter_name="メーター C", category=Category.WATER, location_id=first.location_id
        )
    )
    assert third.location_name == "新名称"
    with pytest.raises(ValidationError, match="一致しません"):
        repository.create_meter_master(
            MeterMasterCreateRequest(
                meter_id="mismatch",
                location_id=first.location_id,
                location_name="旧名称",
                meter_name="不正",
                category=Category.WATER,
            )
        )


def test_meter_validation_and_nested_registration_roll_back(
    file_database_repository: tuple,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    conn, repository = file_database_repository
    build_services(conn, repository)
    service = MeterMasterService(
        repository, transaction_factory=partial(transaction, conn), id_provider=lambda: "new-meter"
    )
    for request in (
        MeterMasterCreateInput(meter_name="x", category=Category.WATER),
        MeterMasterCreateInput(meter_name="x", category=Category.WATER, location_id="missing"),
        MeterMasterCreateInput(
            meter_name="x",
            category=Category.WATER,
            location_id=LOCATION_ID,
            new_location_name="新規",
        ),
    ):
        with pytest.raises(MeterValidationError):
            service.create_meter(request)
    with pytest.raises(RuntimeError, match="交換失敗"), transaction(conn):
        service.create_meter_within_transaction(
            MeterMasterCreateInput(
                meter_name="交換用", category=Category.WATER, location_id=LOCATION_ID
            )
        )
        raise RuntimeError("交換失敗")
    assert repository.get_meter_master("new-meter") is None

    original_rename = repository.rename_meter_location

    def rename_then_fail(location_id: str, name: str) -> None:
        original_rename(location_id, name)
        raise RuntimeError("名称変更失敗")

    monkeypatch.setattr(repository, "rename_meter_location", rename_then_fail)
    with pytest.raises(RuntimeError, match="名称変更失敗"):
        service.rename_location(LOCATION_ID, "新名称")
    assert repository.get_meter_location(LOCATION_ID).location_name == "試験場所"

    duplicate = MeterMasterService(
        repository, transaction_factory=partial(transaction, conn), id_provider=lambda: METER_ID
    )
    with pytest.raises(MeterValidationError, match="既に存在"):
        duplicate.create_meter(
            MeterMasterCreateInput(
                meter_name="重複", category=Category.WATER, location_id=LOCATION_ID
            )
        )
    location_ids = iter(("new-meter", LOCATION_ID))
    duplicate_location = MeterMasterService(
        repository,
        transaction_factory=partial(transaction, conn),
        id_provider=lambda: next(location_ids),
    )
    with pytest.raises(MeterValidationError, match="location_id.*既に存在"):
        duplicate_location.create_meter(
            MeterMasterCreateInput(
                meter_name="重複場所",
                category=Category.WATER,
                new_location_name="別の場所",
            )
        )
    assert repository.get_meter_master("new-meter") is None


def test_measurement_definition_service_persists_initial_schedule_and_assignment_atomically(
    file_database_repository: tuple,
) -> None:
    conn, repository = file_database_repository
    build_services(conn, repository)
    service = _definition_service(conn, repository, id_provider=lambda: "definition-created")
    request = MeasurementDefinitionCreateInput(
        location_id=LOCATION_ID,
        display_name="追加測定対象",
        category=Category.WATER,
        unit="m3",
        value_schema=ValueSchema.DECIMAL_STRING,
        multiplier=Decimal("1.25"),
        decimal_places=2,
        warning_threshold=Decimal("100"),
        rollover_value=Decimal("1000"),
        calculation_method=CalculationMethod.DELTA,
        reading_cycle=ReadingCycle.DAILY_1X,
        purpose=Purpose.BILLING,
        generation_start_date=date(2026, 2, 1),
        initial_schedule_json='{"reading_sessions":[{"reading_session":"08:00","day_offset":0}]}',
        initial_meter_id=METER_ID,
    )

    created = service.create_measurement_definition(request)
    with pytest.raises(ValidationError, match="既に登録"):
        service.create_measurement_definition(request)

    definition_id = created.measurement_definition_id
    assert definition_id == "definition-created"
    history = repository.list_measurement_definition_schedule_history(definition_id)
    assert len(history) == 1
    assert history[0].effective_from_date == request.generation_start_date
    assignment = repository.get_meter_assignment(
        definition_id,
        TEST_NOW,
    )
    assert assignment is not None
    assert assignment.meter_id == METER_ID

    base_update = MeasurementDefinitionUpdateRequest(
        display_name="更新対象",
        category=Category.WATER,
        unit="m3",
        decimal_places=2,
        warning_threshold=None,
        purpose=Purpose.AUDIT,
    )
    updated = service.update_measurement_definition(
        definition_id,
        base_update,
    )
    assert updated.display_name == "更新対象"
    immutable_requests = [
        (replace(base_update, location_id="different-location"), "集計場所"),
        (replace(base_update, reading_cycle=ReadingCycle.WEEKLY), "検針周期"),
        (
            replace(base_update, value_schema=ValueSchema.POWER_FACTOR_PERCENT_PHASE),
            "値形式",
        ),
        (
            replace(base_update, calculation_method=CalculationMethod.RAW_ONLY),
            "計算方法",
        ),
    ]
    for update_request, label in immutable_requests:
        with pytest.raises(ValidationError, match=label):
            service.update_measurement_definition(
                definition_id,
                update_request,
            )
    with pytest.raises(ValidationError, match="集計目的"):
        service.update_measurement_definition(
            definition_id,
            replace(base_update, purpose="invalid"),
        )
    unchanged = service.get_measurement_definition(definition_id)
    assert unchanged is not None
    assert unchanged.location_id == LOCATION_ID


def test_measurement_definition_management_lists_current_assignment_and_threshold(
    file_database_repository: tuple,
) -> None:
    conn, repository = file_database_repository
    build_services(conn, repository)
    service = _definition_service(conn, repository)
    active = service.create_measurement_definition(
        _new_definition_request(
            initial_meter_id=METER_ID,
            warning_threshold=Decimal("123.45"),
        )
    )
    inactive = service.create_measurement_definition(_new_definition_request())
    service.deactivate_measurement_definition(inactive.measurement_definition_id)
    future = service.create_measurement_definition(
        replace(
            _new_definition_request(initial_meter_id=METER_ID),
            generation_start_date=date(2026, 2, 5),
        )
    )

    assert active.warning_threshold == Decimal("123.45")
    assert active.meter_id == METER_ID
    assert future.meter_id is None
    active_ids = {
        definition.measurement_definition_id
        for definition in service.list_measurement_definitions()
    }
    all_ids = {
        definition.measurement_definition_id
        for definition in service.list_measurement_definitions(show_inactive=True)
    }
    assert inactive.measurement_definition_id not in active_ids
    assert all_ids == active_ids | {inactive.measurement_definition_id}
    assert service.get_measurement_definition(active.measurement_definition_id) == active


def test_measurement_definition_rejects_overlapping_current_assignments(
    file_database_repository: tuple,
) -> None:
    conn, repository = file_database_repository
    build_services(conn, repository)
    service = _definition_service(conn, repository)
    with transaction(conn):
        repository.create_meter_master(
            MeterMasterCreateRequest(
                meter_id="meter-overlapping",
                location_id=LOCATION_ID,
                location_name="試験場所",
                meter_name="予備",
                category=Category.WATER,
            )
        )
        conn.execute(
            "INSERT INTO meter_assignments "
            "(measurement_definition_id, meter_id, effective_from, effective_to, multiplier) "
            "VALUES (?, ?, ?, NULL, '1')",
            (DEFINITION_ID, "meter-overlapping", "2026-02-02 00:00:00"),
        )

    with pytest.raises(MeterAssignmentResolutionError, match="複数"):
        service.get_measurement_definition(DEFINITION_ID)
    with pytest.raises(MeterAssignmentResolutionError, match="複数"):
        service.list_measurement_definitions()


def test_measurement_definition_service_validates_create_values(
    file_database_repository: tuple,
) -> None:
    conn, repository = file_database_repository
    service = _definition_service(conn, repository)
    base = _new_definition_request()
    invalid_requests = (
        replace(base, multiplier=Decimal("0")),
        replace(base, warning_threshold=Decimal("-1")),
        replace(base, rollover_value=Decimal("0")),
        replace(base, decimal_places=11),
        replace(base, display_name=" "),
        replace(base, initial_schedule_json="{"),
    )

    for request in invalid_requests:
        with pytest.raises(ValidationError):
            service.create_measurement_definition(request)


def test_measurement_definition_service_sets_initial_assignment_and_reactivation_floor(
    file_database_repository: tuple,
) -> None:
    conn, repository = file_database_repository
    build_services(conn, repository)
    service = _definition_service(conn, repository)
    created = service.create_measurement_definition(_new_definition_request(initial_meter_id=" "))
    definition_id = created.measurement_definition_id
    spare_meter = _meter_service(conn, repository).create_meter(
        MeterMasterCreateInput(
            location_id=LOCATION_ID,
            meter_name="無効化メーター",
            category=Category.WATER,
        )
    )
    _meter_service(conn, repository).deactivate_meter(spare_meter.meter_id)
    with pytest.raises(InactiveMasterError):
        create_plan(
            repository,
            "plan-inactive-meter",
            TEST_NOW,
            meter_id=spare_meter.meter_id,
            measurement_definition_id=definition_id,
        )
    with pytest.raises(InvalidTimezoneError):
        service.set_initial_meter_assignment(
            definition_id,
            METER_ID,
            datetime(2026, 2, 1),
        )
    with pytest.raises(InactiveMasterError):
        service.set_initial_meter_assignment(
            definition_id,
            spare_meter.meter_id,
            TEST_NOW,
        )
    with pytest.raises(ValidationError, match="物理メーターが見つかりません"):
        service.set_initial_meter_assignment(
            definition_id,
            "missing-meter",
            TEST_NOW,
        )
    service.set_initial_meter_assignment(definition_id, METER_ID, TEST_NOW)
    assignment = repository.get_meter_assignment(definition_id, TEST_NOW)
    assert assignment is not None
    assert assignment.meter_id == METER_ID

    create_plan(repository, "plan-no-resurrection", TEST_NOW.replace(day=5))
    service.deactivate_measurement_definition(DEFINITION_ID)
    deactivated_plan = repository.get_planned_reading("plan-no-resurrection")
    assert deactivated_plan is not None
    assert deactivated_plan.task_state.value == "superseded"
    service.activate_measurement_definition(DEFINITION_ID)
    activated = service.get_measurement_definition(DEFINITION_ID)
    assert activated is not None
    assert activated.generation_start_date == date(2026, 2, 4)
    restored_plan = repository.get_planned_reading("plan-no-resurrection")
    assert restored_plan is not None
    assert restored_plan.task_state.value == "superseded"


def test_measurement_definition_service_rolls_back_create_after_partial_repository_write(
    file_database_repository: tuple,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    conn, repository = file_database_repository
    build_services(conn, repository)
    service = _definition_service(conn, repository, id_provider=lambda: "definition-rollback")
    original_create = repository.create_measurement_definition

    def create_then_fail(request: MeasurementDefinitionCreateRequest) -> None:
        original_create(request)
        raise RuntimeError("初期履歴保存後の障害")

    monkeypatch.setattr(repository, "create_measurement_definition", create_then_fail)
    with pytest.raises(RuntimeError, match="初期履歴保存後の障害"):
        service.create_measurement_definition(_new_definition_request(initial_meter_id=METER_ID))
    assert repository.get_measurement_definition("definition-rollback") is None
    assert repository.list_measurement_definition_schedule_history("definition-rollback") == []
    assert repository.list_meter_assignments("definition-rollback") == []


def test_measurement_definition_service_rolls_back_when_readback_fails(
    file_database_repository: tuple,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    conn, repository = file_database_repository
    service = _definition_service(conn, repository, id_provider=lambda: "definition-no-readback")
    original_get = repository.get_measurement_definition
    monkeypatch.setattr(repository, "get_measurement_definition", lambda definition_id: None)

    with pytest.raises(ValidationError, match="登録した測定定義"):
        service.create_measurement_definition(_new_definition_request())

    assert original_get("definition-no-readback") is None


def test_measurement_definition_service_update_readback_failure_rolls_back(
    file_database_repository: tuple,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    conn, repository = file_database_repository
    build_services(conn, repository)
    service = _definition_service(conn, repository)
    original_get = repository.get_measurement_definition
    read_count = 0

    def missing_after_update(measurement_definition_id: str):
        nonlocal read_count
        read_count += 1
        if read_count == 1:
            return original_get(measurement_definition_id)
        return None

    monkeypatch.setattr(repository, "get_measurement_definition", missing_after_update)
    update_request = MeasurementDefinitionUpdateRequest(
        display_name="更新後",
        category=Category.WATER,
        unit="m3",
        decimal_places=2,
        warning_threshold=None,
        purpose=Purpose.AUDIT,
    )
    with pytest.raises(ValidationError, match="更新した測定定義"):
        service.update_measurement_definition(DEFINITION_ID, update_request)

    stored = original_get(DEFINITION_ID)
    assert stored is not None
    assert stored.multiplier == Decimal("1.0")


def test_definition_deactivation_failure_rolls_back_open_slot_updates(
    file_database_repository: tuple,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    conn, repository = file_database_repository
    build_services(conn, repository)
    create_plan(repository, "plan-future-open", TEST_NOW.replace(day=5))
    service = _definition_service(conn, repository)
    original_deactivate = repository.set_measurement_definition_active

    def deactivate_then_fail(
        measurement_definition_id: str,
        is_active: bool,
        *,
        generation_start_date: date | None = None,
    ) -> None:
        original_deactivate(
            measurement_definition_id,
            is_active,
            generation_start_date=generation_start_date,
        )
        raise RuntimeError("open枠更新後の障害")

    monkeypatch.setattr(repository, "set_measurement_definition_active", deactivate_then_fail)
    with pytest.raises(RuntimeError, match="open枠更新後の障害"):
        service.deactivate_measurement_definition(DEFINITION_ID)

    definition = repository.get_measurement_definition(DEFINITION_ID)
    slot = repository.get_planned_reading("plan-future-open")
    assert definition is not None
    assert definition.is_active is True
    assert slot is not None
    assert slot.task_state.value == "open"


def test_staff_service_crud_and_inactive_staff_rejection(
    file_database_repository: tuple,
) -> None:
    conn, repository = file_database_repository
    service = StaffService(repository, transaction_factory=partial(transaction, conn))

    staff = service.create_staff(StaffCreateRequest("  担当者A  "))
    assert staff.staff_id == "1"
    assert staff.display_name == "担当者A"
    assert service.require_active_staff(staff.staff_id) == staff
    updated = service.update_staff(staff.staff_id, StaffUpdateRequest("担当者B"))
    assert updated.display_name == "担当者B"
    service.deactivate_staff(staff.staff_id)
    assert service.list_staff() == []
    with pytest.raises(InactiveMasterError):
        service.require_active_staff(staff.staff_id)
    with pytest.raises(StaffValidationError, match="既に登録"):
        service.create_staff(StaffCreateRequest("担当者B"))
    service.activate_staff(staff.staff_id)
    assert service.require_active_staff(staff.staff_id).is_active is True
    with pytest.raises(StaffValidationError, match="既に登録"):
        service.create_staff(StaffCreateRequest("担当者B"))
    another = service.create_staff(StaffCreateRequest("担当者C"))
    assert another.staff_id == "2"
    with pytest.raises(StaffValidationError, match="既に登録"):
        service.update_staff(another.staff_id, StaffUpdateRequest("担当者B"))
    with pytest.raises(StaffValidationError, match="整数"):
        service.get_staff("invalid")
    with pytest.raises(StaffValidationError, match="正の整数"):
        service.get_staff("0")
    with pytest.raises(StaffValidationError, match="担当者名"):
        service.update_staff(staff.staff_id, StaffUpdateRequest("  "))
    with pytest.raises(StaffNotFoundError):
        service.update_staff("99", StaffUpdateRequest("担当者"))
    with pytest.raises(StaffNotFoundError):
        service.deactivate_staff("99")
    with pytest.raises(StaffNotFoundError):
        service.activate_staff("99")


def test_auto_closed_slot_does_not_block_meter_deactivation(
    file_database_repository: tuple,
) -> None:
    conn, repository = file_database_repository
    build_services(conn, repository)
    create_plan(repository, "auto-closed-plan", TEST_NOW)
    repository.auto_close_open_planned_readings(
        date(2026, 2, 4),
        closed_at=TEST_NOW,
        close_reason=PlannedReadingCloseReason.CUTOFF_ELAPSED,
    )
    definition_service = _definition_service(conn, repository)
    definition_service.deactivate_measurement_definition(DEFINITION_ID)

    _meter_service(conn, repository).deactivate_meter(METER_ID)

    assert repository.get_meter_master(METER_ID).is_active is False


def test_open_slot_protects_meter_after_definition_is_inactive(
    file_database_repository: tuple,
) -> None:
    conn, repository = file_database_repository
    build_services(conn, repository)
    create_plan(repository, "plan-past-open", datetime(2026, 2, 3, 4, tzinfo=UTC))
    _definition_service(conn, repository).deactivate_measurement_definition(DEFINITION_ID)

    usage = repository.get_meter_master_usage(METER_ID)
    assert usage.has_active_assignment is False
    assert usage.has_open_planned_readings is True
    with pytest.raises(MasterInUseError):
        _meter_service(conn, repository).deactivate_meter(METER_ID)
