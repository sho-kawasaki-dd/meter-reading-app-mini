"""PC と Android の API 契約を表す Pydantic モデル群。

このモジュールでは、企画書で定義された固定語彙や payload 形状を API 境界で検証する。
DB 側では文字列として柔軟に持つ項目でも、ここでは enum や Decimal 文字列として明示し、
クライアントとサーバーの契約を早い段階で壊さないようにする。
"""

from __future__ import annotations

import re
from datetime import date
from decimal import Decimal, InvalidOperation
from enum import StrEnum
from typing import Annotated, Any

from pydantic import (
    AwareDatetime,
    BaseModel,
    ConfigDict,
    Field,
    PlainSerializer,
    WithJsonSchema,
    model_validator,
)
from pydantic.functional_validators import BeforeValidator
from pydantic.json_schema import JsonSchemaValue

SIGNED_DECIMAL_STRING_PATTERN = re.compile(r"^-?\d+(\.\d+)?$")
NON_NEGATIVE_DECIMAL_STRING_PATTERN = re.compile(r"^\d+(\.\d+)?$")


def _build_decimal_json_schema(pattern: re.Pattern[str], examples: list[str]) -> JsonSchemaValue:
    """Decimal 文字列向けの JSON Schema 断片を組み立てる。"""

    return {
        "type": "string",
        "pattern": pattern.pattern,
        "examples": examples,
    }


SIGNED_DECIMAL_JSON_SCHEMA = _build_decimal_json_schema(
    SIGNED_DECIMAL_STRING_PATTERN,
    ["-1.25", "0", "123.45"],
)
NON_NEGATIVE_DECIMAL_JSON_SCHEMA = _build_decimal_json_schema(
    NON_NEGATIVE_DECIMAL_STRING_PATTERN,
    ["0", "123.45", "99999.0001"],
)
PERCENT_DECIMAL_JSON_SCHEMA = _build_decimal_json_schema(
    NON_NEGATIVE_DECIMAL_STRING_PATTERN,
    ["0", "85.5", "100"],
)


def _parse_decimal_as_string(value: object, *, pattern: re.Pattern[str]) -> Decimal:
    """JSON 上の decimal string を Decimal へ変換する。"""

    if isinstance(value, Decimal):
        decimal_value = value
    elif isinstance(value, str):
        if not pattern.fullmatch(value):
            raise ValueError("Decimal values must be provided as plain decimal strings.")
        try:
            decimal_value = Decimal(value)
        except InvalidOperation as exc:
            raise ValueError("Invalid decimal string.") from exc
    else:
        raise TypeError("Decimal values must be provided as strings.")

    if not decimal_value.is_finite():
        raise ValueError("Decimal values must be finite.")

    return decimal_value


def _serialize_decimal_as_string(value: Decimal) -> str:
    """Decimal を API 契約どおりの plain string へ戻す。"""

    return format(value, "f")


def _parse_non_negative_decimal_as_string(value: object) -> Decimal:
    return _parse_decimal_as_string(value, pattern=NON_NEGATIVE_DECIMAL_STRING_PATTERN)


def _parse_signed_decimal_as_string(value: object) -> Decimal:
    return _parse_decimal_as_string(value, pattern=SIGNED_DECIMAL_STRING_PATTERN)


DecimalAsString = Annotated[
    Decimal,
    BeforeValidator(_parse_non_negative_decimal_as_string),
    PlainSerializer(_serialize_decimal_as_string, return_type=str, when_used="always"),
    WithJsonSchema(NON_NEGATIVE_DECIMAL_JSON_SCHEMA, mode="validation"),
    WithJsonSchema(NON_NEGATIVE_DECIMAL_JSON_SCHEMA, mode="serialization"),
]

SignedDecimalAsString = Annotated[
    Decimal,
    BeforeValidator(_parse_signed_decimal_as_string),
    PlainSerializer(_serialize_decimal_as_string, return_type=str, when_used="always"),
    WithJsonSchema(SIGNED_DECIMAL_JSON_SCHEMA, mode="validation"),
    WithJsonSchema(SIGNED_DECIMAL_JSON_SCHEMA, mode="serialization"),
]

PercentAsString = Annotated[
    Decimal,
    BeforeValidator(_parse_non_negative_decimal_as_string),
    PlainSerializer(_serialize_decimal_as_string, return_type=str, when_used="always"),
    WithJsonSchema(PERCENT_DECIMAL_JSON_SCHEMA, mode="validation"),
    WithJsonSchema(PERCENT_DECIMAL_JSON_SCHEMA, mode="serialization"),
]


class BaseApiModel(BaseModel):
    """API 契約モデル全体で共有する Pydantic 設定。"""

    model_config = ConfigDict(
        extra="forbid",
        str_strip_whitespace=True,
        validate_assignment=True,
    )


class Category(StrEnum):
    """物理メーターや測定対象のカテゴリ語彙。"""

    ELECTRICITY = "electricity"
    GAS = "gas"
    WATER = "water"
    BOILER = "boiler"
    OTHERS = "others"


class CalculationMethod(StrEnum):
    """measurement definition ごとの計算ポリシー。"""

    DELTA = "delta"
    RAW_ONLY = "raw_only"


class ValueSchema(StrEnum):
    """測定値の表現形式を示す語彙。"""

    DECIMAL_STRING = "decimal_string"
    POWER_FACTOR_PERCENT_PHASE = "power_factor_percent_phase"


class Purpose(StrEnum):
    """measurement definition の検針目的。集計時の用途別フィルタに用いる。"""

    OPERATIONAL = "operational"
    BILLING = "billing"
    AUDIT = "audit"


class ReadingCycle(StrEnum):
    """予定枠生成や UI 分類に使う検針頻度。"""

    DAILY_1X = "daily_1x"
    DAILY_2X = "daily_2x"
    DAILY_3X = "daily_3x"
    WEEKLY = "weekly"
    MONTHLY = "monthly"
    EVERY_TWO_MONTHS = "every_two_months"


class ReadingSession(StrEnum):
    """業務日付の中での検針スロット。"""

    SESSION_0000 = "00:00"
    SESSION_0600 = "06:00"
    SESSION_0800 = "08:00"
    SESSION_1400 = "14:00"
    SESSION_1700 = "17:00"
    SESSION_2200 = "22:00"


class ResultStatus(StrEnum):
    """Android が返す検針結果の状態語彙。"""

    MEASURED = "measured"
    SKIPPED = "skipped"
    UNREACHABLE = "unreachable"
    METER_FAULT = "meter_fault"
    REPLACED = "replaced"


class PowerFactorPhase(StrEnum):
    """力率の位相分類。"""

    LEAD = "lead"
    LAG = "lag"
    UNITY = "unity"


class PowerFactorReading(BaseApiModel):
    """`power_factor_percent_phase` 用の複合 payload。"""

    percent: PercentAsString = Field(
        description="Power factor magnitude represented as a percentage from 0 to 100.",
    )
    phase: PowerFactorPhase = Field(
        description="Lead/lag classification for the measured power factor.",
    )

    @model_validator(mode="after")
    def validate_percent_and_phase(self) -> PowerFactorReading:
        if self.percent > Decimal("100"):
            raise ValueError("power_factor percent must be less than or equal to 100.")
        if self.percent == Decimal("100") and self.phase is not PowerFactorPhase.UNITY:
            raise ValueError("power_factor phase must be unity when percent is 100.")
        if self.percent < Decimal("100") and self.phase is PowerFactorPhase.UNITY:
            raise ValueError("power_factor phase can be unity only when percent is 100.")
        return self


class MeterMasterData(BaseApiModel):
    """Android へ配る物理メーターマスター。"""

    meter_id: str = Field(min_length=1, description="Physical meter identifier.")
    location_id: str = Field(min_length=1, description="Logical location identifier.")
    location_name: str = Field(min_length=1, description="Display name for the location.")
    meter_name: str = Field(min_length=1, description="Display name for the physical meter.")
    category: Category = Field(description="Physical meter category vocabulary.")
    meter_serial: str | None = Field(
        default=None, description="Optional serial number or external asset identifier."
    )


class MeasurementDefinitionData(BaseApiModel):
    """Android へ配る論理的な測定対象マスター。"""

    measurement_definition_id: str = Field(
        min_length=1, description="Logical measurement target identifier."
    )
    location_id: str = Field(min_length=1, description="Logical location identifier.")
    meter_id: str | None = Field(
        default=None, description="Current physical meter assignment snapshot."
    )
    display_name: str = Field(min_length=1, description="Operator-facing measurement label.")
    category: Category = Field(description="Measurement category vocabulary.")
    unit: str = Field(min_length=1, description="Display unit for the measurement target.")
    value_schema: ValueSchema = Field(
        description="Input schema used by the client for this measurement target."
    )
    last_reading: DecimalAsString | None = Field(
        default=None, description="Most recent measured value when represented as a decimal string."
    )
    multiplier: DecimalAsString = Field(
        description="Scaling factor applied to the recorded raw value."
    )
    decimal_places: int | None = Field(
        default=None, description="Optional maximum number of decimal places allowed by the client."
    )
    warning_threshold: DecimalAsString | None = Field(
        default=None, description="Optional client-side warning threshold."
    )
    rollover_value: DecimalAsString | None = Field(
        default=None, description="Optional rollover boundary for cumulative meters."
    )
    calculation_method: CalculationMethod = Field(
        description="Usage calculation policy for this measurement target."
    )
    reading_cycle: ReadingCycle = Field(
        description="Expected reading cadence for this measurement target."
    )
    purpose: Purpose = Field(
        description="Business purpose of the measurement (e.g. operational, billing, audit)."
    )


class PlannedReadingTask(BaseApiModel):
    """Android が表示・入力対象にする予定枠 1 件分。"""

    planned_reading_id: str = Field(
        min_length=1, description="Stable identifier for the planned reading slot."
    )
    measurement_definition_id: str = Field(
        min_length=1, description="Logical measurement target assigned to the slot."
    )
    meter_id: str = Field(
        min_length=1, description="Physical meter assigned to the slot at distribution time."
    )
    location_id: str = Field(
        min_length=1, description="Logical location identifier for aggregation."
    )
    business_date: date = Field(description="Business date assigned by the PC worklist generator.")
    release_date: date = Field(
        description="Date when the planned reading may be distributed to Android."
    )
    reading_session: ReadingSession = Field(description="Business time slot assigned by the PC.")
    scheduled_at: AwareDatetime = Field(
        description="Offset-aware timestamp when this planned reading is scheduled."
    )
    reading_cycle: ReadingCycle = Field(
        description="Measurement cadence retained on each task for audit purposes."
    )
    required: bool = Field(
        description="Whether this slot is expected to be completed in the current package."
    )


class WorkPackageTaskGroups(BaseApiModel):
    """Android UI のページ分割をそのまま表した task group。"""

    daily_tasks: list[PlannedReadingTask] = Field(description="Daily planned reading tasks.")
    weekly_tasks: list[PlannedReadingTask] = Field(description="Weekly planned reading tasks.")
    monthly_tasks: list[PlannedReadingTask] = Field(description="Monthly planned reading tasks.")
    every_two_months_tasks: list[PlannedReadingTask] = Field(
        description="Every-two-months planned reading tasks."
    )
    ad_hoc_tasks: list[PlannedReadingTask] = Field(
        description="Ad-hoc or exceptional planned reading tasks."
    )


class WorkPackageResponse(BaseApiModel):
    """PC から Android へ返す work package 全体。"""

    meters: list[MeterMasterData] = Field(
        description="Physical meter master data required by the Android client."
    )
    measurement_definitions: list[MeasurementDefinitionData] = Field(
        description="Logical measurement targets required by the Android client.",
    )
    work_package: WorkPackageTaskGroups = Field(description="UI-grouped planned reading tasks.")
    backfill_pending: bool = Field(
        default=False,
        description="Whether the requested backfill was capped and another request is needed.",
    )


def _normalize_structured_payload(payload: dict[str, Any]) -> dict[str, Any]:
    """複合 payload を helper schema で検証し、JSON 安全な形へ正規化する。"""

    power_factor = PowerFactorReading.model_validate(payload)
    return power_factor.model_dump(mode="json")


def _count_decimal_places(value: Decimal) -> int:
    """Decimal の末尾ゼロを含む小数桁数を返す。"""

    exponent = value.as_tuple().exponent
    if exponent >= 0:
        return 0
    return -exponent


class MeterReadingSubmission(BaseApiModel):
    """Android から返却される transport-level の検針結果。"""

    planned_reading_id: str = Field(
        min_length=1, description="Natural key of the planned reading slot."
    )
    result_status: ResultStatus = Field(
        description="Outcome recorded for the planned reading slot."
    )
    raw_reading: DecimalAsString | None = Field(
        default=None,
        description=(
            "Decimal reading payload for measured submissions. Transport validation checks only the overall "
            "shape; strict value_schema-dependent requirements are applied after planned_reading_id is resolved."
        ),
    )
    structured_payload: dict[str, Any] | None = Field(
        default=None,
        description=(
            "Flexible object payload for non-decimal measurement schemas. Transport validation accepts a generic "
            "object here; strict payload parsing occurs during resolved validation after planned_reading_id is "
            "mapped to a measurement definition. See PowerFactorReading for the initial helper schema used by "
            "power_factor_percent_phase."
        ),
    )
    recorded_at: AwareDatetime = Field(
        description="Offset-aware ISO8601 timestamp recorded on the client."
    )
    client_updated_at: AwareDatetime = Field(
        description="Offset-aware ISO8601 timestamp used for client-origin conflict resolution.",
    )
    device_id: str = Field(
        description=(
            "Stable per-install Android device identifier generated on first launch and persisted locally. "
            "This field is required by the transport contract and is used as part of the signature seed. "
            "Blank values are rejected by the server-side signature verifier."
        ),
    )
    updated_by: str = Field(
        min_length=1,
        description="Operator identifier or display label that last updated the result.",
    )
    signature: str = Field(min_length=1, description="Opaque tamper-detection signature string.")
    note: str | None = Field(default=None, description="Optional operator note.")

    @model_validator(mode="after")
    def validate_transport_shape(self) -> MeterReadingSubmission:
        """transport contract としての最低限の形だけを検証する。"""

        if self.result_status is ResultStatus.MEASURED:
            has_raw = self.raw_reading is not None
            has_structured = self.structured_payload is not None
            if has_raw == has_structured:
                raise ValueError(
                    "measured submissions must provide exactly one of raw_reading or structured_payload."
                )
            return self

        if self.raw_reading is not None or self.structured_payload is not None:
            raise ValueError(
                "raw_reading and structured_payload must be null unless result_status is measured."
            )
        return self


class ResolvedMeterReadingSubmission(MeterReadingSubmission):
    """planned reading 解決後にだけ使える server-side validation モデル。"""

    measurement_definition_id: str = Field(
        min_length=1,
        description="Resolved logical measurement target identifier for the planned reading slot.",
    )
    value_schema: ValueSchema = Field(
        description="Resolved measurement payload schema used for strict server-side validation.",
    )
    decimal_places: int | None = Field(
        default=None,
        description="Resolved maximum number of decimal places allowed for decimal_string submissions.",
    )
    rollover_value: DecimalAsString | None = Field(
        default=None,
        description="Resolved rollover boundary used for decimal_string submissions.",
    )
    validated_structured_payload: PowerFactorReading | None = Field(
        default=None,
        exclude=True,
        repr=False,
        description="Parsed helper payload retained for server-side processing when structured_payload is used.",
    )

    @model_validator(mode="after")
    def validate_resolved_payload(self) -> ResolvedMeterReadingSubmission:
        """measurement definition にひもづく value_schema で厳密検証する。"""

        if self.result_status is not ResultStatus.MEASURED:
            object.__setattr__(self, "validated_structured_payload", None)
            return self

        if self.value_schema is ValueSchema.DECIMAL_STRING:
            if self.raw_reading is None or self.structured_payload is not None:
                raise ValueError(
                    "resolved decimal_string submissions must provide raw_reading and must not provide structured_payload."
                )
            if (
                self.decimal_places is not None
                and _count_decimal_places(self.raw_reading) > self.decimal_places
            ):
                raise ValueError(
                    f"resolved decimal_string submissions must not exceed {self.decimal_places} decimal place(s)."
                )
            if self.rollover_value is not None and self.raw_reading >= self.rollover_value:
                raise ValueError(
                    "resolved decimal_string submissions must be less than rollover_value."
                )
            object.__setattr__(self, "validated_structured_payload", None)
            return self

        if self.value_schema is ValueSchema.POWER_FACTOR_PERCENT_PHASE:
            # 複合型は helper schema で一度正規化してから保持する。
            if self.raw_reading is not None or self.structured_payload is None:
                raise ValueError(
                    "resolved power_factor_percent_phase submissions must provide structured_payload and must not provide raw_reading."
                )
            normalized_payload = _normalize_structured_payload(self.structured_payload)
            validated_payload = PowerFactorReading.model_validate(normalized_payload)
            object.__setattr__(self, "structured_payload", normalized_payload)
            object.__setattr__(self, "validated_structured_payload", validated_payload)
            return self

        raise ValueError(f"Unsupported value_schema for resolved validation: {self.value_schema}")


def resolve_meter_reading_submission(
    submission: MeterReadingSubmission,
    *,
    measurement_definition_id: str,
    value_schema: ValueSchema,
    decimal_places: int | None,
    rollover_value: Decimal | None,
) -> ResolvedMeterReadingSubmission:
    """transport contract を measurement-aware な resolved model へ変換する。"""

    return ResolvedMeterReadingSubmission.model_validate(
        {
            **submission.model_dump(mode="python"),
            "measurement_definition_id": measurement_definition_id,
            "value_schema": value_schema,
            "decimal_places": decimal_places,
            "rollover_value": rollover_value,
        }
    )


class MeterReplacementSubmission(BaseApiModel):
    """Android から返却されるメーター交換イベント。"""

    replacement_event_id: str = Field(
        min_length=1, description="Natural key for the replacement event."
    )
    location_id: str = Field(
        min_length=1, description="Logical location identifier preserved across swaps."
    )
    old_meter_id: str = Field(min_length=1, description="Meter identifier being retired.")
    new_meter_id: str = Field(
        min_length=1, description="Meter identifier taking over the location."
    )
    replaced_at: AwareDatetime = Field(description="Offset-aware ISO8601 replacement timestamp.")
    client_updated_at: AwareDatetime = Field(
        description="Offset-aware ISO8601 timestamp used for client-origin conflict resolution.",
    )
    old_final_reading: DecimalAsString | None = Field(
        default=None,
        description="Optional final reading captured from the retired meter.",
    )
    new_initial_reading: DecimalAsString | None = Field(
        default=None,
        description="Optional initial reading captured from the new meter.",
    )
    updated_by: str = Field(
        min_length=1, description="User or device identifier that last updated the event."
    )
    note: str | None = Field(default=None, description="Optional operator note.")


class MeterReadingSyncRequest(BaseApiModel):
    """前日分アップロードに使う batch request。"""

    reading_items: list[MeterReadingSubmission] = Field(
        description="Backlog-friendly batch of reading results."
    )
    replacement_items: list[MeterReplacementSubmission] = Field(
        description="Backlog-friendly batch of replacement events.",
    )


class ApiRequestErrorCode(StrEnum):
    """request 単位で API 全体を reject した理由の語彙。"""

    DEVICE_NOT_ACTIVE = "device_not_active"
    MISSING_X_DEVICE_ID_HEADER = "missing_x_device_id_header"
    INVALID_X_DEVICE_ID_HEADER = "invalid_x_device_id_header"
    HEADER_BODY_DEVICE_ID_MISMATCH = "header_body_device_id_mismatch"
    PAYLOAD_TOO_LARGE = "payload_too_large"


class ApiRequestErrorResponse(BaseApiModel):
    """request 単位の 4xx エラーを Android へ返す固定 shape。"""

    code: ApiRequestErrorCode = Field(description="Stable machine-readable request error code.")
    detail: str = Field(
        min_length=1, description="Human-readable Japanese detail for operators or support."
    )
    request_device_id: str | None = Field(
        default=None,
        description="Device identifier parsed from X-Device-Id when available.",
    )
    retryable: bool = Field(
        description="Whether the client can retry without changing local state or server configuration."
    )
    mismatched_reading_item_indexes: list[int] = Field(
        default_factory=list,
        description="Zero-based reading_items indexes whose body device_id did not match X-Device-Id.",
    )


class SyncItemRejectCode(StrEnum):
    """item を reject した理由の語彙。"""

    UNKNOWN_PLANNED_READING = "unknown_planned_reading"
    UNKNOWN_MEASUREMENT_DEFINITION = "unknown_measurement_definition"
    INVALID_READING_SUBMISSION = "invalid_reading_submission"
    MISSING_DEVICE_ID = "missing_device_id"
    SIGNATURE_CANONICALIZATION_FAILED = "signature_canonicalization_failed"
    INVALID_SIGNATURE = "invalid_signature"
    UNEXPECTED_ERROR = "unexpected_error"


class SyncItemIgnoreCode(StrEnum):
    """item は受理したが適用しなかった理由の語彙。"""

    STALE_CLIENT_UPDATE = "stale_client_update"


class IgnoredReadingSyncItem(BaseApiModel):
    """古い再送などで適用されなかった reading item の記録。"""

    item_index: int = Field(
        ge=0, description="Zero-based position in reading_items for correlation on the client."
    )
    planned_reading_id: str = Field(
        min_length=1, description="Natural key of the reading item acknowledged but not applied."
    )
    code: SyncItemIgnoreCode = Field(
        description="Reason the reading item was acknowledged but not applied."
    )
    detail: str | None = Field(default=None, description="Optional detail for operators or logs.")


class IgnoredReplacementSyncItem(BaseApiModel):
    """古い再送などで適用されなかった replacement item の記録。"""

    item_index: int = Field(
        ge=0, description="Zero-based position in replacement_items for correlation on the client."
    )
    replacement_event_id: str = Field(
        min_length=1,
        description="Natural key of the replacement item acknowledged but not applied.",
    )
    code: SyncItemIgnoreCode = Field(
        description="Reason the replacement item was acknowledged but not applied."
    )
    detail: str | None = Field(default=None, description="Optional detail for operators or logs.")


class RejectedReadingSyncItem(BaseApiModel):
    """lookup や validation に失敗して reject された reading item。"""

    item_index: int = Field(
        ge=0, description="Zero-based position in reading_items for correlation on the client."
    )
    planned_reading_id: str = Field(
        min_length=1, description="Natural key of the reading item rejected by the server."
    )
    code: SyncItemRejectCode = Field(description="Reason the server rejected the reading item.")
    detail: str = Field(
        min_length=1, description="Human-readable rejection detail for repair or support."
    )


class RejectedReplacementSyncItem(BaseApiModel):
    """reject された replacement item。"""

    item_index: int = Field(
        ge=0, description="Zero-based position in replacement_items for correlation on the client."
    )
    replacement_event_id: str = Field(
        min_length=1, description="Natural key of the replacement item rejected by the server."
    )
    code: SyncItemRejectCode = Field(description="Reason the server rejected the replacement item.")
    detail: str = Field(
        min_length=1, description="Human-readable rejection detail for repair or support."
    )


class MeterReadingSyncResponse(BaseApiModel):
    """batch upload の処理結果を Android へ返す response。"""

    accepted_reading_items: int = Field(
        ge=0, description="Count of reading items accepted by the server."
    )
    accepted_replacement_items: int = Field(
        ge=0,
        description="Count of replacement items accepted by the server.",
    )
    ignored_reading_items: list[IgnoredReadingSyncItem] = Field(
        default_factory=list,
        description="Reading items acknowledged but not applied, typically because a newer client update already exists.",
    )
    ignored_replacement_items: list[IgnoredReplacementSyncItem] = Field(
        default_factory=list,
        description="Replacement items acknowledged but not applied, typically because a newer client update already exists.",
    )
    rejected_reading_items: list[RejectedReadingSyncItem] = Field(
        default_factory=list,
        description="Reading items rejected while other items in the same batch may still succeed.",
    )
    rejected_replacement_items: list[RejectedReplacementSyncItem] = Field(
        default_factory=list,
        description="Replacement items rejected while other items in the same batch may still succeed.",
    )
    detail: str | None = Field(
        default=None, description="Optional acknowledgement or warning message."
    )


API_CONTRACT_MODELS: tuple[type[BaseApiModel], ...] = (
    MeterMasterData,
    MeasurementDefinitionData,
    PlannedReadingTask,
    PowerFactorReading,
    WorkPackageTaskGroups,
    WorkPackageResponse,
    MeterReadingSubmission,
    MeterReplacementSubmission,
    ApiRequestErrorResponse,
    IgnoredReadingSyncItem,
    IgnoredReplacementSyncItem,
    RejectedReadingSyncItem,
    RejectedReplacementSyncItem,
    MeterReadingSyncRequest,
    MeterReadingSyncResponse,
)

API_CONTRACT_RELATIONSHIP_NOTES: tuple[dict[str, str], ...] = (
    {
        "model": "MeterReadingSubmission",
        "kind": "transport_contract",
        "message": (
            "MeterReadingSubmission remains a generic transport request body keyed by planned_reading_id. "
            "Public Pydantic validation checks syntax only."
        ),
    },
    {
        "model": "MeterReadingSubmission",
        "field": "structured_payload",
        "helper_schema": "PowerFactorReading",
        "value_schema": ValueSchema.POWER_FACTOR_PERCENT_PHASE.value,
        "message": (
            "After planned_reading_id is resolved to a measurement definition, strict resolved validation parses "
            "structured_payload with PowerFactorReading when value_schema is power_factor_percent_phase."
        ),
    },
    {
        "model": "MeterReadingSubmission",
        "field": "raw_reading",
        "value_schema": ValueSchema.DECIMAL_STRING.value,
        "message": (
            "After planned_reading_id is resolved to a measurement definition, strict resolved validation requires "
            "raw_reading and rejects structured_payload when value_schema is decimal_string."
        ),
    },
)


def build_api_contract_schema_snapshot() -> dict[str, Any]:
    """契約確認用の JSON Schema snapshot を組み立てる。"""

    return {
        "$schema": "https://json-schema.org/draft/2020-12/schema",
        "title": "Meter Reading API Contract Snapshot",
        "models": {model.__name__: model.model_json_schema() for model in API_CONTRACT_MODELS},
        "relationship_notes": list(API_CONTRACT_RELATIONSHIP_NOTES),
    }


__all__ = [
    "API_CONTRACT_MODELS",
    "BaseApiModel",
    "CalculationMethod",
    "Category",
    "ApiRequestErrorCode",
    "ApiRequestErrorResponse",
    "DecimalAsString",
    "MeasurementDefinitionData",
    "MeterMasterData",
    "MeterReadingSubmission",
    "MeterReadingSyncRequest",
    "MeterReadingSyncResponse",
    "MeterReplacementSubmission",
    "IgnoredReadingSyncItem",
    "IgnoredReplacementSyncItem",
    "PercentAsString",
    "PlannedReadingTask",
    "PowerFactorPhase",
    "PowerFactorReading",
    "Purpose",
    "RejectedReadingSyncItem",
    "RejectedReplacementSyncItem",
    "ResolvedMeterReadingSubmission",
    "ReadingCycle",
    "ReadingSession",
    "ResultStatus",
    "SignedDecimalAsString",
    "SyncItemIgnoreCode",
    "SyncItemRejectCode",
    "ValueSchema",
    "WorkPackageResponse",
    "WorkPackageTaskGroups",
    "build_api_contract_schema_snapshot",
    "resolve_meter_reading_submission",
]
