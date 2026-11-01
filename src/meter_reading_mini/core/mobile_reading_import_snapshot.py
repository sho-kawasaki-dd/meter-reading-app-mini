"""一括プレビューが参照した DB 状態を、比較可能な不変値として記録する。"""

from __future__ import annotations

from collections.abc import Collection
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from enum import StrEnum

from meter_reading_mini.core.contract_models import CalculationMethod, ValueSchema
from meter_reading_mini.core.meter_reading_sync_repositories import (
    HistoricalMeasuredReadingRecord,
    MeasurementDefinitionRecord,
    MeterAssignmentRecord,
    MobileReadingImportRepository,
    MobileReadingImportTargetRecord,
    PlannedReadingRecord,
    ReplacementBoundaryCalculationRecord,
    ReplacementBoundaryRecord,
)


class DatabaseReadKind(StrEnum):
    TARGETS = "targets"
    PLANNED_READING = "planned_reading"
    DEFINITION = "definition"
    ASSIGNMENT = "assignment"
    ASSIGNMENT_BY_KEY = "assignment_by_key"
    ASSIGNMENTS = "assignments"
    PREVIOUS_READING = "previous_reading"
    FOLLOWING_READINGS = "following_readings"
    READINGS_ON_OR_AFTER = "readings_on_or_after"
    FIRST_READING_BOUNDARY = "first_reading_boundary"
    OLD_METER_BOUNDARIES = "old_meter_boundaries"
    EVENT_BOUNDARIES = "event_boundaries"
    NEW_METER_BOUNDARIES = "new_meter_boundaries"


@dataclass(frozen=True, slots=True)
class DefinitionCalculationSnapshot:
    measurement_definition_id: str
    value_schema: ValueSchema
    calculation_method: CalculationMethod
    decimal_places: int | None
    warning_threshold: Decimal | None


@dataclass(frozen=True, slots=True)
class NewMeterBoundarySnapshot:
    replacement_event_id: str
    measurement_definition_id: str
    new_meter_id: str
    replaced_at: datetime
    new_initial_reading: Decimal | None


type DatabaseReadKey = tuple[str | datetime | None, ...]
type DatabaseReadValue = (
    PlannedReadingRecord
    | DefinitionCalculationSnapshot
    | MeterAssignmentRecord
    | HistoricalMeasuredReadingRecord
    | ReplacementBoundaryRecord
    | tuple[MeterAssignmentRecord, ...]
    | tuple[HistoricalMeasuredReadingRecord, ...]
    | tuple[ReplacementBoundaryCalculationRecord, ...]
    | tuple[NewMeterBoundarySnapshot, ...]
    | tuple[tuple[str, MobileReadingImportTargetRecord | None], ...]
    | None
)


@dataclass(frozen=True, slots=True)
class DatabaseRead:
    kind: DatabaseReadKind
    key: DatabaseReadKey
    value: DatabaseReadValue


@dataclass(frozen=True, slots=True)
class MobileReadingDatabaseSnapshot:
    reads: frozenset[DatabaseRead]


class PreviewRepositoryRecorder:
    """仮想計算に使われた問い合わせの条件・結果・対象行集合を記録する。"""

    def __init__(self, repository: MobileReadingImportRepository) -> None:
        self._repository = repository
        self._reads: set[DatabaseRead] = set()
        self._historical_ids: set[str] = set()
        self._old_boundary_keys: set[tuple[str, str]] = set()
        self._event_reads: list[tuple[str, tuple[ReplacementBoundaryCalculationRecord, ...]]] = []

    def _record(
        self, kind: DatabaseReadKind, key: DatabaseReadKey, value: DatabaseReadValue
    ) -> None:
        self._reads.add(DatabaseRead(kind, key, value))

    def snapshot(self) -> MobileReadingDatabaseSnapshot:
        if self._historical_ids:
            self.get_mobile_reading_import_targets(self._historical_ids)
        for event_id, boundaries in self._event_reads:
            relevant = tuple(
                boundary
                for boundary in boundaries
                if (boundary.replacement_event_id, boundary.measurement_definition_id)
                in self._old_boundary_keys
            )
            self._record(DatabaseReadKind.EVENT_BOUNDARIES, (event_id,), relevant)
        return MobileReadingDatabaseSnapshot(frozenset(self._reads))

    def get_mobile_reading_import_targets(
        self, planned_reading_ids: Collection[str]
    ) -> dict[str, MobileReadingImportTargetRecord]:
        ids = tuple(sorted(set(planned_reading_ids)))
        targets = self._repository.get_mobile_reading_import_targets(ids)
        self._record(
            DatabaseReadKind.TARGETS,
            ids,
            tuple((planned_id, targets.get(planned_id)) for planned_id in ids),
        )
        return targets

    def get_planned_reading(self, planned_reading_id: str) -> PlannedReadingRecord | None:
        planned = self._repository.get_planned_reading(planned_reading_id)
        self._record(DatabaseReadKind.PLANNED_READING, (planned_reading_id,), planned)
        return planned

    def get_measurement_definition(
        self, measurement_definition_id: str
    ) -> MeasurementDefinitionRecord | None:
        definition = self._repository.get_measurement_definition(measurement_definition_id)
        snapshot = (
            DefinitionCalculationSnapshot(
                measurement_definition_id=definition.measurement_definition_id,
                value_schema=definition.value_schema,
                calculation_method=definition.calculation_method,
                decimal_places=definition.decimal_places,
                warning_threshold=definition.warning_threshold,
            )
            if definition is not None
            else None
        )
        self._record(DatabaseReadKind.DEFINITION, (measurement_definition_id,), snapshot)
        return definition

    def get_meter_assignment(
        self, measurement_definition_id: str, at: datetime
    ) -> MeterAssignmentRecord | None:
        assignment = self._repository.get_meter_assignment(measurement_definition_id, at)
        self._record(DatabaseReadKind.ASSIGNMENT, (measurement_definition_id, at), assignment)
        return assignment

    def get_meter_assignment_by_key(
        self, measurement_definition_id: str, meter_id: str, effective_from: datetime
    ) -> MeterAssignmentRecord | None:
        assignment = self._repository.get_meter_assignment_by_key(
            measurement_definition_id, meter_id, effective_from
        )
        self._record(
            DatabaseReadKind.ASSIGNMENT_BY_KEY,
            (measurement_definition_id, meter_id, effective_from),
            assignment,
        )
        return assignment

    def list_meter_assignments(self, measurement_definition_id: str) -> list[MeterAssignmentRecord]:
        assignments = self._repository.list_meter_assignments(measurement_definition_id)
        self._record(DatabaseReadKind.ASSIGNMENTS, (measurement_definition_id,), tuple(assignments))
        return assignments

    def get_latest_measured_reading(
        self,
        measurement_definition_id: str,
        meter_id: str,
        *,
        before_scheduled_at: datetime,
        before_planned_reading_id: str | None = None,
        excluding_planned_reading_id: str | None = None,
    ) -> HistoricalMeasuredReadingRecord | None:
        reading = self._repository.get_latest_measured_reading(
            measurement_definition_id,
            meter_id,
            before_scheduled_at=before_scheduled_at,
            before_planned_reading_id=before_planned_reading_id,
            excluding_planned_reading_id=excluding_planned_reading_id,
        )
        if reading is not None:
            self._historical_ids.add(reading.planned_reading_id)
        self._record(
            DatabaseReadKind.PREVIOUS_READING,
            (
                measurement_definition_id,
                meter_id,
                before_scheduled_at,
                before_planned_reading_id,
                excluding_planned_reading_id,
            ),
            reading,
        )
        return reading

    def list_following_measured_readings(
        self,
        measurement_definition_id: str,
        meter_id: str,
        *,
        after_scheduled_at: datetime,
        after_planned_reading_id: str | None = None,
        excluding_planned_reading_id: str | None = None,
    ) -> list[HistoricalMeasuredReadingRecord]:
        readings = self._repository.list_following_measured_readings(
            measurement_definition_id,
            meter_id,
            after_scheduled_at=after_scheduled_at,
            after_planned_reading_id=after_planned_reading_id,
            excluding_planned_reading_id=excluding_planned_reading_id,
        )
        self._historical_ids.update(reading.planned_reading_id for reading in readings)
        self._record(
            DatabaseReadKind.FOLLOWING_READINGS,
            (
                measurement_definition_id,
                meter_id,
                after_scheduled_at,
                after_planned_reading_id,
                excluding_planned_reading_id,
            ),
            tuple(readings),
        )
        return readings

    def list_measured_readings_on_or_after(
        self,
        measurement_definition_id: str,
        meter_id: str,
        *,
        from_scheduled_at: datetime,
    ) -> list[HistoricalMeasuredReadingRecord]:
        readings = self._repository.list_measured_readings_on_or_after(
            measurement_definition_id, meter_id, from_scheduled_at=from_scheduled_at
        )
        self._historical_ids.update(reading.planned_reading_id for reading in readings)
        self._record(
            DatabaseReadKind.READINGS_ON_OR_AFTER,
            (measurement_definition_id, meter_id, from_scheduled_at),
            tuple(readings),
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
        boundary = self._repository.get_replacement_boundary_for_new_meter_first_reading(
            measurement_definition_id,
            meter_id,
            before_scheduled_at=before_scheduled_at,
            before_planned_reading_id=before_planned_reading_id,
            excluding_planned_reading_id=excluding_planned_reading_id,
        )
        self._record(
            DatabaseReadKind.FIRST_READING_BOUNDARY,
            (
                measurement_definition_id,
                meter_id,
                before_scheduled_at,
                before_planned_reading_id,
                excluding_planned_reading_id,
            ),
            boundary,
        )
        return boundary

    def list_replacement_boundaries_for_old_meter(
        self, measurement_definition_id: str, old_meter_id: str
    ) -> list[ReplacementBoundaryCalculationRecord]:
        boundaries = self._repository.list_replacement_boundaries_for_old_meter(
            measurement_definition_id, old_meter_id
        )
        self._old_boundary_keys.update(
            (boundary.replacement_event_id, boundary.measurement_definition_id)
            for boundary in boundaries
        )
        self._record(
            DatabaseReadKind.OLD_METER_BOUNDARIES,
            (measurement_definition_id, old_meter_id),
            tuple(boundaries),
        )
        return boundaries

    def list_replacement_boundaries_for_event(
        self, replacement_event_id: str
    ) -> list[ReplacementBoundaryCalculationRecord]:
        boundaries = self._repository.list_replacement_boundaries_for_event(replacement_event_id)
        self._event_reads.append((replacement_event_id, tuple(boundaries)))
        return boundaries

    def list_replacement_boundaries_for_new_meter(
        self,
        measurement_definition_id: str,
        new_meter_id: str,
        *,
        before_scheduled_at: datetime,
    ) -> list[ReplacementBoundaryCalculationRecord]:
        boundaries = self._repository.list_replacement_boundaries_for_new_meter(
            measurement_definition_id, new_meter_id, before_scheduled_at=before_scheduled_at
        )
        self._record(
            DatabaseReadKind.NEW_METER_BOUNDARIES,
            (measurement_definition_id, new_meter_id, before_scheduled_at),
            tuple(
                NewMeterBoundarySnapshot(
                    replacement_event_id=boundary.replacement_event_id,
                    measurement_definition_id=boundary.measurement_definition_id,
                    new_meter_id=boundary.new_meter_id,
                    replaced_at=boundary.replaced_at,
                    new_initial_reading=boundary.new_initial_reading,
                )
                for boundary in boundaries
            ),
        )
        return boundaries
