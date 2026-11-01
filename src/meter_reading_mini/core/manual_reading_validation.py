"""直接入力の保存とプレビューで共通の検証を行う。"""

from __future__ import annotations

from datetime import datetime

from pydantic import ValidationError as PydanticValidationError

from meter_reading_mini.core.contract_models import (
    MeterReadingSubmission,
    ResolvedMeterReadingSubmission,
    resolve_meter_reading_submission,
)
from meter_reading_mini.core.errors import ValidationError
from meter_reading_mini.core.meter_reading_sync_repositories import (
    ManualReadingInput,
    MeasurementDefinitionRecord,
    MeterAssignmentRecord,
)
from meter_reading_mini.core.mobile_reading import validate_note
from meter_reading_mini.core.timezone_settings import normalize_to_utc


def resolve_manual_reading(
    reading: ManualReadingInput,
    definition: MeasurementDefinitionRecord,
    assignment: MeterAssignmentRecord,
    *,
    now: datetime,
) -> ResolvedMeterReadingSubmission:
    if normalize_to_utc(reading.recorded_at) > normalize_to_utc(now):
        raise ValidationError("未来の記録日時は指定できません。")
    try:
        submission = MeterReadingSubmission(
            planned_reading_id=reading.planned_reading_id,
            result_status=reading.result_status,
            raw_reading=reading.raw_reading,
            structured_payload=reading.structured_payload_json,
            recorded_at=reading.recorded_at,
            client_updated_at=reading.recorded_at,
            note=validate_note(reading.note),
        )
        return resolve_meter_reading_submission(
            submission,
            measurement_definition_id=definition.measurement_definition_id,
            value_schema=definition.value_schema,
            decimal_places=definition.decimal_places,
            rollover_value=assignment.rollover_value,
        )
    except PydanticValidationError as exc:
        issues = exc.errors(include_url=False)
        message = "; ".join(issue["msg"] for issue in issues) if issues else str(exc)
        raise ValidationError(message) from exc
