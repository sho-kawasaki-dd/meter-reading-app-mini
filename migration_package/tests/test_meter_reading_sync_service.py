# pyright: reportMissingImports=false

"""`MeterReadingSyncService` の業務オーケストレーションを検証するテスト群。

ここでは DB そのものではなく、planned reading lookup → measurement definition 解決 →
resolved validation → upsert の順序が守られているかを重点的に確認する。
"""

from __future__ import annotations

import sys
from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal
from pathlib import Path

sys.path.append(str(Path(__file__).resolve().parents[1] / "src"))

from meter_reading_mini.core.contract_models import (
    MeterReadingSyncRequest,
    ResolvedMeterReadingSubmission,
    ResultStatus,
    ValueSchema,
    resolve_meter_reading_submission,
)  # noqa: E402
from meter_reading_mini.core.meter_reading_sync import MeterReadingSyncService
from meter_reading_mini.core.meter_reading_sync_repositories import (
    MeasurementDefinitionRecord,
    PlannedReadingRecord,
    ReadingUpsertRequest,
    ReplacementUpsertRequest,
    ResultSource,
    UpsertDisposition,
    UpsertOutcome,
)
from meter_reading_mini.core.reading_signature import (
    SignatureCanonicalizationError,
    calculate_meter_reading_signature,
)  # noqa: E402
from conftest import FakeSession  # noqa: E402

TEST_DEVICE_ID = "550e8400-e29b-41d4-a716-446655440000"


@dataclass
class FakeMeterReadingSyncRepository:
    """service の依存境界を再現する fake repository。

    `call_log` で呼び出し順序を記録し、resolved validation が lookup 後にのみ走ることや、
    upsert の戻り値に応じて service が response をどう組み立てるかを確認できるようにする。
    """

    planned_readings: dict[str, PlannedReadingRecord] = field(default_factory=dict)
    measurement_definitions: dict[str, MeasurementDefinitionRecord] = field(default_factory=dict)
    reading_outcomes: list[UpsertOutcome] = field(default_factory=list)
    replacement_outcomes: list[UpsertOutcome] = field(default_factory=list)
    call_log: list[str] = field(default_factory=list)
    upserted_readings: list[ReadingUpsertRequest] = field(default_factory=list)
    upserted_replacements: list[ReplacementUpsertRequest] = field(default_factory=list)
    reflected_replacements: list[ReplacementUpsertRequest] = field(default_factory=list)

    def get_planned_reading(self, planned_reading_id: str) -> PlannedReadingRecord | None:
        self.call_log.append(f"planned:{planned_reading_id}")
        return self.planned_readings.get(planned_reading_id)

    def get_measurement_definition(
        self,
        measurement_definition_id: str,
    ) -> MeasurementDefinitionRecord | None:
        self.call_log.append(f"measurement:{measurement_definition_id}")
        return self.measurement_definitions.get(measurement_definition_id)

    def upsert_reading(self, request: ReadingUpsertRequest) -> UpsertOutcome:
        self.call_log.append(f"upsert-reading:{request.planned_reading_id}")
        self.upserted_readings.append(request)
        if self.reading_outcomes:
            return self.reading_outcomes.pop(0)
        return UpsertOutcome(UpsertDisposition.INSERTED)

    def upsert_replacement(self, request: ReplacementUpsertRequest) -> UpsertOutcome:
        self.call_log.append(f"upsert-replacement:{request.replacement_event_id}")
        self.upserted_replacements.append(request)
        if self.replacement_outcomes:
            return self.replacement_outcomes.pop(0)
        return UpsertOutcome(UpsertDisposition.INSERTED)

    def reflect_replacement(self, request: ReplacementUpsertRequest) -> None:
        self.call_log.append(f"reflect-replacement:{request.replacement_event_id}")
        self.reflected_replacements.append(request)


def build_service(
    repository: FakeMeterReadingSyncRepository,
    *,
    resolve_submission=None,
    verify_signature=None,
) -> MeterReadingSyncService:
    """fake repository と fake session を 注入して service を組み立てる。"""

    if resolve_submission is None:

        def resolve_submission(
            submission,
            measurement_definition_id: str,
            value_schema: ValueSchema,
            decimal_places: int | None,
            rollover_value: Decimal | None,
        ):
            return resolve_meter_reading_submission(
                submission,
                measurement_definition_id=measurement_definition_id,
                value_schema=value_schema,
                decimal_places=decimal_places,
                rollover_value=rollover_value,
            )

    service_kwargs = {
        "session": FakeSession(),
        "planned_reading_repository": repository,
        "measurement_definition_repository": repository,
        "reading_repository": repository,
        "replacement_repository": repository,
        "replacement_reflection_repository": repository,
        "resolve_submission": resolve_submission,
    }
    if verify_signature is not None:
        service_kwargs["verify_signature"] = verify_signature
    return MeterReadingSyncService(**service_kwargs)


def build_reading_item(
    *,
    planned_reading_id: str,
    result_status: str = "measured",
    raw_reading: str | None = None,
    structured_payload: dict[str, object] | None = None,
    recorded_at: str = "2026-04-01T08:00:00+09:00",
    client_updated_at: str = "2026-04-01T08:05:00+09:00",
    device_id: str = TEST_DEVICE_ID,
    updated_by: str = "android-01",
    signature: str | None = None,
) -> dict[str, object]:
    item: dict[str, object] = {
        "planned_reading_id": planned_reading_id,
        "result_status": result_status,
        "recorded_at": recorded_at,
        "client_updated_at": client_updated_at,
        "device_id": device_id,
        "updated_by": updated_by,
    }
    if raw_reading is not None:
        item["raw_reading"] = raw_reading
    if structured_payload is not None:
        item["structured_payload"] = structured_payload
    if signature is None:
        signature = calculate_meter_reading_signature(
            planned_reading_id=planned_reading_id,
            result_status=ResultStatus(result_status),
            raw_reading=Decimal(raw_reading) if raw_reading is not None else None,
            structured_payload_json=structured_payload,
            recorded_at=datetime.fromisoformat(recorded_at),
            device_id=device_id,
        )
    item["signature"] = signature
    return item


def build_decimal_sync_request() -> MeterReadingSyncRequest:
    """decimal_string 系の最小 upload request を返す。"""

    return MeterReadingSyncRequest.model_validate(
        {
            "reading_items": [
                build_reading_item(planned_reading_id="plan-decimal-001", raw_reading="10.5")
            ],
            "replacement_items": [],
        }
    )


def build_power_factor_sync_request() -> MeterReadingSyncRequest:
    """power_factor_percent_phase 系の最小 upload request を返す。"""

    return MeterReadingSyncRequest.model_validate(
        {
            "reading_items": [
                build_reading_item(
                    planned_reading_id="plan-pf-001",
                    structured_payload={"percent": "97.5", "phase": "lag"},
                )
            ],
            "replacement_items": [],
        }
    )


def test_sync_service_rejects_unknown_planned_reading_before_resolved_validation() -> None:
    """未知の planned_reading_id は strict validation 前に reject されることを確認する。"""

    repository = FakeMeterReadingSyncRepository()
    resolver_calls: list[str] = []

    def resolver(
        submission,
        measurement_definition_id: str,
        value_schema: ValueSchema,
        decimal_places: int | None,
        rollover_value: Decimal | None,
    ) -> ResolvedMeterReadingSubmission:
        resolver_calls.append(submission.planned_reading_id)
        return resolve_meter_reading_submission(
            submission,
            measurement_definition_id=measurement_definition_id,
            value_schema=value_schema,
            decimal_places=decimal_places,
            rollover_value=rollover_value,
        )

    service = build_service(repository, resolve_submission=resolver)

    response = service.sync(build_decimal_sync_request())

    assert repository.call_log == ["planned:plan-decimal-001"]
    assert resolver_calls == []
    assert response.accepted_reading_items == 0
    assert response.rejected_reading_items[0].planned_reading_id == "plan-decimal-001"
    assert response.rejected_reading_items[0].code == "unknown_planned_reading"


def test_sync_service_resolves_metadata_before_running_strict_validation() -> None:
    """measurement definition 解決後にだけ strict validation が走ることを確認する。"""

    repository = FakeMeterReadingSyncRepository(
        planned_readings={
            "plan-pf-001": PlannedReadingRecord(
                planned_reading_id="plan-pf-001",
                measurement_definition_id="md-pf-001",
                meter_id="meter-001",
            )
        },
        measurement_definitions={
            "md-pf-001": MeasurementDefinitionRecord(
                measurement_definition_id="md-pf-001",
                value_schema=ValueSchema.POWER_FACTOR_PERCENT_PHASE,
            )
        },
    )
    call_order: list[str] = []

    def resolver(
        submission,
        measurement_definition_id: str,
        value_schema: ValueSchema,
        decimal_places: int | None,
        rollover_value: Decimal | None,
    ) -> ResolvedMeterReadingSubmission:
        call_order.append(
            f"resolve:{submission.planned_reading_id}:{measurement_definition_id}:{value_schema}"
        )
        return resolve_meter_reading_submission(
            submission,
            measurement_definition_id=measurement_definition_id,
            value_schema=value_schema,
            decimal_places=decimal_places,
            rollover_value=rollover_value,
        )

    service = build_service(repository, resolve_submission=resolver)

    response = service.sync(build_power_factor_sync_request())

    assert response.accepted_reading_items == 1
    assert repository.call_log[:2] == ["planned:plan-pf-001", "measurement:md-pf-001"]
    assert call_order == ["resolve:plan-pf-001:md-pf-001:power_factor_percent_phase"]
    assert repository.upserted_readings[0].structured_payload_json == {
        "percent": "97.5",
        "phase": "lag",
    }


def test_sync_service_applies_decimal_value_schema_after_resolution() -> None:
    """decimal_string の測定対象では raw_reading が保存要求へ入ることを確認する。"""

    repository = FakeMeterReadingSyncRepository(
        planned_readings={
            "plan-decimal-001": PlannedReadingRecord(
                planned_reading_id="plan-decimal-001",
                measurement_definition_id="md-decimal-001",
                meter_id="meter-001",
            )
        },
        measurement_definitions={
            "md-decimal-001": MeasurementDefinitionRecord(
                measurement_definition_id="md-decimal-001",
                value_schema=ValueSchema.DECIMAL_STRING,
            )
        },
    )
    service = build_service(repository)

    response = service.sync(build_decimal_sync_request())

    assert response.accepted_reading_items == 1
    assert repository.upserted_readings[0].measurement_definition_id == "md-decimal-001"
    assert repository.upserted_readings[0].raw_reading == Decimal("10.5")
    assert repository.upserted_readings[0].structured_payload_json is None
    assert repository.upserted_readings[0].result_source is ResultSource.CLIENT


def test_sync_service_surfaces_strict_validation_failure_after_lookup() -> None:
    """lookup 成功後の payload エラーが reject として返ることを確認する。"""

    repository = FakeMeterReadingSyncRepository(
        planned_readings={
            "plan-pf-001": PlannedReadingRecord(
                planned_reading_id="plan-pf-001",
                measurement_definition_id="md-pf-001",
                meter_id="meter-001",
            )
        },
        measurement_definitions={
            "md-pf-001": MeasurementDefinitionRecord(
                measurement_definition_id="md-pf-001",
                value_schema=ValueSchema.POWER_FACTOR_PERCENT_PHASE,
            )
        },
    )
    service = build_service(repository)
    request = MeterReadingSyncRequest.model_validate(
        {
            "reading_items": [
                build_reading_item(
                    planned_reading_id="plan-pf-001",
                    structured_payload={"percent": "101", "phase": "lag"},
                )
            ],
            "replacement_items": [],
        }
    )

    response = service.sync(request)

    assert repository.call_log == ["planned:plan-pf-001", "measurement:md-pf-001"]
    assert response.accepted_reading_items == 0
    assert response.rejected_reading_items[0].planned_reading_id == "plan-pf-001"
    assert response.rejected_reading_items[0].code == "invalid_reading_submission"


def test_sync_service_normalizes_replacement_items_and_counts_only_accepted_upserts() -> None:
    """replacement event の正規化と ignored/accepted 集計を確認する。"""

    repository = FakeMeterReadingSyncRepository(
        planned_readings={
            "plan-decimal-001": PlannedReadingRecord(
                planned_reading_id="plan-decimal-001",
                measurement_definition_id="md-decimal-001",
                meter_id="meter-001",
            )
        },
        measurement_definitions={
            "md-decimal-001": MeasurementDefinitionRecord(
                measurement_definition_id="md-decimal-001",
                value_schema=ValueSchema.DECIMAL_STRING,
            )
        },
        reading_outcomes=[UpsertOutcome(UpsertDisposition.IGNORED_STALE)],
        replacement_outcomes=[UpsertOutcome(UpsertDisposition.REPLACED_ON_TIE)],
    )
    service = build_service(repository)
    request = MeterReadingSyncRequest.model_validate(
        {
            "reading_items": [
                build_reading_item(planned_reading_id="plan-decimal-001", raw_reading="10.5")
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

    response = service.sync(request)

    assert response.accepted_reading_items == 0
    assert response.accepted_replacement_items == 1
    assert response.detail == "Ignored 1 stale reading item(s)."
    assert response.ignored_reading_items[0].planned_reading_id == "plan-decimal-001"
    assert response.rejected_reading_items == []
    assert repository.upserted_replacements[0].replacement_event_id == "replace-001"
    assert repository.upserted_replacements[0].old_final_reading == Decimal("11.0")
    assert repository.upserted_replacements[0].new_initial_reading == Decimal("0.0")
    assert repository.reflected_replacements[0].replacement_event_id == "replace-001"


def test_sync_service_does_not_reflect_stale_replacement() -> None:
    """stale replacement は ignored にとどまり、副作用反映を起こさないことを確認する。"""

    repository = FakeMeterReadingSyncRepository(
        replacement_outcomes=[UpsertOutcome(UpsertDisposition.IGNORED_STALE)],
    )
    service = build_service(repository)
    request = MeterReadingSyncRequest.model_validate(
        {
            "reading_items": [],
            "replacement_items": [
                {
                    "replacement_event_id": "replace-stale-001",
                    "location_id": "loc-001",
                    "old_meter_id": "meter-001",
                    "new_meter_id": "meter-002",
                    "replaced_at": "2026-04-01T09:00:00+09:00",
                    "client_updated_at": "2026-04-01T08:00:00+09:00",
                    "updated_by": "android-01",
                }
            ],
        }
    )

    response = service.sync(request)

    assert response.accepted_replacement_items == 0
    assert response.ignored_replacement_items[0].replacement_event_id == "replace-stale-001"
    assert repository.reflected_replacements == []


def test_sync_service_processes_mixed_batch_without_aborting_on_rejected_items() -> None:
    """一部 item が reject されても残りを処理し続けることを確認する。"""

    repository = FakeMeterReadingSyncRepository(
        planned_readings={
            "plan-good-001": PlannedReadingRecord(
                planned_reading_id="plan-good-001",
                measurement_definition_id="md-decimal-001",
                meter_id="meter-001",
            ),
            "plan-bad-001": PlannedReadingRecord(
                planned_reading_id="plan-bad-001",
                measurement_definition_id="md-pf-001",
                meter_id="meter-001",
            ),
        },
        measurement_definitions={
            "md-decimal-001": MeasurementDefinitionRecord(
                measurement_definition_id="md-decimal-001",
                value_schema=ValueSchema.DECIMAL_STRING,
            ),
            "md-pf-001": MeasurementDefinitionRecord(
                measurement_definition_id="md-pf-001",
                value_schema=ValueSchema.POWER_FACTOR_PERCENT_PHASE,
            ),
        },
    )
    service = build_service(repository)
    request = MeterReadingSyncRequest.model_validate(
        {
            "reading_items": [
                build_reading_item(planned_reading_id="plan-good-001", raw_reading="10.5"),
                build_reading_item(
                    planned_reading_id="plan-bad-001",
                    structured_payload={"percent": "101", "phase": "lag"},
                    recorded_at="2026-04-01T08:10:00+09:00",
                    client_updated_at="2026-04-01T08:15:00+09:00",
                ),
            ],
            "replacement_items": [
                {
                    "replacement_event_id": "replace-001",
                    "location_id": "loc-001",
                    "old_meter_id": "meter-001",
                    "new_meter_id": "meter-002",
                    "replaced_at": "2026-04-01T09:00:00+09:00",
                    "client_updated_at": "2026-04-01T09:10:00+09:00",
                    "updated_by": "android-01",
                }
            ],
        }
    )

    response = service.sync(request)

    assert response.accepted_reading_items == 1
    assert response.accepted_replacement_items == 1
    assert response.rejected_reading_items[0].item_index == 1
    assert response.rejected_reading_items[0].planned_reading_id == "plan-bad-001"
    assert response.ignored_reading_items == []
    assert len(repository.upserted_readings) == 1
    assert repository.upserted_readings[0].planned_reading_id == "plan-good-001"
    assert len(repository.upserted_replacements) == 1


def test_sync_service_rejects_blank_device_id_before_upsert() -> None:
    repository = FakeMeterReadingSyncRepository(
        planned_readings={
            "plan-decimal-001": PlannedReadingRecord(
                planned_reading_id="plan-decimal-001",
                measurement_definition_id="md-decimal-001",
                meter_id="meter-001",
            )
        },
        measurement_definitions={
            "md-decimal-001": MeasurementDefinitionRecord(
                measurement_definition_id="md-decimal-001",
                value_schema=ValueSchema.DECIMAL_STRING,
            )
        },
    )
    service = build_service(repository)
    request = MeterReadingSyncRequest.model_validate(
        {
            "reading_items": [
                build_reading_item(
                    planned_reading_id="plan-decimal-001",
                    raw_reading="10.5",
                    device_id="",
                    signature="opaque-signature",
                )
            ],
            "replacement_items": [],
        }
    )

    response = service.sync(request)

    assert response.accepted_reading_items == 0
    assert response.rejected_reading_items[0].code == "missing_device_id"
    assert repository.upserted_readings == []


def test_sync_service_rejects_invalid_signature_after_lookup() -> None:
    repository = FakeMeterReadingSyncRepository(
        planned_readings={
            "plan-decimal-001": PlannedReadingRecord(
                planned_reading_id="plan-decimal-001",
                measurement_definition_id="md-decimal-001",
                meter_id="meter-001",
            )
        },
        measurement_definitions={
            "md-decimal-001": MeasurementDefinitionRecord(
                measurement_definition_id="md-decimal-001",
                value_schema=ValueSchema.DECIMAL_STRING,
            )
        },
    )
    service = build_service(repository)
    request = MeterReadingSyncRequest.model_validate(
        {
            "reading_items": [
                build_reading_item(
                    planned_reading_id="plan-decimal-001",
                    raw_reading="10.5",
                    signature="bad-signature",
                )
            ],
            "replacement_items": [],
        }
    )

    response = service.sync(request)

    assert response.accepted_reading_items == 0
    assert response.rejected_reading_items[0].code == "invalid_signature"
    assert repository.upserted_readings == []


def test_sync_service_maps_signature_canonicalization_failure_to_rejected_item() -> None:
    repository = FakeMeterReadingSyncRepository(
        planned_readings={
            "plan-pf-001": PlannedReadingRecord(
                planned_reading_id="plan-pf-001",
                measurement_definition_id="md-pf-001",
                meter_id="meter-001",
            )
        },
        measurement_definitions={
            "md-pf-001": MeasurementDefinitionRecord(
                measurement_definition_id="md-pf-001",
                value_schema=ValueSchema.POWER_FACTOR_PERCENT_PHASE,
            )
        },
    )

    def verifier(_: ResolvedMeterReadingSubmission) -> None:
        raise SignatureCanonicalizationError("boom")

    service = build_service(repository, verify_signature=verifier)
    request = MeterReadingSyncRequest.model_validate(
        {
            "reading_items": [
                build_reading_item(
                    planned_reading_id="plan-pf-001",
                    structured_payload={"percent": "97.5", "phase": "lag"},
                )
            ],
            "replacement_items": [],
        }
    )

    response = service.sync(request)

    assert response.accepted_reading_items == 0
    assert response.rejected_reading_items[0].code == "signature_canonicalization_failed"
    assert repository.upserted_readings == []


def test_sync_service_rejects_decimal_place_overflow_after_resolution() -> None:
    repository = FakeMeterReadingSyncRepository(
        planned_readings={
            "plan-decimal-001": PlannedReadingRecord(
                planned_reading_id="plan-decimal-001",
                measurement_definition_id="md-decimal-001",
                meter_id="meter-001",
            )
        },
        measurement_definitions={
            "md-decimal-001": MeasurementDefinitionRecord(
                measurement_definition_id="md-decimal-001",
                value_schema=ValueSchema.DECIMAL_STRING,
                decimal_places=1,
                rollover_value=Decimal("100000"),
            )
        },
    )
    service = build_service(repository)
    request = MeterReadingSyncRequest.model_validate(
        {
            "reading_items": [
                build_reading_item(planned_reading_id="plan-decimal-001", raw_reading="10.50")
            ],
            "replacement_items": [],
        }
    )

    response = service.sync(request)

    assert response.accepted_reading_items == 0
    assert response.rejected_reading_items[0].code == "invalid_reading_submission"
    assert repository.upserted_readings == []


def test_sync_service_rejects_rollover_boundary_after_resolution() -> None:
    repository = FakeMeterReadingSyncRepository(
        planned_readings={
            "plan-decimal-001": PlannedReadingRecord(
                planned_reading_id="plan-decimal-001",
                measurement_definition_id="md-decimal-001",
                meter_id="meter-001",
            )
        },
        measurement_definitions={
            "md-decimal-001": MeasurementDefinitionRecord(
                measurement_definition_id="md-decimal-001",
                value_schema=ValueSchema.DECIMAL_STRING,
                decimal_places=None,
                rollover_value=Decimal("100000"),
            )
        },
    )
    service = build_service(repository)
    request = MeterReadingSyncRequest.model_validate(
        {
            "reading_items": [
                build_reading_item(planned_reading_id="plan-decimal-001", raw_reading="100000")
            ],
            "replacement_items": [],
        }
    )

    response = service.sync(request)

    assert response.accepted_reading_items == 0
    assert response.rejected_reading_items[0].code == "invalid_reading_submission"
    assert repository.upserted_readings == []
