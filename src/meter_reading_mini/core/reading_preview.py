"""直接入力の検証と使用量を DB に保存せずプレビューする。"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal

from meter_reading_mini.core.assignment_calculation import (
    definition_with_assignment_conditions,
    resolve_planned_assignment,
    resolve_planned_calculation_context,
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
    ManualReadingInput,
    MeasurementDefinitionLookupRepository,
    PlannedReadingLookupRepository,
    PlannedReadingTaskState,
    ReplacementBoundaryLookupRepository,
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
        *,
        now_provider: Callable[[], datetime] = utc_now,
        calculation_service: UsageDeltaCalculationService | None = None,
    ) -> None:
        self._planned_repository = planned_repository
        self._definition_repository = definition_repository
        self._assignment_repository = assignment_repository
        self._historical_repository = historical_repository
        self._boundary_repository = boundary_repository
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
            except NegativeUsageDeltaError as exc:
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
