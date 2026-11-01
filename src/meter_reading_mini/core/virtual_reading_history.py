"""保存処理と同じ順序で複数の検針結果を仮想的に評価する。"""

from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import datetime
from enum import StrEnum

from meter_reading_mini.core.assignment_calculation import resolve_planned_calculation_context
from meter_reading_mini.core.contract_models import ResultStatus
from meter_reading_mini.core.errors import DomainError, ValidationError
from meter_reading_mini.core.meter_reading_sync_repositories import (
    CalculatedUsageUpdate,
    HistoricalMeasuredReadingRecord,
    MeasurementDefinitionRecord,
    MobileReadingImportRepository,
    MobileReadingImportResultRecord,
    PlannedReadingRecord,
    ReadingUpsertRequest,
    ReplacementBoundaryCalculationRecord,
    ReplacementBoundaryCalculationUpdate,
    ReplacementBoundaryRecord,
    ResultSource,
    UpsertDisposition,
    UpsertOutcome,
)
from meter_reading_mini.core.previous_reading import CalculationContext
from meter_reading_mini.core.reading_comparison import (
    ComparableReadingContent,
    ComparableReadingHeader,
    normalize_stored_structured_payload,
    reading_content_matches,
    serialize_structured_payload,
)
from meter_reading_mini.core.reading_write_orchestration import (
    CalculatedReading,
    ReadingWriteOrchestrationService,
)
from meter_reading_mini.core.timeutil import parse_utc, utc_now_str
from meter_reading_mini.core.usage_delta_calculation import UsageDeltaCalculationService


class VirtualRecalculationStage(StrEnum):
    READING = "reading"
    BOUNDARY = "boundary"


class VirtualRecalculationError(DomainError):
    """後続再計算の位置と、保存処理が返す元の業務エラーを保持する。"""

    def __init__(
        self,
        stage: VirtualRecalculationStage,
        original_error: DomainError,
        *,
        meter_id: str,
        replacement_event_id: str | None = None,
    ) -> None:
        super().__init__(str(original_error))
        self.stage = stage
        self.original_error = original_error
        self.meter_id = meter_id
        self.replacement_event_id = replacement_event_id


@dataclass(frozen=True, slots=True)
class VirtualReadingHistorySnapshot:
    """採用・間接再計算された結果と交換境界の最終状態。"""

    results: tuple[tuple[str, MobileReadingImportResultRecord], ...]
    accepted_reading_ids: frozenset[str]
    recalculated_reading_ids: frozenset[str]
    boundary_updates: tuple[ReplacementBoundaryCalculationUpdate, ...]


class VirtualReadingHistory:
    """入力ごとに仮想状態を確定し、失敗した入力の変更だけを破棄する。"""

    def __init__(
        self,
        repository: MobileReadingImportRepository,
        *,
        calculation_service: UsageDeltaCalculationService | None = None,
    ) -> None:
        self._repository = repository
        self._calculation_service = calculation_service or UsageDeltaCalculationService()
        self._state = _VirtualState(repository)

    def calculate_reading(self, request: ReadingUpsertRequest) -> CalculatedReading:
        """現在の仮想履歴を使い、競合判定・後続再計算前の対象値を求める。"""

        return self._writer(self._state).calculate_reading(request)

    def apply(self, request: ReadingUpsertRequest) -> UpsertOutcome:
        """検証済み要求を評価し、採用時のみ後続値を含む仮想変更を反映する。"""

        trial = self._state.fork()
        outcome = self._writer(trial).upsert_reading(request)
        if outcome.accepted:
            self._state = trial
        return outcome

    def get_result(self, planned_reading_id: str) -> MobileReadingImportResultRecord | None:
        """既存結果または採用した入力の、全行評価後の値を返す。"""

        return self._state.get_result(planned_reading_id)

    def get_calculation_context(self, planned_reading_id: str) -> CalculationContext:
        """全行評価後の仮想履歴に基づく前回値と交換境界を返す。"""

        planned = self._repository.get_planned_reading(planned_reading_id)
        if planned is None:
            raise ValidationError(f"予定枠が見つかりません: {planned_reading_id}")
        definition = self._repository.get_measurement_definition(planned.measurement_definition_id)
        if definition is None:
            raise ValidationError(f"測定定義が見つかりません: {planned.measurement_definition_id}")
        _, context = resolve_planned_calculation_context(
            self._repository,
            self._state,
            self._state,
            planned,
            definition,
        )
        return context

    def snapshot(self) -> VirtualReadingHistorySnapshot:
        return self._state.snapshot()

    def _writer(self, state: _VirtualState) -> _VirtualReadingWriter:
        return _VirtualReadingWriter(
            self._repository,
            state,
            state,
            planned_reading_repository=self._repository,
            assignment_repository=self._repository,
            boundary_repository=state,
            boundary_maintenance_repository=state,
            calculation_service=self._calculation_service,
        )


class _VirtualReadingWriter(ReadingWriteOrchestrationService):
    def _refresh_old_meter_boundaries(
        self,
        measurement_definition: MeasurementDefinitionRecord,
        planned_reading: PlannedReadingRecord,
    ) -> None:
        try:
            super()._refresh_old_meter_boundaries(measurement_definition, planned_reading)
        except VirtualRecalculationError:
            raise
        except DomainError as exc:
            raise VirtualRecalculationError(
                VirtualRecalculationStage.BOUNDARY, exc, meter_id=planned_reading.meter_id
            ) from exc

    def _refresh_boundary(
        self,
        measurement_definition: MeasurementDefinitionRecord,
        boundary: ReplacementBoundaryCalculationRecord,
    ) -> None:
        try:
            super()._refresh_boundary(measurement_definition, boundary)
        except DomainError as exc:
            raise VirtualRecalculationError(
                VirtualRecalculationStage.BOUNDARY,
                exc,
                meter_id=boundary.old_meter_id,
                replacement_event_id=boundary.replacement_event_id,
            ) from exc

    def _recalculate_downstream_from_boundary(
        self,
        measurement_definition: MeasurementDefinitionRecord,
        boundary: ReplacementBoundaryCalculationRecord,
    ) -> None:
        try:
            super()._recalculate_downstream_from_boundary(measurement_definition, boundary)
        except VirtualRecalculationError:
            raise
        except DomainError as exc:
            raise VirtualRecalculationError(
                VirtualRecalculationStage.BOUNDARY,
                exc,
                meter_id=boundary.new_meter_id,
                replacement_event_id=boundary.replacement_event_id,
            ) from exc

    def _recalculate_meter_readings(
        self,
        measurement_definition: MeasurementDefinitionRecord,
        meter_id: str,
        *,
        from_scheduled_at: datetime,
        before_scheduled_at: datetime | None,
    ) -> None:
        try:
            super()._recalculate_meter_readings(
                measurement_definition,
                meter_id,
                from_scheduled_at=from_scheduled_at,
                before_scheduled_at=before_scheduled_at,
            )
        except DomainError as exc:
            raise VirtualRecalculationError(
                VirtualRecalculationStage.READING, exc, meter_id=meter_id
            ) from exc

    def _recalculate_following_measured_readings(
        self,
        measurement_definition: MeasurementDefinitionRecord,
        planned_reading: PlannedReadingRecord,
        *,
        after_scheduled_at: datetime,
    ) -> None:
        try:
            super()._recalculate_following_measured_readings(
                measurement_definition, planned_reading, after_scheduled_at=after_scheduled_at
            )
        except DomainError as exc:
            raise VirtualRecalculationError(
                VirtualRecalculationStage.READING, exc, meter_id=planned_reading.meter_id
            ) from exc


@dataclass(frozen=True, slots=True)
class _VirtualEntry:
    planned: PlannedReadingRecord
    result: MobileReadingImportResultRecord


class _VirtualState:
    def __init__(self, repository: MobileReadingImportRepository) -> None:
        self._repository = repository
        self._entries: dict[str, _VirtualEntry] = {}
        self._usage_updates: dict[str, CalculatedUsageUpdate] = {}
        self._recalculated_reading_ids: set[str] = set()
        self._boundary_updates: dict[tuple[str, str], ReplacementBoundaryCalculationUpdate] = {}

    def fork(self) -> _VirtualState:
        trial = _VirtualState(self._repository)
        trial._entries = self._entries.copy()
        trial._usage_updates = self._usage_updates.copy()
        trial._recalculated_reading_ids = self._recalculated_reading_ids.copy()
        trial._boundary_updates = self._boundary_updates.copy()
        return trial

    def get_result(self, planned_reading_id: str) -> MobileReadingImportResultRecord | None:
        entry = self._entries.get(planned_reading_id)
        if entry is not None:
            result = entry.result
        else:
            target = self._repository.get_mobile_reading_import_targets([planned_reading_id]).get(
                planned_reading_id
            )
            result = target.existing_result if target is not None else None
        update = self._usage_updates.get(planned_reading_id)
        if result is not None and update is not None:
            return replace(result, last_reading=update.last_reading, usage_delta=update.usage_delta)
        return result

    def snapshot(self) -> VirtualReadingHistorySnapshot:
        results: list[tuple[str, MobileReadingImportResultRecord]] = []
        for planned_id in sorted(self._entries.keys() | self._usage_updates.keys()):
            result = self.get_result(planned_id)
            if result is None:
                raise ValidationError(f"仮想結果が見つかりません: {planned_id}")
            results.append((planned_id, result))
        return VirtualReadingHistorySnapshot(
            results=tuple(results),
            accepted_reading_ids=frozenset(self._entries),
            recalculated_reading_ids=frozenset(self._recalculated_reading_ids),
            boundary_updates=tuple(
                self._boundary_updates[key] for key in sorted(self._boundary_updates)
            ),
        )

    def upsert_reading(self, request: ReadingUpsertRequest) -> UpsertOutcome:
        planned = self._repository.get_planned_reading(request.planned_reading_id)
        if planned is None:
            raise ValidationError(f"予定枠が見つかりません: {request.planned_reading_id}")
        recorded_at = utc_now_str(request.recorded_at)
        client_updated_at = utc_now_str(request.client_updated_at)
        payload = serialize_structured_payload(request.structured_payload_json)
        existing = self.get_result(request.planned_reading_id)
        if existing is None:
            disposition = UpsertDisposition.INSERTED
        else:
            existing_updated_at = utc_now_str(existing.client_updated_at)
            is_mobile = request.result_source is ResultSource.MOBILE_HTML
            if is_mobile:
                existing_header = ComparableReadingHeader(
                    result_status=existing.result_status.value,
                    recorded_at=utc_now_str(existing.recorded_at),
                    client_updated_at=existing_updated_at,
                )
                incoming_header = ComparableReadingHeader(
                    result_status=request.result_status.value,
                    recorded_at=recorded_at,
                    client_updated_at=client_updated_at,
                )
                stored_payload = normalize_stored_structured_payload(
                    existing.structured_payload_json
                )
                if reading_content_matches(
                    ComparableReadingContent(
                        existing_header, existing.raw_reading, stored_payload, existing.note
                    ),
                    ComparableReadingContent(
                        incoming_header, request.raw_reading, payload, request.note
                    ),
                ):
                    return UpsertOutcome(
                        UpsertDisposition.IGNORED_DUPLICATE,
                        detail="同一内容の検針結果を再受信しました。",
                    )
                if client_updated_at < existing_updated_at:
                    return UpsertOutcome(
                        UpsertDisposition.IGNORED_STALE,
                        detail="古い client_updated_at の入力を無視しました。",
                    )
            disposition = (
                UpsertDisposition.REPLACED_ON_TIE
                if is_mobile and client_updated_at == existing_updated_at
                else UpsertDisposition.UPDATED
            )

        result = MobileReadingImportResultRecord(
            result_status=request.result_status,
            raw_reading=request.raw_reading,
            structured_payload_json=payload,
            note=request.note,
            recorded_at=parse_utc(recorded_at),
            client_updated_at=parse_utc(client_updated_at),
            result_source=request.result_source,
            device_id=request.device_id,
            updated_by=request.updated_by,
            signature=request.signature,
            last_reading=request.last_reading,
            usage_delta=request.usage_delta,
            arrival_sequence=1 if existing is None else existing.arrival_sequence + 1,
        )
        self._entries[request.planned_reading_id] = _VirtualEntry(planned, result)
        self._usage_updates[request.planned_reading_id] = CalculatedUsageUpdate(
            request.planned_reading_id, request.last_reading, request.usage_delta
        )
        return UpsertOutcome(disposition)

    def save_calculated_usage(self, update: CalculatedUsageUpdate) -> None:
        if self.get_result(update.planned_reading_id) is None:
            raise ValidationError(
                f"再計算対象の検針結果が見つかりません: {update.planned_reading_id}"
            )
        self._usage_updates[update.planned_reading_id] = update
        self._recalculated_reading_ids.add(update.planned_reading_id)

    def get_reading_client_updated_at(self, planned_reading_id: str) -> datetime | None:
        result = self.get_result(planned_reading_id)
        return result.client_updated_at if result is not None else None

    def get_latest_measured_reading(
        self,
        measurement_definition_id: str,
        meter_id: str,
        *,
        before_scheduled_at: datetime,
        before_planned_reading_id: str | None = None,
        excluding_planned_reading_id: str | None = None,
    ) -> HistoricalMeasuredReadingRecord | None:
        before = parse_utc(utc_now_str(before_scheduled_at))
        latest = self._repository.get_latest_measured_reading(
            measurement_definition_id,
            meter_id,
            before_scheduled_at=before,
            before_planned_reading_id=before_planned_reading_id,
            excluding_planned_reading_id=excluding_planned_reading_id,
        )
        while latest is not None and latest.planned_reading_id in self._entries:
            scheduled_at, planned_id = _reading_order(latest)
            latest = self._repository.get_latest_measured_reading(
                measurement_definition_id,
                meter_id,
                before_scheduled_at=scheduled_at,
                before_planned_reading_id=planned_id,
                excluding_planned_reading_id=excluding_planned_reading_id,
            )
        for candidate in self._virtual_measured_readings(measurement_definition_id, meter_id):
            scheduled_at, planned_id = _reading_order(candidate)
            if planned_id == excluding_planned_reading_id:
                continue
            if scheduled_at > before or (
                scheduled_at == before
                and (before_planned_reading_id is None or planned_id >= before_planned_reading_id)
            ):
                continue
            if latest is None or _reading_order(candidate) > _reading_order(latest):
                latest = candidate
        return latest

    def list_following_measured_readings(
        self,
        measurement_definition_id: str,
        meter_id: str,
        *,
        after_scheduled_at: datetime,
        after_planned_reading_id: str | None = None,
        excluding_planned_reading_id: str | None = None,
    ) -> list[HistoricalMeasuredReadingRecord]:
        after = parse_utc(utc_now_str(after_scheduled_at))
        readings = {
            reading.planned_reading_id: reading
            for reading in self._repository.list_following_measured_readings(
                measurement_definition_id,
                meter_id,
                after_scheduled_at=after,
                after_planned_reading_id=after_planned_reading_id,
                excluding_planned_reading_id=excluding_planned_reading_id,
            )
            if reading.planned_reading_id not in self._entries
        }
        for candidate in self._virtual_measured_readings(measurement_definition_id, meter_id):
            scheduled_at, planned_id = _reading_order(candidate)
            if planned_id == excluding_planned_reading_id:
                continue
            if scheduled_at > after or (
                scheduled_at == after
                and after_planned_reading_id is not None
                and planned_id > after_planned_reading_id
            ):
                readings[planned_id] = candidate
        return sorted(readings.values(), key=_reading_order)

    def list_measured_readings_on_or_after(
        self,
        measurement_definition_id: str,
        meter_id: str,
        *,
        from_scheduled_at: datetime,
    ) -> list[HistoricalMeasuredReadingRecord]:
        start = parse_utc(utc_now_str(from_scheduled_at))
        readings = {
            reading.planned_reading_id: reading
            for reading in self._repository.list_measured_readings_on_or_after(
                measurement_definition_id, meter_id, from_scheduled_at=start
            )
            if reading.planned_reading_id not in self._entries
        }
        for candidate in self._virtual_measured_readings(measurement_definition_id, meter_id):
            if _reading_order(candidate)[0] >= start:
                readings[candidate.planned_reading_id] = candidate
        return sorted(readings.values(), key=_reading_order)

    def _virtual_measured_readings(
        self, measurement_definition_id: str, meter_id: str
    ) -> list[HistoricalMeasuredReadingRecord]:
        readings: list[HistoricalMeasuredReadingRecord] = []
        for entry in self._entries.values():
            planned, result = entry.planned, entry.result
            if (
                planned.measurement_definition_id != measurement_definition_id
                or planned.meter_id != meter_id
                or result.result_status is not ResultStatus.MEASURED
                or result.raw_reading is None
            ):
                continue
            if planned.scheduled_at is None:
                raise ValidationError(f"予定枠の予定日時がありません: {planned.planned_reading_id}")
            readings.append(
                HistoricalMeasuredReadingRecord(
                    planned_reading_id=planned.planned_reading_id,
                    raw_reading=result.raw_reading,
                    recorded_at=result.recorded_at,
                    meter_id=planned.meter_id,
                    scheduled_at=planned.scheduled_at,
                )
            )
        return readings

    def get_replacement_boundary_for_new_meter_first_reading(
        self,
        measurement_definition_id: str,
        meter_id: str,
        *,
        before_scheduled_at: datetime,
        before_planned_reading_id: str | None = None,
        excluding_planned_reading_id: str | None = None,
    ) -> ReplacementBoundaryRecord | None:
        before = parse_utc(utc_now_str(before_scheduled_at))
        boundaries = self._repository.list_replacement_boundaries_for_new_meter(
            measurement_definition_id, meter_id, before_scheduled_at=before
        )
        if not boundaries:
            return None
        previous = self.get_latest_measured_reading(
            measurement_definition_id,
            meter_id,
            before_scheduled_at=before,
            before_planned_reading_id=before_planned_reading_id,
            excluding_planned_reading_id=excluding_planned_reading_id,
        )
        for boundary in boundaries:
            if previous is None or _reading_order(previous)[0] < boundary.replaced_at:
                return ReplacementBoundaryRecord(
                    replacement_event_id=boundary.replacement_event_id,
                    measurement_definition_id=boundary.measurement_definition_id,
                    new_initial_reading=boundary.new_initial_reading,
                    new_meter_id=boundary.new_meter_id,
                    replaced_at=boundary.replaced_at,
                )
        return None

    def list_replacement_boundaries_for_event(
        self, replacement_event_id: str
    ) -> list[ReplacementBoundaryCalculationRecord]:
        return [
            self._updated_boundary(boundary)
            for boundary in self._repository.list_replacement_boundaries_for_event(
                replacement_event_id
            )
        ]

    def list_replacement_boundaries_for_old_meter(
        self, measurement_definition_id: str, old_meter_id: str
    ) -> list[ReplacementBoundaryCalculationRecord]:
        return [
            self._updated_boundary(boundary)
            for boundary in self._repository.list_replacement_boundaries_for_old_meter(
                measurement_definition_id, old_meter_id
            )
        ]

    def update_replacement_boundary_calculation(
        self, update: ReplacementBoundaryCalculationUpdate
    ) -> None:
        boundaries = self._repository.list_replacement_boundaries_for_event(
            update.replacement_event_id
        )
        if not any(
            boundary.measurement_definition_id == update.measurement_definition_id
            for boundary in boundaries
        ):
            raise ValidationError(
                "更新対象の交換境界が見つかりません: "
                f"{update.replacement_event_id}/{update.measurement_definition_id}"
            )
        self._boundary_updates[(update.replacement_event_id, update.measurement_definition_id)] = (
            update
        )

    def _updated_boundary(
        self, boundary: ReplacementBoundaryCalculationRecord
    ) -> ReplacementBoundaryCalculationRecord:
        update = self._boundary_updates.get(
            (boundary.replacement_event_id, boundary.measurement_definition_id)
        )
        if update is None:
            return boundary
        return replace(
            boundary,
            previous_measured_reading=update.previous_measured_reading,
            previous_measured_planned_reading_id=update.previous_measured_planned_reading_id,
            old_meter_usage_delta=update.old_meter_usage_delta,
        )


def _reading_order(reading: HistoricalMeasuredReadingRecord) -> tuple[datetime, str]:
    if reading.scheduled_at is None:
        raise ValidationError(f"実測値の予定日時がありません: {reading.planned_reading_id}")
    return reading.scheduled_at, reading.planned_reading_id


__all__ = [
    "VirtualReadingHistory",
    "VirtualReadingHistorySnapshot",
    "VirtualRecalculationError",
    "VirtualRecalculationStage",
]
