"""スマホ検針結果の一括プレビューと確定を行う。"""

from __future__ import annotations

import json
from dataclasses import dataclass, replace
from datetime import date, datetime
from decimal import Decimal
from enum import StrEnum

from pydantic import ValidationError as PydanticValidationError

from meter_reading_mini.core.assignment_calculation import resolve_planned_assignment
from meter_reading_mini.core.contract_models import (
    CalculationMethod,
    MeterReadingSubmission,
    MeterReadingSyncRequest,
    ResultStatus,
)
from meter_reading_mini.core.errors import (
    AssignmentNeedsReviewError,
    DomainError,
    MissingPlannedAssignmentError,
    NegativeUsageDeltaError,
    SupersededPlannedReadingError,
    ValidationError,
)
from meter_reading_mini.core.meter_reading_sync import (
    InvalidReadingSubmissionError,
    MeterReadingSyncService,
    MissingMeasurementDefinitionError,
    MissingPlannedReadingError,
    ReadingSyncBatchResult,
    build_reading_upsert_request,
)
from meter_reading_mini.core.meter_reading_sync_repositories import (
    MobileReadingImportRepository,
    MobileReadingImportResultRecord,
    MobileReadingImportTargetRecord,
    PlannedReadingCloseReason,
    PlannedReadingTaskState,
    ReadingConfirmationContext,
    ReplacementBoundaryCalculationRecord,
    ReplacementBoundaryCalculationUpdate,
    ResultSource,
    UpsertDisposition,
)
from meter_reading_mini.core.mobile_reading import (
    MobileReadingResult,
    SheetFilter,
    cycle_group_for,
    parse_result_header_timestamp,
    validate_note,
    validate_sheet_id,
    validate_unique_planned_reading_ids,
)
from meter_reading_mini.core.mobile_reading_import_snapshot import (
    MobileReadingDatabaseSnapshot,
    PreviewRepositoryRecorder,
)
from meter_reading_mini.core.previous_reading import (
    InitialPreviousReading,
    describe_initial_previous_reading,
)
from meter_reading_mini.core.reading_comparison import serialize_structured_payload
from meter_reading_mini.core.reading_write_orchestration import (
    CalculatedReading,
    TransactionFactory,
)
from meter_reading_mini.core.usage_delta_calculation import UsageDeltaCalculationService
from meter_reading_mini.core.virtual_reading_history import (
    VirtualReadingHistory,
    VirtualReadingRejected,
    VirtualRecalculationError,
    VirtualRecalculationStage,
)


class MobileReadingDisposition(StrEnum):
    NEW = "new"
    REVIVED = "revived"
    OVERWRITTEN = "overwritten"
    DUPLICATE = "duplicate"
    STALE = "stale"
    REJECTED = "rejected"


class MobileReadingRejectCode(StrEnum):
    UNKNOWN_PLANNED_READING = "unknown_planned_reading"
    SUPERSEDED = "superseded"
    UNKNOWN_MEASUREMENT_DEFINITION = "unknown_measurement_definition"
    MISSING_ASSIGNMENT = "missing_assignment"
    ASSIGNMENT_NEEDS_REVIEW = "assignment_needs_review"
    INVALID_INPUT = "invalid_input"
    NEGATIVE_USAGE = "negative_usage"
    DOWNSTREAM_RECALCULATION = "downstream_recalculation"
    BOUNDARY_RECALCULATION = "boundary_recalculation"
    BUSINESS_RULE = "business_rule"


class MobileReadingWarningCode(StrEnum):
    BUSINESS_DATE_MISMATCH = "business_date_mismatch"
    READING_SESSION_MISMATCH = "reading_session_mismatch"
    CYCLE_GROUP_MISMATCH = "cycle_group_mismatch"
    THRESHOLD_EXCEEDED = "threshold_exceeded"
    ROLLOVER_APPLIED = "rollover_applied"


class MobileReadingPreviewChangedError(ValidationError):
    """確定前後の入力・DB 状態または保存結果がプレビューと異なる。"""

    def __init__(self) -> None:
        super().__init__("検針結果または保存状態が変わりました。再度プレビューしてください。")


class MobileReadingNothingToCommitError(ValidationError):
    """採用できる検針結果がない。"""

    def __init__(self) -> None:
        super().__init__("確定できる検針結果がありません。")


@dataclass(frozen=True, slots=True)
class MobileReadingInputEntry:
    planned_reading_id: str
    result_status: ResultStatus
    raw_reading: str | None
    structured_payload_json: str | None
    recorded_at: str
    client_updated_at: str
    note: str | None

    def to_submission(self) -> MeterReadingSubmission:
        payload: object = (
            json.loads(self.structured_payload_json)
            if self.structured_payload_json is not None
            else None
        )
        return MeterReadingSubmission.model_validate(
            {
                "planned_reading_id": self.planned_reading_id,
                "result_status": self.result_status,
                "raw_reading": self.raw_reading,
                "structured_payload": payload,
                "recorded_at": self.recorded_at,
                "client_updated_at": self.client_updated_at,
                "note": self.note,
            }
        )


@dataclass(frozen=True, slots=True)
class MobileReadingInputSnapshot:
    sheet_id: str
    business_date: date | None
    generated_at: str | None
    filter: SheetFilter | None
    entries: tuple[MobileReadingInputEntry, ...]

    def to_result(self) -> MobileReadingResult:
        return MobileReadingResult(
            sheet_id=self.sheet_id,
            business_date=self.business_date,
            generated_at=self.generated_at,
            filter=self.filter,
            entries=tuple(entry.to_submission() for entry in self.entries),
        )


@dataclass(frozen=True, slots=True)
class MobileReadingRejectReason:
    code: MobileReadingRejectCode
    planned_reading_id: str
    measurement_definition_id: str | None = None
    meter_id: str | None = None
    replacement_event_id: str | None = None
    cause: MobileReadingRejectCode | None = None
    raw_reading: Decimal | None = None
    last_reading: Decimal | None = None
    rollover_value: Decimal | None = None


@dataclass(frozen=True, slots=True)
class MobileReadingWarning:
    code: MobileReadingWarningCode
    expected: tuple[str, ...] | None = None
    actual: str | None = None


@dataclass(frozen=True, slots=True)
class MobileReadingCalculation:
    previous: InitialPreviousReading
    usage_delta: Decimal | None
    rollover_applied: bool | None
    warning_threshold_exceeded: bool | None


@dataclass(frozen=True, slots=True)
class MobileReadingProjectedResult:
    result_status: ResultStatus
    raw_reading: Decimal | None
    structured_payload_json: str | None
    note: str | None
    recorded_at: datetime
    client_updated_at: datetime
    result_source: ResultSource
    device_id: str
    updated_by: str | None
    signature: str
    last_reading: Decimal | None
    usage_delta: Decimal | None
    arrival_sequence: int


@dataclass(frozen=True, slots=True)
class MobileReadingFinalEstimate:
    task_state: PlannedReadingTaskState
    closed_at: datetime | None
    close_reason: PlannedReadingCloseReason | None
    result: MobileReadingProjectedResult | None
    previous: InitialPreviousReading | None
    calculation_error: MobileReadingRejectReason | None
    rollover_applied: bool | None
    warning_threshold_exceeded: bool | None


@dataclass(frozen=True, slots=True)
class MobileReadingPreviewRow:
    row_number: int
    input: MobileReadingInputEntry
    target: MobileReadingImportTargetRecord | None
    disposition: MobileReadingDisposition
    reason: MobileReadingRejectReason | None
    input_calculation: MobileReadingCalculation | None
    final: MobileReadingFinalEstimate | None
    warnings: tuple[MobileReadingWarning, ...]


@dataclass(frozen=True, slots=True)
class MobileReadingPreviewSummary:
    new: int
    revived: int
    overwritten: int
    duplicate: int
    stale: int
    rejected: int
    recalculated_existing: int
    recalculated_boundaries: int

    @property
    def accepted(self) -> int:
        return self.new + self.revived + self.overwritten


@dataclass(frozen=True, slots=True)
class MobileReadingExpectedEffects:
    results: tuple[tuple[str, MobileReadingProjectedResult], ...]
    accepted_reading_ids: frozenset[str]
    recalculated_existing_ids: frozenset[str]
    boundary_updates: tuple[ReplacementBoundaryCalculationUpdate, ...]


@dataclass(frozen=True, slots=True)
class MobileReadingImportPreview:
    input: MobileReadingInputSnapshot
    database: MobileReadingDatabaseSnapshot
    rows: tuple[MobileReadingPreviewRow, ...]
    summary: MobileReadingPreviewSummary
    effects: MobileReadingExpectedEffects

    @property
    def can_commit(self) -> bool:
        return self.summary.accepted > 0


@dataclass(frozen=True, slots=True)
class MobileReadingCommitRow:
    row_number: int
    planned_reading_id: str
    disposition: MobileReadingDisposition
    save_disposition: UpsertDisposition | None
    reason: MobileReadingRejectReason | None
    reason_message: str | None


@dataclass(frozen=True, slots=True)
class MobileReadingRejectionSummary:
    code: MobileReadingRejectCode
    message: str
    count: int


@dataclass(frozen=True, slots=True)
class MobileReadingCommitSummary:
    confirmed: int
    new: int
    revived: int
    overwritten: int
    duplicate: int
    stale: int
    rejected: int
    recalculated_existing: int
    recalculated_boundaries: int
    rejection_reasons: tuple[MobileReadingRejectionSummary, ...]

    @property
    def message(self) -> str:
        summary = (
            f"確定 {self.confirmed} 件、重複 {self.duplicate} 件、"
            f"古い入力 {self.stale} 件、拒否 {self.rejected} 件"
        )
        if self.rejection_reasons:
            reasons = "、".join(
                f"{reason.message} {reason.count} 件" for reason in self.rejection_reasons
            )
            return f"{summary}\n拒否理由: {reasons}"
        return summary


@dataclass(frozen=True, slots=True)
class MobileReadingCommitResult:
    rows: tuple[MobileReadingCommitRow, ...]
    summary: MobileReadingCommitSummary


@dataclass(frozen=True, slots=True)
class _EvaluatedRow:
    input: MobileReadingInputEntry
    target: MobileReadingImportTargetRecord | None
    disposition: MobileReadingDisposition
    reason: MobileReadingRejectReason | None
    input_calculation: MobileReadingCalculation | None


class MobileReadingImportService:
    def __init__(
        self,
        repository: MobileReadingImportRepository,
        *,
        calculation_service: UsageDeltaCalculationService | None = None,
        sync_service: MeterReadingSyncService | None = None,
        transaction_factory: TransactionFactory | None = None,
    ) -> None:
        self._repository = repository
        self._calculation_service = calculation_service
        self._sync_service = sync_service
        self._transaction_factory = transaction_factory

    def preview(self, result: MobileReadingResult) -> MobileReadingImportPreview:
        """入力と参照 DB 状態を固定し、全行評価後の見込値を返す。"""

        input_snapshot = _snapshot_input(result)
        repository = PreviewRepositoryRecorder(self._repository)
        history = VirtualReadingHistory(repository, calculation_service=self._calculation_service)
        targets = repository.get_mobile_reading_import_targets(
            [entry.planned_reading_id for entry in input_snapshot.entries]
        )
        evaluated: list[_EvaluatedRow] = []
        for entry in input_snapshot.entries:
            target = targets.get(entry.planned_reading_id)
            calculated: MobileReadingCalculation | None = None
            try:
                request = build_reading_upsert_request(
                    entry.to_submission(),
                    planned_reading_repository=repository,
                    measurement_definition_repository=repository,
                    assignment_repository=repository,
                    device_id=input_snapshot.sheet_id,
                    updated_by="",
                )
                if target is None:
                    raise MissingPlannedReadingError(entry.planned_reading_id)
                evaluation = history.evaluate(request)
                if evaluation.calculated is not None:
                    calculated = _calculation(evaluation.calculated)
                if isinstance(evaluation, VirtualReadingRejected):
                    raise evaluation.error
                disposition = _disposition(evaluation.outcome.disposition, target)
                reason = None
            except DomainError as exc:
                disposition = MobileReadingDisposition.REJECTED
                reason = _reject_reason(exc, entry.planned_reading_id, target)
            evaluated.append(_EvaluatedRow(entry, target, disposition, reason, calculated))

        history_snapshot = history.snapshot()
        accepted_ids = history_snapshot.accepted_reading_ids
        recalculated_ids = history_snapshot.recalculated_reading_ids - accepted_ids
        effects = MobileReadingExpectedEffects(
            results=tuple(
                (planned_id, _project_result(record, planned_id in accepted_ids))
                for planned_id, record in history_snapshot.results
            ),
            accepted_reading_ids=accepted_ids,
            recalculated_existing_ids=recalculated_ids,
            boundary_updates=history_snapshot.boundary_updates,
        )
        rows: list[MobileReadingPreviewRow] = []
        for row_number, item in enumerate(evaluated, start=1):
            final = _final_estimate(history, repository, item, accepted_ids)
            rows.append(
                MobileReadingPreviewRow(
                    row_number=row_number,
                    input=item.input,
                    target=item.target,
                    disposition=item.disposition,
                    reason=item.reason,
                    input_calculation=item.input_calculation,
                    final=final,
                    warnings=_warnings(input_snapshot, item.target, final),
                )
            )
        counts = {disposition: 0 for disposition in MobileReadingDisposition}
        for row in rows:
            counts[row.disposition] += 1
        summary = MobileReadingPreviewSummary(
            new=counts[MobileReadingDisposition.NEW],
            revived=counts[MobileReadingDisposition.REVIVED],
            overwritten=counts[MobileReadingDisposition.OVERWRITTEN],
            duplicate=counts[MobileReadingDisposition.DUPLICATE],
            stale=counts[MobileReadingDisposition.STALE],
            rejected=counts[MobileReadingDisposition.REJECTED],
            recalculated_existing=len(recalculated_ids),
            recalculated_boundaries=len(effects.boundary_updates),
        )
        return MobileReadingImportPreview(
            input=input_snapshot,
            database=repository.snapshot(),
            rows=tuple(rows),
            summary=summary,
            effects=effects,
        )

    def commit(
        self,
        result: MobileReadingResult,
        expected_preview: MobileReadingImportPreview,
        *,
        staff_id: str,
    ) -> MobileReadingCommitResult:
        """担当者とプレビューを再検証し、採用行を同一トランザクションで確定する。"""

        if self._sync_service is None or self._transaction_factory is None:
            raise RuntimeError("一括確定には同期サービスとトランザクションが必要です。")
        with self._transaction_factory():
            confirmation = ReadingConfirmationContext(
                sheet_id=result.sheet_id, staff_id=staff_id, staff_name=""
            )
            _, updated_by = self._sync_service.resolve_confirmation_context(confirmation)
            current = self.preview(result)
            if current != expected_preview:
                raise MobileReadingPreviewChangedError()
            if not current.can_commit:
                raise MobileReadingNothingToCommitError()

            accepted_rows = tuple(
                row
                for row in current.rows
                if row.disposition
                in {
                    MobileReadingDisposition.NEW,
                    MobileReadingDisposition.REVIVED,
                    MobileReadingDisposition.OVERWRITTEN,
                }
            )
            existing_before = self._repository.get_mobile_reading_import_targets(
                current.effects.recalculated_existing_ids
            )
            boundaries_before = self._read_boundaries(current.effects.boundary_updates)
            request = MeterReadingSyncRequest(
                reading_items=[row.input.to_submission() for row in accepted_rows],
                replacement_items=[],
            )
            batch = self._sync_service.sync_in_transaction(
                request,
                confirmation,
                source_row_numbers=tuple(row.row_number for row in accepted_rows),
            )
            _verify_sync_results(accepted_rows, batch)
            self._verify_final_state(current, existing_before, boundaries_before, updated_by)
            committed = _build_commit_result(current, batch)
        return committed

    def _read_boundaries(
        self, updates: tuple[ReplacementBoundaryCalculationUpdate, ...]
    ) -> dict[tuple[str, str], ReplacementBoundaryCalculationRecord]:
        event_ids = {update.replacement_event_id for update in updates}
        return {
            (boundary.replacement_event_id, boundary.measurement_definition_id): boundary
            for event_id in event_ids
            for boundary in self._repository.list_replacement_boundaries_for_event(event_id)
        }

    def _verify_final_state(
        self,
        preview: MobileReadingImportPreview,
        existing_before: dict[str, MobileReadingImportTargetRecord],
        boundaries_before: dict[tuple[str, str], ReplacementBoundaryCalculationRecord],
        updated_by: str,
    ) -> None:
        ids = {row.input.planned_reading_id for row in preview.rows}
        ids.update(planned_id for planned_id, _ in preview.effects.results)
        targets = self._repository.get_mobile_reading_import_targets(ids)
        accepted_ids = preview.effects.accepted_reading_ids
        for row in preview.rows:
            target = targets.get(row.input.planned_reading_id)
            if row.target is None:
                if target is not None or row.final is not None:
                    raise MobileReadingPreviewChangedError()
                continue
            if target is None or row.final is None:
                raise MobileReadingPreviewChangedError()
            if (
                target.task_state != row.final.task_state
                or target.closed_at != row.final.closed_at
                or target.close_reason != row.final.close_reason
                or replace(
                    target,
                    task_state=row.target.task_state,
                    closed_at=row.target.closed_at,
                    close_reason=row.target.close_reason,
                    existing_result=row.target.existing_result,
                )
                != row.target
            ):
                raise MobileReadingPreviewChangedError()
            _verify_projected_result(
                target.existing_result,
                row.final.result,
                accepted=row.input.planned_reading_id in accepted_ids,
                updated_by=updated_by,
            )

        for planned_id, expected in preview.effects.results:
            target = targets.get(planned_id)
            if target is None:
                raise MobileReadingPreviewChangedError()
            _verify_projected_result(
                target.existing_result,
                expected,
                accepted=planned_id in accepted_ids,
                updated_by=updated_by,
            )
        for planned_id in preview.effects.recalculated_existing_ids:
            before = existing_before.get(planned_id)
            after = targets.get(planned_id)
            if (
                before is None
                or after is None
                or before.existing_result is None
                or replace(after, existing_result=before.existing_result) != before
            ):
                raise MobileReadingPreviewChangedError()

        boundaries = self._read_boundaries(preview.effects.boundary_updates)
        for expected in preview.effects.boundary_updates:
            key = (expected.replacement_event_id, expected.measurement_definition_id)
            before = boundaries_before.get(key)
            actual = boundaries.get(key)
            if before is None or actual != replace(
                before,
                previous_measured_reading=expected.previous_measured_reading,
                previous_measured_planned_reading_id=expected.previous_measured_planned_reading_id,
                old_meter_usage_delta=expected.old_meter_usage_delta,
            ):
                raise MobileReadingPreviewChangedError()


_REJECTION_MESSAGES = {
    MobileReadingRejectCode.UNKNOWN_PLANNED_READING: "予定枠が見つかりません",
    MobileReadingRejectCode.SUPERSEDED: "置き換え済みの予定枠です",
    MobileReadingRejectCode.UNKNOWN_MEASUREMENT_DEFINITION: "測定定義が見つかりません",
    MobileReadingRejectCode.MISSING_ASSIGNMENT: "対応するメーター割当がありません",
    MobileReadingRejectCode.ASSIGNMENT_NEEDS_REVIEW: "割当条件の確認が必要です",
    MobileReadingRejectCode.INVALID_INPUT: "入力内容が測定条件に合いません",
    MobileReadingRejectCode.NEGATIVE_USAGE: "使用量が負になります",
    MobileReadingRejectCode.DOWNSTREAM_RECALCULATION: "後続実測値の再計算に失敗しました",
    MobileReadingRejectCode.BOUNDARY_RECALCULATION: "交換境界の再計算に失敗しました",
    MobileReadingRejectCode.BUSINESS_RULE: "業務ルールにより確定できません",
}


def describe_mobile_reading_rejection(reason: MobileReadingRejectReason) -> str:
    message = _REJECTION_MESSAGES[reason.code]
    if reason.cause is not None:
        message += f"（原因: {_REJECTION_MESSAGES[reason.cause]}）"
    return message


def _verify_sync_results(
    accepted_rows: tuple[MobileReadingPreviewRow, ...], batch: ReadingSyncBatchResult
) -> None:
    response = batch.response
    if (
        len(batch.reading_items) != len(accepted_rows)
        or response.accepted_reading_items != len(accepted_rows)
        or response.accepted_replacement_items != 0
        or response.ignored_reading_items
        or response.rejected_reading_items
        or response.ignored_replacement_items
        or response.rejected_replacement_items
    ):
        raise MobileReadingPreviewChangedError()
    for index, (row, item) in enumerate(zip(accepted_rows, batch.reading_items, strict=True)):
        if (
            row.target is None
            or row.final is None
            or row.final.result is None
            or item.item_index != index
            or item.source_row_number != row.row_number
            or item.planned_reading_id != row.input.planned_reading_id
            or item.task_state_before != row.target.task_state
            or item.reject_code is not None
            or item.disposition is None
        ):
            raise MobileReadingPreviewChangedError()
        existing = row.target.existing_result
        if existing is None:
            expected_disposition = UpsertDisposition.INSERTED
        elif row.final.result.client_updated_at == existing.client_updated_at:
            expected_disposition = UpsertDisposition.REPLACED_ON_TIE
        else:
            expected_disposition = UpsertDisposition.UPDATED
        if (
            item.disposition is not expected_disposition
            or _disposition(item.disposition, row.target) is not row.disposition
        ):
            raise MobileReadingPreviewChangedError()


def _verify_projected_result(
    actual: MobileReadingImportResultRecord | None,
    expected: MobileReadingProjectedResult | None,
    *,
    accepted: bool,
    updated_by: str,
) -> None:
    if expected is None:
        if actual is not None or accepted:
            raise MobileReadingPreviewChangedError()
        return
    if (
        actual is None
        or (accepted and actual.updated_by != updated_by)
        or _project_result(actual, accepted) != expected
    ):
        raise MobileReadingPreviewChangedError()


def _build_commit_result(
    preview: MobileReadingImportPreview, batch: ReadingSyncBatchResult
) -> MobileReadingCommitResult:
    saved = {item.source_row_number: item.disposition for item in batch.reading_items}
    reasons: dict[MobileReadingRejectCode, int] = {}
    rows: list[MobileReadingCommitRow] = []
    for row in preview.rows:
        if row.reason is not None:
            reasons[row.reason.code] = reasons.get(row.reason.code, 0) + 1
        rows.append(
            MobileReadingCommitRow(
                row_number=row.row_number,
                planned_reading_id=row.input.planned_reading_id,
                disposition=row.disposition,
                save_disposition=saved.get(row.row_number),
                reason=row.reason,
                reason_message=(
                    describe_mobile_reading_rejection(row.reason) if row.reason else None
                ),
            )
        )
    summary = preview.summary
    return MobileReadingCommitResult(
        rows=tuple(rows),
        summary=MobileReadingCommitSummary(
            confirmed=summary.accepted,
            new=summary.new,
            revived=summary.revived,
            overwritten=summary.overwritten,
            duplicate=summary.duplicate,
            stale=summary.stale,
            rejected=summary.rejected,
            recalculated_existing=summary.recalculated_existing,
            recalculated_boundaries=summary.recalculated_boundaries,
            rejection_reasons=tuple(
                MobileReadingRejectionSummary(code, _REJECTION_MESSAGES[code], count)
                for code, count in reasons.items()
            ),
        ),
    )


def _snapshot_input(result: MobileReadingResult) -> MobileReadingInputSnapshot:
    result.validate_unique_entries()
    sheet_id = validate_sheet_id(result.sheet_id)
    business_date = _validate_business_date(result.business_date)
    generated_at = parse_result_header_timestamp(result.generated_at)
    sheet_filter = None
    if result.filter is not None:
        SheetFilter.create(result.filter.reading_sessions, result.filter.cycle_groups)
        sheet_filter = SheetFilter(
            reading_sessions=(
                tuple(result.filter.reading_sessions)
                if result.filter.reading_sessions is not None
                else None
            ),
            cycle_groups=(
                tuple(result.filter.cycle_groups)
                if result.filter.cycle_groups is not None
                else None
            ),
        )
    if not result.entries:
        raise ValidationError("結果データに検針結果が 1 件以上必要です")
    entries: list[MobileReadingInputEntry] = []
    submissions: list[MeterReadingSubmission] = []
    for row_number, entry in enumerate(result.entries, start=1):
        try:
            validated = MeterReadingSubmission.model_validate(
                {
                    "planned_reading_id": entry.planned_reading_id,
                    "result_status": entry.result_status,
                    "raw_reading": entry.raw_reading,
                    "structured_payload": entry.structured_payload,
                    "recorded_at": entry.recorded_at,
                    "client_updated_at": entry.client_updated_at,
                    "note": entry.note,
                }
            )
        except (PydanticValidationError, TypeError) as exc:
            raise ValidationError(
                f"検針結果 {row_number} 件目の基本形式が正しくありません"
            ) from exc
        if validated.result_status is ResultStatus.REPLACED:
            raise ValidationError(f"検針結果 {row_number} 件目の結果状態が正しくありません")
        validate_note(validated.note)
        submissions.append(validated)
        entries.append(
            MobileReadingInputEntry(
                planned_reading_id=validated.planned_reading_id,
                result_status=validated.result_status,
                raw_reading=(
                    format(validated.raw_reading, "f")
                    if validated.raw_reading is not None
                    else None
                ),
                structured_payload_json=serialize_structured_payload(validated.structured_payload),
                recorded_at=validated.recorded_at.isoformat(),
                client_updated_at=validated.client_updated_at.isoformat(),
                note=validated.note,
            )
        )
    validate_unique_planned_reading_ids(submissions)
    return MobileReadingInputSnapshot(
        sheet_id=sheet_id,
        business_date=business_date,
        generated_at=generated_at,
        filter=sheet_filter,
        entries=tuple(entries),
    )


def _validate_business_date(value: object) -> date | None:
    if value is None:
        return None
    if not isinstance(value, date) or isinstance(value, datetime):
        raise ValidationError("結果データの対象業務日の形式が正しくありません")
    return value


def _reject_code(error: DomainError) -> MobileReadingRejectCode:
    if isinstance(error, MissingPlannedReadingError):
        return MobileReadingRejectCode.UNKNOWN_PLANNED_READING
    if isinstance(error, SupersededPlannedReadingError):
        return MobileReadingRejectCode.SUPERSEDED
    if isinstance(error, MissingMeasurementDefinitionError):
        return MobileReadingRejectCode.UNKNOWN_MEASUREMENT_DEFINITION
    if isinstance(error, MissingPlannedAssignmentError):
        return MobileReadingRejectCode.MISSING_ASSIGNMENT
    if isinstance(error, AssignmentNeedsReviewError):
        return MobileReadingRejectCode.ASSIGNMENT_NEEDS_REVIEW
    if isinstance(error, NegativeUsageDeltaError):
        return MobileReadingRejectCode.NEGATIVE_USAGE
    if isinstance(error, (InvalidReadingSubmissionError, ValidationError)):
        return MobileReadingRejectCode.INVALID_INPUT
    return MobileReadingRejectCode.BUSINESS_RULE


def _reject_reason(
    error: DomainError, planned_reading_id: str, target: MobileReadingImportTargetRecord | None
) -> MobileReadingRejectReason:
    code = _reject_code(error)
    cause: MobileReadingRejectCode | None = None
    meter_id = target.meter_id if target is not None else None
    replacement_event_id = None
    underlying_error = error
    if isinstance(error, VirtualRecalculationError):
        code = (
            MobileReadingRejectCode.BOUNDARY_RECALCULATION
            if error.stage is VirtualRecalculationStage.BOUNDARY
            else MobileReadingRejectCode.DOWNSTREAM_RECALCULATION
        )
        cause = _reject_code(error.original_error)
        underlying_error = error.original_error
        meter_id = error.meter_id
        replacement_event_id = error.replacement_event_id
    return MobileReadingRejectReason(
        code=code,
        planned_reading_id=planned_reading_id,
        measurement_definition_id=(
            target.measurement_definition_id if target is not None else None
        ),
        meter_id=meter_id,
        replacement_event_id=replacement_event_id,
        cause=cause,
        raw_reading=(
            underlying_error.raw_reading
            if isinstance(underlying_error, NegativeUsageDeltaError)
            else None
        ),
        last_reading=(
            underlying_error.last_reading
            if isinstance(underlying_error, NegativeUsageDeltaError)
            else None
        ),
        rollover_value=(
            underlying_error.rollover_value
            if isinstance(underlying_error, NegativeUsageDeltaError)
            else None
        ),
    )


def _disposition(
    disposition: UpsertDisposition, target: MobileReadingImportTargetRecord
) -> MobileReadingDisposition:
    if disposition is UpsertDisposition.IGNORED_DUPLICATE:
        return MobileReadingDisposition.DUPLICATE
    if disposition is UpsertDisposition.IGNORED_STALE:
        return MobileReadingDisposition.STALE
    if target.task_state is PlannedReadingTaskState.AUTO_CLOSED:
        return MobileReadingDisposition.REVIVED
    if target.existing_result is not None:
        return MobileReadingDisposition.OVERWRITTEN
    return MobileReadingDisposition.NEW


def _calculation(calculated: CalculatedReading) -> MobileReadingCalculation:
    request = calculated.request
    rollover_applied = None
    if request.usage_delta is not None:
        rollover_applied = (
            calculated.definition.calculation_method is not CalculationMethod.RAW_ONLY
            and request.result_status is ResultStatus.MEASURED
            and request.raw_reading is not None
            and request.last_reading is not None
            and request.raw_reading < request.last_reading
            and calculated.assignment.rollover_value is not None
        )
    threshold = calculated.definition.warning_threshold
    return MobileReadingCalculation(
        previous=describe_initial_previous_reading(calculated.context),
        usage_delta=request.usage_delta,
        rollover_applied=rollover_applied,
        warning_threshold_exceeded=(
            request.usage_delta > threshold
            if request.usage_delta is not None and threshold is not None
            else None
        ),
    )


def _project_result(
    result: MobileReadingImportResultRecord, accepted: bool
) -> MobileReadingProjectedResult:
    return MobileReadingProjectedResult(
        result_status=result.result_status,
        raw_reading=result.raw_reading,
        structured_payload_json=result.structured_payload_json,
        note=result.note,
        recorded_at=result.recorded_at,
        client_updated_at=result.client_updated_at,
        result_source=result.result_source,
        device_id=result.device_id,
        updated_by=None if accepted else result.updated_by,
        signature=result.signature,
        last_reading=result.last_reading,
        usage_delta=result.usage_delta,
        arrival_sequence=result.arrival_sequence,
    )


def _final_estimate(
    history: VirtualReadingHistory,
    repository: PreviewRepositoryRecorder,
    row: _EvaluatedRow,
    accepted_ids: frozenset[str],
) -> MobileReadingFinalEstimate | None:
    target = row.target
    if target is None:
        return None
    planned_id = row.input.planned_reading_id
    accepted = planned_id in accepted_ids
    result = history.get_result(planned_id)
    projected = _project_result(result, accepted) if result is not None else None
    task_state = target.task_state
    closed_at = target.closed_at
    close_reason = target.close_reason
    if accepted and task_state in {
        PlannedReadingTaskState.OPEN,
        PlannedReadingTaskState.AUTO_CLOSED,
    }:
        if result is None:
            raise ValidationError(f"採用された仮想結果が見つかりません: {planned_id}")
        task_state = PlannedReadingTaskState.COMPLETED
        closed_at = result.recorded_at
        close_reason = None

    previous: InitialPreviousReading | None = None
    calculation_error: MobileReadingRejectReason | None = None
    rollover_applied: bool | None = None
    if result is not None:
        try:
            context = history.get_calculation_context(planned_id)
            previous = describe_initial_previous_reading(context)
            planned = repository.get_planned_reading(planned_id)
            if planned is None:
                raise MissingPlannedReadingError(planned_id)
            assignment = resolve_planned_assignment(repository, planned)
            if result.usage_delta is not None:
                rollover_applied = (
                    target.calculation_method is not CalculationMethod.RAW_ONLY
                    and result.result_status is ResultStatus.MEASURED
                    and result.raw_reading is not None
                    and result.last_reading is not None
                    and result.raw_reading < result.last_reading
                    and assignment.rollover_value is not None
                )
        except DomainError as exc:
            calculation_error = _reject_reason(exc, planned_id, target)
    threshold_exceeded = (
        result.usage_delta > target.warning_threshold
        if result is not None
        and result.usage_delta is not None
        and target.warning_threshold is not None
        else None
    )
    return MobileReadingFinalEstimate(
        task_state=task_state,
        closed_at=closed_at,
        close_reason=close_reason,
        result=projected,
        previous=previous,
        calculation_error=calculation_error,
        rollover_applied=rollover_applied,
        warning_threshold_exceeded=threshold_exceeded,
    )


def _warnings(
    snapshot: MobileReadingInputSnapshot,
    target: MobileReadingImportTargetRecord | None,
    final: MobileReadingFinalEstimate | None,
) -> tuple[MobileReadingWarning, ...]:
    if target is None:
        return ()
    warnings: list[MobileReadingWarning] = []
    if snapshot.business_date is not None and snapshot.business_date != target.business_date:
        warnings.append(
            MobileReadingWarning(
                MobileReadingWarningCode.BUSINESS_DATE_MISMATCH,
                (snapshot.business_date.isoformat(),),
                target.business_date.isoformat(),
            )
        )
    sheet_filter = snapshot.filter
    if sheet_filter is not None:
        sessions = sheet_filter.reading_sessions
        if sessions is not None and target.reading_session.value not in sessions:
            warnings.append(
                MobileReadingWarning(
                    MobileReadingWarningCode.READING_SESSION_MISMATCH,
                    sessions,
                    target.reading_session.value,
                )
            )
        cycles = sheet_filter.cycle_groups
        cycle = cycle_group_for(target.reading_cycle)
        if cycles is not None and cycle not in cycles:
            warnings.append(
                MobileReadingWarning(MobileReadingWarningCode.CYCLE_GROUP_MISMATCH, cycles, cycle)
            )
    if final is not None:
        if final.warning_threshold_exceeded:
            warnings.append(MobileReadingWarning(MobileReadingWarningCode.THRESHOLD_EXCEEDED))
        if final.rollover_applied:
            warnings.append(MobileReadingWarning(MobileReadingWarningCode.ROLLOVER_APPLIED))
    return tuple(warnings)
