"""`POST /api/readings` の業務オーケストレーションを担当する service。

この service の役割は、Android から受けた transport contract をそのまま保存することではない。
まず planned reading と measurement definition を解決し、その結果に基づいて strict validation を
行い、最後に repository へ「保存してよい形」の要求だけを渡す。
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from decimal import Decimal

logger = logging.getLogger(__name__)

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
from meter_reading_mini.core.meter_reading_sync_repositories import (
    MeasurementDefinitionLookupRepository,
    MeasurementDefinitionRecord,
    PlannedReadingLookupRepository,
    PlannedReadingRecord,
    ReadingUpsertRepository,
    ReadingUpsertRequest,
    ReplacementEventUpsertRepository,
    ReplacementReflectionRepository,
    ReplacementUpsertRequest,
    UpsertDisposition,
    UpsertOutcome,
)
from meter_reading_mini.core.reading_signature import (
    InvalidMeterReadingSignatureError,
    MissingDeviceIdForSignatureError,
    SignatureCanonicalizationError,
    verify_meter_reading_signature,
)
from pydantic import ValidationError
from sqlalchemy.orm import Session


class MeterReadingSyncServiceError(ValueError):
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


class MissingDeviceIdError(MeterReadingSyncServiceError):
    """signature seed に必要な device_id が空だったことを表す。"""

    code = SyncItemRejectCode.MISSING_DEVICE_ID

    def __init__(self, planned_reading_id: str) -> None:
        super().__init__(f"Missing device_id for planned_reading_id {planned_reading_id}.")
        self.planned_reading_id = planned_reading_id


class SignatureCanonicalizationFailedError(MeterReadingSyncServiceError):
    """署名検証用の canonical JSON を生成できなかったことを表す。"""

    code = SyncItemRejectCode.SIGNATURE_CANONICALIZATION_FAILED

    def __init__(self, planned_reading_id: str) -> None:
        super().__init__(
            "structured_payload could not be canonicalized for signature verification "
            f"for planned_reading_id {planned_reading_id}."
        )
        self.planned_reading_id = planned_reading_id


class InvalidSignatureError(MeterReadingSyncServiceError):
    """サーバー再計算した署名と一致しないことを表す。"""

    code = SyncItemRejectCode.INVALID_SIGNATURE

    def __init__(self, planned_reading_id: str) -> None:
        super().__init__(f"Invalid signature for planned_reading_id {planned_reading_id}.")
        self.planned_reading_id = planned_reading_id


ReadingSubmissionResolver = Callable[
    [MeterReadingSubmission, str, ValueSchema, int | None, Decimal | None],
    ResolvedMeterReadingSubmission,
]
ReadingSignatureVerifier = Callable[[ResolvedMeterReadingSubmission], None]


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


def _default_reading_signature_verifier(submission: ResolvedMeterReadingSubmission) -> None:
    """既定の署名 verifier として canonical JSON 化と SHA-256 検証を呼び出す。"""

    verify_meter_reading_signature(
        planned_reading_id=submission.planned_reading_id,
        result_status=submission.result_status,
        raw_reading=submission.raw_reading,
        structured_payload_json=submission.structured_payload,
        recorded_at=submission.recorded_at,
        device_id=submission.device_id,
        signature=submission.signature,
    )


class MeterReadingSyncService:
    """metadata lookup、resolved validation、upsert を順番にまとめる service。"""

    def __init__(
        self,
        session: Session,
        planned_reading_repository: PlannedReadingLookupRepository,
        measurement_definition_repository: MeasurementDefinitionLookupRepository,
        reading_repository: ReadingUpsertRepository,
        replacement_repository: ReplacementEventUpsertRepository,
        replacement_reflection_repository: ReplacementReflectionRepository | None = None,
        *,
        resolve_submission: ReadingSubmissionResolver = _default_reading_submission_resolver,
        verify_signature: ReadingSignatureVerifier = _default_reading_signature_verifier,
    ) -> None:
        self._session = session
        self._planned_reading_repository = planned_reading_repository
        self._measurement_definition_repository = measurement_definition_repository
        self._reading_repository = reading_repository
        self._replacement_repository = replacement_repository
        self._replacement_reflection_repository = replacement_reflection_repository
        self._resolve_submission = resolve_submission
        self._verify_signature = verify_signature

    def sync(self, request: MeterReadingSyncRequest) -> MeterReadingSyncResponse:
        """バッチ request を item 単位で処理し、accepted/ignored/rejected を集計する。

        外側トランザクションで全体を保護し、各 item は nested savepoint で個別に扱う。
        個別エラーは rollback し item を rejected として記録、残り item は続行。

        トランザクション分岐ロジック：
        - 既にトランザクション中（外側から開始済み）：savepoint のみ使用
        - トランザクション未開始：begin() で開始して保護
        """

        accepted_reading_items = 0
        accepted_replacement_items = 0
        ignored_reading_items: list[IgnoredReadingSyncItem] = []
        ignored_replacement_items: list[IgnoredReplacementSyncItem] = []
        rejected_reading_items: list[RejectedReadingSyncItem] = []
        rejected_replacement_items: list[RejectedReplacementSyncItem] = []

        def _process_items() -> None:
            """reading_items と replacement_items を処理する内部メソッド。"""
            nonlocal accepted_reading_items, accepted_replacement_items
            nonlocal ignored_reading_items, ignored_replacement_items
            nonlocal rejected_reading_items, rejected_replacement_items

            for item_index, submission in enumerate(request.reading_items):
                sp = self._session.begin_nested()  # SavePoint
                try:
                    # まず planned reading と measurement definition を解決しないと、
                    # value_schema に応じた厳密検証へ進めない。
                    reading_request = self._build_reading_upsert_request(submission)
                    outcome = self._reading_repository.upsert_reading(reading_request)
                    if outcome.accepted:
                        accepted_reading_items += 1
                        sp.commit()
                    else:
                        sp.commit()
                        ignored_reading_items.append(
                            IgnoredReadingSyncItem(
                                item_index=item_index,
                                planned_reading_id=submission.planned_reading_id,
                                code=SyncItemIgnoreCode.STALE_CLIENT_UPDATE,
                                detail=outcome.detail,
                            )
                        )
                except MeterReadingSyncServiceError as exc:
                    sp.rollback()
                    logger.warning(
                        "Rejected reading item %s at index %d with code=%s: %s",
                        submission.planned_reading_id,
                        item_index,
                        exc.code.value,
                        str(exc),
                    )
                    rejected_reading_items.append(
                        RejectedReadingSyncItem(
                            item_index=item_index,
                            planned_reading_id=submission.planned_reading_id,
                            code=exc.code,
                            detail=str(exc),
                        )
                    )
                except Exception as exc:
                    sp.rollback()
                    logger.warning(
                        "Rejected reading item %s at index %d (unexpected error): %s",
                        submission.planned_reading_id,
                        item_index,
                        str(exc),
                    )
                    rejected_reading_items.append(
                        RejectedReadingSyncItem(
                            item_index=item_index,
                            planned_reading_id=submission.planned_reading_id,
                            code=SyncItemRejectCode.UNEXPECTED_ERROR,
                            detail=str(exc),
                        )
                    )

            for item_index, submission in enumerate(request.replacement_items):
                sp = self._session.begin_nested()  # SavePoint
                try:
                    replacement_request = self._build_replacement_upsert_request(submission)
                    outcome = self._replacement_repository.upsert_replacement(replacement_request)
                    if outcome.accepted:
                        if self._replacement_reflection_repository is not None:
                            self._replacement_reflection_repository.reflect_replacement(
                                replacement_request
                            )
                        accepted_replacement_items += 1
                        sp.commit()
                    else:
                        sp.commit()
                        ignored_replacement_items.append(
                            IgnoredReplacementSyncItem(
                                item_index=item_index,
                                replacement_event_id=submission.replacement_event_id,
                                code=SyncItemIgnoreCode.STALE_CLIENT_UPDATE,
                                detail=outcome.detail,
                            )
                        )
                except Exception as exc:
                    sp.rollback()
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
                            code=SyncItemRejectCode.UNEXPECTED_ERROR,
                            detail=str(exc),
                        )
                    )

        try:
            # トランザクション分岐：既に開始済みなら外側 begin() をスキップ
            if self._session.in_transaction():
                _process_items()
            else:
                with self._session.begin():
                    _process_items()
        except Exception as exc:
            # 想定外エラー: 全体ロールバック
            logger.error("Unexpected error during sync, rolling back entire batch: %s", str(exc))
            raise

        logger.info(
            "Sync batch: readings accepted=%d ignored=%d rejected=%d; "
            "replacements accepted=%d ignored=%d rejected=%d",
            accepted_reading_items,
            len(ignored_reading_items),
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
                ignored_replacement_items=len(ignored_replacement_items),
                rejected_reading_items=len(rejected_reading_items),
                rejected_replacement_items=len(rejected_replacement_items),
            ),
        )

    def _build_reading_upsert_request(
        self, submission: MeterReadingSubmission
    ) -> ReadingUpsertRequest:
        """transport contract を resolved 済みの保存要求へ変換する。"""

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
            # measurement definition が解決できたあとでだけ、
            # raw_reading / structured_payload の厳密な妥当性を判断できる。
            resolved_submission = self._resolve_submission(
                submission,
                measurement_definition.measurement_definition_id,
                measurement_definition.value_schema,
                measurement_definition.decimal_places,
                measurement_definition.rollover_value,
            )
        except ValidationError as exc:
            raise InvalidReadingSubmissionError(
                submission.planned_reading_id,
                _format_validation_error(exc),
            ) from exc

        try:
            self._verify_signature(resolved_submission)
        except MissingDeviceIdForSignatureError as exc:
            raise MissingDeviceIdError(submission.planned_reading_id) from exc
        except SignatureCanonicalizationError as exc:
            raise SignatureCanonicalizationFailedError(submission.planned_reading_id) from exc
        except InvalidMeterReadingSignatureError as exc:
            raise InvalidSignatureError(submission.planned_reading_id) from exc

        return ReadingUpsertRequest(
            planned_reading_id=resolved_submission.planned_reading_id,
            measurement_definition_id=resolved_submission.measurement_definition_id,
            result_status=resolved_submission.result_status,
            recorded_at=resolved_submission.recorded_at,
            client_updated_at=resolved_submission.client_updated_at,
            raw_reading=resolved_submission.raw_reading,
            structured_payload_json=resolved_submission.structured_payload,
            device_id=resolved_submission.device_id,
            updated_by=resolved_submission.updated_by,
            signature=resolved_submission.signature,
            note=resolved_submission.note,
        )

    def _build_replacement_upsert_request(
        self,
        submission: MeterReplacementSubmission,
    ) -> ReplacementUpsertRequest:
        """交換イベントの transport contract を保存要求へ正規化する。"""

        return ReplacementUpsertRequest(
            replacement_event_id=submission.replacement_event_id,
            location_id=submission.location_id,
            old_meter_id=submission.old_meter_id,
            new_meter_id=submission.new_meter_id,
            replaced_at=submission.replaced_at,
            client_updated_at=submission.client_updated_at,
            old_final_reading=submission.old_final_reading,
            new_initial_reading=submission.new_initial_reading,
            updated_by=submission.updated_by,
            note=submission.note,
        )


def _format_validation_error(exc: ValidationError) -> str:
    """Pydantic の ValidationError を API response 向けの短い文字列へ整える。"""

    issues = exc.errors(include_url=False)
    if not issues:
        return str(exc)
    return "; ".join(issue["msg"] for issue in issues)


def _build_response_detail(
    *,
    ignored_reading_items: int,
    ignored_replacement_items: int,
    rejected_reading_items: int,
    rejected_replacement_items: int,
) -> str | None:
    """ignored / rejected 件数から、オペレーター向けの概要メッセージを組み立てる。"""

    detail_parts: list[str] = []
    if ignored_reading_items:
        detail_parts.append(f"Ignored {ignored_reading_items} stale reading item(s).")
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
    "InvalidSignatureError",
    "MissingMeasurementDefinitionError",
    "MissingDeviceIdError",
    "MissingPlannedReadingError",
    "PlannedReadingRecord",
    "PlannedReadingLookupRepository",
    "MeasurementDefinitionLookupRepository",
    "ReadingUpsertRepository",
    "ReadingUpsertRequest",
    "ReplacementEventUpsertRepository",
    "ReplacementUpsertRequest",
    "SignatureCanonicalizationFailedError",
    "UpsertDisposition",
    "UpsertOutcome",
]
