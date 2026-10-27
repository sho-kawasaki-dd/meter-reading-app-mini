"""使用量計算とシート出力で共有する、枠ごとの前回値判定。"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from enum import StrEnum

from meter_reading_mini.core.errors import ValidationError
from meter_reading_mini.core.meter_reading_sync_repositories import (
    HistoricalMeasuredReadingLookupRepository,
    HistoricalMeasuredReadingRecord,
    PlannedReadingRecord,
    ReplacementBoundaryLookupRepository,
    ReplacementBoundaryRecord,
)


class PreviousReadingSource(StrEnum):
    """前回値の由来。値はシートデータの `previous_source` と一致する。"""

    MEASURED = "measured"
    REPLACEMENT_INITIAL = "replacement_initial"
    REPLACEMENT_NO_INITIAL = "replacement_no_initial"
    NONE = "none"


@dataclass(frozen=True, slots=True)
class CalculationContext:
    """予定枠の使用量計算に使う前回実測と交換境界。"""

    previous: HistoricalMeasuredReadingRecord | None
    boundary: ReplacementBoundaryRecord | None


@dataclass(frozen=True, slots=True)
class InitialPreviousReading:
    """シートに埋め込む初期前回値と、その参照元。"""

    source: PreviousReadingSource
    reading: Decimal | None
    scheduled_at: datetime | None
    planned_reading_id: str | None


def resolve_calculation_context(
    historical_repository: HistoricalMeasuredReadingLookupRepository,
    boundary_repository: ReplacementBoundaryLookupRepository,
    planned_reading: PlannedReadingRecord,
    *,
    excluding_planned_reading_id: str | None = None,
) -> CalculationContext:
    """交換境界があれば前回実測を使わず、境界だけを返す。"""

    scheduled_at = planned_reading.scheduled_at
    if scheduled_at is None:
        raise ValidationError(f"予定枠の予定日時がありません: {planned_reading.planned_reading_id}")
    measurement_definition_id = planned_reading.measurement_definition_id
    boundary = boundary_repository.get_replacement_boundary_for_new_meter_first_reading(
        measurement_definition_id,
        planned_reading.meter_id,
        before_scheduled_at=scheduled_at,
        before_planned_reading_id=planned_reading.planned_reading_id,
        excluding_planned_reading_id=excluding_planned_reading_id,
    )
    previous = historical_repository.get_latest_measured_reading(
        measurement_definition_id,
        planned_reading.meter_id,
        before_scheduled_at=scheduled_at,
        before_planned_reading_id=planned_reading.planned_reading_id,
        excluding_planned_reading_id=excluding_planned_reading_id,
    )
    if boundary is not None:
        previous = None
    return CalculationContext(previous=previous, boundary=boundary)


def describe_initial_previous_reading(context: CalculationContext) -> InitialPreviousReading:
    """計算文脈から、シートの初期前回値と参照元を決める。"""

    previous = context.previous
    if previous is not None:
        if previous.scheduled_at is None:
            raise ValidationError(f"前回実測の予定日時がありません: {previous.planned_reading_id}")
        return InitialPreviousReading(
            source=PreviousReadingSource.MEASURED,
            reading=previous.raw_reading,
            scheduled_at=previous.scheduled_at,
            planned_reading_id=previous.planned_reading_id,
        )
    boundary = context.boundary
    if boundary is None:
        return InitialPreviousReading(PreviousReadingSource.NONE, None, None, None)
    if boundary.new_initial_reading is None:
        return InitialPreviousReading(
            PreviousReadingSource.REPLACEMENT_NO_INITIAL, None, None, None
        )
    return InitialPreviousReading(
        PreviousReadingSource.REPLACEMENT_INITIAL, boundary.new_initial_reading, None, None
    )


__all__ = [
    "CalculationContext",
    "InitialPreviousReading",
    "PreviousReadingSource",
    "describe_initial_previous_reading",
    "resolve_calculation_context",
]
