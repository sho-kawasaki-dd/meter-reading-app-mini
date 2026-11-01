"""メーター交換のプレビュー・登録・訂正を原子的に実行する。"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from contextlib import AbstractContextManager
from dataclasses import dataclass, field, replace
from datetime import datetime, timedelta, tzinfo
from decimal import Decimal
from uuid import uuid4

from meter_reading_mini.core.assignment_calculation import (
    calculate_boundary_usage,
    validate_assignment_reading,
)
from meter_reading_mini.core.contract_models import CalculationMethod, ResultStatus, ValueSchema
from meter_reading_mini.core.errors import InactiveMasterError, ValidationError
from meter_reading_mini.core.meter_master_service import MeterMasterService
from meter_reading_mini.core.meter_reading_sync_repositories import (
    AssignmentCalculationLookupRepository,
    AssignmentCalculationTerms,
    HistoricalMeasuredReadingLookupRepository,
    MeasurementDefinitionLookupRepository,
    MeasurementDefinitionRecord,
    MeterAssignmentRecord,
    MeterMasterCreateInput,
    MeterMasterWriteRepository,
    MeterReplacementRepository,
    PlannedReadingTaskState,
    ReplacementAffectedSlot,
    ReplacementBoundaryCalculationRecord,
    ReplacementBoundaryLookupRepository,
    ReplacementBoundaryMaintenanceRepository,
    ReplacementDefinitionReading,
    ReplacementEventRecord,
    ReplacementUpsertRequest,
    StaffRepository,
    UpsertDisposition,
)
from meter_reading_mini.core.mobile_reading import validate_note
from meter_reading_mini.core.reading_write_orchestration import ReadingWriteOrchestrationService
from meter_reading_mini.core.timeutil import parse_utc, utc_now, utc_now_str
from meter_reading_mini.core.timezone_settings import DEFAULT_APP_TIMEZONE, normalize_to_utc
from meter_reading_mini.core.usage_delta_calculation import UsageDeltaCalculationService


@dataclass(frozen=True, slots=True)
class MeterReplacementInput:
    old_meter_id: str
    replaced_at: datetime
    old_final_reading: Decimal | None
    new_initial_reading: Decimal | None
    staff_id: str
    new_meter_id: str | None = None
    new_meter: MeterMasterCreateInput | None = None
    note: str | None = None
    new_terms: Mapping[str, AssignmentCalculationTerms] = field(
        default_factory=dict[str, AssignmentCalculationTerms]
    )
    definition_readings: tuple[ReplacementDefinitionReading, ...] = ()


@dataclass(frozen=True, slots=True)
class MeterReplacementCorrectionInput:
    replacement_event_id: str
    old_final_reading: Decimal | None
    new_initial_reading: Decimal | None
    staff_id: str
    note: str | None = None
    definition_readings: tuple[ReplacementDefinitionReading, ...] = ()


@dataclass(frozen=True, slots=True)
class ReplacementDefinitionPreview:
    measurement_definition_id: str
    old_assignment: MeterAssignmentRecord
    new_terms: AssignmentCalculationTerms
    old_usage_delta: Decimal | None
    rollover_applied: bool


@dataclass(frozen=True, slots=True)
class ReplacementPreview:
    location_id: str
    incomplete_count: int
    nonmeasured_count: int
    definitions: tuple[ReplacementDefinitionPreview, ...]
    errors: tuple[str, ...]

    @property
    def can_register(self) -> bool:
        return not self.errors


@dataclass(frozen=True, slots=True)
class ReplacementHistoryEntry:
    event: ReplacementEventRecord
    local_replaced_at: datetime
    definition_readings: tuple[ReplacementDefinitionReading, ...] = ()


@dataclass(frozen=True, slots=True)
class ReplacementTarget:
    definition: MeasurementDefinitionRecord
    assignment: MeterAssignmentRecord


class _ReplacementPreviewLookup:
    def __init__(
        self,
        repository: AssignmentCalculationLookupRepository,
        old: MeterAssignmentRecord,
        new: MeterAssignmentRecord,
    ) -> None:
        self._repository = repository
        self._old = old
        self._new = new

    def get_meter_assignment(
        self, measurement_definition_id: str, at: datetime
    ) -> MeterAssignmentRecord | None:
        return self._repository.get_meter_assignment(measurement_definition_id, at)

    def get_meter_assignment_by_key(
        self, measurement_definition_id: str, meter_id: str, effective_from: datetime
    ) -> MeterAssignmentRecord | None:
        for assignment in (self._old, self._new):
            if (
                measurement_definition_id == assignment.measurement_definition_id
                and meter_id == assignment.meter_id
                and effective_from == assignment.effective_from
            ):
                return assignment
        return self._repository.get_meter_assignment_by_key(
            measurement_definition_id, meter_id, effective_from
        )


class MeterReplacementService:
    def __init__(
        self,
        repository: MeterReplacementRepository,
        meter_repository: MeterMasterWriteRepository,
        definition_repository: MeasurementDefinitionLookupRepository,
        assignment_repository: AssignmentCalculationLookupRepository,
        historical_repository: HistoricalMeasuredReadingLookupRepository,
        boundary_repository: ReplacementBoundaryLookupRepository,
        boundary_maintenance_repository: ReplacementBoundaryMaintenanceRepository,
        staff_repository: StaffRepository,
        meter_service: MeterMasterService,
        reading_writer: ReadingWriteOrchestrationService,
        *,
        transaction_factory: Callable[[], AbstractContextManager[object]],
        now_provider: Callable[[], datetime] = utc_now,
        id_provider: Callable[[], str] = lambda: uuid4().hex,
        timezone_provider: Callable[[], tzinfo] = lambda: DEFAULT_APP_TIMEZONE,
    ) -> None:
        self._repository = repository
        self._meters = meter_repository
        self._definitions = definition_repository
        self._assignments = assignment_repository
        self._history = historical_repository
        self._boundaries = boundary_repository
        self._boundary_maintenance = boundary_maintenance_repository
        self._staff = staff_repository
        self._meter_service = meter_service
        self._reading_writer = reading_writer
        self._transaction_factory = transaction_factory
        self._now_provider = now_provider
        self._id_provider = id_provider
        self._timezone_provider = timezone_provider
        self._calculation = UsageDeltaCalculationService()

    def list_replacement_targets(
        self, old_meter_id: str, replaced_at: datetime
    ) -> tuple[ReplacementTarget, ...]:
        requested_at = normalize_to_utc(replaced_at)
        if requested_at > normalize_to_utc(self._now_provider()):
            raise ValidationError("未来の交換日時は指定できません。")
        at = parse_utc(utc_now_str(requested_at))
        old_meter = self._meters.get_meter_master(old_meter_id)
        if old_meter is None:
            raise ValidationError(f"交換元メーターが見つかりません: {old_meter_id}")
        assignments = self._repository.list_assignments_spanning_meter(old_meter_id, at)
        if not assignments:
            raise ValidationError("交換日時をまたぐ旧メーターの割当がありません。")
        definition_ids = {assignment.measurement_definition_id for assignment in assignments}
        if len(definition_ids) != len(assignments):
            raise ValidationError("交換対象のメーター割当が重複しています。")
        targets: list[ReplacementTarget] = []
        for assignment in assignments:
            definition = self._definitions.get_measurement_definition(
                assignment.measurement_definition_id
            )
            if definition is None or definition.location_id != old_meter.location_id:
                raise ValidationError(
                    "交換対象の測定定義または場所が不正です: "
                    f"{assignment.measurement_definition_id}"
                )
            targets.append(ReplacementTarget(definition, assignment))
        return tuple(targets)

    def preview_replacement(self, request: MeterReplacementInput) -> ReplacementPreview:
        requested_at = normalize_to_utc(request.replaced_at)
        if requested_at > normalize_to_utc(self._now_provider()):
            raise ValidationError("未来の交換日時は指定できません。")
        at = parse_utc(utc_now_str(requested_at))
        self._staff_name(request.staff_id)
        validate_note(request.note)
        old_meter = self._meters.get_meter_master(request.old_meter_id)
        if old_meter is None:
            raise ValidationError(f"交換元メーターが見つかりません: {request.old_meter_id}")
        if (request.new_meter_id is None) == (request.new_meter is None):
            raise ValidationError("既存メーターか新規メーターのいずれかを指定してください。")
        if request.new_meter_id is not None:
            new_meter = self._meters.get_meter_master(request.new_meter_id)
            if new_meter is None or not new_meter.is_active:
                raise InactiveMasterError("有効な交換先メーターを指定してください。")
            if new_meter.meter_id == old_meter.meter_id:
                raise ValidationError("交換元と交換先に同じメーターは指定できません。")
            if new_meter.location_id != old_meter.location_id:
                raise ValidationError("交換元と交換先の場所が一致しません。")
            if self._repository.has_meter_assignment_on_or_after(new_meter.meter_id, at):
                raise ValidationError("交換先メーターは既に割当済みです。")
            new_meter_id = new_meter.meter_id
        else:
            assert request.new_meter is not None
            prepared = self._meter_service.prepare_meter_creation(request.new_meter)
            if prepared.location is None or prepared.location.location_id != old_meter.location_id:
                raise ValidationError("交換先メーターは交換元と同じ既存の場所に登録してください。")
            new_meter_id = "__new_meter_preview__"
            if new_meter_id == old_meter.meter_id:
                new_meter_id += "_"
        targets = self.list_replacement_targets(old_meter.meter_id, at)
        assignments = [target.assignment for target in targets]
        definition_ids = {assignment.measurement_definition_id for assignment in assignments}
        if set(request.new_terms) - definition_ids:
            raise ValidationError("交換対象外の測定定義に計算条件が指定されています。")
        for assignment in assignments:
            if (
                assignment.effective_to is not None
                or self._repository.has_later_assignment_or_replacement(
                    assignment.measurement_definition_id, at
                )
            ):
                raise ValidationError(
                    "交換日時より後に割当切替または交換があります: "
                    f"{assignment.measurement_definition_id}"
                )
        requested_readings = _resolve_definition_readings(
            definition_ids,
            request.definition_readings,
            request.old_final_reading,
            request.new_initial_reading,
        )

        definitions: list[ReplacementDefinitionPreview] = []
        errors: list[str] = []
        incomplete_count = 0
        nonmeasured_count = 0
        for target in targets:
            assignment = target.assignment
            definition = target.definition
            definition_id = assignment.measurement_definition_id
            if self._assignments.get_meter_assignment(definition_id, at) != assignment:
                raise ValidationError(f"交換対象の割当が一意ではありません: {definition_id}")
            if (
                assignment.effective_to is not None
                or self._repository.has_later_assignment_or_replacement(definition_id, at)
            ):
                raise ValidationError(
                    f"交換日時より後に割当切替または交換があります: {definition_id}"
                )
            terms = request.new_terms.get(
                definition_id,
                AssignmentCalculationTerms(assignment.multiplier, assignment.rollover_value),
            )
            if not terms.multiplier.is_finite() or terms.multiplier <= 0:
                raise ValidationError(f"新しい倍率は有限の正数で指定してください: {definition_id}")
            if terms.rollover_value is not None and (
                not terms.rollover_value.is_finite() or terms.rollover_value <= 0
            ):
                raise ValidationError(f"新しいロールオーバー値が不正です: {definition_id}")
            for slot in self._repository.list_replacement_affected_slots(
                definition_id, old_meter.meter_id, at
            ):
                category = _validate_affected_slot(slot)
                incomplete_count += category == "incomplete"
                nonmeasured_count += category == "nonmeasured"
            old_assignment = replace(assignment, effective_to=at)
            new_assignment = MeterAssignmentRecord(
                measurement_definition_id=definition_id,
                meter_id=new_meter_id,
                effective_from=at,
                multiplier=terms.multiplier,
                rollover_value=terms.rollover_value,
            )
            boundary = ReplacementBoundaryCalculationRecord(
                replacement_event_id="preview",
                measurement_definition_id=definition_id,
                old_meter_id=old_meter.meter_id,
                new_meter_id=new_meter_id,
                replaced_at=at,
                old_final_reading=requested_readings[definition_id].old_final_reading,
                new_initial_reading=requested_readings[definition_id].new_initial_reading,
                old_assignment_effective_from=assignment.effective_from,
                new_assignment_effective_from=at,
            )
            try:
                _validate_replacement_definition_reading(
                    requested_readings[definition_id], definition, assignment, new_assignment
                )
                update = calculate_boundary_usage(
                    definition,
                    boundary,
                    _ReplacementPreviewLookup(self._assignments, old_assignment, new_assignment),
                    self._history,
                    self._boundaries,
                    self._calculation,
                )
                previous = update.previous_measured_reading
                if previous is None:
                    prior_boundary = (
                        self._boundaries.get_replacement_boundary_for_new_meter_first_reading(
                            definition_id,
                            old_meter.meter_id,
                            before_scheduled_at=at,
                        )
                    )
                    if prior_boundary is not None:
                        previous = prior_boundary.new_initial_reading
                rollover = (
                    definition.calculation_method is not CalculationMethod.RAW_ONLY
                    and boundary.old_final_reading is not None
                    and previous is not None
                    and boundary.old_final_reading < previous
                    and assignment.rollover_value is not None
                )
                definitions.append(
                    ReplacementDefinitionPreview(
                        definition_id, assignment, terms, update.old_meter_usage_delta, rollover
                    )
                )
            except ValidationError as exc:
                errors.append(f"{definition_id}: {exc}")
                definitions.append(
                    ReplacementDefinitionPreview(definition_id, assignment, terms, None, False)
                )
        return ReplacementPreview(
            old_meter.location_id,
            incomplete_count,
            nonmeasured_count,
            tuple(definitions),
            tuple(errors),
        )

    def register_replacement(
        self,
        request: MeterReplacementInput,
        *,
        expected_preview: ReplacementPreview | None = None,
    ) -> ReplacementEventRecord:
        with self._transaction_factory():
            preview = self.preview_replacement(request)
            if expected_preview is not None and preview != expected_preview:
                raise ValidationError("交換の状況が変わりました。再度プレビューしてください。")
            if preview.errors:
                raise ValidationError("交換できません: " + "、".join(preview.errors))
            if request.new_meter is not None:
                new_meter = self._meter_service.create_meter_within_transaction(request.new_meter)
                request = replace(request, new_meter_id=new_meter.meter_id, new_meter=None)
                preview = self.preview_replacement(request)
                if preview.errors:
                    raise ValidationError("交換できません: " + "、".join(preview.errors))
            assert request.new_meter_id is not None
            event_id = self._id_provider().strip()
            if not event_id:
                raise ValidationError("交換イベント ID を採番できません。")
            if self._repository.get_replacement_event(event_id) is not None:
                raise ValidationError(f"交換イベント ID は既に登録されています: {event_id}")
            requested_readings = _resolve_definition_readings(
                {item.measurement_definition_id for item in preview.definitions},
                request.definition_readings,
                request.old_final_reading,
                request.new_initial_reading,
            )
            saved = ReplacementUpsertRequest(
                replacement_event_id=event_id,
                location_id=preview.location_id,
                old_meter_id=request.old_meter_id,
                new_meter_id=request.new_meter_id,
                replaced_at=parse_utc(utc_now_str(request.replaced_at)),
                client_updated_at=parse_utc(utc_now_str(self._now_provider())),
                old_final_reading=None,
                new_initial_reading=None,
                updated_by=self._staff_name(request.staff_id),
                note=validate_note(request.note),
                definition_readings=tuple(requested_readings.values()),
            )
            outcome = self._repository.upsert_replacement(saved)
            if outcome.disposition is not UpsertDisposition.INSERTED:
                raise ValidationError("交換イベントを新規登録できません。")
            self._repository.reflect_verified_replacement(
                saved,
                {item.measurement_definition_id: item.new_terms for item in preview.definitions},
            )
            self._reading_writer.recalculate_after_replacement(saved)
            event = self._repository.get_replacement_event(event_id)
            if event is None:
                raise ValidationError("登録した交換イベントを取得できません。")
        return event

    def correct_replacement(
        self,
        request: MeterReplacementCorrectionInput,
        *,
        expected_history: ReplacementHistoryEntry | None = None,
    ) -> ReplacementEventRecord:
        with self._transaction_factory():
            event = self._repository.get_replacement_event(request.replacement_event_id)
            if event is None:
                raise ValidationError(
                    f"交換イベントが見つかりません: {request.replacement_event_id}"
                )
            staff_name = self._staff_name(request.staff_id)
            note = validate_note(request.note)
            boundaries = self._boundary_maintenance.list_replacement_boundaries_for_event(
                event.replacement_event_id
            )
            if not boundaries:
                raise ValidationError("訂正対象の交換境界がありません。")
            current_readings = tuple(
                ReplacementDefinitionReading(
                    boundary.measurement_definition_id,
                    boundary.old_final_reading,
                    boundary.new_initial_reading,
                )
                for boundary in boundaries
            )
            if expected_history is not None and (
                event != expected_history.event
                or current_readings != expected_history.definition_readings
            ):
                raise ValidationError("交換履歴が変わりました。再読み込みしてください。")
            requested_readings = _resolve_definition_readings(
                {boundary.measurement_definition_id for boundary in boundaries},
                request.definition_readings,
                request.old_final_reading,
                request.new_initial_reading,
            )
            for boundary in boundaries:
                if (
                    boundary.old_meter_id != event.old_meter_id
                    or boundary.new_meter_id != event.new_meter_id
                    or boundary.replaced_at != event.replaced_at
                ):
                    raise ValidationError("交換イベントと交換境界が一致しません。")
                definition = self._definitions.get_measurement_definition(
                    boundary.measurement_definition_id
                )
                if definition is None:
                    raise ValidationError("交換境界の測定定義が見つかりません。")
                reading = requested_readings[boundary.measurement_definition_id]
                old_assignment = self._assignments.get_meter_assignment_by_key(
                    boundary.measurement_definition_id,
                    boundary.old_meter_id,
                    boundary.old_assignment_effective_from,
                )
                new_assignment = self._assignments.get_meter_assignment_by_key(
                    boundary.measurement_definition_id,
                    boundary.new_meter_id,
                    boundary.new_assignment_effective_from,
                )
                if old_assignment is None or new_assignment is None:
                    raise ValidationError("交換境界の割当条件が見つかりません。")
                _validate_replacement_definition_reading(
                    reading, definition, old_assignment, new_assignment
                )
                calculate_boundary_usage(
                    definition,
                    replace(
                        boundary,
                        old_final_reading=reading.old_final_reading,
                        new_initial_reading=reading.new_initial_reading,
                    ),
                    self._assignments,
                    self._history,
                    self._boundaries,
                    self._calculation,
                )
            client_updated_at = max(
                parse_utc(utc_now_str(self._now_provider())),
                event.client_updated_at + timedelta(seconds=1),
            )
            saved = ReplacementUpsertRequest(
                replacement_event_id=event.replacement_event_id,
                location_id=event.location_id,
                old_meter_id=event.old_meter_id,
                new_meter_id=event.new_meter_id,
                replaced_at=event.replaced_at,
                client_updated_at=client_updated_at,
                old_final_reading=None,
                new_initial_reading=None,
                updated_by=staff_name,
                note=note,
                definition_readings=tuple(requested_readings.values()),
            )
            self._repository.correct_replacement_event(saved)
            self._reading_writer.recalculate_after_replacement(saved)
            updated = self._repository.get_replacement_event(event.replacement_event_id)
            if updated is None:
                raise ValidationError("訂正した交換イベントを取得できません。")
        return updated

    def list_replacements(self, location_id: str) -> list[ReplacementHistoryEntry]:
        timezone = self._timezone_provider()
        entries: list[ReplacementHistoryEntry] = []
        for event in self._repository.list_replacement_events(location_id):
            boundaries = self._boundary_maintenance.list_replacement_boundaries_for_event(
                event.replacement_event_id
            )
            entries.append(
                ReplacementHistoryEntry(
                    event,
                    event.replaced_at.astimezone(timezone),
                    tuple(
                        ReplacementDefinitionReading(
                            boundary.measurement_definition_id,
                            boundary.old_final_reading,
                            boundary.new_initial_reading,
                        )
                        for boundary in boundaries
                    ),
                )
            )
        return entries

    def _staff_name(self, staff_id: str) -> str:
        staff = self._staff.get_staff(staff_id)
        if staff is None or not staff.is_active or not staff.display_name.strip():
            raise InactiveMasterError(f"有効な担当者を指定してください: {staff_id}")
        return staff.display_name.strip()


def _validate_affected_slot(slot: ReplacementAffectedSlot) -> str | None:
    values_present = any(
        value is not None
        for value in (
            slot.raw_reading,
            slot.last_reading,
            slot.usage_delta,
            slot.structured_payload_json,
        )
    )
    if slot.task_state in (PlannedReadingTaskState.OPEN, PlannedReadingTaskState.AUTO_CLOSED):
        if slot.result_status is not None:
            raise ValidationError(f"未完了枠に結果が存在します: {slot.planned_reading_id}")
        return "incomplete"
    if slot.result_status is ResultStatus.MEASURED:
        raise ValidationError(
            f"実測済みの枠は交換できません。先に非実測へ訂正してください: {slot.planned_reading_id}"
        )
    if slot.result_status is ResultStatus.REPLACED:
        raise ValidationError(f"既存の交換結果を確認してください: {slot.planned_reading_id}")
    if slot.task_state is PlannedReadingTaskState.COMPLETED and slot.result_status is None:
        raise ValidationError(f"完了枠の結果がありません: {slot.planned_reading_id}")
    if slot.result_status is not None and values_present:
        raise ValidationError(f"非実測結果に値が保存されています: {slot.planned_reading_id}")
    if slot.task_state is PlannedReadingTaskState.COMPLETED:
        return "nonmeasured"
    return None


def _resolve_definition_readings(
    definition_ids: set[str],
    supplied: tuple[ReplacementDefinitionReading, ...],
    legacy_old_final: Decimal | None,
    legacy_new_initial: Decimal | None,
) -> dict[str, ReplacementDefinitionReading]:
    if not supplied:
        if (legacy_old_final is not None or legacy_new_initial is not None) and len(
            definition_ids
        ) != 1:
            raise ValidationError(
                "複数の測定定義がある交換では、測定定義ごとの読値を指定してください。"
            )
        return {
            definition_id: ReplacementDefinitionReading(
                definition_id,
                legacy_old_final if len(definition_ids) == 1 else None,
                legacy_new_initial if len(definition_ids) == 1 else None,
            )
            for definition_id in sorted(definition_ids)
        }
    if legacy_old_final is not None or legacy_new_initial is not None:
        raise ValidationError("測定定義別読値と共通読値は同時に指定できません。")
    readings = {reading.measurement_definition_id: reading for reading in supplied}
    if len(readings) != len(supplied):
        raise ValidationError("測定定義別読値が重複しています。")
    if set(readings) != definition_ids:
        raise ValidationError("測定定義別読値が交換対象と一致しません。")
    return {definition_id: readings[definition_id] for definition_id in sorted(definition_ids)}


def _validate_replacement_definition_reading(
    reading: ReplacementDefinitionReading,
    definition: MeasurementDefinitionRecord,
    old_assignment: MeterAssignmentRecord,
    new_assignment: MeterAssignmentRecord,
) -> None:
    if definition.value_schema is not ValueSchema.DECIMAL_STRING:
        if reading.old_final_reading is not None or reading.new_initial_reading is not None:
            raise ValidationError("数値形式でない測定定義に読値は指定できません。")
        return
    if reading.old_final_reading is not None:
        validate_assignment_reading(reading.old_final_reading, definition, old_assignment)
    if reading.new_initial_reading is not None:
        validate_assignment_reading(reading.new_initial_reading, definition, new_assignment)
