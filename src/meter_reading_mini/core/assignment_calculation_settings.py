"""割当期間の計算条件をプレビューし、監査履歴とともに訂正する。"""

from __future__ import annotations

from collections.abc import Callable
from contextlib import AbstractContextManager
from dataclasses import dataclass, replace
from datetime import datetime
from decimal import Decimal
from uuid import uuid4

from meter_reading_mini.core.assignment_calculation import (
    calculate_boundary_usage,
    definition_with_assignment_conditions,
    resolve_planned_calculation_context,
    validate_persisted_assignment_reading,
)
from meter_reading_mini.core.contract_models import ValueSchema
from meter_reading_mini.core.errors import InactiveMasterError, ValidationError
from meter_reading_mini.core.meter_reading_sync_repositories import (
    AssignmentCalculationCorrectionRecord,
    AssignmentCalculationLookupRepository,
    AssignmentCalculationSettingsRepository,
    AssignmentReviewFlagRecord,
    CalculatedUsageUpdate,
    HistoricalMeasuredReadingLookupRepository,
    MeasurementDefinitionLookupRepository,
    MeterAssignmentRecord,
    PlannedReadingLookupRepository,
    ReadingPersistenceRepository,
    ReplacementBoundaryCalculationUpdate,
    ReplacementBoundaryLookupRepository,
    ReplacementBoundaryMaintenanceRepository,
    StaffRepository,
)
from meter_reading_mini.core.timeutil import parse_utc, utc_now, utc_now_str
from meter_reading_mini.core.timezone_settings import normalize_to_utc
from meter_reading_mini.core.usage_delta_calculation import UsageDeltaCalculationService


@dataclass(frozen=True, slots=True)
class AssignmentCalculationCorrectionInput:
    measurement_definition_id: str
    meter_id: str
    effective_from: datetime
    multiplier: Decimal
    rollover_value: Decimal | None
    staff_id: str
    reason: str


@dataclass(frozen=True, slots=True)
class AssignmentReadingImpact:
    planned_reading_id: str
    old_last_reading: Decimal | None
    new_last_reading: Decimal | None
    old_usage_delta: Decimal | None
    new_usage_delta: Decimal | None


@dataclass(frozen=True, slots=True)
class AssignmentBoundaryImpact:
    replacement_event_id: str
    old_usage_delta: Decimal | None
    update: ReplacementBoundaryCalculationUpdate


@dataclass(frozen=True, slots=True)
class AssignmentCorrectionIssue:
    target: str
    message: str


@dataclass(frozen=True, slots=True)
class AssignmentCalculationCorrectionPreview:
    current: MeterAssignmentRecord
    proposed: MeterAssignmentRecord
    readings: tuple[AssignmentReadingImpact, ...]
    boundaries: tuple[AssignmentBoundaryImpact, ...]
    issues: tuple[AssignmentCorrectionIssue, ...]

    @property
    def can_apply(self) -> bool:
        return not self.issues


class _ProposedAssignmentLookup:
    def __init__(
        self, repository: AssignmentCalculationLookupRepository, proposed: MeterAssignmentRecord
    ) -> None:
        self._repository = repository
        self._proposed = proposed

    def _substitute(self, assignment: MeterAssignmentRecord | None) -> MeterAssignmentRecord | None:
        if (
            assignment is not None
            and assignment.measurement_definition_id == self._proposed.measurement_definition_id
            and assignment.meter_id == self._proposed.meter_id
            and assignment.effective_from == self._proposed.effective_from
        ):
            return self._proposed
        return assignment

    def get_meter_assignment(
        self, measurement_definition_id: str, at: datetime
    ) -> MeterAssignmentRecord | None:
        return self._substitute(
            self._repository.get_meter_assignment(measurement_definition_id, at)
        )

    def get_meter_assignment_by_key(
        self, measurement_definition_id: str, meter_id: str, effective_from: datetime
    ) -> MeterAssignmentRecord | None:
        return self._substitute(
            self._repository.get_meter_assignment_by_key(
                measurement_definition_id, meter_id, effective_from
            )
        )


class AssignmentCalculationSettingsService:
    def __init__(
        self,
        assignment_repository: AssignmentCalculationSettingsRepository,
        definition_repository: MeasurementDefinitionLookupRepository,
        planned_repository: PlannedReadingLookupRepository,
        historical_repository: HistoricalMeasuredReadingLookupRepository,
        reading_repository: ReadingPersistenceRepository,
        boundary_repository: ReplacementBoundaryLookupRepository,
        boundary_maintenance_repository: ReplacementBoundaryMaintenanceRepository,
        staff_repository: StaffRepository,
        *,
        transaction_factory: Callable[[], AbstractContextManager[object]],
        now_provider: Callable[[], datetime] = utc_now,
        id_provider: Callable[[], str] = lambda: uuid4().hex,
    ) -> None:
        self._assignments = assignment_repository
        self._definitions = definition_repository
        self._planned = planned_repository
        self._history = historical_repository
        self._readings = reading_repository
        self._boundaries = boundary_repository
        self._boundary_maintenance = boundary_maintenance_repository
        self._staff = staff_repository
        self._transaction_factory = transaction_factory
        self._now_provider = now_provider
        self._id_provider = id_provider
        self._calculation = UsageDeltaCalculationService()

    def list_assignments(self, measurement_definition_id: str) -> list[MeterAssignmentRecord]:
        return self._assignments.list_meter_assignments(measurement_definition_id)

    def list_review_flags(self) -> list[AssignmentReviewFlagRecord]:
        return self._assignments.list_assignment_review_flags()

    def list_corrections(
        self, measurement_definition_id: str, meter_id: str, effective_from: datetime
    ) -> list[AssignmentCalculationCorrectionRecord]:
        assignment = self._require_assignment(measurement_definition_id, meter_id, effective_from)
        return self._assignments.list_assignment_calculation_corrections(assignment)

    def preview_correction(
        self, request: AssignmentCalculationCorrectionInput
    ) -> AssignmentCalculationCorrectionPreview:
        current = self._require_assignment(
            request.measurement_definition_id, request.meter_id, request.effective_from
        )
        self._require_staff(request.staff_id)
        if not request.reason.strip():
            raise ValidationError("計算条件の訂正理由を入力してください。")
        if not request.multiplier.is_finite() or request.multiplier <= 0:
            raise ValidationError("倍率は有限の正数で指定してください。")
        if request.rollover_value is not None and (
            not request.rollover_value.is_finite() or request.rollover_value <= 0
        ):
            raise ValidationError("ロールオーバー値は有限の正数で指定してください。")
        definition = self._definitions.get_measurement_definition(current.measurement_definition_id)
        if definition is None:
            raise ValidationError(f"測定定義が見つかりません: {current.measurement_definition_id}")
        proposed = replace(
            current,
            multiplier=request.multiplier,
            rollover_value=request.rollover_value,
            needs_review=False,
        )
        lookup = _ProposedAssignmentLookup(self._assignments, proposed)
        reading_impacts: list[AssignmentReadingImpact] = []
        boundary_impacts: list[AssignmentBoundaryImpact] = []
        issues: list[AssignmentCorrectionIssue] = []

        for reading in self._assignments.list_assignment_measured_readings(current):
            try:
                if definition.value_schema is ValueSchema.POWER_FACTOR_PERCENT_PHASE:
                    if reading.raw_reading is not None:
                        raise ValidationError("力率の実測値に数値読値が保存されています。")
                    continue
                if reading.raw_reading is None:
                    raise ValidationError("実測値の読値がありません。")
                planned = self._planned.get_planned_reading(reading.planned_reading_id)
                if planned is None:
                    raise ValidationError("予定枠が見つかりません。")
                assignment, context = resolve_planned_calculation_context(
                    lookup, self._history, self._boundaries, planned, definition
                )
                if assignment != proposed:
                    raise ValidationError("予定枠と訂正対象の割当が一致しません。")
                validate_persisted_assignment_reading(reading.raw_reading, assignment)
                result = self._calculation.calculate_measured_reading(
                    definition_with_assignment_conditions(definition, assignment),
                    reading.raw_reading,
                    context.previous,
                    context.boundary,
                )
                reading_impacts.append(
                    AssignmentReadingImpact(
                        planned_reading_id=reading.planned_reading_id,
                        old_last_reading=reading.last_reading,
                        new_last_reading=result.last_reading,
                        old_usage_delta=reading.usage_delta,
                        new_usage_delta=result.usage_delta,
                    )
                )
            except ValidationError as exc:
                issues.append(AssignmentCorrectionIssue(reading.planned_reading_id, str(exc)))

        for boundary in self._assignments.list_assignment_boundaries(current):
            try:
                update = calculate_boundary_usage(
                    definition,
                    boundary,
                    lookup,
                    self._history,
                    self._boundaries,
                    self._calculation,
                )
                boundary_impacts.append(
                    AssignmentBoundaryImpact(
                        replacement_event_id=boundary.replacement_event_id,
                        old_usage_delta=boundary.old_meter_usage_delta,
                        update=update,
                    )
                )
            except ValidationError as exc:
                issues.append(AssignmentCorrectionIssue(boundary.replacement_event_id, str(exc)))

        return AssignmentCalculationCorrectionPreview(
            current=current,
            proposed=proposed,
            readings=tuple(reading_impacts),
            boundaries=tuple(boundary_impacts),
            issues=tuple(issues),
        )

    def correct_assignment(
        self, request: AssignmentCalculationCorrectionInput
    ) -> AssignmentCalculationCorrectionPreview:
        with self._transaction_factory():
            preview = self.preview_correction(request)
            if preview.issues:
                detail = "、".join(f"{issue.target}: {issue.message}" for issue in preview.issues)
                raise ValidationError(f"計算条件を訂正できません: {detail}")
            staff_name = self._require_staff(request.staff_id)
            correction_id = self._id_provider().strip()
            if not correction_id:
                raise ValidationError("訂正履歴 ID を採番できません。")
            correction = AssignmentCalculationCorrectionRecord(
                correction_id=correction_id,
                measurement_definition_id=preview.current.measurement_definition_id,
                meter_id=preview.current.meter_id,
                assignment_effective_from=preview.current.effective_from,
                old_multiplier=preview.current.multiplier,
                old_rollover_value=preview.current.rollover_value,
                new_multiplier=preview.proposed.multiplier,
                new_rollover_value=preview.proposed.rollover_value,
                staff_id=request.staff_id,
                staff_name=staff_name,
                corrected_at=parse_utc(utc_now_str(self._now_provider())),
                reason=request.reason.strip(),
            )
            self._assignments.update_assignment_calculation(preview.proposed)
            self._assignments.save_assignment_calculation_correction(correction)
            self._assignments.clear_assignment_review_flag(preview.current)
            for impact in preview.readings:
                self._readings.save_calculated_usage(
                    CalculatedUsageUpdate(
                        planned_reading_id=impact.planned_reading_id,
                        last_reading=impact.new_last_reading,
                        usage_delta=impact.new_usage_delta,
                    )
                )
            for impact in preview.boundaries:
                self._boundary_maintenance.update_replacement_boundary_calculation(impact.update)
        return preview

    def _require_staff(self, staff_id: str) -> str:
        staff = self._staff.get_staff(staff_id)
        if staff is None or not staff.is_active or not staff.display_name.strip():
            raise InactiveMasterError(f"有効な担当者を指定してください: {staff_id}")
        return staff.display_name.strip()

    def _require_assignment(
        self, measurement_definition_id: str, meter_id: str, effective_from: datetime
    ) -> MeterAssignmentRecord:
        start = normalize_to_utc(effective_from)
        assignment = self._assignments.get_meter_assignment_by_key(
            measurement_definition_id, meter_id, start
        )
        if assignment is None:
            raise ValidationError("訂正対象のメーター割当が見つかりません。")
        for other in self._assignments.list_meter_assignments(measurement_definition_id):
            if (
                other.meter_id == assignment.meter_id
                and other.effective_from == assignment.effective_from
            ):
                continue
            if (
                assignment.effective_to is None or other.effective_from < assignment.effective_to
            ) and (other.effective_to is None or other.effective_to > assignment.effective_from):
                raise ValidationError("訂正対象の期間に重複するメーター割当があります。")
        return assignment
