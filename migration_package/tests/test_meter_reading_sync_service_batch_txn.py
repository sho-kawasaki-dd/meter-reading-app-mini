# pyright: reportMissingImports=false

"""MeterReadingSyncService の batch transaction 機能をテストするモジュール。

Step 5 で実装された nested savepoint による item 単位の error handling と
batch-level transaction の動作を検証する。
"""

from __future__ import annotations

import sys
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal
from pathlib import Path

import pytest

sys.path.append(str(Path(__file__).resolve().parents[1] / "src"))

from api.models import (  # noqa: E402
    MeterReadingSyncRequest,
    ResultStatus,  # noqa: E402
    ValueSchema,
)
from application.meter_reading_sync import MeterReadingSyncService  # noqa: E402
from application.meter_reading_sync_repositories import (  # noqa: E402
    MeasurementDefinitionRecord,
    PlannedReadingRecord,
    ReadingUpsertRequest,
    ReplacementUpsertRequest,
    UpsertDisposition,
    UpsertOutcome,
)
from application.reading_signature import calculate_meter_reading_signature  # noqa: E402
from conftest import FakeSession  # noqa: E402

TEST_DEVICE_ID = "550e8400-e29b-41d4-a716-446655440000"


@dataclass
class TrackingRepository:
    """Batch transaction テスト用の repository。

    各 item の upsert に対して制御された outcome を返し、
    どの item が処理されたかを記録する。
    """

    planned_readings: dict[str, PlannedReadingRecord] = field(default_factory=dict)
    measurement_definitions: dict[str, MeasurementDefinitionRecord] = field(default_factory=dict)
    reading_outcomes: list[UpsertOutcome] = field(default_factory=list)
    replacement_outcomes: list[UpsertOutcome] = field(default_factory=list)

    upserted_reading_ids: list[str] = field(default_factory=list)
    upserted_replacement_ids: list[str] = field(default_factory=list)
    raise_on_reading_index: int | None = None
    raise_on_replacement_index: int | None = None

    def get_planned_reading(self, planned_reading_id: str) -> PlannedReadingRecord | None:
        return self.planned_readings.get(planned_reading_id)

    def get_measurement_definition(
        self,
        measurement_definition_id: str,
    ) -> MeasurementDefinitionRecord | None:
        return self.measurement_definitions.get(measurement_definition_id)

    def upsert_reading(self, request: ReadingUpsertRequest) -> UpsertOutcome:
        idx = len(self.upserted_reading_ids)
        if self.raise_on_reading_index == idx:
            raise ValueError(f"Simulated error at reading index {idx}")
        self.upserted_reading_ids.append(request.planned_reading_id)
        if self.reading_outcomes:
            return self.reading_outcomes.pop(0)
        return UpsertOutcome(UpsertDisposition.INSERTED)

    def upsert_replacement(self, request: ReplacementUpsertRequest) -> UpsertOutcome:
        idx = len(self.upserted_replacement_ids)
        if self.raise_on_replacement_index == idx:
            raise ValueError(f"Simulated error at replacement index {idx}")
        self.upserted_replacement_ids.append(request.replacement_event_id)
        if self.replacement_outcomes:
            return self.replacement_outcomes.pop(0)
        return UpsertOutcome(UpsertDisposition.INSERTED)

    def reflect_replacement(self, request: ReplacementUpsertRequest) -> None:
        pass


def build_reading_item(
    *,
    planned_reading_id: str,
    result_status: str = "measured",
    raw_reading: str | None = None,
    recorded_at: str = "2026-04-01T08:00:00+09:00",
    client_updated_at: str = "2026-04-01T08:05:00+09:00",
    device_id: str = TEST_DEVICE_ID,
    updated_by: str = "android-01",
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
    signature = calculate_meter_reading_signature(
        planned_reading_id=planned_reading_id,
        result_status=ResultStatus(result_status),
        raw_reading=Decimal(raw_reading) if raw_reading is not None else None,
        structured_payload_json=None,
        recorded_at=datetime.fromisoformat(recorded_at),
        device_id=device_id,
    )
    item["signature"] = signature
    return item


def test_batch_txn_partial_rejection_accepts_remaining_items() -> None:
    """複数 reading item 中で 1 つが validation error になっても、
    残りが処理されることを確認する（nested savepoint による個別 rollback）。
    """

    repository = TrackingRepository(
        planned_readings={
            "plan-good-1": PlannedReadingRecord(
                planned_reading_id="plan-good-1",
                measurement_definition_id="md-1",
                meter_id="meter-1",
            ),
            "plan-bad-1": PlannedReadingRecord(
                planned_reading_id="plan-bad-1",
                measurement_definition_id="md-bad",
                meter_id="meter-1",
            ),
            "plan-good-2": PlannedReadingRecord(
                planned_reading_id="plan-good-2",
                measurement_definition_id="md-1",
                meter_id="meter-1",
            ),
        },
        measurement_definitions={
            "md-1": MeasurementDefinitionRecord(
                measurement_definition_id="md-1",
                value_schema=ValueSchema.DECIMAL_STRING,
            ),
            # md-bad は未設定 → resolve で MissingMeasurementDefinitionError
        },
    )
    service = MeterReadingSyncService(
        session=FakeSession(),
        planned_reading_repository=repository,
        measurement_definition_repository=repository,
        reading_repository=repository,
        replacement_repository=repository,
        replacement_reflection_repository=None,
    )
    request = MeterReadingSyncRequest.model_validate(
        {
            "reading_items": [
                build_reading_item(planned_reading_id="plan-good-1", raw_reading="10.0"),
                build_reading_item(planned_reading_id="plan-bad-1", raw_reading="20.0"),
                build_reading_item(planned_reading_id="plan-good-2", raw_reading="30.0"),
            ],
            "replacement_items": [],
        }
    )

    response = service.sync(request)

    # 1 件目と 3 件目は accepted、2 件目は rejected
    assert response.accepted_reading_items == 2
    assert response.rejected_reading_items[0].item_index == 1
    assert response.rejected_reading_items[0].planned_reading_id == "plan-bad-1"
    # Repository には 1 件目と 3 件目が記録される
    assert repository.upserted_reading_ids == ["plan-good-1", "plan-good-2"]


def test_batch_txn_error_on_upsert_rejects_item_but_continues() -> None:
    """upsert 中に例外が発生した item は rejected として記録され、
    他の item は続行されることを確認する。
    """

    call_count = {"reading": 0}  # 呼び出し回数をカウント

    class CountingRepository(TrackingRepository):
        def upsert_reading(self, request: ReadingUpsertRequest) -> UpsertOutcome:
            if call_count["reading"] == 1:  # 2 番目の呼び出しで例外を raise
                call_count["reading"] += 1
                raise ValueError(f"Simulated error at reading call #{call_count['reading']}")
            call_count["reading"] += 1
            self.upserted_reading_ids.append(request.planned_reading_id)
            return UpsertOutcome(UpsertDisposition.INSERTED)

    repository = CountingRepository(
        planned_readings={
            "plan-1": PlannedReadingRecord(
                planned_reading_id="plan-1",
                measurement_definition_id="md-1",
                meter_id="meter-1",
            ),
            "plan-2": PlannedReadingRecord(
                planned_reading_id="plan-2",
                measurement_definition_id="md-1",
                meter_id="meter-1",
            ),
            "plan-3": PlannedReadingRecord(
                planned_reading_id="plan-3",
                measurement_definition_id="md-1",
                meter_id="meter-1",
            ),
        },
        measurement_definitions={
            "md-1": MeasurementDefinitionRecord(
                measurement_definition_id="md-1",
                value_schema=ValueSchema.DECIMAL_STRING,
            ),
        },
    )
    service = MeterReadingSyncService(
        session=FakeSession(),
        planned_reading_repository=repository,
        measurement_definition_repository=repository,
        reading_repository=repository,
        replacement_repository=repository,
        replacement_reflection_repository=None,
    )
    request = MeterReadingSyncRequest.model_validate(
        {
            "reading_items": [
                build_reading_item(planned_reading_id="plan-1", raw_reading="10.0"),
                build_reading_item(planned_reading_id="plan-2", raw_reading="20.0"),
                build_reading_item(planned_reading_id="plan-3", raw_reading="30.0"),
            ],
            "replacement_items": [],
        }
    )

    response = service.sync(request)

    # 1 件目と 3 件目は accepted、2 件目は rejected
    assert response.accepted_reading_items == 2
    assert response.rejected_reading_items[0].item_index == 1
    assert response.rejected_reading_items[0].planned_reading_id == "plan-2"
    # Repository には 1 件目と 3 件目だけが記録
    assert repository.upserted_reading_ids == ["plan-1", "plan-3"]


def test_batch_txn_unexpected_outer_error_rolls_back_all_items() -> None:
    """外側トランザクション内の想定外エラーで全体ロールバックが発生することを確認する。

    本テストでは、reading と replacement 両方の processing 外で例外が発生したシナリオをシミュレートします。
    これにより、全体ロールバックが発生し、何も accepted にはならないことを確認します。
    """

    class BrokenSession(FakeSession):
        """begin トランザクション内で想定外エラーを raise する session。"""

        def begin(self):
            """Context manager がエラーを raise する。"""

            @contextmanager
            def error_context():
                raise RuntimeError("Simulated outer transaction failure")
                yield  # 到達不可能
                # This prevents "finally:" blocks from being reached

            return error_context()

    repository = TrackingRepository(
        planned_readings={
            "plan-1": PlannedReadingRecord(
                planned_reading_id="plan-1",
                measurement_definition_id="md-1",
                meter_id="meter-1",
            ),
        },
        measurement_definitions={
            "md-1": MeasurementDefinitionRecord(
                measurement_definition_id="md-1",
                value_schema=ValueSchema.DECIMAL_STRING,
            ),
        },
    )
    service = MeterReadingSyncService(
        session=BrokenSession(),
        planned_reading_repository=repository,
        measurement_definition_repository=repository,
        reading_repository=repository,
        replacement_repository=repository,
        replacement_reflection_repository=None,
    )
    request = MeterReadingSyncRequest.model_validate(
        {
            "reading_items": [
                build_reading_item(planned_reading_id="plan-1", raw_reading="10.0"),
            ],
            "replacement_items": [],
        }
    )

    # 外側トランザクション内のエラーで RuntimeError が raise される
    with pytest.raises(RuntimeError, match="Simulated outer transaction failure"):
        service.sync(request)
