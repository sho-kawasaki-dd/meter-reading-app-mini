"""保存済みの検針履歴を読取スナップショットから組み立てる。"""

from __future__ import annotations

import calendar
from bisect import bisect_left
from collections.abc import Callable, Sequence
from contextlib import AbstractContextManager
from dataclasses import dataclass
from datetime import date, datetime, timedelta, tzinfo
from decimal import Decimal

from pydantic import ValidationError as PydanticValidationError

from meter_reading_mini.core.contract_models import (
    CalculationMethod,
    PowerFactorPhase,
    PowerFactorReading,
    PreviousReadingSource,
    ResultStatus,
    ValueSchema,
)
from meter_reading_mini.core.decimal_arithmetic import exact_add
from meter_reading_mini.core.errors import ValidationError
from meter_reading_mini.core.meter_reading_sync_repositories import (
    AssignmentReviewFlagRecord,
    MeasurementDefinitionRecord,
    MeterAssignmentRecord,
    PlannedReadingCloseReason,
    PlannedReadingTaskState,
    ReadingHistoryBoundaryRecord,
    ReadingHistoryReadingRecord,
    ReadingHistoryRepository,
)
from meter_reading_mini.core.reading_history_contracts import (
    ReadingHistory,
    ReadingHistoryCode,
    ReadingHistoryDefinitionOption,
    ReadingHistoryIssue,
    ReadingHistoryQuery,
    ReadingHistoryRow,
    ReadingHistoryRowKind,
)
from meter_reading_mini.core.timezone_settings import normalize_to_utc

type _TransactionFactory = Callable[[], AbstractContextManager[object]]

_WARNING_CODES = frozenset(
    {
        ReadingHistoryCode.STATE_MISMATCH,
        ReadingHistoryCode.BASELINE_MISMATCH,
        ReadingHistoryCode.ASSIGNMENT_UNRESOLVED,
        ReadingHistoryCode.ASSIGNMENT_PENDING,
        ReadingHistoryCode.REPLACEMENT_FINAL_MISSING,
        ReadingHistoryCode.REPLACEMENT_INITIAL_MISSING,
    }
)
_PHASE_LABELS = {
    PowerFactorPhase.LEAD: "進み",
    PowerFactorPhase.LAG: "遅れ",
    PowerFactorPhase.UNITY: "力率1",
}


def default_history_start_date(today: date) -> date:
    """前月の同日または前月末の翌日を返す。"""
    if type(today) is not date:
        raise ValidationError("現在業務日は日付でなければなりません。")
    try:
        year, month = (today.year - 1, 12) if today.month == 1 else (today.year, today.month - 1)
        previous = date(year, month, min(today.day, calendar.monthrange(year, month)[1]))
        return previous + timedelta(days=1)
    except (ValueError, OverflowError) as exc:
        raise ValidationError("履歴の既定期間を表現できません。") from exc


@dataclass(frozen=True, slots=True)
class _Baseline:
    source: PreviousReadingSource
    value: Decimal | None
    reference_id: str | None
    business_date: date | None


def _option(
    definition: MeasurementDefinitionRecord, locations: dict[str, str]
) -> ReadingHistoryDefinitionOption:
    if (
        not definition.location_id
        or definition.location_id not in locations
        or not definition.display_name
        or not definition.unit
        or definition.category is None
        or definition.purpose is None
    ):
        raise ValidationError(f"測定定義の属性が不正です: {definition.measurement_definition_id}")
    return ReadingHistoryDefinitionOption(
        definition.measurement_definition_id,
        definition.display_name,
        definition.location_id,
        locations[definition.location_id],
        definition.category,
        definition.unit,
        definition.purpose,
        definition.calculation_method,
        definition.value_schema,
        definition.is_active,
    )


class ReadingHistoryService:
    def __init__(
        self,
        repository: ReadingHistoryRepository,
        *,
        read_transaction_factory: _TransactionFactory,
        current_business_date_provider: Callable[[], date],
        timezone_provider: Callable[[], tzinfo],
    ) -> None:
        self._repository = repository
        self._transaction = read_transaction_factory
        self._today = current_business_date_provider
        self._timezone = timezone_provider

    def _definitions(self, *, include_inactive: bool) -> tuple[ReadingHistoryDefinitionOption, ...]:
        locations = {
            item.location_id: " / ".join(item.location_names) or item.location_name
            for item in self._repository.list_meter_locations()
        }
        return tuple(
            _option(item, locations)
            for item in self._repository.list_reading_history_definitions(
                show_inactive=include_inactive
            )
        )

    def list_definitions(
        self, *, include_inactive: bool
    ) -> tuple[ReadingHistoryDefinitionOption, ...]:
        with self._transaction():
            return self._definitions(include_inactive=include_inactive)

    def get_history(self, query: ReadingHistoryQuery) -> ReadingHistory:
        if (
            type(query.measurement_definition_id) is not str
            or not query.measurement_definition_id.strip()
            or type(query.start_date) is not date
            or type(query.end_date) is not date
            or query.start_date > query.end_date
            or type(query.include_superseded) is not bool
        ):
            raise ValidationError("履歴の取得条件が不正です。")
        with self._transaction():
            today = self._today()
            if type(today) is not date or query.end_date > today:
                raise ValidationError("履歴の期間に未来日を指定できません。")
            definition = next(
                (
                    item
                    for item in self._definitions(include_inactive=True)
                    if item.measurement_definition_id == query.measurement_definition_id
                ),
                None,
            )
            if definition is None:
                raise ValidationError("測定定義が見つかりません。")
            builder = _HistoryBuilder(
                definition,
                query,
                self._repository.list_reading_history_readings(query.measurement_definition_id),
                self._repository.list_reading_history_boundaries(query.measurement_definition_id),
                self._repository.list_meter_assignments(query.measurement_definition_id),
                self._repository.list_assignment_review_flags(),
                self._timezone(),
            )
            return builder.build()


class _HistoryBuilder:
    def __init__(
        self,
        definition: ReadingHistoryDefinitionOption,
        query: ReadingHistoryQuery,
        readings: Sequence[ReadingHistoryReadingRecord],
        boundaries: Sequence[ReadingHistoryBoundaryRecord],
        assignments: Sequence[MeterAssignmentRecord],
        flags: Sequence[AssignmentReviewFlagRecord],
        timezone: tzinfo,
    ) -> None:
        self.definition = definition
        self.query = query
        self.readings = readings
        self.boundaries = boundaries
        self.assignments = assignments
        self.flags = flags
        self.timezone = timezone
        self.is_delta = (
            definition.calculation_method is CalculationMethod.DELTA
            and definition.value_schema is ValueSchema.DECIMAL_STRING
        )
        self.by_id = {reading.planned_reading_id: reading for reading in readings}
        self.measured: dict[str, list[ReadingHistoryReadingRecord]] = {}
        self.measured_keys: dict[str, list[tuple[datetime, str]]] = {}
        for reading in sorted(
            readings, key=lambda item: (item.scheduled_at, item.planned_reading_id)
        ):
            if (
                reading.task_state is not PlannedReadingTaskState.SUPERSEDED
                and reading.result_status is ResultStatus.MEASURED
                and reading.raw_reading is not None
            ):
                self.measured.setdefault(reading.meter_id, []).append(reading)
                self.measured_keys.setdefault(reading.meter_id, []).append(
                    (reading.scheduled_at, reading.planned_reading_id)
                )

    def _assignments_at(self, at: datetime) -> list[MeterAssignmentRecord]:
        return [
            item
            for item in self.assignments
            if item.effective_from <= at and (item.effective_to is None or at < item.effective_to)
        ]

    def _boundary_assignment(
        self, boundary: ReadingHistoryBoundaryRecord, *, old: bool
    ) -> MeterAssignmentRecord | None:
        meter_id = boundary.old_meter_id if old else boundary.new_meter_id
        start = (
            boundary.old_assignment_effective_from
            if old
            else boundary.new_assignment_effective_from
        )
        matches = [
            item
            for item in self.assignments
            if item.meter_id == meter_id and item.effective_from == start
        ]
        if len(matches) != 1:
            return None
        item = matches[0]
        if old:
            return item if item.effective_from < boundary.replaced_at == item.effective_to else None
        return (
            item
            if item.effective_from == boundary.replaced_at
            and (item.effective_to is None or item.effective_to > boundary.replaced_at)
            else None
        )

    def _assignment_issues(
        self, assignment: MeterAssignmentRecord | None
    ) -> list[ReadingHistoryIssue]:
        if assignment is None:
            return [
                ReadingHistoryIssue(
                    ReadingHistoryCode.ASSIGNMENT_UNRESOLVED, "割当を一意に解決できません。"
                )
            ]
        flags = [
            flag
            for flag in self.flags
            if flag.measurement_definition_id == assignment.measurement_definition_id
            and flag.meter_id == assignment.meter_id
            and flag.assignment_effective_from == assignment.effective_from
        ]
        if assignment.needs_review or flags:
            reason = " / ".join(flag.reason for flag in flags if flag.reason.strip())
            if not assignment.needs_review or len(flags) != 1 or not reason:
                reason = "確認待ちの状態または理由が不整合です。"
            return [
                ReadingHistoryIssue(
                    ReadingHistoryCode.ASSIGNMENT_PENDING,
                    reason,
                    assignment.meter_id,
                    assignment.effective_from,
                )
            ]
        return []

    def _baseline(
        self, assignment: MeterAssignmentRecord, at: datetime, before_id: str
    ) -> _Baseline | None:
        index = bisect_left(self.measured_keys.get(assignment.meter_id, []), (at, before_id)) - 1
        if index >= 0:
            item = self.measured[assignment.meter_id][index]
            if item.scheduled_at >= assignment.effective_from:
                return _Baseline(
                    PreviousReadingSource.MEASURED,
                    item.raw_reading,
                    item.planned_reading_id,
                    item.business_date,
                )
        incoming = [
            item
            for item in self.boundaries
            if item.new_meter_id == assignment.meter_id
            and item.new_assignment_effective_from == assignment.effective_from
            and item.replaced_at == assignment.effective_from
        ]
        if len(incoming) == 1:
            item = incoming[0]
            source = (
                PreviousReadingSource.REPLACEMENT_INITIAL
                if item.new_initial_reading is not None
                else PreviousReadingSource.REPLACEMENT_NO_INITIAL
            )
            return _Baseline(
                source, item.new_initial_reading, item.replacement_event_id, item.business_date
            )
        if incoming or (
            self.assignments
            and assignment != min(self.assignments, key=lambda item: item.effective_from)
        ):
            return None
        if (
            not incoming
            and self.assignments
            and assignment == min(self.assignments, key=lambda item: item.effective_from)
            and assignment.initial_reading is not None
        ):
            return _Baseline(
                PreviousReadingSource.DEFINITION_INITIAL,
                assignment.initial_reading,
                self.definition.measurement_definition_id,
                assignment.effective_from.astimezone(self.timezone).date(),
            )
        return _Baseline(PreviousReadingSource.NONE, None, None, None)

    def _baseline_issues(self, baseline: _Baseline, issues: list[ReadingHistoryIssue]) -> None:
        if baseline.business_date is not None and not (
            self.query.start_date <= baseline.business_date <= self.query.end_date
        ):
            issues.append(
                ReadingHistoryIssue(
                    ReadingHistoryCode.BASELINE_OUTSIDE_PERIOD, "前回基準は表示期間外です。"
                )
            )

    def _replacement_successor(
        self, reading: ReadingHistoryReadingRecord
    ) -> ReadingHistoryReadingRecord | None:
        if (
            reading.task_state is not PlannedReadingTaskState.SUPERSEDED
            or reading.close_reason is not PlannedReadingCloseReason.METER_REPLACED
        ):
            return None
        current = reading
        visited: set[str] = set()
        while (
            current.task_state is PlannedReadingTaskState.SUPERSEDED
            and current.close_reason is PlannedReadingCloseReason.METER_REPLACED
        ):
            if current.planned_reading_id in visited:
                return None
            visited.add(current.planned_reading_id)
            successor = self.by_id.get(current.superseded_by_planned_reading_id or "")
            if successor is None or not (
                successor.measurement_definition_id == current.measurement_definition_id
                and successor.location_id == current.location_id
                and successor.business_date == current.business_date
                and successor.reading_session == current.reading_session
                and any(
                    boundary.old_meter_id == current.meter_id
                    and boundary.replaced_at <= current.scheduled_at
                    for boundary in self.boundaries
                )
            ):
                return None
            current = successor
        return current

    def _reading(self, reading: ReadingHistoryReadingRecord) -> ReadingHistoryRow:
        issues: list[ReadingHistoryIssue] = []
        is_superseded = reading.task_state is PlannedReadingTaskState.SUPERSEDED
        successor = self._replacement_successor(reading)
        replacement_superseded = successor is not None
        if is_superseded:
            issues.append(
                ReadingHistoryIssue(ReadingHistoryCode.SUPERSEDED, "差替え済みの予定枠です。")
            )
        invalid = reading.location_id != self.definition.location_id
        if is_superseded and reading.close_reason is PlannedReadingCloseReason.METER_REPLACED:
            invalid |= not replacement_superseded
            invalid |= reading.result_status is ResultStatus.MEASURED
        elif not is_superseded and reading.superseded_by_planned_reading_id is not None:
            invalid = True
        if reading.task_state is PlannedReadingTaskState.COMPLETED:
            invalid |= reading.result_status is None
        elif not is_superseded:
            invalid |= reading.result_status is not None
        values = (
            reading.raw_reading,
            reading.last_reading,
            reading.usage_delta,
            reading.structured_payload_json,
        )
        power_factor = None
        if reading.result_status is ResultStatus.MEASURED:
            if self.definition.value_schema is ValueSchema.POWER_FACTOR_PERCENT_PHASE:
                invalid |= any(value is not None for value in values[:3])
                try:
                    payload = PowerFactorReading.model_validate_json(
                        reading.structured_payload_json or ""
                    )
                    power_factor = (
                        f"{format(payload.percent, 'f')}%（{_PHASE_LABELS[payload.phase]}）"
                    )
                except PydanticValidationError:
                    invalid = True
            elif self.is_delta:
                invalid |= (
                    reading.raw_reading is None
                    or reading.usage_delta is None
                    or values[3] is not None
                )
            else:
                invalid |= reading.raw_reading is None or any(
                    value is not None for value in values[1:]
                )
        else:
            invalid |= any(value is not None for value in values)
        invalid |= any(value is not None and value < 0 for value in values[:3])
        if invalid:
            issues.append(
                ReadingHistoryIssue(
                    ReadingHistoryCode.STATE_MISMATCH, "状態と保存値または参照が不整合です。"
                )
            )
        active = self._assignments_at(reading.scheduled_at)
        assignment = (
            active[0] if len(active) == 1 and active[0].meter_id == reading.meter_id else None
        )
        valid_replacement_assignment = (
            successor is not None and len(active) == 1 and active[0].meter_id != reading.meter_id
        )
        if not (valid_replacement_assignment and assignment is None):
            issues.extend(self._assignment_issues(assignment))
        if not self.definition.is_active or not reading.meter_is_active:
            issues.append(
                ReadingHistoryIssue(ReadingHistoryCode.INACTIVE_MASTER, "無効化済みのマスタです。")
            )
        source = None
        reference = None
        if (
            self.is_delta
            and not is_superseded
            and reading.result_status is ResultStatus.MEASURED
            and assignment
        ):
            baseline = self._baseline(assignment, reading.scheduled_at, reading.planned_reading_id)
            if baseline is None:
                issues.append(
                    ReadingHistoryIssue(
                        ReadingHistoryCode.BASELINE_MISMATCH, "前回基準を一意に解決できません。"
                    )
                )
            elif reading.last_reading != baseline.value:
                issues.append(
                    ReadingHistoryIssue(
                        ReadingHistoryCode.BASELINE_MISMATCH,
                        f"保存済み前回値と解決した基準 ({baseline.value}, "
                        f"{baseline.reference_id}) が一致しません。",
                    )
                )
            else:
                source, reference = baseline.source, baseline.reference_id
                self._baseline_issues(baseline, issues)
                if baseline.value is None and reading.usage_delta == 0:
                    issues.append(
                        ReadingHistoryIssue(
                            ReadingHistoryCode.NO_BASELINE,
                            "前回基準がないため使用量 0 は参考値です。",
                        )
                    )
                elif baseline.value is None and reading.usage_delta is not None:
                    issues.append(
                        ReadingHistoryIssue(
                            ReadingHistoryCode.STATE_MISMATCH,
                            "前回基準がない実測の使用量が 0 ではありません。",
                        )
                    )
                if (
                    reading.raw_reading is not None
                    and baseline.value is not None
                    and reading.raw_reading < baseline.value
                    and assignment.rollover_value is not None
                ):
                    issues.append(
                        ReadingHistoryIssue(
                            ReadingHistoryCode.ROLLOVER, "ロールオーバー補正を含みます。"
                        )
                    )
        return ReadingHistoryRow(
            ReadingHistoryRowKind.READING,
            reading.business_date,
            reading.scheduled_at.astimezone(self.timezone),
            reading.reading_session,
            reading.task_state,
            reading.result_status,
            reading.meter_id,
            reading.meter_name,
            reading.meter_serial,
            reading.raw_reading
            if self.definition.value_schema is ValueSchema.DECIMAL_STRING
            else None,
            reading.last_reading if self.is_delta else None,
            source,
            reference,
            assignment.multiplier if assignment else None,
            assignment.rollover_value if assignment else None,
            reading.usage_delta if self.is_delta else None,
            power_factor,
            reading.recorded_at.astimezone(self.timezone) if reading.recorded_at else None,
            reading.updated_by,
            reading.result_source,
            reading.note,
            None,
            reading.planned_reading_id,
            tuple(issues),
        )

    def _boundary(self, boundary: ReadingHistoryBoundaryRecord, *, old: bool) -> ReadingHistoryRow:
        issues: list[ReadingHistoryIssue] = []
        assignment = self._boundary_assignment(boundary, old=old)
        issues.extend(self._assignment_issues(assignment))
        if not self.definition.is_active or not (
            boundary.old_meter_is_active if old else boundary.new_meter_is_active
        ):
            issues.append(
                ReadingHistoryIssue(ReadingHistoryCode.INACTIVE_MASTER, "無効化済みのマスタです。")
            )
        raw = boundary.old_final_reading if old else boundary.new_initial_reading
        usage = boundary.old_meter_usage_delta if old else None
        last = None
        source = None
        reference = None
        if boundary.location_id != self.definition.location_id or any(
            value is not None and value < 0
            for value in (raw, usage, boundary.previous_measured_reading)
        ):
            issues.append(
                ReadingHistoryIssue(
                    ReadingHistoryCode.STATE_MISMATCH, "交換境界の場所または保存値が不正です。"
                )
            )
        if self.is_delta and raw is None:
            issues.append(
                ReadingHistoryIssue(
                    ReadingHistoryCode.REPLACEMENT_FINAL_MISSING
                    if old
                    else ReadingHistoryCode.REPLACEMENT_INITIAL_MISSING,
                    "交換時の読値がありません。",
                )
            )
        if old and self.is_delta:
            last = boundary.previous_measured_reading
            reference = boundary.previous_measured_planned_reading_id
            if assignment:
                baseline = self._baseline(assignment, boundary.replaced_at, "")
                matches = baseline is not None and (
                    (last == baseline.value and reference == baseline.reference_id)
                    if baseline.source is PreviousReadingSource.MEASURED
                    else last is None and reference is None
                )
                if matches and baseline is not None:
                    last, source, reference = baseline.value, baseline.source, baseline.reference_id
                    self._baseline_issues(baseline, issues)
                    if (
                        raw is not None
                        and last is not None
                        and raw < last
                        and assignment.rollover_value is not None
                    ):
                        issues.append(
                            ReadingHistoryIssue(
                                ReadingHistoryCode.ROLLOVER, "ロールオーバー補正を含みます。"
                            )
                        )
                else:
                    detail = (
                        f" ({baseline.value}, {baseline.reference_id})"
                        if baseline is not None
                        else ""
                    )
                    issues.append(
                        ReadingHistoryIssue(
                            ReadingHistoryCode.BASELINE_MISMATCH,
                            f"保存済み前回値と解決した基準{detail}が一致しません。",
                        )
                    )
            elif (last is None) != (reference is None):
                issues.append(
                    ReadingHistoryIssue(
                        ReadingHistoryCode.BASELINE_MISMATCH,
                        "保存済み前回値と参照 ID が揃っていません。",
                    )
                )
            if (raw is None) != (usage is None):
                issues.append(
                    ReadingHistoryIssue(
                        ReadingHistoryCode.STATE_MISMATCH, "交換時の旧読値と使用量が不整合です。"
                    )
                )
        elif old and (
            boundary.old_meter_usage_delta is not None
            or (
                self.definition.value_schema is ValueSchema.POWER_FACTOR_PERCENT_PHASE
                and boundary.old_final_reading is not None
            )
        ):
            issues.append(
                ReadingHistoryIssue(
                    ReadingHistoryCode.STATE_MISMATCH, "交換境界の保存値が測定方式と不整合です。"
                )
            )
        if (
            not old
            and self.definition.value_schema is ValueSchema.POWER_FACTOR_PERCENT_PHASE
            and boundary.new_initial_reading is not None
        ):
            issues.append(
                ReadingHistoryIssue(
                    ReadingHistoryCode.STATE_MISMATCH, "交換境界の保存値が測定方式と不整合です。"
                )
            )
        return ReadingHistoryRow(
            ReadingHistoryRowKind.REPLACEMENT_OLD_FINAL
            if old
            else ReadingHistoryRowKind.REPLACEMENT_NEW_INITIAL,
            boundary.business_date,
            boundary.replaced_at.astimezone(self.timezone),
            None,
            None,
            None,
            boundary.old_meter_id if old else boundary.new_meter_id,
            boundary.old_meter_name if old else boundary.new_meter_name,
            boundary.old_meter_serial if old else boundary.new_meter_serial,
            raw if self.definition.value_schema is ValueSchema.DECIMAL_STRING else None,
            last,
            source,
            reference,
            assignment.multiplier if assignment else None,
            assignment.rollover_value if assignment else None,
            usage if self.is_delta else None,
            None,
            None,
            boundary.updated_by,
            None,
            boundary.note,
            boundary.replacement_event_id,
            None,
            tuple(issues),
        )

    def build(self) -> ReadingHistory:
        rows = [self._reading(item) for item in self.readings]
        for boundary in self.boundaries:
            rows.extend((self._boundary(boundary, old=True), self._boundary(boundary, old=False)))
        order = {
            ReadingHistoryRowKind.REPLACEMENT_OLD_FINAL: 0,
            ReadingHistoryRowKind.REPLACEMENT_NEW_INITIAL: 1,
            ReadingHistoryRowKind.READING: 2,
        }
        rows.sort(
            key=lambda row: (
                normalize_to_utc(row.at),
                order[row.row_kind],
                row.planned_reading_id or row.replacement_event_id or "",
            )
        )
        selected = tuple(
            row
            for row in rows
            if self.query.start_date <= row.business_date <= self.query.end_date
            and (
                self.query.include_superseded
                or row.task_state is not PlannedReadingTaskState.SUPERSEDED
            )
        )
        total: Decimal | None = None
        if self.is_delta:
            for row in selected:
                if (
                    row.task_state is not PlannedReadingTaskState.SUPERSEDED
                    and row.usage_delta is not None
                ):
                    total = row.usage_delta if total is None else exact_add(total, row.usage_delta)
        warnings = sum(
            any(issue.code in _WARNING_CODES for issue in row.issues) for row in selected
        )
        return ReadingHistory(self.definition, self.query, selected, total, warnings)
