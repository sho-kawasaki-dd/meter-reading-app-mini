# pyright: reportMissingImports=false

"""API 契約モデルの shape と validation ルールを確認するテスト群。

このファイルは比較的 self-documenting だが、Decimal 文字列、power factor payload、
transport validation と resolved validation の差を保証する役割を持つ。
"""

from __future__ import annotations

import json
from datetime import datetime
from decimal import Decimal

import pytest
from pydantic import ValidationError

from meter_reading_mini.core.contract_models import (
    Category,
    IgnoredReadingSyncItem,
    MeasurementDefinitionData,
    MeterMasterData,
    MeterReadingSubmission,
    MeterReadingSyncRequest,
    MeterReadingSyncResponse,
    PowerFactorPhase,
    PowerFactorReading,
    ReadingCycle,
    ReadingSession,
    RejectedReadingSyncItem,
    ResolvedMeterReadingSubmission,
    ResultStatus,
    SyncItemIgnoreCode,
    SyncItemRejectCode,
    ValueSchema,
    WorkPackageResponse,
    WorkPackageTaskGroups,
    build_api_contract_schema_snapshot,
    resolve_meter_reading_submission,
)
from meter_reading_mini.core.meter_reading_sync_repositories import (
    PC_DIRECT_DEVICE_ID,
    ReadingConfirmationContext,
)
from meter_reading_mini.core.reading_signature import calculate_meter_reading_signature

TEST_DEVICE_ID = "550e8400-e29b-41d4-a716-446655440000"


def build_planned_task() -> dict[str, object]:
    return {
        "planned_reading_id": "plan-2026-04-01-0800-001",
        "measurement_definition_id": "md-001",
        "meter_id": "meter-001",
        "location_id": "loc-001",
        "business_date": "2026-04-01",
        "release_date": "2026-04-01",
        "reading_session": "08:00",
        "scheduled_at": "2026-03-31T23:00:00Z",
        "reading_cycle": "daily_1x",
        "required": True,
    }


def build_meter_master() -> dict[str, object]:
    return {
        "meter_id": "meter-001",
        "location_id": "loc-001",
        "location_name": "Hotel Main",
        "meter_name": "Main Incoming Meter",
        "category": "boiler",
        "meter_serial": "SER-0001",
    }


def build_measurement_definition() -> dict[str, object]:
    return {
        "measurement_definition_id": "md-001",
        "location_id": "loc-001",
        "meter_id": "meter-001",
        "display_name": "Main Incoming kWh",
        "category": "electricity",
        "unit": "kWh",
        "value_schema": "decimal_string",
        "last_reading": "12345.67",
        "multiplier": "1.0",
        "warning_threshold": "150.50",
        "decimal_places": 1,
        "rollover_value": "100000",
        "calculation_method": "delta",
        "reading_cycle": "daily_1x",
        "purpose": "operational",
    }


def test_meter_master_data_accepts_physical_meter_fields() -> None:
    model = MeterMasterData.model_validate(build_meter_master())

    assert model.category is Category.BOILER
    assert model.meter_name == "Main Incoming Meter"
    assert json.loads(model.model_dump_json())["meter_serial"] == "SER-0001"


def test_measurement_definition_serializes_decimals_as_strings() -> None:
    model = MeasurementDefinitionData.model_validate(build_measurement_definition())

    assert model.category is Category.ELECTRICITY
    assert model.value_schema is ValueSchema.DECIMAL_STRING
    assert model.reading_cycle is ReadingCycle.DAILY_1X
    assert model.last_reading == Decimal("12345.67")
    assert model.decimal_places == 1
    assert model.model_dump()["last_reading"] == "12345.67"
    assert model.model_dump()["decimal_places"] == 1
    assert json.loads(model.model_dump_json())["warning_threshold"] == "150.50"
    assert json.loads(model.model_dump_json())["decimal_places"] == 1


def test_measurement_definition_allows_nullable_decimal_places() -> None:
    model = MeasurementDefinitionData.model_validate(
        {**build_measurement_definition(), "decimal_places": None}
    )

    assert model.decimal_places is None


def test_measurement_definition_rejects_negative_decimal_fields() -> None:
    with pytest.raises(ValidationError):
        MeasurementDefinitionData.model_validate(
            {
                "measurement_definition_id": "md-001",
                "location_id": "loc-001",
                "meter_id": "meter-001",
                "display_name": "Main Incoming kWh",
                "category": "electricity",
                "unit": "kWh",
                "value_schema": "decimal_string",
                "last_reading": "12345.67",
                "multiplier": "1.0",
                "warning_threshold": "-150.50",
                "rollover_value": "100000",
                "calculation_method": "delta",
                "reading_cycle": "daily_1x",
                "purpose": "operational",
            }
        )


def test_meter_reading_submission_requires_raw_reading_for_measured() -> None:
    with pytest.raises(ValidationError):
        MeterReadingSubmission.model_validate(
            {
                "planned_reading_id": "plan-001",
                "result_status": "measured",
                "raw_reading": None,
                "structured_payload": None,
                "recorded_at": "2026-04-01T08:00:00+09:00",
                "client_updated_at": "2026-04-01T08:05:00+09:00",
            }
        )


def test_mobile_submission_needs_no_pc_signature_or_confirmed_staff() -> None:
    submission = MeterReadingSubmission.model_validate(
        {
            "planned_reading_id": "plan-001",
            "result_status": "measured",
            "raw_reading": "10.0",
            "recorded_at": "2026-04-01T08:00:00+09:00",
            "client_updated_at": "2026-04-01T08:05:00+09:00",
        }
    )
    confirmation = ReadingConfirmationContext(
        sheet_id="a" * 32,
        staff_id="staff-001",
        staff_name="担当者A",
    )

    assert submission.raw_reading == Decimal("10.0")
    assert not {"device_id", "updated_by", "signature"} & MeterReadingSubmission.model_fields.keys()
    assert confirmation.sheet_id == "a" * 32
    assert confirmation.staff_name == "担当者A"
    assert PC_DIRECT_DEVICE_ID == "pc-direct"


def test_calculate_meter_reading_signature_is_stable_for_raw_reading() -> None:
    signature = calculate_meter_reading_signature(
        planned_reading_id="plan-001",
        result_status=ResultStatus.MEASURED,
        raw_reading=Decimal("10.50"),
        structured_payload_json=None,
        recorded_at=datetime.fromisoformat("2026-04-01T08:00:00+09:00"),
        device_id=TEST_DEVICE_ID,
    )

    assert len(signature) == 64
    assert signature == calculate_meter_reading_signature(
        planned_reading_id="plan-001",
        result_status=ResultStatus.MEASURED,
        raw_reading=Decimal("10.50"),
        structured_payload_json=None,
        recorded_at=datetime.fromisoformat("2026-04-01T08:00:00+09:00"),
        device_id=TEST_DEVICE_ID,
    )


def test_calculate_meter_reading_signature_canonicalizes_structured_payload_key_order() -> None:
    signature_a = calculate_meter_reading_signature(
        planned_reading_id="plan-pf-001",
        result_status=ResultStatus.MEASURED,
        raw_reading=None,
        structured_payload_json={"phase": "lag", "percent": "97.5"},
        recorded_at=datetime.fromisoformat("2026-04-01T14:00:00+09:00"),
        device_id=TEST_DEVICE_ID,
    )
    signature_b = calculate_meter_reading_signature(
        planned_reading_id="plan-pf-001",
        result_status=ResultStatus.MEASURED,
        raw_reading=None,
        structured_payload_json={"percent": "97.5", "phase": "lag"},
        recorded_at=datetime.fromisoformat("2026-04-01T14:00:00+09:00"),
        device_id=TEST_DEVICE_ID,
    )

    assert signature_a == signature_b


def test_power_factor_reading_requires_unity_for_100_percent() -> None:
    valid = PowerFactorReading.model_validate(
        {
            "percent": "100",
            "phase": "unity",
        }
    )

    assert valid.phase is PowerFactorPhase.UNITY

    with pytest.raises(ValidationError):
        PowerFactorReading.model_validate(
            {
                "percent": "100",
                "phase": "lag",
            }
        )

    with pytest.raises(ValidationError):
        PowerFactorReading.model_validate(
            {
                "percent": "98.5",
                "phase": "unity",
            }
        )

    with pytest.raises(ValidationError):
        PowerFactorReading.model_validate(
            {
                "percent": "101",
                "phase": "lag",
            }
        )


def test_meter_reading_submission_accepts_generic_structured_payload_in_transport_contract() -> (
    None
):
    submission = MeterReadingSubmission.model_validate(
        {
            "planned_reading_id": "plan-001",
            "result_status": "measured",
            "structured_payload": {
                "percent": "101",
                "phase": "lag",
            },
            "recorded_at": "2026-04-01T08:00:00+09:00",
            "client_updated_at": "2026-04-01T08:05:00+09:00",
        }
    )

    assert submission.raw_reading is None
    assert submission.structured_payload == {
        "percent": "101",
        "phase": "lag",
    }


def test_meter_reading_submission_rejects_raw_reading_for_non_measured() -> None:
    with pytest.raises(ValidationError):
        MeterReadingSubmission.model_validate(
            {
                "planned_reading_id": "plan-001",
                "result_status": "skipped",
                "raw_reading": "10.0",
                "structured_payload": None,
                "recorded_at": "2026-04-01T08:00:00+09:00",
                "client_updated_at": "2026-04-01T08:05:00+09:00",
            }
        )


def test_resolved_meter_reading_submission_validates_power_factor_payload() -> None:
    transport_submission = MeterReadingSubmission.model_validate(
        {
            "planned_reading_id": "plan-001",
            "result_status": "measured",
            "structured_payload": {
                "percent": "97.5",
                "phase": "lag",
            },
            "recorded_at": "2026-04-01T08:00:00+09:00",
            "client_updated_at": "2026-04-01T08:05:00+09:00",
        }
    )

    resolved = resolve_meter_reading_submission(
        transport_submission,
        measurement_definition_id="md-pf-001",
        value_schema=ValueSchema.POWER_FACTOR_PERCENT_PHASE,
        decimal_places=None,
        rollover_value=None,
    )

    assert isinstance(resolved, ResolvedMeterReadingSubmission)
    assert resolved.measurement_definition_id == "md-pf-001"
    assert resolved.validated_structured_payload is not None
    assert resolved.structured_payload == {
        "percent": "97.5",
        "phase": "lag",
    }

    invalid_transport_submission = MeterReadingSubmission.model_validate(
        {
            "planned_reading_id": "plan-001",
            "result_status": "measured",
            "structured_payload": {
                "percent": "101",
                "phase": "lag",
            },
            "recorded_at": "2026-04-01T08:00:00+09:00",
            "client_updated_at": "2026-04-01T08:05:00+09:00",
        }
    )

    with pytest.raises(ValidationError):
        resolve_meter_reading_submission(
            invalid_transport_submission,
            measurement_definition_id="md-pf-001",
            value_schema=ValueSchema.POWER_FACTOR_PERCENT_PHASE,
            decimal_places=None,
            rollover_value=None,
        )


def test_resolved_meter_reading_submission_requires_value_schema_specific_payload() -> None:
    raw_transport = MeterReadingSubmission.model_validate(
        {
            "planned_reading_id": "plan-raw",
            "result_status": "measured",
            "raw_reading": "10.0",
            "recorded_at": "2026-04-01T08:00:00+09:00",
            "client_updated_at": "2026-04-01T08:05:00+09:00",
        }
    )

    resolved_decimal = resolve_meter_reading_submission(
        raw_transport,
        measurement_definition_id="md-decimal-001",
        value_schema=ValueSchema.DECIMAL_STRING,
        decimal_places=1,
        rollover_value=Decimal("100000"),
    )
    assert resolved_decimal.raw_reading == Decimal("10.0")
    assert resolved_decimal.decimal_places == 1
    assert resolved_decimal.rollover_value == Decimal("100000")
    assert resolved_decimal.validated_structured_payload is None

    with pytest.raises(ValidationError):
        resolve_meter_reading_submission(
            raw_transport,
            measurement_definition_id="md-pf-001",
            value_schema=ValueSchema.POWER_FACTOR_PERCENT_PHASE,
            decimal_places=None,
            rollover_value=None,
        )

    structured_transport = MeterReadingSubmission.model_validate(
        {
            "planned_reading_id": "plan-pf",
            "result_status": "measured",
            "structured_payload": {
                "percent": "97.5",
                "phase": "lag",
            },
            "recorded_at": "2026-04-01T08:00:00+09:00",
            "client_updated_at": "2026-04-01T08:05:00+09:00",
        }
    )

    with pytest.raises(ValidationError):
        resolve_meter_reading_submission(
            structured_transport,
            measurement_definition_id="md-decimal-001",
            value_schema=ValueSchema.DECIMAL_STRING,
            decimal_places=1,
            rollover_value=Decimal("100000"),
        )

    with pytest.raises(ValidationError):
        resolve_meter_reading_submission(
            MeterReadingSubmission.model_validate(
                {
                    "planned_reading_id": "plan-raw-precise",
                    "result_status": "measured",
                    "raw_reading": "10.50",
                    "recorded_at": "2026-04-01T08:00:00+09:00",
                    "client_updated_at": "2026-04-01T08:05:00+09:00",
                }
            ),
            measurement_definition_id="md-decimal-001",
            value_schema=ValueSchema.DECIMAL_STRING,
            decimal_places=1,
            rollover_value=Decimal("100000"),
        )

    with pytest.raises(ValidationError):
        resolve_meter_reading_submission(
            raw_transport,
            measurement_definition_id="md-decimal-001",
            value_schema=ValueSchema.DECIMAL_STRING,
            decimal_places=1,
            rollover_value=Decimal("10.0"),
        )

    with pytest.raises(ValidationError):
        MeterReadingSubmission.model_validate(
            {
                "planned_reading_id": "plan-001",
                "result_status": "measured",
                "raw_reading": "10.0",
                "structured_payload": {
                    "percent": "97.5",
                    "phase": "lag",
                },
                "recorded_at": "2026-04-01T08:00:00+09:00",
                "client_updated_at": "2026-04-01T08:05:00+09:00",
            }
        )


def test_meter_reading_submission_requires_offset_aware_datetimes() -> None:
    with pytest.raises(ValidationError):
        MeterReadingSubmission.model_validate(
            {
                "planned_reading_id": "plan-001",
                "result_status": "measured",
                "raw_reading": "10.0",
                "structured_payload": None,
                "recorded_at": "2026-04-01T08:00:00",
                "client_updated_at": "2026-04-01T08:05:00+09:00",
            }
        )


def test_work_package_response_uses_supported_task_groups() -> None:
    task_groups = WorkPackageTaskGroups.model_validate(
        {
            "daily_tasks": [build_planned_task()],
            "weekly_tasks": [],
            "monthly_tasks": [],
            "every_two_months_tasks": [],
        }
    )
    assert task_groups.daily_tasks[0].reading_session is ReadingSession.SESSION_0800

    response = WorkPackageResponse.model_validate(
        {
            "meters": [build_meter_master()],
            "measurement_definitions": [build_measurement_definition()],
            "work_package": {
                "daily_tasks": [build_planned_task()],
                "weekly_tasks": [],
                "monthly_tasks": [],
                "every_two_months_tasks": [],
            },
        }
    )

    assert response.work_package.daily_tasks[0].reading_session is ReadingSession.SESSION_0800


def test_sync_request_accepts_backlog_batches() -> None:
    sync_request = MeterReadingSyncRequest.model_validate(
        {
            "reading_items": [
                {
                    "planned_reading_id": "plan-001",
                    "result_status": "measured",
                    "structured_payload": {
                        "percent": "97.5",
                        "phase": "lag",
                    },
                    "recorded_at": "2026-04-01T08:00:00+09:00",
                    "client_updated_at": "2026-04-01T08:05:00+09:00",
                }
            ],
            "replacement_items": [
                {
                    "replacement_event_id": "replace-001",
                    "location_id": "loc-001",
                    "old_meter_id": "meter-001",
                    "new_meter_id": "meter-002",
                    "replaced_at": "2026-04-01T09:00:00+09:00",
                    "client_updated_at": "2026-04-01T09:10:00+09:00",
                    "old_final_reading": "11.0",
                    "new_initial_reading": "0.0",
                    "updated_by": "android-01",
                }
            ],
        }
    )

    assert sync_request.reading_items[0].result_status is ResultStatus.MEASURED
    assert sync_request.reading_items[0].structured_payload == {
        "percent": "97.5",
        "phase": "lag",
    }
    assert sync_request.replacement_items[0].old_final_reading == Decimal("11.0")


def test_sync_response_supports_partial_success_feedback() -> None:
    response = MeterReadingSyncResponse.model_validate(
        {
            "accepted_reading_items": 1,
            "accepted_replacement_items": 0,
            "ignored_reading_items": [
                {
                    "item_index": 1,
                    "planned_reading_id": "plan-002",
                    "code": "stale_client_update",
                }
            ],
            "rejected_reading_items": [
                {
                    "item_index": 2,
                    "planned_reading_id": "plan-003",
                    "code": "unknown_planned_reading",
                    "detail": "Unknown planned_reading_id: plan-003",
                }
            ],
        }
    )

    assert response.ignored_reading_items[0].code is SyncItemIgnoreCode.STALE_CLIENT_UPDATE
    assert response.rejected_reading_items[0].code is SyncItemRejectCode.UNKNOWN_PLANNED_READING
    assert response.ignored_replacement_items == []
    assert response.rejected_replacement_items == []

    ignored_item = IgnoredReadingSyncItem.model_validate(
        {
            "item_index": 0,
            "planned_reading_id": "plan-001",
            "code": "stale_client_update",
        }
    )
    assert ignored_item.code is SyncItemIgnoreCode.STALE_CLIENT_UPDATE

    rejected_item = RejectedReadingSyncItem.model_validate(
        {
            "item_index": 0,
            "planned_reading_id": "plan-002",
            "code": "invalid_reading_submission",
            "detail": "Invalid reading submission for planned_reading_id plan-002: bad payload",
        }
    )
    assert rejected_item.code is SyncItemRejectCode.INVALID_READING_SUBMISSION


def test_schema_snapshot_exposes_work_package_shape_and_enums() -> None:
    snapshot = build_api_contract_schema_snapshot()

    work_package_schema = snapshot["models"]["WorkPackageTaskGroups"]
    assert set(work_package_schema["properties"]) == {
        "daily_tasks",
        "weekly_tasks",
        "monthly_tasks",
        "every_two_months_tasks",
    }

    measurement_definition_schema = snapshot["models"]["MeasurementDefinitionData"]
    assert "decimal_places" in measurement_definition_schema["properties"]
    assert measurement_definition_schema["$defs"]["Category"]["enum"] == [
        "electricity",
        "gas",
        "water",
        "boiler",
        "others",
    ]

    sync_response_schema = snapshot["models"]["MeterReadingSyncResponse"]
    assert set(sync_response_schema["properties"]) == {
        "accepted_reading_items",
        "accepted_replacement_items",
        "ignored_reading_items",
        "ignored_replacement_items",
        "rejected_reading_items",
        "rejected_replacement_items",
        "detail",
    }
    assert measurement_definition_schema["$defs"]["ValueSchema"]["enum"] == [
        "decimal_string",
        "power_factor_percent_phase",
    ]

    reading_schema = snapshot["models"]["MeterReadingSubmission"]
    assert "structured_payload" in reading_schema["properties"]
    assert not {"device_id", "updated_by", "signature"} & reading_schema["properties"].keys()
    assert reading_schema["$defs"]["ResultStatus"]["enum"] == [
        "measured",
        "skipped",
        "unreachable",
        "meter_fault",
        "replaced",
    ]

    planned_task_schema = snapshot["models"]["PlannedReadingTask"]
    assert "release_date" in planned_task_schema["properties"]
    assert "release_date" in planned_task_schema["required"]

    rejected_reading_schema = snapshot["models"]["RejectedReadingSyncItem"]
    assert rejected_reading_schema["$defs"]["SyncItemRejectCode"]["enum"] == [
        "unknown_planned_reading",
        "unknown_measurement_definition",
        "invalid_reading_submission",
        "invalid_replacement_submission",
        "unexpected_error",
    ]

    power_factor_schema = snapshot["models"]["PowerFactorReading"]
    assert power_factor_schema["$defs"]["PowerFactorPhase"]["enum"] == [
        "lead",
        "lag",
        "unity",
    ]


def test_contract_relationship_notes_document_transport_validation() -> None:
    snapshot = build_api_contract_schema_snapshot()

    relationship_notes = snapshot["relationship_notes"]
    assert any(
        note.get("model") == "MeterReadingSubmission" and note.get("kind") == "transport_contract"
        for note in relationship_notes
    )
    assert any(
        note.get("helper_schema") == "PowerFactorReading"
        and note.get("value_schema") == "power_factor_percent_phase"
        for note in relationship_notes
    )
