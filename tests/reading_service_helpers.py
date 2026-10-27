from __future__ import annotations

import sqlite3
from datetime import UTC, date, datetime
from decimal import Decimal
from functools import partial

from meter_reading_mini.core.contract_models import (
    CalculationMethod,
    Category,
    MeterReadingSubmission,
    MeterReadingSyncRequest,
    Purpose,
    ReadingCycle,
    ReadingSession,
    ValueSchema,
)
from meter_reading_mini.core.meter_reading_sync import MeterReadingSyncService
from meter_reading_mini.core.meter_reading_sync_repositories import (
    MeasurementDefinitionCreateRequest,
    MeterMasterCreateRequest,
    PlannedReadingCreateRequest,
    ReadingConfirmationContext,
    StaffCreateRequest,
)
from meter_reading_mini.core.reading_write_orchestration import (
    ReadingWriteOrchestrationService,
)
from meter_reading_mini.db.connection import transaction
from meter_reading_mini.db.repositories import SQLiteRepository

TEST_NOW = datetime(2026, 2, 4, 3, 0, tzinfo=UTC)
LOCATION_ID = "test-location"
DEFINITION_ID = "test-definition"
METER_ID = "test-meter"
STAFF_ID = "1"
SHEET_ID = "a" * 32


def build_services(
    conn: sqlite3.Connection,
    repository: SQLiteRepository,
    *,
    multiplier: Decimal = Decimal("1"),
    decimal_places: int = 2,
    rollover_value: Decimal | None = None,
) -> tuple[ReadingWriteOrchestrationService, MeterReadingSyncService]:
    with transaction(conn):
        repository.create_meter_master(
            MeterMasterCreateRequest(
                meter_id=METER_ID,
                location_id=LOCATION_ID,
                location_name="試験場所",
                meter_name="試験メーター",
                category=Category.WATER,
            )
        )
        repository.create_measurement_definition(
            MeasurementDefinitionCreateRequest(
                measurement_definition_id=DEFINITION_ID,
                location_id=LOCATION_ID,
                display_name="試験測定対象",
                category=Category.WATER,
                unit="m3",
                value_schema=ValueSchema.DECIMAL_STRING,
                multiplier=multiplier,
                decimal_places=decimal_places,
                warning_threshold=None,
                rollover_value=rollover_value,
                calculation_method=CalculationMethod.DELTA,
                reading_cycle=ReadingCycle.DAILY_1X,
                purpose=Purpose.OPERATIONAL,
                generation_start_date=date(2026, 2, 1),
                initial_schedule_json=None,
                initial_meter_id=METER_ID,
            )
        )
        repository.create_staff(StaffCreateRequest(STAFF_ID, "試験担当"))

    transaction_factory = partial(transaction, conn)
    writer = ReadingWriteOrchestrationService(
        repository,
        repository,
        repository,
        planned_reading_repository=repository,
        boundary_repository=repository,
        boundary_maintenance_repository=repository,
        staff_repository=repository,
        transaction_factory=transaction_factory,
        now_provider=lambda: TEST_NOW,
    )
    sync_service = MeterReadingSyncService(
        conn,
        repository,
        repository,
        writer,
        repository,
        repository,
        replacement_reflection_repository=repository,
    )
    return writer, sync_service


def create_plan(
    repository: SQLiteRepository,
    planned_reading_id: str,
    scheduled_at: datetime,
    *,
    meter_id: str = METER_ID,
    measurement_definition_id: str = DEFINITION_ID,
) -> None:
    repository.create_planned_reading_if_missing(
        PlannedReadingCreateRequest(
            planned_reading_id=planned_reading_id,
            measurement_definition_id=measurement_definition_id,
            meter_id=meter_id,
            location_id=LOCATION_ID,
            business_date=scheduled_at.date(),
            release_date=scheduled_at.date(),
            reading_session=ReadingSession.SESSION_0800,
            scheduled_at=scheduled_at,
        )
    )


def mobile_submission(
    planned_reading_id: str,
    raw_reading: str,
    *,
    recorded_at: str = "2026-02-04T08:00:00+09:00",
    client_updated_at: str = "2026-02-04T08:05:00+09:00",
) -> MeterReadingSubmission:
    return MeterReadingSubmission.model_validate(
        {
            "planned_reading_id": planned_reading_id,
            "result_status": "measured",
            "raw_reading": raw_reading,
            "recorded_at": recorded_at,
            "client_updated_at": client_updated_at,
        }
    )


def sync_request(
    *readings: MeterReadingSubmission,
) -> MeterReadingSyncRequest:
    return MeterReadingSyncRequest(reading_items=list(readings), replacement_items=[])


def confirmation_context(
    *, staff_id: str = STAFF_ID, sheet_id: str = SHEET_ID
) -> ReadingConfirmationContext:
    return ReadingConfirmationContext(
        sheet_id=sheet_id,
        staff_id=staff_id,
        staff_name="入力側の名前は採用しない",
    )
