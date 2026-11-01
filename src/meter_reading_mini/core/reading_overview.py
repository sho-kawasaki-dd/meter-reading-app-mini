"""予定枠の一覧、絞り込み、状態別件数を提供する。"""

from __future__ import annotations

from collections.abc import Callable, Iterable
from dataclasses import dataclass
from datetime import date

from meter_reading_mini.core.contract_models import Category, ReadingCycle, ResultStatus
from meter_reading_mini.core.errors import ValidationError
from meter_reading_mini.core.meter_reading_sync_repositories import (
    PlannedReadingTaskState,
    ReadingOverviewRecord,
    ReadingOverviewRepository,
)
from meter_reading_mini.core.timezone_settings import build_business_date_provider


@dataclass(frozen=True, slots=True)
class ReadingOverviewFilter:
    reading_cycles: frozenset[ReadingCycle] = frozenset()
    task_states: frozenset[PlannedReadingTaskState] = frozenset()
    result_statuses: frozenset[ResultStatus] = frozenset()
    categories: frozenset[Category] = frozenset()
    location_ids: frozenset[str] = frozenset()
    keyword: str = ""


@dataclass(frozen=True, slots=True)
class ReadingOverviewCounts:
    total: int
    open: int
    auto_closed: int
    completed: int
    superseded: int
    warnings: int


@dataclass(frozen=True, slots=True)
class ReadingOverview:
    readings: tuple[ReadingOverviewRecord, ...]
    counts: ReadingOverviewCounts


def is_reading_warning(reading: ReadingOverviewRecord) -> bool:
    return (
        reading.result_status is ResultStatus.MEASURED
        and reading.warning_threshold is not None
        and reading.usage_delta is not None
        and reading.usage_delta > reading.warning_threshold
    )


def filter_reading_overview(
    readings: Iterable[ReadingOverviewRecord], criteria: ReadingOverviewFilter
) -> tuple[ReadingOverviewRecord, ...]:
    keyword = criteria.keyword.strip().casefold()
    return tuple(
        reading
        for reading in readings
        if (not criteria.reading_cycles or reading.reading_cycle in criteria.reading_cycles)
        and (not criteria.task_states or reading.task_state in criteria.task_states)
        and (not criteria.result_statuses or reading.result_status in criteria.result_statuses)
        and (not criteria.categories or reading.category in criteria.categories)
        and (not criteria.location_ids or reading.location_id in criteria.location_ids)
        and (
            not keyword
            or any(
                keyword in value.casefold()
                for value in (reading.location_name, reading.meter_name, reading.display_name)
            )
        )
    )


def summarize_reading_overview(readings: Iterable[ReadingOverviewRecord]) -> ReadingOverviewCounts:
    values = tuple(readings)
    return ReadingOverviewCounts(
        total=len(values),
        open=sum(reading.task_state is PlannedReadingTaskState.OPEN for reading in values),
        auto_closed=sum(
            reading.task_state is PlannedReadingTaskState.AUTO_CLOSED for reading in values
        ),
        completed=sum(
            reading.task_state is PlannedReadingTaskState.COMPLETED for reading in values
        ),
        superseded=sum(
            reading.task_state is PlannedReadingTaskState.SUPERSEDED for reading in values
        ),
        warnings=sum(is_reading_warning(reading) for reading in values),
    )


class ReadingOverviewQueryService:
    def __init__(
        self,
        repository: ReadingOverviewRepository,
        *,
        business_date_provider: Callable[[], date] | None = None,
    ) -> None:
        self._repository = repository
        self._business_date_provider = business_date_provider or build_business_date_provider()

    def get_overview(
        self, business_date: date, criteria: ReadingOverviewFilter | None = None
    ) -> ReadingOverview:
        if business_date > self._business_date_provider():
            raise ValidationError("未来の業務日は表示できません。")
        readings = filter_reading_overview(
            self._repository.list_reading_overview(business_date),
            criteria if criteria is not None else ReadingOverviewFilter(),
        )
        return ReadingOverview(readings=readings, counts=summarize_reading_overview(readings))
