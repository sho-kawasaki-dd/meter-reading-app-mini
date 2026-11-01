"""直接入力の検証と使用量を DB に保存せずプレビューする。"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, replace
from datetime import datetime
from decimal import Decimal

from meter_reading_mini.core.assignment_calculation import (
    calculate_boundary_usage,
    definition_with_assignment_conditions,
    resolve_planned_assignment,
    resolve_planned_calculation_context,
    validate_persisted_assignment_reading,
)
from meter_reading_mini.core.contract_models import CalculationMethod, ResultStatus
from meter_reading_mini.core.errors import (
    NegativeUsageDeltaError,
    SupersededPlannedReadingError,
    ValidationError,
)
from meter_reading_mini.core.manual_reading_validation import resolve_manual_reading
from meter_reading_mini.core.meter_reading_sync_repositories import (
    AssignmentCalculationLookupRepository,
    HistoricalMeasuredReadingLookupRepository,
    HistoricalMeasuredReadingRecord,
    ManualReadingInput,
    MeasurementDefinitionLookupRepository,
    MeasurementDefinitionRecord,
    PlannedReadingLookupRepository,
    PlannedReadingRecord,
    PlannedReadingTaskState,
    ReplacementBoundaryLookupRepository,
    ReplacementBoundaryMaintenanceRepository,
    ReplacementBoundaryRecord,
)
from meter_reading_mini.core.previous_reading import (
    PreviousReadingSource,
    describe_initial_previous_reading,
)
from meter_reading_mini.core.timeutil import utc_now
from meter_reading_mini.core.usage_delta_calculation import UsageDeltaCalculationService


@dataclass(frozen=True, slots=True)
class ManualReadingPreview:
    previous_reading: Decimal | None
    previous_source: PreviousReadingSource
    usage_delta: Decimal | None
    rollover_applied: bool
    warning_threshold_exceeded: bool
    error: str | None

    @property
    def can_save(self) -> bool:
        return self.error is None


class ReadingPreviewService:
    def __init__(
        self,
        planned_repository: PlannedReadingLookupRepository,
        definition_repository: MeasurementDefinitionLookupRepository,
        assignment_repository: AssignmentCalculationLookupRepository,
        historical_repository: HistoricalMeasuredReadingLookupRepository,
        boundary_repository: ReplacementBoundaryLookupRepository,
        boundary_maintenance_repository: ReplacementBoundaryMaintenanceRepository,
        *,
        now_provider: Callable[[], datetime] = utc_now,
        calculation_service: UsageDeltaCalculationService | None = None,
    ) -> None:
        self._planned_repository = planned_repository
        self._definition_repository = definition_repository
        self._assignment_repository = assignment_repository
        self._historical_repository = historical_repository
        self._boundary_repository = boundary_repository
        self._boundary_maintenance_repository = boundary_maintenance_repository
        self._now_provider = now_provider
        self._calculation_service = calculation_service or UsageDeltaCalculationService()

    def preview_manual_reading(self, reading: ManualReadingInput) -> ManualReadingPreview:
        """保存せず検証する。入力変更・検証失敗時は旧プレビューと確定用入力を破棄する。

        確定には正常に確認した入力を渡し、保存側で現在の DB 状態を再検証する。
        """
        planned = self._planned_repository.get_planned_reading(reading.planned_reading_id)
        if planned is None:
            raise ValidationError(f"予定枠が見つかりません: {reading.planned_reading_id}")
        if planned.task_state is PlannedReadingTaskState.SUPERSEDED:
            raise SupersededPlannedReadingError(planned.planned_reading_id)
        definition = self._definition_repository.get_measurement_definition(
            planned.measurement_definition_id
        )
        if definition is None:
            raise ValidationError(f"測定定義が見つかりません: {planned.measurement_definition_id}")
        assignment = resolve_planned_assignment(self._assignment_repository, planned)
        resolved = resolve_manual_reading(reading, definition, assignment, now=self._now_provider())
        _, context = resolve_planned_calculation_context(
            self._assignment_repository,
            self._historical_repository,
            self._boundary_repository,
            planned,
            definition,
        )
        previous = describe_initial_previous_reading(context)
        usage_delta: Decimal | None = None
        rollover_applied = False
        error: str | None = None
        if resolved.result_status is ResultStatus.MEASURED and resolved.raw_reading is not None:
            try:
                calculation = self._calculation_service.calculate_measured_reading(
                    definition_with_assignment_conditions(definition, assignment),
                    resolved.raw_reading,
                    context.previous,
                    context.boundary,
                )
                usage_delta = calculation.usage_delta
                rollover_applied = (
                    definition.calculation_method is not CalculationMethod.RAW_ONLY
                    and calculation.last_reading is not None
                    and resolved.raw_reading < calculation.last_reading
                    and assignment.rollover_value is not None
                )
                virtual_history = _VirtualHistoricalReadings(
                    self._historical_repository,
                    {
                        planned.planned_reading_id: resolved.raw_reading,
                    },
                )
                self._validate_downstream(
                    planned,
                    definition,
                    virtual_history,
                )
            except NegativeUsageDeltaError as exc:
                error = str(exc)
            except ValidationError as exc:
                error = str(exc)
        else:
            virtual_history = _VirtualHistoricalReadings(
                self._historical_repository,
                {planned.planned_reading_id: None},
            )
            try:
                self._validate_downstream(planned, definition, virtual_history)
            except ValidationError as exc:
                error = str(exc)
        threshold_exceeded = (
            usage_delta is not None
            and definition.warning_threshold is not None
            and usage_delta > definition.warning_threshold
        )
        return ManualReadingPreview(
            previous_reading=previous.reading,
            previous_source=previous.source,
            usage_delta=usage_delta,
            rollover_applied=rollover_applied,
            warning_threshold_exceeded=threshold_exceeded,
            error=error,
        )

    def _validate_downstream(
        self,
        target: PlannedReadingRecord,
        definition: MeasurementDefinitionRecord,
        historical: _VirtualHistoricalReadings,
    ) -> None:
        measurement_definition_id = target.measurement_definition_id
        meter_id = target.meter_id
        cursor = target.scheduled_at
        if cursor is None:
            raise ValidationError(f"予定枠の予定日時がありません: {target.planned_reading_id}")
        after_planned_reading_id: str | None = target.planned_reading_id
        boundary_repository = self._boundary_maintenance_repository
        while True:
            boundaries = sorted(
                (
                    boundary
                    for boundary in boundary_repository.list_replacement_boundaries_for_old_meter(
                        measurement_definition_id,
                        meter_id,
                    )
                    if boundary.replaced_at > cursor
                ),
                key=lambda boundary: (boundary.replaced_at, boundary.replacement_event_id),
            )
            next_boundary = boundaries[0] if boundaries else None
            readings = (
                self._historical_repository.list_following_measured_readings(
                    measurement_definition_id,
                    meter_id,
                    after_scheduled_at=cursor,
                    after_planned_reading_id=after_planned_reading_id,
                )
                if after_planned_reading_id is not None
                else self._historical_repository.list_measured_readings_on_or_after(
                    measurement_definition_id,
                    meter_id,
                    from_scheduled_at=cursor,
                )
            )
            for reading in readings:
                if reading.planned_reading_id == target.planned_reading_id:
                    continue
                planned = self._planned_repository.get_planned_reading(reading.planned_reading_id)
                if planned is None or planned.scheduled_at is None:
                    raise ValidationError(
                        f"後続予定枠が見つからないか日時がありません: {reading.planned_reading_id}"
                    )
                if next_boundary is not None and planned.scheduled_at >= next_boundary.replaced_at:
                    break
                assignment = resolve_planned_assignment(self._assignment_repository, planned)
                validate_persisted_assignment_reading(reading.raw_reading, assignment)
                previous, replacement_boundary = self._calculation_context(
                    planned,
                    definition,
                    historical,
                )
                self._calculation_service.calculate_measured_reading(
                    definition_with_assignment_conditions(definition, assignment),
                    historical.raw_for(reading),
                    previous,
                    replacement_boundary,
                )
                historical.record(reading.planned_reading_id, historical.raw_for(reading))
            if next_boundary is None:
                return
            calculate_boundary_usage(
                definition,
                next_boundary,
                self._assignment_repository,
                historical,
                self._boundary_repository,
                self._calculation_service,
            )
            meter_id = next_boundary.new_meter_id
            cursor = next_boundary.replaced_at
            after_planned_reading_id = None

    def _calculation_context(
        self,
        planned: PlannedReadingRecord,
        definition: MeasurementDefinitionRecord,
        historical: HistoricalMeasuredReadingLookupRepository,
    ) -> tuple[HistoricalMeasuredReadingRecord | None, ReplacementBoundaryRecord | None]:
        _, context = resolve_planned_calculation_context(
            self._assignment_repository,
            historical,
            self._boundary_repository,
            planned,
            definition,
        )
        return context.previous, context.boundary


class _VirtualHistoricalReadings:
    def __init__(
        self,
        repository: HistoricalMeasuredReadingLookupRepository,
        overrides: dict[str, Decimal | None],
    ) -> None:
        self._repository = repository
        self._overrides = overrides

    def record(self, planned_reading_id: str, raw_reading: Decimal | None) -> None:
        self._overrides[planned_reading_id] = raw_reading

    def raw_for(self, reading: HistoricalMeasuredReadingRecord) -> Decimal:
        raw = self._overrides.get(reading.planned_reading_id, reading.raw_reading)
        if raw is None:
            raise ValidationError(f"後続実測値の読値がありません: {reading.planned_reading_id}")
        return raw

    def get_latest_measured_reading(
        self,
        measurement_definition_id: str,
        meter_id: str,
        *,
        before_scheduled_at: datetime,
        before_planned_reading_id: str | None = None,
        excluding_planned_reading_id: str | None = None,
    ) -> HistoricalMeasuredReadingRecord | None:
        latest = self._repository.get_latest_measured_reading(
            measurement_definition_id,
            meter_id,
            before_scheduled_at=before_scheduled_at,
            before_planned_reading_id=before_planned_reading_id,
            excluding_planned_reading_id=excluding_planned_reading_id,
        )
        while latest is not None and latest.planned_reading_id in self._overrides:
            raw = self._overrides[latest.planned_reading_id]
            if raw is not None:
                return replace(latest, raw_reading=raw)
            if latest.scheduled_at is None:
                raise ValidationError(
                    f"後続実測値の予定日時がありません: {latest.planned_reading_id}"
                )
            latest = self._repository.get_latest_measured_reading(
                measurement_definition_id,
                meter_id,
                before_scheduled_at=latest.scheduled_at,
                before_planned_reading_id=latest.planned_reading_id,
                excluding_planned_reading_id=excluding_planned_reading_id,
            )
        return latest

    def list_following_measured_readings(
        self,
        measurement_definition_id: str,
        meter_id: str,
        *,
        after_scheduled_at: datetime,
        after_planned_reading_id: str | None = None,
        excluding_planned_reading_id: str | None = None,
    ) -> list[HistoricalMeasuredReadingRecord]:
        return [
            replace(reading, raw_reading=self.raw_for(reading))
            for reading in self._repository.list_following_measured_readings(
                measurement_definition_id,
                meter_id,
                after_scheduled_at=after_scheduled_at,
                after_planned_reading_id=after_planned_reading_id,
                excluding_planned_reading_id=excluding_planned_reading_id,
            )
            if self._overrides.get(reading.planned_reading_id, reading.raw_reading) is not None
        ]

    def list_measured_readings_on_or_after(
        self,
        measurement_definition_id: str,
        meter_id: str,
        *,
        from_scheduled_at: datetime,
    ) -> list[HistoricalMeasuredReadingRecord]:
        return [
            replace(reading, raw_reading=self.raw_for(reading))
            for reading in self._repository.list_measured_readings_on_or_after(
                measurement_definition_id,
                meter_id,
                from_scheduled_at=from_scheduled_at,
            )
            if self._overrides.get(reading.planned_reading_id, reading.raw_reading) is not None
        ]
