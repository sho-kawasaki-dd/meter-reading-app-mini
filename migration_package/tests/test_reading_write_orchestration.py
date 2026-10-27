# pyright: reportMissingImports=false

from __future__ import annotations

import sys
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path

sys.path.append(str(Path(__file__).resolve().parents[1] / "src"))

from meter_reading_mini.core.contract_models import CalculationMethod, ResultStatus, ValueSchema
from meter_reading_mini.core.meter_reading_sync_repositories import (
    HistoricalMeasuredReadingRecord,
    MeasurementDefinitionRecord,
    PlannedReadingRecord,
    ReadingUpsertRequest,
    UpsertDisposition,
    UpsertOutcome,
)
from meter_reading_mini.core.reading_write_orchestration import ReadingWriteOrchestrationService
from meter_reading_mini.core.usage_delta_calculation import UsageDeltaCalculationResult


@dataclass
class FakeOrchestrationRepository:
    measurement_definition: MeasurementDefinitionRecord | None = None
    planned_reading: PlannedReadingRecord | None = None
    previous_measured_reading: HistoricalMeasuredReadingRecord | None = None
    persisted_requests: list[ReadingUpsertRequest] = field(default_factory=list)
    recalculated_next_readings: list[tuple[str, datetime]] = field(default_factory=list)
    call_log: list[str] = field(default_factory=list)

    def get_measurement_definition(
        self, measurement_definition_id: str
    ) -> MeasurementDefinitionRecord | None:
        self.call_log.append(f"measurement:{measurement_definition_id}")
        return self.measurement_definition

    def get_planned_reading(self, planned_reading_id: str) -> PlannedReadingRecord | None:
        self.call_log.append(f"planned:{planned_reading_id}")
        return self.planned_reading

    def get_latest_measured_reading(
        self,
        measurement_definition_id: str,
        *,
        before_scheduled_at: datetime,
        before_planned_reading_id: str | None = None,
        excluding_planned_reading_id: str | None = None,
    ) -> HistoricalMeasuredReadingRecord | None:
        self.call_log.append(f"history:{measurement_definition_id}:{excluding_planned_reading_id}")
        return self.previous_measured_reading

    def upsert_reading(self, request: ReadingUpsertRequest) -> UpsertOutcome:
        self.call_log.append(f"persist:{request.planned_reading_id}")
        self.persisted_requests.append(request)
        return UpsertOutcome(UpsertDisposition.INSERTED)

    def recalculate_following_measured_reading(
        self,
        measurement_definition_id: str,
        *,
        after_scheduled_at: datetime,
        after_planned_reading_id: str | None = None,
    ) -> int:
        self.call_log.append(f"recalc:{measurement_definition_id}:{after_planned_reading_id}")
        self.recalculated_next_readings.append((measurement_definition_id, after_scheduled_at))
        return 1


class FakeCalculationService:
    def __init__(self, result: UsageDeltaCalculationResult) -> None:
        self.result = result
        self.calls: list[
            tuple[
                MeasurementDefinitionRecord,
                ReadingUpsertRequest,
                HistoricalMeasuredReadingRecord | None,
            ]
        ] = []

    def calculate(
        self,
        measurement_definition,
        reading_request,
        previous_measured_reading,
        replacement_boundary=None,
    ):
        self.calls.append((measurement_definition, reading_request, previous_measured_reading))
        return self.result


def build_request() -> ReadingUpsertRequest:
    return ReadingUpsertRequest(
        planned_reading_id="plan-001",
        measurement_definition_id="md-001",
        result_status=ResultStatus.MEASURED,
        recorded_at=datetime(2026, 4, 1, 8, 0, tzinfo=UTC),
        client_updated_at=datetime(2026, 4, 1, 8, 5, tzinfo=UTC),
        raw_reading=Decimal("12.5"),
        structured_payload_json=None,
        updated_by="android-01",
        signature="sig-001",
        note=None,
    )


def test_orchestration_service_loads_history_runs_calculator_and_persists_result() -> None:
    repository = FakeOrchestrationRepository(
        measurement_definition=MeasurementDefinitionRecord(
            measurement_definition_id="md-001",
            value_schema=ValueSchema.DECIMAL_STRING,
            calculation_method=CalculationMethod.DELTA,
            multiplier=Decimal("1.0"),
        ),
        planned_reading=PlannedReadingRecord(
            planned_reading_id="plan-001",
            measurement_definition_id="md-001",
            meter_id="meter-001",
            scheduled_at=datetime(2026, 4, 1, 8, 0, tzinfo=UTC),
        ),
        previous_measured_reading=HistoricalMeasuredReadingRecord(
            planned_reading_id="plan-prev",
            raw_reading=Decimal("10.0"),
            recorded_at=datetime(2026, 4, 1, 7, 0, tzinfo=UTC),
        ),
    )
    calculator = FakeCalculationService(
        UsageDeltaCalculationResult(last_reading=Decimal("10.0"), usage_delta=Decimal("2.5"))
    )
    service = ReadingWriteOrchestrationService(
        measurement_definition_repository=repository,
        historical_reading_repository=repository,
        persistence_repository=repository,
        planned_reading_repository=repository,
        calculation_service=calculator,
    )

    outcome = service.upsert_reading(build_request())

    assert outcome.disposition is UpsertDisposition.INSERTED
    assert repository.call_log == [
        "measurement:md-001",
        "planned:plan-001",
        "history:md-001:plan-001",
        "persist:plan-001",
        "recalc:md-001:plan-001",
    ]
    assert calculator.calls[0][2] is repository.previous_measured_reading
    assert repository.persisted_requests[0].last_reading == Decimal("10.0")
    assert repository.persisted_requests[0].usage_delta == Decimal("2.5")


def test_orchestration_service_skips_following_recalc_for_non_measured_reading() -> None:
    repository = FakeOrchestrationRepository(
        measurement_definition=MeasurementDefinitionRecord(
            measurement_definition_id="md-001",
            value_schema=ValueSchema.DECIMAL_STRING,
            calculation_method=CalculationMethod.DELTA,
            multiplier=Decimal("1.0"),
        ),
        planned_reading=PlannedReadingRecord(
            planned_reading_id="plan-001",
            measurement_definition_id="md-001",
            meter_id="meter-001",
            scheduled_at=datetime(2026, 4, 1, 8, 0, tzinfo=UTC),
        ),
    )
    calculator = FakeCalculationService(
        UsageDeltaCalculationResult(last_reading=None, usage_delta=None)
    )
    service = ReadingWriteOrchestrationService(
        measurement_definition_repository=repository,
        historical_reading_repository=repository,
        persistence_repository=repository,
        planned_reading_repository=repository,
        calculation_service=calculator,
    )

    request = replace(build_request(), result_status=ResultStatus.SKIPPED, raw_reading=None)
    outcome = service.upsert_reading(request)

    assert outcome.disposition is UpsertDisposition.INSERTED
    assert not any(entry.startswith("recalc:") for entry in repository.call_log)
