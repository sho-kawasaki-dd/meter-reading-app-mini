"""保存済み検針結果と交換境界から、期間別の使用量を集計する。"""

from __future__ import annotations

from bisect import bisect_left, bisect_right
from collections.abc import Callable, Collection, Sequence
from contextlib import AbstractContextManager
from dataclasses import dataclass, replace
from datetime import date, datetime, timedelta
from decimal import Decimal

from meter_reading_mini.core.aggregation_contracts import (
    AggregationApplication,
    AggregationBaseline,
    AggregationBaselineSource,
    AggregationCode,
    AggregationCycleGroup,
    AggregationDetail,
    AggregationDetailSource,
    AggregationFormulaRecord,
    AggregationFormulaVersionRecord,
    AggregationIssue,
    AggregationPeriod,
    AggregationPeriodUnit,
    AggregationReport,
    AggregationResult,
    AggregationTargetType,
    parse_aggregation_coefficient,
    serialize_aggregation_coefficient,
    validate_aggregation_term_attributes,
)
from meter_reading_mini.core.contract_models import (
    CalculationMethod,
    Category,
    Purpose,
    ResultStatus,
    ValueSchema,
)
from meter_reading_mini.core.decimal_arithmetic import exact_add, exact_multiply
from meter_reading_mini.core.errors import FormulaValidationError, ValidationError
from meter_reading_mini.core.meter_reading_sync_repositories import (
    AggregationBoundaryRecord,
    AggregationReadingRecord,
    AggregationReportRepository,
    AssignmentReviewFlagRecord,
    MeasurementDefinitionRecord,
    MeterAssignmentRecord,
    PlannedReadingTaskState,
)
from meter_reading_mini.core.timezone_settings import build_business_date_provider

type _Event = AggregationReadingRecord | AggregationBoundaryRecord
type _ReviewKey = tuple[str, str, datetime]
type _TransactionFactory = Callable[[], AbstractContextManager[object]]


@dataclass(frozen=True, slots=True)
class _Observation:
    source: AggregationDetailSource
    source_id: str
    definition_id: str
    location_id: str
    business_date: date
    at: datetime
    status: str
    baseline: AggregationBaseline | None
    source_usage: Decimal | None
    included: bool
    issues: tuple[AggregationIssue, ...]


@dataclass(frozen=True, slots=True)
class _FormulaSelection:
    formula: AggregationFormulaRecord
    versions: tuple[AggregationFormulaVersionRecord, ...]


def _event_at(event: _Event) -> datetime:
    return event.scheduled_at if isinstance(event, AggregationReadingRecord) else event.replaced_at


def _event_id(event: _Event) -> str:
    if isinstance(event, AggregationReadingRecord):
        return event.planned_reading_id
    return event.replacement_event_id


def _definition_location(definition: MeasurementDefinitionRecord) -> str:
    if not definition.location_id:
        raise ValidationError(f"測定定義の場所がありません: {definition.measurement_definition_id}")
    return definition.location_id


def _definition_metadata(
    definition: MeasurementDefinitionRecord,
) -> tuple[str, Category, str, Purpose]:
    if not definition.display_name or definition.category is None or not definition.unit:
        raise ValidationError(f"測定定義の属性が不正です: {definition.measurement_definition_id}")
    if definition.purpose is None:
        raise ValidationError(f"測定定義の目的がありません: {definition.measurement_definition_id}")
    return definition.display_name, definition.category, definition.unit, definition.purpose


def _eligible(definition: MeasurementDefinitionRecord) -> bool:
    return (
        definition.calculation_method is CalculationMethod.DELTA
        and definition.value_schema is ValueSchema.DECIMAL_STRING
    )


def _applications(
    versions: Sequence[AggregationFormulaVersionRecord], start: date, end: date
) -> tuple[AggregationApplication, ...]:
    applications: list[AggregationApplication] = []
    for index, version in enumerate(versions):
        next_start = versions[index + 1].effective_from_date if index + 1 < len(versions) else end
        active_start = max(start, version.effective_from_date)
        active_end = min(end, next_start)
        if version.is_active and active_start < active_end:
            applications.append(AggregationApplication(active_start, active_end, version))
    return tuple(applications)


def _period_boundaries(day: date, unit: AggregationPeriodUnit) -> tuple[date, date]:
    try:
        if unit is AggregationPeriodUnit.DAY:
            return day, day + timedelta(days=1)
        if unit is AggregationPeriodUnit.MONTH:
            start = date(day.year, day.month, 1)
            end = date(day.year + 1, 1, 1) if day.month == 12 else date(day.year, day.month + 1, 1)
            return start, end
        if unit is AggregationPeriodUnit.CALENDAR_YEAR:
            return date(day.year, 1, 1), date(day.year + 1, 1, 1)
        year = day.year if day.month >= 4 else day.year - 1
        return date(year, 4, 1), date(year + 1, 4, 1)
    except (OverflowError, ValueError) as exc:
        raise ValidationError("集計期間の境界を表現できません。") from exc


def _validate_reading(reading: AggregationReadingRecord) -> None:
    label = f"予定枠 {reading.planned_reading_id}"
    if reading.task_state is PlannedReadingTaskState.COMPLETED:
        if reading.result_status is None:
            raise ValidationError(f"{label}は完了ですが結果がありません。")
    elif (
        reading.task_state in {PlannedReadingTaskState.OPEN, PlannedReadingTaskState.AUTO_CLOSED}
        and reading.result_status is not None
    ):
        raise ValidationError(f"{label}は未完了ですが結果があります。")
    if reading.result_status is None:
        if any(
            value is not None
            for value in (
                reading.raw_reading,
                reading.last_reading,
                reading.usage_delta,
                reading.structured_payload_json,
            )
        ):
            raise ValidationError(f"{label}の状態と結果が一致しません。")
        return
    if reading.result_status is ResultStatus.MEASURED:
        if (
            reading.raw_reading is None
            or reading.usage_delta is None
            or reading.structured_payload_json is not None
        ):
            raise ValidationError(f"{label}の実測値または使用量が不正です。")
        if any(
            value is not None and value < 0
            for value in (reading.raw_reading, reading.last_reading, reading.usage_delta)
        ):
            raise ValidationError(f"{label}の実測値または使用量が負数です。")
    elif any(
        value is not None
        for value in (
            reading.raw_reading,
            reading.last_reading,
            reading.usage_delta,
            reading.structured_payload_json,
        )
    ):
        raise ValidationError(f"{label}の非実測結果に値が保存されています。")


class _DefinitionHistory:
    def __init__(
        self,
        definition: MeasurementDefinitionRecord,
        readings: Sequence[AggregationReadingRecord],
        boundaries: Sequence[AggregationBoundaryRecord],
        assignments: Sequence[MeterAssignmentRecord],
        review_flags: dict[_ReviewKey, AssignmentReviewFlagRecord],
    ) -> None:
        self.definition = definition
        self._readings = sorted(
            readings, key=lambda item: (item.scheduled_at, item.planned_reading_id)
        )
        self._assignments = tuple(assignments)
        self._review_flags = review_flags
        self._cache: dict[tuple[AggregationDetailSource, str], _Observation] = {}
        self._measured: dict[str, list[AggregationReadingRecord]] = {}
        self._measured_keys: dict[str, list[tuple[datetime, str]]] = {}
        self._gaps: list[AggregationReadingRecord] = []
        self._gap_keys: list[tuple[datetime, str]] = []
        for reading in self._readings:
            key = (reading.scheduled_at, reading.planned_reading_id)
            if reading.result_status is ResultStatus.MEASURED and reading.raw_reading is not None:
                self._measured.setdefault(reading.meter_id, []).append(reading)
                self._measured_keys.setdefault(reading.meter_id, []).append(key)
            if reading.task_state is not PlannedReadingTaskState.SUPERSEDED and (
                reading.task_state is not PlannedReadingTaskState.COMPLETED
                or reading.result_status is not ResultStatus.MEASURED
                or reading.raw_reading is None
                or reading.usage_delta is None
                or reading.structured_payload_json is not None
                or any(
                    value is not None and value < 0
                    for value in (reading.raw_reading, reading.last_reading, reading.usage_delta)
                )
            ):
                self._gaps.append(reading)
                self._gap_keys.append(key)
        self._incoming: dict[tuple[str, datetime], list[AggregationBoundaryRecord]] = {}
        for boundary in boundaries:
            self._incoming.setdefault(
                (boundary.new_meter_id, boundary.new_assignment_effective_from), []
            ).append(boundary)
        self._events: tuple[_Event, ...] = tuple(
            sorted(
                [
                    *(
                        reading
                        for reading in readings
                        if reading.task_state is not PlannedReadingTaskState.SUPERSEDED
                    ),
                    *boundaries,
                ],
                key=lambda event: (event.business_date, _event_at(event), _event_id(event)),
            )
        )
        self._event_dates = tuple(event.business_date for event in self._events)

    def events(self, start: date, end: date) -> tuple[_Event, ...]:
        return self._events[
            bisect_left(self._event_dates, start) : bisect_left(self._event_dates, end)
        ]

    def _assignment(self, meter_id: str, at: datetime, label: str) -> MeterAssignmentRecord:
        matches = [
            item
            for item in self._assignments
            if item.effective_from <= at and (item.effective_to is None or at < item.effective_to)
        ]
        if len(matches) != 1 or matches[0].meter_id != meter_id:
            raise ValidationError(f"{label}のメーター割当を一意に解決できません。")
        return matches[0]

    def _boundary_assignments(
        self, boundary: AggregationBoundaryRecord
    ) -> tuple[MeterAssignmentRecord, MeterAssignmentRecord]:
        old = [
            item
            for item in self._assignments
            if item.meter_id == boundary.old_meter_id
            and item.effective_from == boundary.old_assignment_effective_from
        ]
        new = [
            item
            for item in self._assignments
            if item.meter_id == boundary.new_meter_id
            and item.effective_from == boundary.new_assignment_effective_from
        ]
        if (
            len(old) != 1
            or len(new) != 1
            or old[0].effective_from >= boundary.replaced_at
            or old[0].effective_to != boundary.replaced_at
            or new[0].effective_from != boundary.replaced_at
            or (new[0].effective_to is not None and new[0].effective_to <= boundary.replaced_at)
        ):
            raise ValidationError(f"交換境界と割当が一致しません: {boundary.replacement_event_id}")
        return old[0], new[0]

    def _review_issues(
        self, assignments: Collection[MeterAssignmentRecord], source_id: str
    ) -> list[AggregationIssue]:
        issues: list[AggregationIssue] = []
        for assignment in assignments:
            if not assignment.needs_review:
                continue
            key = (
                assignment.measurement_definition_id,
                assignment.meter_id,
                assignment.effective_from,
            )
            flag = self._review_flags.get(key)
            if flag is None or not flag.reason.strip():
                raise ValidationError(f"割当条件の確認待ち理由がありません: {source_id}")
            issues.append(
                AggregationIssue(
                    AggregationCode.MISSING_ASSIGNMENT_PENDING,
                    f"割当条件が確認待ちのため使用量を除外しました: {flag.reason}",
                    measurement_definition_id=assignment.measurement_definition_id,
                    source_id=source_id,
                    meter_id=assignment.meter_id,
                    assignment_effective_from=assignment.effective_from,
                    resolution="割当条件の訂正画面で確認・訂正してください。",
                )
            )
        return issues

    def _baseline(
        self, assignment: MeterAssignmentRecord, at: datetime, before_id: str | None
    ) -> AggregationBaseline | None:
        previous = self._measured.get(assignment.meter_id, [])
        keys = self._measured_keys.get(assignment.meter_id, [])
        index = bisect_left(keys, (at, before_id or "")) - 1
        if index >= 0 and previous[index].scheduled_at >= assignment.effective_from:
            reading = previous[index]
            _validate_reading(reading)
            return AggregationBaseline(
                AggregationBaselineSource.READING,
                reading.planned_reading_id,
                reading.business_date,
                reading.scheduled_at,
                reading.raw_reading,
            )
        incoming = [
            boundary
            for boundary in self._incoming.get((assignment.meter_id, assignment.effective_from), [])
            if boundary.replaced_at <= at
        ]
        if len(incoming) > 1:
            raise ValidationError(f"メーターの交換基準が複数あります: {assignment.meter_id}")
        if not incoming:
            return None
        boundary = incoming[0]
        self._boundary_assignments(boundary)
        return AggregationBaseline(
            AggregationBaselineSource.REPLACEMENT_INITIAL,
            boundary.replacement_event_id,
            boundary.business_date,
            boundary.replaced_at,
            boundary.new_initial_reading,
        )

    def _gap_issue(
        self,
        baseline: AggregationBaseline | None,
        at: datetime,
        before_id: str | None,
        source_id: str,
    ) -> AggregationIssue | None:
        if baseline is None or baseline.reading is None:
            return None
        lower = (baseline.at, baseline.source_id)
        start = (
            bisect_right(self._gap_keys, lower)
            if baseline.source is AggregationBaselineSource.READING
            else bisect_left(self._gap_keys, (baseline.at, ""))
        )
        end = bisect_left(self._gap_keys, (at, before_id or ""))
        gaps: list[str] = []
        for reading in self._gaps[start:end]:
            _validate_reading(reading)
            if reading.result_status is not ResultStatus.MEASURED:
                gaps.append(reading.planned_reading_id)
        if not gaps:
            return None
        return AggregationIssue(
            AggregationCode.MISSING_GAP_BETWEEN_BASELINE,
            "前回基準と今回の間に未実測または非実測の予定枠があります。",
            self.definition.measurement_definition_id,
            source_id,
            tuple(gaps),
        )

    def _baseline_issues(
        self, baseline: AggregationBaseline | None, source_id: str
    ) -> list[AggregationIssue]:
        definition_id = self.definition.measurement_definition_id
        if baseline is None:
            return [
                AggregationIssue(
                    AggregationCode.MISSING_NO_BASELINE,
                    "前回基準がなく、保存済みの初回使用量 0 は実使用量を表しません。",
                    definition_id,
                    source_id,
                )
            ]
        if baseline.source is AggregationBaselineSource.REPLACEMENT_INITIAL:
            issues = [
                AggregationIssue(
                    AggregationCode.INFO_REPLACEMENT,
                    "交換時の新メーター初期値を前回基準に使用します。",
                    definition_id,
                    source_id,
                    (baseline.source_id,),
                )
            ]
            if baseline.reading is None:
                issues.append(
                    AggregationIssue(
                        AggregationCode.MISSING_REPLACEMENT_READING,
                        "交換時の新メーター初期値がありません。",
                        definition_id,
                        source_id,
                        (baseline.source_id,),
                    )
                )
            return issues
        return []

    def observe(self, event: _Event) -> _Observation:
        source = (
            AggregationDetailSource.READING
            if isinstance(event, AggregationReadingRecord)
            else AggregationDetailSource.REPLACEMENT
        )
        key = (source, _event_id(event))
        if key not in self._cache:
            self._cache[key] = (
                self._observe_reading(event)
                if isinstance(event, AggregationReadingRecord)
                else self._observe_boundary(event)
            )
        return self._cache[key]

    def _observe_reading(self, reading: AggregationReadingRecord) -> _Observation:
        _validate_reading(reading)
        if reading.location_id != _definition_location(self.definition):
            raise ValidationError(
                f"予定枠と測定定義の場所が一致しません: {reading.planned_reading_id}"
            )
        assignment = self._assignment(
            reading.meter_id, reading.scheduled_at, reading.planned_reading_id
        )
        issues = self._review_issues((assignment,), reading.planned_reading_id)
        baseline: AggregationBaseline | None = None
        included = False
        if reading.result_status is None:
            issues.append(
                AggregationIssue(
                    AggregationCode.MISSING_UNMEASURED,
                    "予定枠が未実測です。",
                    reading.measurement_definition_id,
                    reading.planned_reading_id,
                )
            )
        elif reading.result_status is not ResultStatus.MEASURED:
            issues.append(
                AggregationIssue(
                    AggregationCode.MISSING_NON_MEASURED,
                    f"予定枠は非実測です: {reading.result_status.value}",
                    reading.measurement_definition_id,
                    reading.planned_reading_id,
                )
            )
        elif not issues:
            baseline = self._baseline(assignment, reading.scheduled_at, reading.planned_reading_id)
            expected = baseline.reading if baseline is not None else None
            if reading.last_reading != expected or (expected is None and reading.usage_delta != 0):
                raise ValidationError(
                    f"予定枠の前回基準と保存済み使用量が一致しません: {reading.planned_reading_id}"
                )
            issues.extend(self._baseline_issues(baseline, reading.planned_reading_id))
            gap = self._gap_issue(
                baseline,
                reading.scheduled_at,
                reading.planned_reading_id,
                reading.planned_reading_id,
            )
            if gap is not None:
                issues.append(gap)
            included = True
        return _Observation(
            AggregationDetailSource.READING,
            reading.planned_reading_id,
            reading.measurement_definition_id,
            reading.location_id,
            reading.business_date,
            reading.scheduled_at,
            reading.result_status.value
            if reading.result_status is not None
            else reading.task_state.value,
            baseline,
            reading.usage_delta,
            included,
            tuple(issues),
        )

    def _observe_boundary(self, boundary: AggregationBoundaryRecord) -> _Observation:
        label = f"交換境界 {boundary.replacement_event_id}"
        if boundary.location_id != _definition_location(self.definition):
            raise ValidationError(f"{label}と測定定義の場所が一致しません。")
        if (boundary.previous_measured_reading is None) != (
            boundary.previous_measured_planned_reading_id is None
        ):
            raise ValidationError(f"{label}の前回実測値と参照先が一致しません。")
        if (boundary.old_final_reading is None) != (boundary.old_meter_usage_delta is None):
            raise ValidationError(f"{label}の旧最終値と使用量が一致しません。")
        if any(
            value is not None and value < 0
            for value in (
                boundary.previous_measured_reading,
                boundary.old_final_reading,
                boundary.old_meter_usage_delta,
                boundary.new_initial_reading,
            )
        ):
            raise ValidationError(f"{label}の読値または使用量が負数です。")
        old, new = self._boundary_assignments(boundary)
        issues = [
            AggregationIssue(
                AggregationCode.INFO_REPLACEMENT,
                "メーター交換を含みます。",
                boundary.measurement_definition_id,
                boundary.replacement_event_id,
            )
        ]
        pending = self._review_issues((old, new), boundary.replacement_event_id)
        issues.extend(pending)
        if boundary.old_final_reading is None:
            issues.append(
                AggregationIssue(
                    AggregationCode.MISSING_REPLACEMENT_READING,
                    "交換時の旧メーター最終値がありません。",
                    boundary.measurement_definition_id,
                    boundary.replacement_event_id,
                )
            )
        if boundary.new_initial_reading is None:
            issues.append(
                AggregationIssue(
                    AggregationCode.MISSING_REPLACEMENT_READING,
                    "交換時の新メーター初期値がありません。",
                    boundary.measurement_definition_id,
                    boundary.replacement_event_id,
                )
            )
        baseline: AggregationBaseline | None = None
        if not pending:
            baseline = self._baseline(old, boundary.replaced_at, None)
            expected_id = (
                baseline.source_id
                if baseline is not None and baseline.source is AggregationBaselineSource.READING
                else None
            )
            expected_value = (
                baseline.reading if expected_id is not None and baseline is not None else None
            )
            if (
                boundary.previous_measured_planned_reading_id != expected_id
                or boundary.previous_measured_reading != expected_value
            ):
                raise ValidationError(f"{label}の前回実測と保存済み基準が一致しません。")
            if boundary.old_final_reading is not None:
                if (
                    baseline is None or baseline.reading is None
                ) and boundary.old_meter_usage_delta != 0:
                    raise ValidationError(f"{label}の基準なし使用量が不正です。")
                issues.extend(self._baseline_issues(baseline, boundary.replacement_event_id))
                gap = self._gap_issue(
                    baseline, boundary.replaced_at, None, boundary.replacement_event_id
                )
                if gap is not None:
                    issues.append(gap)
        return _Observation(
            AggregationDetailSource.REPLACEMENT,
            boundary.replacement_event_id,
            boundary.measurement_definition_id,
            boundary.location_id,
            boundary.business_date,
            boundary.replaced_at,
            "replacement",
            baseline,
            boundary.old_meter_usage_delta,
            not pending and boundary.old_meter_usage_delta is not None,
            tuple(issues),
        )


def _detail(
    observation: _Observation,
    coefficient: Decimal,
    period_start: date,
    version: AggregationFormulaVersionRecord | None,
) -> AggregationDetail:
    issues = list(observation.issues)
    baseline = observation.baseline
    if (
        observation.included
        and baseline is not None
        and baseline.reading is not None
        and baseline.business_date < period_start
        and not any(issue.code is AggregationCode.MISSING_GAP_BETWEEN_BASELINE for issue in issues)
    ):
        issues.append(
            AggregationIssue(
                AggregationCode.INFO_CARRYOVER,
                "期間前の前回基準からの通常の繰越を含みます。",
                observation.definition_id,
                observation.source_id,
                (baseline.source_id,),
            )
        )
    return AggregationDetail(
        source=observation.source,
        source_id=observation.source_id,
        measurement_definition_id=observation.definition_id,
        location_id=observation.location_id,
        business_date=observation.business_date,
        at=observation.at,
        status=observation.status,
        baseline=baseline,
        source_usage=observation.source_usage,
        coefficient=coefficient,
        contribution=(
            exact_multiply(observation.source_usage, coefficient)
            if observation.included and observation.source_usage is not None
            else None
        ),
        version_effective_from=version.effective_from_date if version is not None else None,
        retroactive=version.is_retroactive if version is not None else False,
        issues=tuple(issues),
    )


def _total(details: Sequence[AggregationDetail]) -> Decimal | None:
    total: Decimal | None = None
    for detail in details:
        if detail.contribution is not None:
            total = detail.contribution if total is None else exact_add(total, detail.contribution)
    return total


def _disabled_issue(definition: MeasurementDefinitionRecord) -> AggregationIssue:
    return AggregationIssue(
        AggregationCode.INFO_DISABLED_DEFINITION,
        "現在は無効な測定定義の過去データを含みます。",
        definition.measurement_definition_id,
    )


def _definition_result(
    history: _DefinitionHistory | None,
    definition: MeasurementDefinitionRecord,
    start: date,
    end: date,
) -> AggregationResult:
    display_name, category, unit, purpose = _definition_metadata(definition)
    details = (
        tuple(
            _detail(history.observe(event), Decimal(1), start, None)
            for event in history.events(start, end)
        )
        if history is not None
        else ()
    )
    issues = [issue for detail in details for issue in detail.issues]
    if not definition.is_active:
        issues.append(_disabled_issue(definition))
    exclusion_reason = None
    if not _eligible(definition):
        exclusion_reason = (
            "力率は使用量集計の対象外です。"
            if definition.value_schema is ValueSchema.POWER_FACTOR_PERCENT_PHASE
            else "差分計算ではないため使用量集計の対象外です。"
        )
    return AggregationResult(
        target_type=AggregationTargetType.DEFINITION,
        target_id=definition.measurement_definition_id,
        display_name=display_name,
        purpose=purpose,
        category=category,
        unit=unit,
        decimal_places=definition.decimal_places,
        location_ids=(_definition_location(definition),),
        value=_total(details),
        details=details,
        issues=tuple(issues),
        exclusion_reason=exclusion_reason,
        uses_current_attributes=True,
    )


def _merge_intervals(values: Sequence[tuple[date, date]]) -> tuple[tuple[date, date], ...]:
    merged: list[tuple[date, date]] = []
    for start, end in sorted(values):
        if merged and start <= end and merged[-1][0] <= merged[-1][1] and start <= merged[-1][1]:
            previous_start, previous_end = merged[-1]
            merged[-1] = (previous_start, max(previous_end, end))
        else:
            merged.append((start, end))
    return tuple(merged)


def _period_mismatch(details: Sequence[AggregationDetail]) -> bool:
    intervals: dict[str, list[tuple[date, date]]] = {}
    for detail in details:
        if (
            detail.contribution is None
            or detail.baseline is None
            or detail.baseline.reading is None
        ):
            continue
        intervals.setdefault(detail.measurement_definition_id, []).append(
            (detail.baseline.business_date, detail.business_date)
        )
    return (
        len(intervals) > 1 and len({_merge_intervals(values) for values in intervals.values()}) > 1
    )


def _formula_result(
    selection: _FormulaSelection,
    definitions: dict[str, MeasurementDefinitionRecord],
    histories: dict[str, _DefinitionHistory],
    start: date,
    end: date,
) -> AggregationResult | None:
    formula, versions = selection.formula, selection.versions
    applications = _applications(versions, start, end)
    if not applications:
        return None
    details: list[AggregationDetail] = []
    calculated_applications: list[AggregationApplication] = []
    definition_ids: set[str] = set()
    location_ids: set[str] = set()
    for application in applications:
        first_detail = len(details)
        for term in application.version.terms:
            definition_ids.add(term.measurement_definition_id)
            definition = definitions[term.measurement_definition_id]
            location_ids.add(_definition_location(definition))
            history = histories[term.measurement_definition_id]
            details.extend(
                _detail(history.observe(event), term.coefficient, start, application.version)
                for event in history.events(application.start, application.end)
            )
        calculated_applications.append(
            replace(application, contribution=_total(details[first_detail:]))
        )
    details.sort(
        key=lambda item: (
            item.business_date,
            item.at,
            item.source_id,
            item.measurement_definition_id,
        )
    )
    value = _total(details)
    issues = [issue for detail in details for issue in detail.issues]
    for definition_id in sorted(definition_ids):
        if not definitions[definition_id].is_active:
            issues.append(_disabled_issue(definitions[definition_id]))
    with_values = {
        detail.measurement_definition_id for detail in details if detail.contribution is not None
    }
    missing_ids = tuple(sorted(definition_ids - with_values)) if with_values else ()
    if missing_ids:
        issues.append(
            AggregationIssue(
                AggregationCode.MISSING_PARTIAL_ITEMS,
                "一部の式項目に使用量がないため部分値です。",
                related_ids=missing_ids,
            )
        )
    if value is not None and value < 0:
        issues.append(AggregationIssue(AggregationCode.WARN_NEGATIVE, "集計式の結果が負数です。"))
    if formula.cycle_group in {
        AggregationCycleGroup.MONTHLY,
        AggregationCycleGroup.EVERY_TWO_MONTHS,
    } and _period_mismatch(details):
        issues.append(
            AggregationIssue(
                AggregationCode.WARN_PERIOD_MISMATCH,
                "式項目の検針期間が一致しません。明細の前回基準と今回業務日を確認してください。",
            )
        )
    if any(application.version.is_retroactive for application in applications):
        issues.append(
            AggregationIssue(
                AggregationCode.INFO_RETROACTIVE_FORMULA,
                "後から設定した初版による再集計です。",
            )
        )
    if any(start < version.effective_from_date < end for version in versions[1:]):
        issues.append(
            AggregationIssue(AggregationCode.INFO_VERSION_SWITCH, "期間内に式版が切り替わります。")
        )
    if len(location_ids) > 1:
        issues.append(
            AggregationIssue(AggregationCode.INFO_MULTI_LOCATION, "複数の場所を含む式です。")
        )
    covered_days = sum((application.end - application.start).days for application in applications)
    return AggregationResult(
        target_type=AggregationTargetType.FORMULA,
        target_id=formula.formula_id,
        display_name=formula.display_name,
        purpose=formula.purpose,
        category=formula.category,
        unit=formula.unit,
        decimal_places=formula.decimal_places,
        location_ids=tuple(sorted(location_ids)),
        value=value,
        details=tuple(details),
        issues=tuple(issues),
        applications=tuple(calculated_applications),
        missing_definition_ids=missing_ids,
        is_partial_application=covered_days < (end - start).days,
    )


class AggregationReportService:
    def __init__(
        self,
        repository: AggregationReportRepository,
        *,
        read_transaction_factory: _TransactionFactory,
        current_business_date_provider: Callable[[], date] | None = None,
    ) -> None:
        self._repository = repository
        self._read_transaction_factory = read_transaction_factory
        self._current_business_date_provider = (
            current_business_date_provider or build_business_date_provider()
        )

    def get_report(
        self,
        start: date,
        end: date,
        *,
        unit: AggregationPeriodUnit = AggregationPeriodUnit.DAY,
        purpose: Purpose = Purpose.OPERATIONAL,
        category: Category | None = None,
        location_id: str | None = None,
    ) -> AggregationReport:
        if type(start) is not date or type(end) is not date or start >= end:
            raise ValidationError("集計期間は開始日以上・終了日未満の日付で指定してください。")
        try:
            unit = AggregationPeriodUnit(unit)
            purpose = Purpose(purpose)
            category = Category(category) if category is not None else None
        except (TypeError, ValueError) as exc:
            raise ValidationError("集計単位・目的・カテゴリが不正です。") from exc
        if location_id is not None and (type(location_id) is not str or not location_id.strip()):
            raise ValidationError("集計場所 ID が不正です。")
        with self._read_transaction_factory():
            today = self._current_business_date_provider()
            if type(today) is not date:
                raise ValidationError("現在業務日は日付でなければなりません。")
            try:
                effective_end = min(end, today + timedelta(days=1))
            except OverflowError as exc:
                raise ValidationError("現在業務日の翌日を表現できません。") from exc
            if start >= effective_end:
                return AggregationReport(start, end, today, purpose, ())
            return self._build_report(
                start, end, effective_end, today, unit, purpose, category, location_id
            )

    def _build_report(
        self,
        start: date,
        requested_end: date,
        end: date,
        today: date,
        unit: AggregationPeriodUnit,
        purpose: Purpose,
        category: Category | None,
        location_id: str | None,
    ) -> AggregationReport:
        definitions = {
            definition.measurement_definition_id: definition
            for definition in self._repository.list_measurement_definitions(show_inactive=True)
        }
        direct: list[MeasurementDefinitionRecord] = []
        for definition in definitions.values():
            _, definition_category, _, definition_purpose = _definition_metadata(definition)
            if (
                definition_purpose == purpose
                and (category is None or category == definition_category)
                and (location_id is None or location_id == _definition_location(definition))
            ):
                direct.append(definition)

        selections: list[_FormulaSelection] = []
        for formula in self._repository.list_aggregation_formulas():
            if formula.purpose != purpose or (
                category is not None and formula.category != category
            ):
                continue
            versions = tuple(self._repository.list_aggregation_formula_versions(formula.formula_id))
            self._validate_versions(formula, versions)
            applications = _applications(versions, start, end)
            if not applications:
                continue
            for application in applications:
                for term in application.version.terms:
                    definition = definitions.get(term.measurement_definition_id)
                    if definition is None:
                        raise FormulaValidationError(
                            f"集計式の測定定義が見つかりません: {term.measurement_definition_id}"
                        )
                    validate_aggregation_term_attributes(
                        formula,
                        measurement_definition_id=definition.measurement_definition_id,
                        calculation_method=definition.calculation_method,
                        value_schema=definition.value_schema,
                        unit=definition.unit,
                        category=definition.category,
                        reading_cycle=definition.reading_cycle,
                    )
            if location_id is not None and not any(
                _definition_location(definitions[term.measurement_definition_id]) == location_id
                for application in applications
                for term in application.version.terms
            ):
                continue
            selections.append(_FormulaSelection(formula, versions))

        ids = {
            definition.measurement_definition_id for definition in direct if _eligible(definition)
        }
        ids.update(
            term.measurement_definition_id
            for selection in selections
            for application in _applications(selection.versions, start, end)
            for term in application.version.terms
        )
        readings = self._repository.list_aggregation_readings(ids)
        boundaries = self._repository.list_aggregation_boundaries(ids)
        flags = {
            (flag.measurement_definition_id, flag.meter_id, flag.assignment_effective_from): flag
            for flag in self._repository.list_assignment_review_flags()
        }
        readings_by_definition: dict[str, list[AggregationReadingRecord]] = {
            definition_id: [] for definition_id in ids
        }
        boundaries_by_definition: dict[str, list[AggregationBoundaryRecord]] = {
            definition_id: [] for definition_id in ids
        }
        for reading in readings:
            readings_by_definition[reading.measurement_definition_id].append(reading)
        for boundary in boundaries:
            boundaries_by_definition[boundary.measurement_definition_id].append(boundary)
        histories = {
            definition_id: _DefinitionHistory(
                definitions[definition_id],
                readings_by_definition[definition_id],
                boundaries_by_definition[definition_id],
                self._repository.list_meter_assignments(definition_id),
                flags,
            )
            for definition_id in ids
        }
        periods: list[AggregationPeriod] = []
        cursor = start
        while cursor < end:
            nominal_start, nominal_end = _period_boundaries(cursor, unit)
            period_end = min(end, nominal_end)
            results = [
                _definition_result(
                    histories.get(definition.measurement_definition_id),
                    definition,
                    cursor,
                    period_end,
                )
                for definition in direct
            ]
            for selection in selections:
                result = _formula_result(selection, definitions, histories, cursor, period_end)
                if result is not None:
                    results.append(result)
            periods.append(
                AggregationPeriod(
                    unit=unit,
                    start=cursor,
                    end=period_end,
                    nominal_start=nominal_start,
                    nominal_end=nominal_end,
                    is_in_progress=(
                        unit is not AggregationPeriodUnit.DAY
                        and nominal_start <= today < nominal_end
                        and period_end > today
                    ),
                    results=tuple(results),
                )
            )
            cursor = period_end
        return AggregationReport(start, requested_end, today, purpose, tuple(periods))

    @staticmethod
    def _validate_versions(
        formula: AggregationFormulaRecord, versions: Sequence[AggregationFormulaVersionRecord]
    ) -> None:
        if not versions or sum(version.is_initial for version in versions) != 1:
            raise FormulaValidationError(f"集計式の初版履歴が不正です: {formula.formula_id}")
        if any(
            version.formula_id != formula.formula_id
            or version.is_active != bool(version.terms)
            or (
                index > 0 and versions[index - 1].effective_from_date >= version.effective_from_date
            )
            for index, version in enumerate(versions)
        ):
            raise FormulaValidationError(f"集計式の版履歴が不正です: {formula.formula_id}")
        for version in versions:
            ids: set[str] = set()
            orders: set[int] = set()
            for term in version.terms:
                if (
                    not term.measurement_definition_id.strip()
                    or term.measurement_definition_id in ids
                    or type(term.sort_order) is not int
                    or term.sort_order < 0
                    or term.sort_order in orders
                ):
                    raise FormulaValidationError(f"集計式の項目が不正です: {formula.formula_id}")
                parse_aggregation_coefficient(serialize_aggregation_coefficient(term.coefficient))
                ids.add(term.measurement_definition_id)
                orders.add(term.sort_order)
