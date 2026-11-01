"""割当期間の計算条件を検針枠と交換境界から解決する。"""

from __future__ import annotations

from dataclasses import replace
from decimal import Decimal
from typing import Protocol

from meter_reading_mini.core.errors import ValidationError
from meter_reading_mini.core.meter_reading_sync_repositories import (
    AssignmentCalculationLookupRepository,
    HistoricalMeasuredReadingLookupRepository,
    MeasurementDefinitionRecord,
    MeterAssignmentRecord,
    PlannedReadingRecord,
    ReplacementBoundaryCalculationRecord,
    ReplacementBoundaryCalculationUpdate,
    ReplacementBoundaryLookupRepository,
)
from meter_reading_mini.core.previous_reading import CalculationContext, resolve_calculation_context
from meter_reading_mini.core.reading_values import validate_reading_value
from meter_reading_mini.core.usage_delta_calculation import UsageDeltaCalculationService


class ReadingPrecision(Protocol):
    @property
    def decimal_places(self) -> int | None: ...


def resolve_planned_assignment(
    repository: AssignmentCalculationLookupRepository, planned: PlannedReadingRecord
) -> MeterAssignmentRecord:
    if planned.scheduled_at is None:
        raise ValidationError(f"予定枠の予定日時がありません: {planned.planned_reading_id}")
    assignment = repository.get_meter_assignment(
        planned.measurement_definition_id, planned.scheduled_at
    )
    if assignment is None or assignment.meter_id != planned.meter_id:
        raise ValidationError(
            "予定枠に対応するメーター割当がありません: "
            f"{planned.planned_reading_id}/{planned.meter_id}"
        )
    if assignment.needs_review:
        raise ValidationError(
            "割当条件の確認が必要です。先に条件を訂正してください: "
            f"{planned.measurement_definition_id}/{assignment.meter_id}"
        )
    return assignment


def resolve_boundary_assignments(
    repository: AssignmentCalculationLookupRepository,
    boundary: ReplacementBoundaryCalculationRecord,
) -> tuple[MeterAssignmentRecord, MeterAssignmentRecord]:
    old = repository.get_meter_assignment_by_key(
        boundary.measurement_definition_id,
        boundary.old_meter_id,
        boundary.old_assignment_effective_from,
    )
    new = repository.get_meter_assignment_by_key(
        boundary.measurement_definition_id,
        boundary.new_meter_id,
        boundary.new_assignment_effective_from,
    )
    if (
        old is None
        or new is None
        or old.effective_to != boundary.replaced_at
        or new.effective_from != boundary.replaced_at
        or old.effective_from >= boundary.replaced_at
        or (new.effective_to is not None and new.effective_to <= boundary.replaced_at)
    ):
        raise ValidationError(
            "交換境界に対応する旧・新メーター割当が不正です: "
            f"{boundary.replacement_event_id}/{boundary.measurement_definition_id}"
        )
    return old, new


def resolve_planned_calculation_context(
    assignment_repository: AssignmentCalculationLookupRepository,
    historical_repository: HistoricalMeasuredReadingLookupRepository,
    boundary_repository: ReplacementBoundaryLookupRepository,
    planned: PlannedReadingRecord,
    definition: ReadingPrecision,
) -> tuple[MeterAssignmentRecord, CalculationContext]:
    assignment = resolve_planned_assignment(assignment_repository, planned)
    context = resolve_calculation_context(
        historical_repository,
        boundary_repository,
        planned,
        excluding_planned_reading_id=planned.planned_reading_id,
    )
    previous, boundary = context.previous, context.boundary
    if boundary is not None:
        if (
            boundary.replaced_at != assignment.effective_from
            or boundary.new_meter_id != planned.meter_id
        ):
            raise ValidationError(
                f"交換境界と予定枠の割当が一致しません: {planned.planned_reading_id}"
            )
        if boundary.new_initial_reading is not None:
            validate_persisted_assignment_reading(boundary.new_initial_reading, assignment)
    if previous is not None:
        if previous.scheduled_at is None:
            raise ValidationError(f"前回実測の予定日時がありません: {previous.planned_reading_id}")
        if previous.scheduled_at < assignment.effective_from:
            previous = None
        else:
            validate_persisted_assignment_reading(previous.raw_reading, assignment)
    return assignment, CalculationContext(previous=previous, boundary=boundary)


def definition_with_assignment_conditions(
    definition: MeasurementDefinitionRecord, assignment: MeterAssignmentRecord
) -> MeasurementDefinitionRecord:
    if definition.measurement_definition_id != assignment.measurement_definition_id:
        raise ValidationError("測定定義とメーター割当が一致しません。")
    return replace(
        definition,
        multiplier=assignment.multiplier,
        rollover_value=assignment.rollover_value,
    )


def validate_assignment_reading(
    value: Decimal, definition: ReadingPrecision, assignment: MeterAssignmentRecord
) -> None:
    validate_reading_value(
        value,
        decimal_places=definition.decimal_places,
        rollover_value=assignment.rollover_value,
    )


def validate_persisted_assignment_reading(
    value: Decimal, assignment: MeterAssignmentRecord
) -> None:
    validate_reading_value(
        value,
        decimal_places=None,
        rollover_value=None if assignment.needs_review else assignment.rollover_value,
    )


def calculate_boundary_usage(
    definition: MeasurementDefinitionRecord,
    boundary: ReplacementBoundaryCalculationRecord,
    assignment_repository: AssignmentCalculationLookupRepository,
    historical_repository: HistoricalMeasuredReadingLookupRepository,
    boundary_repository: ReplacementBoundaryLookupRepository,
    calculation_service: UsageDeltaCalculationService,
) -> ReplacementBoundaryCalculationUpdate:
    old_assignment, new_assignment = resolve_boundary_assignments(assignment_repository, boundary)
    if boundary.new_initial_reading is not None:
        validate_persisted_assignment_reading(boundary.new_initial_reading, new_assignment)
    previous = historical_repository.get_latest_measured_reading(
        boundary.measurement_definition_id,
        boundary.old_meter_id,
        before_scheduled_at=boundary.replaced_at,
    )
    previous_boundary = boundary_repository.get_replacement_boundary_for_new_meter_first_reading(
        boundary.measurement_definition_id,
        boundary.old_meter_id,
        before_scheduled_at=boundary.replaced_at,
    )
    if previous_boundary is not None:
        if previous_boundary.replaced_at != old_assignment.effective_from:
            raise ValidationError("交換境界と旧メーター割当が一致しません。")
        previous = None
        if previous_boundary.new_initial_reading is not None:
            validate_persisted_assignment_reading(
                previous_boundary.new_initial_reading, old_assignment
            )
    elif previous is not None:
        if previous.scheduled_at is None:
            raise ValidationError(f"前回実測の予定日時がありません: {previous.planned_reading_id}")
        if previous.scheduled_at < old_assignment.effective_from:
            previous = None
        else:
            validate_persisted_assignment_reading(previous.raw_reading, old_assignment)
    usage_delta = None
    if boundary.old_final_reading is not None:
        validate_persisted_assignment_reading(boundary.old_final_reading, old_assignment)
        result = calculation_service.calculate_measured_reading(
            definition_with_assignment_conditions(definition, old_assignment),
            boundary.old_final_reading,
            previous,
            previous_boundary,
        )
        usage_delta = result.usage_delta
    return ReplacementBoundaryCalculationUpdate(
        replacement_event_id=boundary.replacement_event_id,
        measurement_definition_id=boundary.measurement_definition_id,
        previous_measured_reading=previous.raw_reading if previous is not None else None,
        previous_measured_planned_reading_id=(
            previous.planned_reading_id if previous is not None else None
        ),
        old_meter_usage_delta=usage_delta,
    )
