"""検針結果の計算・監査保存・後続再計算を束ねる service。"""

from __future__ import annotations

import logging
from collections.abc import Callable
from contextlib import AbstractContextManager
from dataclasses import replace
from datetime import datetime, timedelta

from pydantic import ValidationError as PydanticValidationError

from meter_reading_mini.core.contract_models import (
    MeterReadingSubmission,
    resolve_meter_reading_submission,
)
from meter_reading_mini.core.errors import (
    DomainError,
    InactiveMasterError,
    SupersededPlannedReadingError,
    ValidationError,
)
from meter_reading_mini.core.meter_reading_sync_repositories import (
    PC_DIRECT_DEVICE_ID,
    CalculatedUsageUpdate,
    HistoricalMeasuredReadingLookupRepository,
    HistoricalMeasuredReadingRecord,
    ManualReadingInput,
    MeasurementDefinitionLookupRepository,
    MeasurementDefinitionRecord,
    PlannedReadingLookupRepository,
    PlannedReadingRecord,
    PlannedReadingTaskState,
    ReadingPersistenceRepository,
    ReadingUpsertRequest,
    ReplacementBoundaryCalculationRecord,
    ReplacementBoundaryCalculationUpdate,
    ReplacementBoundaryLookupRepository,
    ReplacementBoundaryMaintenanceRepository,
    ReplacementBoundaryRecord,
    ReplacementUpsertRequest,
    ResultSource,
    StaffRepository,
    UpsertOutcome,
)
from meter_reading_mini.core.mobile_reading import validate_note
from meter_reading_mini.core.previous_reading import resolve_calculation_context
from meter_reading_mini.core.reading_signature import calculate_meter_reading_signature
from meter_reading_mini.core.timeutil import parse_utc, utc_now, utc_now_str
from meter_reading_mini.core.usage_delta_calculation import UsageDeltaCalculationService

logger = logging.getLogger(__name__)

TransactionFactory = Callable[[], AbstractContextManager[object]]


class ReadingWriteOrchestrationError(DomainError):
    """検針結果書込の業務処理を完了できなかったことを表す。"""


class ReadingWriteOrchestrationService:
    """PC 直接入力・スマホ取込で共有する計算済み書込経路。"""

    def __init__(
        self,
        measurement_definition_repository: MeasurementDefinitionLookupRepository,
        historical_reading_repository: HistoricalMeasuredReadingLookupRepository,
        persistence_repository: ReadingPersistenceRepository,
        *,
        planned_reading_repository: PlannedReadingLookupRepository,
        boundary_repository: ReplacementBoundaryLookupRepository,
        boundary_maintenance_repository: ReplacementBoundaryMaintenanceRepository,
        staff_repository: StaffRepository | None = None,
        transaction_factory: TransactionFactory | None = None,
        now_provider: Callable[[], datetime] = utc_now,
        calculation_service: UsageDeltaCalculationService | None = None,
    ) -> None:
        self._measurement_definition_repository = measurement_definition_repository
        self._historical_reading_repository = historical_reading_repository
        self._persistence_repository = persistence_repository
        self._planned_reading_repository = planned_reading_repository
        self._boundary_repository = boundary_repository
        self._boundary_maintenance_repository = boundary_maintenance_repository
        self._staff_repository = staff_repository
        self._transaction_factory = transaction_factory
        self._now_provider = now_provider
        self._calculation_service = calculation_service or UsageDeltaCalculationService()

    def upsert_reading(self, request: ReadingUpsertRequest) -> UpsertOutcome:
        """署名済み要求を計算・保存し、交換境界と後続実測値を再計算する。"""

        measurement_definition = self._get_measurement_definition(request.measurement_definition_id)
        planned_reading = self._get_planned_reading(request.planned_reading_id)
        if planned_reading.measurement_definition_id != request.measurement_definition_id:
            raise ReadingWriteOrchestrationError("予定枠と検針結果の測定定義が一致しません。")
        if planned_reading.task_state is PlannedReadingTaskState.SUPERSEDED:
            raise SupersededPlannedReadingError(planned_reading.planned_reading_id)

        scheduled_at = planned_reading.scheduled_at or request.recorded_at
        previous, boundary = self._get_calculation_context(
            planned_reading,
            excluding_planned_reading_id=request.planned_reading_id,
        )
        calculation = self._calculation_service.calculate(
            measurement_definition,
            request,
            previous,
            boundary,
        )
        calculated_request = replace(
            request,
            last_reading=calculation.last_reading,
            usage_delta=calculation.usage_delta,
        )
        outcome = self._persistence_repository.upsert_reading(calculated_request)
        if not outcome.accepted:
            return outcome

        self._refresh_old_meter_boundaries(
            measurement_definition,
            planned_reading,
        )
        self._recalculate_following_measured_readings(
            measurement_definition,
            planned_reading,
            after_scheduled_at=scheduled_at,
        )
        logger.debug(
            "usage_delta for %s: %s (last_reading=%s)",
            request.planned_reading_id,
            calculation.usage_delta,
            calculation.last_reading,
        )
        return outcome

    def save_manual_reading(
        self,
        reading: ManualReadingInput,
        *,
        staff_id: str,
    ) -> UpsertOutcome:
        """PC 直接入力を有効な担当者で署名し、独立トランザクションで確定する。"""

        if self._transaction_factory is None or self._staff_repository is None:
            raise RuntimeError("PC 直接入力にはトランザクションと担当者 Repository が必要です。")

        with self._transaction_factory():
            staff = self._staff_repository.get_staff(staff_id)
            if staff is None or not staff.is_active:
                raise InactiveMasterError(f"有効な担当者を指定してください: {staff_id}")
            updated_by = staff.display_name.strip()
            if not updated_by:
                raise InactiveMasterError("担当者名が空の担当者は指定できません。")

            planned_reading = self._get_planned_reading(reading.planned_reading_id)
            measurement_definition = self._get_measurement_definition(
                planned_reading.measurement_definition_id
            )
            try:
                submission = MeterReadingSubmission(
                    planned_reading_id=reading.planned_reading_id,
                    result_status=reading.result_status,
                    raw_reading=reading.raw_reading,
                    structured_payload=reading.structured_payload_json,
                    recorded_at=reading.recorded_at,
                    client_updated_at=reading.recorded_at,
                    note=validate_note(reading.note),
                )
                resolved = resolve_meter_reading_submission(
                    submission,
                    measurement_definition_id=measurement_definition.measurement_definition_id,
                    value_schema=measurement_definition.value_schema,
                    decimal_places=measurement_definition.decimal_places,
                    rollover_value=measurement_definition.rollover_value,
                )
            except PydanticValidationError as exc:
                raise ValidationError(_format_validation_error(exc)) from exc

            recorded_at = parse_utc(utc_now_str(resolved.recorded_at))
            current_client_time = parse_utc(utc_now_str(self._now_provider()))
            existing_client_time = self._persistence_repository.get_reading_client_updated_at(
                reading.planned_reading_id
            )
            next_client_time = (
                max(current_client_time, existing_client_time + timedelta(seconds=1))
                if existing_client_time is not None
                else current_client_time
            )
            signature = calculate_meter_reading_signature(
                planned_reading_id=resolved.planned_reading_id,
                result_status=resolved.result_status,
                raw_reading=resolved.raw_reading,
                structured_payload_json=resolved.structured_payload,
                recorded_at=recorded_at,
                device_id=PC_DIRECT_DEVICE_ID,
            )
            request = ReadingUpsertRequest(
                planned_reading_id=resolved.planned_reading_id,
                measurement_definition_id=resolved.measurement_definition_id,
                result_status=resolved.result_status,
                recorded_at=recorded_at,
                client_updated_at=next_client_time,
                raw_reading=resolved.raw_reading,
                structured_payload_json=resolved.structured_payload,
                updated_by=updated_by,
                signature=signature,
                note=resolved.note,
                device_id=PC_DIRECT_DEVICE_ID,
                result_source=ResultSource.MANUAL,
            )
            return self.upsert_reading(request)

    def recalculate_after_replacement(self, request: ReplacementUpsertRequest) -> None:
        """交換確定後に境界と遅延到着済みの新メーター実測値を再計算する。"""

        boundaries = self._boundary_maintenance_repository.list_replacement_boundaries_for_event(
            request.replacement_event_id
        )
        for boundary in boundaries:
            definition = self._get_measurement_definition(boundary.measurement_definition_id)
            self._refresh_boundary(definition, boundary)
            measured_readings = (
                self._historical_reading_repository.list_measured_readings_on_or_after(
                    boundary.measurement_definition_id,
                    boundary.new_meter_id,
                    from_scheduled_at=boundary.replaced_at,
                )
            )
            for measured_reading in measured_readings:
                planned = self._get_planned_reading(measured_reading.planned_reading_id)
                previous, replacement_boundary = self._get_calculation_context(
                    planned,
                    excluding_planned_reading_id=measured_reading.planned_reading_id,
                )
                result = self._calculation_service.calculate_measured_reading(
                    definition,
                    measured_reading.raw_reading,
                    previous,
                    replacement_boundary,
                )
                self._persistence_repository.save_calculated_usage(
                    CalculatedUsageUpdate(
                        planned_reading_id=measured_reading.planned_reading_id,
                        last_reading=result.last_reading,
                        usage_delta=result.usage_delta,
                    )
                )

    def _get_calculation_context(
        self,
        planned_reading: PlannedReadingRecord,
        *,
        excluding_planned_reading_id: str,
    ) -> tuple[HistoricalMeasuredReadingRecord | None, ReplacementBoundaryRecord | None]:
        scheduled_at = planned_reading.scheduled_at
        if scheduled_at is None:
            raise ReadingWriteOrchestrationError(
                f"予定枠の予定日時がありません: {planned_reading.planned_reading_id}"
            )
        context = resolve_calculation_context(
            self._historical_reading_repository,
            self._boundary_repository,
            planned_reading,
            excluding_planned_reading_id=excluding_planned_reading_id,
        )
        return context.previous, context.boundary

    def _recalculate_following_measured_readings(
        self,
        measurement_definition: MeasurementDefinitionRecord,
        planned_reading: PlannedReadingRecord,
        *,
        after_scheduled_at: datetime,
    ) -> None:
        following = self._historical_reading_repository.list_following_measured_readings(
            measurement_definition.measurement_definition_id,
            planned_reading.meter_id,
            after_scheduled_at=after_scheduled_at,
            after_planned_reading_id=planned_reading.planned_reading_id,
        )
        for measured_reading in following:
            current_planned = self._get_planned_reading(measured_reading.planned_reading_id)
            previous, boundary = self._get_calculation_context(
                current_planned,
                excluding_planned_reading_id=measured_reading.planned_reading_id,
            )
            result = self._calculation_service.calculate_measured_reading(
                measurement_definition,
                measured_reading.raw_reading,
                previous,
                boundary,
            )
            self._persistence_repository.save_calculated_usage(
                CalculatedUsageUpdate(
                    planned_reading_id=measured_reading.planned_reading_id,
                    last_reading=result.last_reading,
                    usage_delta=result.usage_delta,
                )
            )

    def _refresh_old_meter_boundaries(
        self,
        measurement_definition: MeasurementDefinitionRecord,
        planned_reading: PlannedReadingRecord,
    ) -> None:
        boundaries = (
            self._boundary_maintenance_repository.list_replacement_boundaries_for_old_meter(
                measurement_definition.measurement_definition_id,
                planned_reading.meter_id,
            )
        )
        for boundary in boundaries:
            self._refresh_boundary(measurement_definition, boundary)

    def _refresh_boundary(
        self,
        measurement_definition: MeasurementDefinitionRecord,
        boundary: ReplacementBoundaryCalculationRecord,
    ) -> None:
        previous = self._historical_reading_repository.get_latest_measured_reading(
            boundary.measurement_definition_id,
            boundary.old_meter_id,
            before_scheduled_at=boundary.replaced_at,
        )
        usage_delta = None
        if boundary.old_final_reading is not None:
            result = self._calculation_service.calculate_measured_reading(
                measurement_definition,
                boundary.old_final_reading,
                previous,
            )
            usage_delta = result.usage_delta
        self._boundary_maintenance_repository.update_replacement_boundary_calculation(
            ReplacementBoundaryCalculationUpdate(
                replacement_event_id=boundary.replacement_event_id,
                measurement_definition_id=boundary.measurement_definition_id,
                previous_measured_reading=previous.raw_reading if previous is not None else None,
                previous_measured_planned_reading_id=(
                    previous.planned_reading_id if previous is not None else None
                ),
                old_meter_usage_delta=usage_delta,
            )
        )

    def _get_measurement_definition(
        self, measurement_definition_id: str
    ) -> MeasurementDefinitionRecord:
        definition = self._measurement_definition_repository.get_measurement_definition(
            measurement_definition_id
        )
        if definition is None:
            raise ReadingWriteOrchestrationError(
                f"測定定義が見つかりません: {measurement_definition_id}"
            )
        return definition

    def _get_planned_reading(self, planned_reading_id: str) -> PlannedReadingRecord:
        planned = self._planned_reading_repository.get_planned_reading(planned_reading_id)
        if planned is None:
            raise ReadingWriteOrchestrationError(f"予定枠が見つかりません: {planned_reading_id}")
        return planned


def _format_validation_error(exc: PydanticValidationError) -> str:
    issues = exc.errors(include_url=False)
    if not issues:
        return str(exc)
    return "; ".join(issue["msg"] for issue in issues)


__all__ = [
    "ReadingWriteOrchestrationError",
    "ReadingWriteOrchestrationService",
]
