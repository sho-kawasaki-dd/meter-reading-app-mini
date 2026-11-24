"""検針履歴の読取条件と表示結果の契約。"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal
from enum import StrEnum

from meter_reading_mini.core.contract_models import (
    CalculationMethod,
    Category,
    PreviousReadingSource,
    Purpose,
    ReadingSession,
    ResultStatus,
    ValueSchema,
)
from meter_reading_mini.core.meter_reading_sync_repositories import PlannedReadingTaskState


class ReadingHistoryRowKind(StrEnum):
    REPLACEMENT_OLD_FINAL = "replacement_old_final"
    REPLACEMENT_NEW_INITIAL = "replacement_new_initial"
    READING = "reading"


class ReadingHistoryCode(StrEnum):
    STATE_MISMATCH = "HIST_STATE_MISMATCH"
    BASELINE_MISMATCH = "HIST_BASELINE_MISMATCH"
    ASSIGNMENT_UNRESOLVED = "HIST_ASSIGNMENT_UNRESOLVED"
    ASSIGNMENT_PENDING = "HIST_ASSIGNMENT_PENDING"
    REPLACEMENT_FINAL_MISSING = "HIST_REPLACEMENT_FINAL_MISSING"
    REPLACEMENT_INITIAL_MISSING = "HIST_REPLACEMENT_INITIAL_MISSING"
    NO_BASELINE = "HIST_NO_BASELINE"
    ROLLOVER = "HIST_ROLLOVER"
    BASELINE_OUTSIDE_PERIOD = "HIST_BASELINE_OUTSIDE_PERIOD"
    INACTIVE_MASTER = "HIST_INACTIVE_MASTER"
    SUPERSEDED = "HIST_SUPERSEDED"


@dataclass(frozen=True, slots=True)
class ReadingHistoryQuery:
    measurement_definition_id: str
    start_date: date
    end_date: date
    include_superseded: bool = False


@dataclass(frozen=True, slots=True)
class ReadingHistoryDefinitionOption:
    measurement_definition_id: str
    display_name: str
    location_id: str
    location_name: str
    category: Category
    unit: str
    purpose: Purpose
    calculation_method: CalculationMethod
    value_schema: ValueSchema
    is_active: bool


@dataclass(frozen=True, slots=True)
class ReadingHistoryIssue:
    code: ReadingHistoryCode
    message: str
    meter_id: str | None = None
    assignment_effective_from: datetime | None = None


@dataclass(frozen=True, slots=True)
class ReadingHistoryRow:
    row_kind: ReadingHistoryRowKind
    business_date: date
    at: datetime
    reading_session: ReadingSession | None
    task_state: PlannedReadingTaskState | None
    result_status: ResultStatus | None
    meter_id: str
    meter_name: str
    meter_serial: str | None
    raw_reading: Decimal | None
    last_reading: Decimal | None
    previous_source: PreviousReadingSource | None
    previous_reference_id: str | None
    multiplier: Decimal | None
    rollover_value: Decimal | None
    usage_delta: Decimal | None
    power_factor: str | None
    recorded_at: datetime | None
    updated_by: str | None
    result_source: str | None
    note: str | None
    replacement_event_id: str | None
    planned_reading_id: str | None
    issues: tuple[ReadingHistoryIssue, ...]


@dataclass(frozen=True, slots=True)
class ReadingHistory:
    definition: ReadingHistoryDefinitionOption
    query: ReadingHistoryQuery
    rows: tuple[ReadingHistoryRow, ...]
    total_usage: Decimal | None
    warning_count: int
