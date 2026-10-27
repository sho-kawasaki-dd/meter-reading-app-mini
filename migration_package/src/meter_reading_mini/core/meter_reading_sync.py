"""`POST /api/readings` の業務オーケストレーションを担当する service。

この service の役割は、Android から受けた transport contract をそのまま保存することではない。
まず planned reading と measurement definition を解決し、その結果に基づいて strict validation を
行い、最後に repository へ「保存してよい形」の要求だけを渡す。
"""

from __future__ import annotations

import logging
import sqlite3
from collections.abc import Callable
from decimal import Decimal

from pydantic import ValidationError as PydanticValidationError

from meter_reading_mini.core.contract_models import (
    IgnoredReadingSyncItem,
    IgnoredReplacementSyncItem,
    MeterReadingSubmission,
    MeterReadingSyncRequest,
    MeterReadingSyncResponse,
    MeterReplacementSubmission,
    RejectedReadingSyncItem,
    RejectedReplacementSyncItem,
    ResolvedMeterReadingSubmission,
    SyncItemIgnoreCode,
    SyncItemRejectCode,
    ValueSchema,
    resolve_meter_reading_submission,
)
from meter_reading_mini.core.errors import DomainError, InactiveMasterError
from meter_reading_mini.core.meter_reading_sync_repositories import (
    MeasurementDefinitionLookupRepository,
    MeasurementDefinitionRecord,
    PlannedReadingLookupRepository,
    PlannedReadingRecord,
    ReadingConfirmationContext,
    ReadingUpsertRequest,
    ReadingWriteService,
    ReplacementEventUpsertRepository,
    ReplacementReflectionRepository,
    ReplacementUpsertRequest,
    ResultSource,
    StaffRepository,
    UpsertDisposition,
    UpsertOutcome,
)
from meter_reading_mini.core.mobile_reading import validate_note, validate_sheet_id
from meter_reading_mini.core.reading_signature import (
    SignatureCanonicalizationError,
    calculate_meter_reading_signature,
)
from meter_reading_mini.core.timeutil import parse_utc, utc_now_str
from meter_reading_mini.db.connection import savepoint, transaction

logger = logging.getLogger(__name__)


class MeterReadingSyncServiceError(DomainError):
    """sync 中に item 単位で処理できなかったことを表す基底例外。"""

    code: SyncItemRejectCode


class MissingPlannedReadingError(MeterReadingSyncServiceError):
    """未知の planned_reading_id を参照したときに送出する。"""

    code = SyncItemRejectCode.UNKNOWN_PLANNED_READING

    def __init__(self, planned_reading_id: str) -> None:
        super().__init__(f"Unknown planned_reading_id: {planned_reading_id}")
        self.planned_reading_id = planned_reading_id


class MissingMeasurementDefinitionError(MeterReadingSyncServiceError):
    """planned reading からたどった measurement definition が存在しないことを表す。"""

    code = SyncItemRejectCode.UNKNOWN_MEASUREMENT_DEFINITION

    def __init__(self, measurement_definition_id: str) -> None:
        super().__init__(f"Unknown measurement_definition_id: {measurement_definition_id}")
        self.measurement_definition_id = measurement_definition_id


class InvalidReadingSubmissionError(MeterReadingSyncServiceError):
    """lookup 後の resolved validation に失敗したことを表す。"""

    code = SyncItemRejectCode.INVALID_READING_SUBMISSION

    def __init__(self, planned_reading_id: str, message: str) -> None:
        super().__init__(
            f"Invalid reading submission for planned_reading_id {planned_reading_id}: {message}"
        )
        self.planned_reading_id = planned_reading_id


ReadingSubmissionResolver = Callable[
    [MeterReadingSubmission, str, ValueSchema, int | None, Decimal | None],
    ResolvedMeterReadingSubmission,
]


def _default_reading_submission_resolver(
    submission: MeterReadingSubmission,
    measurement_definition_id: str,
    value_schema: ValueSchema,
    decimal_places: int | None,
    rollover_value: Decimal | None,
) -> ResolvedMeterReadingSubmission:
    """既定の resolver として measurement-aware validation を呼び出す。"""

    return resolve_meter_reading_submission(
        submission,
        measurement_definition_id=measurement_definition_id,
        value_schema=value_schema,
        decimal_places=decimal_places,
        rollover_value=rollover_value,
    )


class MeterReadingSyncService:
    """検針結果を確定し、バッチ全体のトランザクションを所有する service。"""

    def __init__(
        self,
        conn: sqlite3.Connection,
        planned_reading_repository: PlannedReadingLookupRepository,
        measurement_definition_repository: MeasurementDefinitionLookupRepository,
        reading_repository: ReadingWriteService,
        replacement_repository: ReplacementEventUpsertRepository,
        staff_repository: StaffRepository,
        replacement_reflection_repository: ReplacementReflectionRepository,
        *,
        resolve_submission: ReadingSubmissionResolver = _default_reading_submission_resolver,
    ) -> None:
        self._conn = conn
        self._planned_reading_repository = planned_reading_repository
        self._measurement_definition_repository = measurement_definition_repository
        self._reading_repository = reading_repository
        self._replacement_repository = replacement_repository
        self._staff_repository = staff_repository
        self._replacement_reflection_repository = replacement_reflection_repository
        self._resolve_submission = resolve_submission

    def sync(
        self,
        request: MeterReadingSyncRequest,
        confirmation_context: ReadingConfirmationContext,
    ) -> MeterReadingSyncResponse:
        """業務拒否だけを item の SAVEPOINT で戻し、予期しない例外はバッチを戻す。"""

        accepted_reading_items = 0
        accepted_replacement_items = 0
        ignored_reading_items: list[IgnoredReadingSyncItem] = []
        ignored_replacement_items: list[IgnoredReplacementSyncItem] = []
        rejected_reading_items: list[RejectedReadingSyncItem] = []
        rejected_replacement_items: list[RejectedReplacementSyncItem] = []

        with transaction(self._conn):
            sheet_id = validate_sheet_id(confirmation_context.sheet_id)
            staff = self._staff_repository.get_staff(confirmation_context.staff_id)
            if staff is None or not staff.is_active:
                raise InactiveMasterError(
                    f"有効な確定担当者を指定してください: {confirmation_context.staff_id}"
                )
            updated_by = staff.display_name.strip()
            if not updated_by:
                raise InactiveMasterError("確定担当者名が空の担当者は指定できません。")

            for item_index, submission in enumerate(request.reading_items):
                try:
                    with savepoint(self._conn, f"reading_item_{item_index}"):
                        reading_request = self._build_reading_upsert_request(
                            submission,
                            device_id=sheet_id,
                            updated_by=updated_by,
                        )
                        outcome = self._reading_repository.upsert_reading(reading_request)
                except DomainError as exc:
                    code = (
                        exc.code
                        if isinstance(exc, MeterReadingSyncServiceError)
                        else SyncItemRejectCode.INVALID_READING_SUBMISSION
                    )
                    logger.warning(
                        "Rejected reading item %s at index %d with code=%s: %s",
                        submission.planned_reading_id,
                        item_index,
                        code.value,
                        str(exc),
                    )
                    rejected_reading_items.append(
                        RejectedReadingSyncItem(
                            item_index=item_index,
                            planned_reading_id=submission.planned_reading_id,
                            code=code,
                            detail=str(exc),
                        )
                    )
                else:
                    if outcome.accepted:
                        accepted_reading_items += 1
                    else:
                        ignored_reading_items.append(
                            IgnoredReadingSyncItem(
                                item_index=item_index,
                                planned_reading_id=submission.planned_reading_id,
                                code=_ignore_code(outcome),
                                detail=outcome.detail,
                            )
                        )

            for item_index, submission in enumerate(request.replacement_items):
                try:
                    with savepoint(self._conn, f"replacement_item_{item_index}"):
                        replacement_request = self._build_replacement_upsert_request(
                            submission,
                            updated_by=updated_by,
                        )
                        outcome = self._replacement_repository.upsert_replacement(
                            replacement_request
                        )
                        if outcome.accepted:
                            self._replacement_reflection_repository.reflect_replacement(
                                replacement_request
                            )
                            self._reading_repository.recalculate_after_replacement(
                                replacement_request
                            )
                except DomainError as exc:
                    logger.warning(
                        "Rejected replacement item %s at index %d: %s",
                        submission.replacement_event_id,
                        item_index,
                        str(exc),
                    )
                    rejected_replacement_items.append(
                        RejectedReplacementSyncItem(
                            item_index=item_index,
                            replacement_event_id=submission.replacement_event_id,
                            code=SyncItemRejectCode.INVALID_REPLACEMENT_SUBMISSION,
                            detail=str(exc),
                        )
                    )
                else:
                    if outcome.accepted:
                        accepted_replacement_items += 1
                    else:
                        ignored_replacement_items.append(
                            IgnoredReplacementSyncItem(
                                item_index=item_index,
                                replacement_event_id=submission.replacement_event_id,
                                code=_ignore_code(outcome),
                                detail=outcome.detail,
                            )
                        )

        duplicate_reading_items = sum(
            item.code is SyncItemIgnoreCode.DUPLICATE_SUBMISSION for item in ignored_reading_items
        )
        logger.info(
            "Sync batch: readings accepted=%d ignored=%d duplicate=%d rejected=%d; "
            "replacements accepted=%d ignored=%d rejected=%d",
            accepted_reading_items,
            len(ignored_reading_items),
            duplicate_reading_items,
            len(rejected_reading_items),
            accepted_replacement_items,
            len(ignored_replacement_items),
            len(rejected_replacement_items),
        )
        return MeterReadingSyncResponse(
            accepted_reading_items=accepted_reading_items,
            accepted_replacement_items=accepted_replacement_items,
            ignored_reading_items=ignored_reading_items,
            ignored_replacement_items=ignored_replacement_items,
            rejected_reading_items=rejected_reading_items,
            rejected_replacement_items=rejected_replacement_items,
            detail=_build_response_detail(
                ignored_reading_items=len(ignored_reading_items),
                duplicate_reading_items=duplicate_reading_items,
                ignored_replacement_items=len(ignored_replacement_items),
                rejected_reading_items=len(rejected_reading_items),
                rejected_replacement_items=len(rejected_replacement_items),
            ),
        )

    def _build_reading_upsert_request(
        self,
        submission: MeterReadingSubmission,
        *,
        device_id: str,
        updated_by: str,
    ) -> ReadingUpsertRequest:
        """未署名入力を検証し、PC 側監査情報と署名を付ける。"""

        planned_reading = self._planned_reading_repository.get_planned_reading(
            submission.planned_reading_id
        )
        if planned_reading is None:
            raise MissingPlannedReadingError(submission.planned_reading_id)
        measurement_definition = self._measurement_definition_repository.get_measurement_definition(
            planned_reading.measurement_definition_id
        )
        if measurement_definition is None:
            raise MissingMeasurementDefinitionError(planned_reading.measurement_definition_id)
        try:
            resolved_submission = self._resolve_submission(
                submission,
                measurement_definition.measurement_definition_id,
                measurement_definition.value_schema,
                measurement_definition.decimal_places,
                measurement_definition.rollover_value,
            )
        except PydanticValidationError as exc:
            raise InvalidReadingSubmissionError(
                submission.planned_reading_id,
                _format_validation_error(exc),
            ) from exc

        recorded_at = parse_utc(utc_now_str(resolved_submission.recorded_at))
        client_updated_at = parse_utc(utc_now_str(resolved_submission.client_updated_at))
        try:
            signature = calculate_meter_reading_signature(
                planned_reading_id=resolved_submission.planned_reading_id,
                result_status=resolved_submission.result_status,
                raw_reading=resolved_submission.raw_reading,
                structured_payload_json=resolved_submission.structured_payload,
                recorded_at=recorded_at,
                device_id=device_id,
            )
        except SignatureCanonicalizationError as exc:
            raise InvalidReadingSubmissionError(
                submission.planned_reading_id,
                str(exc),
            ) from exc

        return ReadingUpsertRequest(
            planned_reading_id=resolved_submission.planned_reading_id,
            measurement_definition_id=resolved_submission.measurement_definition_id,
            result_status=resolved_submission.result_status,
            recorded_at=recorded_at,
            client_updated_at=client_updated_at,
            raw_reading=resolved_submission.raw_reading,
            structured_payload_json=resolved_submission.structured_payload,
            device_id=device_id,
            updated_by=updated_by,
            signature=signature,
            note=validate_note(resolved_submission.note),
            result_source=ResultSource.MOBILE_HTML,
        )

    def _build_replacement_upsert_request(
        self,
        submission: MeterReplacementSubmission,
        *,
        updated_by: str,
    ) -> ReplacementUpsertRequest:
        """交換イベントの確定担当者を PC 側で補完する。"""

        return ReplacementUpsertRequest(
            replacement_event_id=submission.replacement_event_id,
            location_id=submission.location_id,
            old_meter_id=submission.old_meter_id,
            new_meter_id=submission.new_meter_id,
            replaced_at=parse_utc(utc_now_str(submission.replaced_at)),
            client_updated_at=parse_utc(utc_now_str(submission.client_updated_at)),
            old_final_reading=submission.old_final_reading,
            new_initial_reading=submission.new_initial_reading,
            updated_by=updated_by,
            note=submission.note,
        )


def _ignore_code(outcome: UpsertOutcome) -> SyncItemIgnoreCode:
    if outcome.disposition is UpsertDisposition.IGNORED_DUPLICATE:
        return SyncItemIgnoreCode.DUPLICATE_SUBMISSION
    return SyncItemIgnoreCode.STALE_CLIENT_UPDATE


def _format_validation_error(exc: PydanticValidationError) -> str:
    """Pydantic の ValidationError を API response 向けの短い文字列へ整える。"""

    issues = exc.errors(include_url=False)
    if not issues:
        return str(exc)
    return "; ".join(issue["msg"] for issue in issues)


def _build_response_detail(
    *,
    ignored_reading_items: int,
    duplicate_reading_items: int,
    ignored_replacement_items: int,
    rejected_reading_items: int,
    rejected_replacement_items: int,
) -> str | None:
    """ignored / rejected 件数から、オペレーター向けの概要メッセージを組み立てる。"""

    detail_parts: list[str] = []
    stale_reading_items = ignored_reading_items - duplicate_reading_items
    if stale_reading_items:
        detail_parts.append(f"Ignored {stale_reading_items} stale reading item(s).")
    if duplicate_reading_items:
        detail_parts.append(f"Ignored {duplicate_reading_items} duplicate reading item(s).")
    if ignored_replacement_items:
        detail_parts.append(f"Ignored {ignored_replacement_items} stale replacement item(s).")
    if rejected_reading_items:
        detail_parts.append(f"Rejected {rejected_reading_items} reading item(s).")
    if rejected_replacement_items:
        detail_parts.append(f"Rejected {rejected_replacement_items} replacement item(s).")
    if not detail_parts:
        return None
    return " ".join(detail_parts)


__all__ = [
    "InvalidReadingSubmissionError",
    "MeasurementDefinitionRecord",
    "MeterReadingSyncService",
    "MeterReadingSyncServiceError",
    "MissingMeasurementDefinitionError",
    "MissingPlannedReadingError",
    "PlannedReadingRecord",
    "PlannedReadingLookupRepository",
    "MeasurementDefinitionLookupRepository",
    "ReadingConfirmationContext",
    "ReadingWriteService",
    "ReadingUpsertRequest",
    "ReplacementEventUpsertRepository",
    "ReplacementUpsertRequest",
    "UpsertDisposition",
    "UpsertOutcome",
]
