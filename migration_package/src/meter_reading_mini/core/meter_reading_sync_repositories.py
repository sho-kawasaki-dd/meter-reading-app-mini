"""`POST /api/readings` 用の repository interface と保存要求モデルを定義する。

service 層は SQLAlchemy を直接知らず、lookup と upsert に必要な最小責務だけへ依存する。
このモジュールの dataclass は「永続化へ渡す確定済み要求」を表し、transport contract を
そのまま DB へ流し込まないための境界になっている。
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal
from enum import StrEnum
from typing import Any, Protocol

from meter_reading_mini.core.contract_models import (
    CalculationMethod,
    Category,
    MeasurementDefinitionData,
    MeterMasterData,
    PlannedReadingTask,
    Purpose,
    ReadingCycle,
    ReadingSession,
    ResultStatus,
    ValueSchema,
)


class MonthlyScheduleSelectorType(StrEnum):
    DAY_OF_MONTH = "day_of_month"
    NTH_WEEKDAY = "nth_weekday"
    MONTH_EDGE = "month_edge"


class MonthParity(StrEnum):
    ODD = "odd"
    EVEN = "even"


@dataclass(frozen=True, slots=True)
class ScheduleItem:
    reading_session: ReadingSession
    day_offset: int = 0


@dataclass(frozen=True, slots=True)
class DailySchedule:
    reading_items: tuple[ScheduleItem, ...]

    @property
    def reading_sessions(self) -> tuple[ReadingSession, ...]:
        return tuple(item.reading_session for item in self.reading_items)

    def get_day_offset(self, reading_session: ReadingSession) -> int:
        for item in self.reading_items:
            if item.reading_session is reading_session:
                return item.day_offset
        return 0


@dataclass(frozen=True, slots=True)
class WeeklySchedule:
    weekday: int
    reading_session: ReadingSession
    day_offset: int = 0


@dataclass(frozen=True, slots=True)
class MonthlySchedule:
    selector_type: MonthlyScheduleSelectorType
    day_of_month: int | None = None
    nth: int | None = None
    nth_weekday: int | None = None
    edge: str | None = None
    reading_session: ReadingSession = ReadingSession.SESSION_0800
    day_offset: int = 0


@dataclass(frozen=True, slots=True)
class EveryTwoMonthsSchedule:
    selector_type: MonthlyScheduleSelectorType
    month_parity: MonthParity
    day_of_month: int | None = None
    nth: int | None = None
    nth_weekday: int | None = None
    edge: str | None = None
    reading_session: ReadingSession = ReadingSession.SESSION_0800
    day_offset: int = 0


ScheduleSettings = DailySchedule | WeeklySchedule | MonthlySchedule | EveryTwoMonthsSchedule


class PlannedReadingTaskState(StrEnum):
    OPEN = "open"
    COMPLETED = "completed"
    AUTO_CLOSED = "auto_closed"
    SUPERSEDED = "superseded"


class PlannedReadingCloseReason(StrEnum):
    CUTOFF_ELAPSED = "cutoff_elapsed"
    METER_REPLACED = "meter_replaced"
    SCHEDULE_RECOMPOSED = "schedule_recomposed"
    DEFINITION_DEACTIVATED = "definition_deactivated"


@dataclass(frozen=True, slots=True)
class MeasurementDefinitionScheduleRecord:
    measurement_definition_id: str
    effective_from_date: date
    schedule_json: str | None
    created_at: datetime


@dataclass(frozen=True, slots=True)
class ReadingCycleScheduleDefaultRecord:
    reading_cycle: ReadingCycle
    effective_from_date: date
    schedule_json: str
    created_at: datetime


@dataclass(frozen=True, slots=True)
class ReadingSessionReleaseOffsetRecord:
    reading_session: ReadingSession
    effective_from_date: date
    offset_days: int
    created_at: datetime


@dataclass(frozen=True, slots=True)
class MeterAssignmentRecord:
    measurement_definition_id: str
    meter_id: str
    effective_from: datetime
    effective_to: datetime | None = None


@dataclass(frozen=True, slots=True)
class MeterMasterRecord:
    meter_id: str
    location_id: str
    location_name: str
    meter_name: str
    category: Category
    meter_serial: str | None
    is_active: bool


@dataclass(frozen=True, slots=True)
class MeterMasterCreateRequest:
    meter_id: str
    location_id: str
    location_name: str
    meter_name: str
    category: Category
    meter_serial: str | None = None


@dataclass(frozen=True, slots=True)
class MeterMasterUpdateRequest:
    location_name: str
    meter_name: str
    category: Category
    meter_serial: str | None = None


@dataclass(frozen=True, slots=True)
class MeterMasterUsageRecord:
    has_active_assignment: bool
    has_open_planned_readings: bool


@dataclass(frozen=True, slots=True)
class MeasurementDefinitionCreateRequest:
    measurement_definition_id: str
    location_id: str
    display_name: str
    category: Category
    unit: str
    value_schema: ValueSchema
    multiplier: Decimal
    decimal_places: int | None
    warning_threshold: Decimal | None
    rollover_value: Decimal | None
    calculation_method: CalculationMethod
    reading_cycle: ReadingCycle
    purpose: Purpose
    generation_start_date: date
    initial_schedule_json: str | None
    initial_meter_id: str | None


@dataclass(frozen=True, slots=True)
class MeasurementDefinitionUpdateRequest:
    display_name: str
    category: Category
    unit: str
    multiplier: Decimal
    decimal_places: int | None
    warning_threshold: Decimal | None
    rollover_value: Decimal | None
    purpose: str


@dataclass(frozen=True, slots=True)
class StaffRecord:
    staff_id: str
    display_name: str
    is_active: bool


@dataclass(frozen=True, slots=True)
class StaffCreateRequest:
    staff_id: str
    display_name: str


@dataclass(frozen=True, slots=True)
class StaffUpdateRequest:
    display_name: str


@dataclass(frozen=True, slots=True)
class ReadingConfirmationContext:
    sheet_id: str
    staff_id: str
    staff_name: str


@dataclass(frozen=True, slots=True)
class PlannedReadingLogicalSlotRecord:
    measurement_definition_id: str
    business_date: date
    reading_session: ReadingSession
    planned_reading_id: str
    task_state: PlannedReadingTaskState
    close_reason: PlannedReadingCloseReason | None = None
    superseded_by_planned_reading_id: str | None = None


PC_DIRECT_DEVICE_ID = "pc-direct"


@dataclass(frozen=True, slots=True)
class PlannedReadingRecord:
    """planned_reading lookup の最小結果。

    strict validation に入る前に、planned reading がどの measurement definition を
    指しているかだけを知れれば十分なので、この段階では必要最小限の情報に絞る。
    boundary lookup では meter_id も必要になるため保持する。
    """

    planned_reading_id: str
    measurement_definition_id: str
    meter_id: str
    scheduled_at: datetime | None = None
    business_date: date | None = None
    reading_session: ReadingSession | None = None
    task_state: PlannedReadingTaskState | None = None
    close_reason: PlannedReadingCloseReason | None = None
    superseded_by_planned_reading_id: str | None = None


@dataclass(frozen=True, slots=True)
class MeasurementDefinitionRecord:
    """measurement definition lookup の最小結果。"""

    measurement_definition_id: str
    value_schema: ValueSchema
    calculation_method: CalculationMethod = CalculationMethod.DELTA
    multiplier: Decimal = Decimal("1.0")
    decimal_places: int | None = None
    rollover_value: Decimal | None = None


@dataclass(frozen=True, slots=True)
class PlannedReadingGenerationDefinitionRecord:
    """planned reading 生成に必要な measurement definition 情報。"""

    measurement_definition_id: str
    location_id: str
    meter_id: str
    reading_cycle: ReadingCycle
    generation_start_date: date
    schedule: ScheduleSettings | None = None


@dataclass(frozen=True, slots=True)
class MeasurementDefinitionWithoutActiveAssignmentRecord:
    measurement_definition_id: str


@dataclass(frozen=True, slots=True)
class PlannedReadingCreateRequest:
    """planned reading を冪等に作るための保存要求。"""

    planned_reading_id: str
    measurement_definition_id: str
    meter_id: str
    location_id: str
    business_date: date
    release_date: date
    reading_session: ReadingSession
    scheduled_at: datetime
    day_offset: int = 0


@dataclass(frozen=True, slots=True)
class PlannedReadingRecompositionRecord:
    """future planned reading の再構成に必要な最小結果。"""

    planned_reading_id: str
    measurement_definition_id: str
    meter_id: str
    location_id: str
    business_date: date
    reading_session: ReadingSession
    scheduled_at: datetime
    task_state: PlannedReadingTaskState
    close_reason: PlannedReadingCloseReason | None = None
    superseded_by_planned_reading_id: str | None = None


class ResultSource(StrEnum):
    """reading transaction の監査用ソース語彙。"""

    MOBILE_HTML = "mobile_html"
    MANUAL = "manual"


@dataclass(frozen=True, slots=True)
class ReadingUpsertRequest:
    """resolved validation 後の検針結果を保存するための要求オブジェクト。"""

    planned_reading_id: str
    measurement_definition_id: str
    result_status: ResultStatus
    recorded_at: datetime
    client_updated_at: datetime
    raw_reading: Decimal | None
    structured_payload_json: dict[str, Any] | None
    updated_by: str
    signature: str
    note: str | None
    device_id: str
    result_source: ResultSource
    last_reading: Decimal | None = None
    usage_delta: Decimal | None = None


@dataclass(frozen=True, slots=True)
class HistoricalMeasuredReadingRecord:
    """usage_delta 計算に使う直近 measured 読値。"""

    planned_reading_id: str
    raw_reading: Decimal
    recorded_at: datetime
    meter_id: str | None = None
    scheduled_at: datetime | None = None


@dataclass(frozen=True, slots=True)
class CalculatedUsageUpdate:
    planned_reading_id: str
    last_reading: Decimal | None
    usage_delta: Decimal | None


@dataclass(frozen=True, slots=True)
class ReplacementUpsertRequest:
    """メーター交換イベントを保存するための要求オブジェクト。"""

    replacement_event_id: str
    location_id: str
    old_meter_id: str
    new_meter_id: str
    replaced_at: datetime
    client_updated_at: datetime
    old_final_reading: Decimal | None
    new_initial_reading: Decimal | None
    updated_by: str
    note: str | None


@dataclass(frozen=True, slots=True)
class ReplacementBoundaryRecord:
    """replacement boundary lookup の結果。"""

    replacement_event_id: str
    measurement_definition_id: str
    new_initial_reading: Decimal | None
    new_meter_id: str | None = None
    replaced_at: datetime | None = None


class UpsertDisposition(StrEnum):
    """upsert の結果を service が解釈しやすい語彙で表したもの。"""

    INSERTED = "inserted"
    UPDATED = "updated"
    REPLACED_ON_TIE = "replaced_on_tie"
    IGNORED_STALE = "ignored_stale"
    IGNORED_DUPLICATE = "ignored_duplicate"


@dataclass(frozen=True, slots=True)
class UpsertOutcome:
    """repository が返す upsert 結果。"""

    disposition: UpsertDisposition
    detail: str | None = None

    @property
    def accepted(self) -> bool:
        return self.disposition not in {
            UpsertDisposition.IGNORED_STALE,
            UpsertDisposition.IGNORED_DUPLICATE,
        }


class PlannedReadingLookupRepository(Protocol):
    """planned_reading_id から予定枠の最小メタデータを引く repository。"""

    def get_planned_reading(self, planned_reading_id: str) -> PlannedReadingRecord | None:
        """Return planned reading metadata for the provided natural key."""
        ...


class MeasurementDefinitionLookupRepository(Protocol):
    """strict validation に必要な measurement definition 情報を引く repository。"""

    def get_measurement_definition(
        self,
        measurement_definition_id: str,
    ) -> MeasurementDefinitionRecord | None:
        """Return measurement definition metadata for the provided identifier."""
        ...


class PlannedReadingGenerationRepository(Protocol):
    """planned reading の backfill 生成に必要な repository。"""

    def get_latest_planned_business_date(self) -> date | None:
        """Return the latest generated business_date, if any."""
        ...

    def list_planned_reading_generation_definitions(
        self,
    ) -> list[PlannedReadingGenerationDefinitionRecord]:
        """Return measurement definitions eligible for planned reading generation."""
        ...

    def list_measurement_definitions_without_active_assignment(
        self,
    ) -> list[MeasurementDefinitionWithoutActiveAssignmentRecord]:
        """Return active measurement definitions that have no active meter assignment."""
        ...

    def create_planned_reading_if_missing(self, request: PlannedReadingCreateRequest) -> bool:
        """Create a planned reading when its stable identifier is not present yet."""
        ...

    def create_planned_readings_if_missing(
        self, requests: list[PlannedReadingCreateRequest]
    ) -> int:
        """Create multiple planned readings in one batch and return the number inserted."""
        ...


class MeasurementDefinitionScheduleRepository(Protocol):
    """measurement_definition_schedules の読み書きに必要な repository。"""

    def get_measurement_definition_schedule(self, measurement_definition_id: str) -> str | None:
        """Return schedule JSON for the provided measurement definition, if any."""
        ...

    def upsert_measurement_definition_schedule(
        self, measurement_definition_id: str, schedule_json: str
    ) -> None:
        """Persist schedule JSON for the provided measurement definition."""
        ...

    def list_measurement_definition_schedule_history(
        self, measurement_definition_id: str
    ) -> list[MeasurementDefinitionScheduleRecord]:
        """Return schedule history ordered by effective date."""
        ...

    def get_measurement_definition_schedule_for_date(
        self, measurement_definition_id: str, business_date: date
    ) -> MeasurementDefinitionScheduleRecord | None:
        """Return the schedule version effective on the requested business date."""
        ...


class ReadingCycleScheduleDefaultsRepository(Protocol):
    def get_reading_cycle_schedule_default(
        self, reading_cycle: ReadingCycle, business_date: date
    ) -> ReadingCycleScheduleDefaultRecord | None: ...

    def list_reading_cycle_schedule_defaults(
        self, reading_cycle: ReadingCycle
    ) -> list[ReadingCycleScheduleDefaultRecord]: ...

    def save_reading_cycle_schedule_default(
        self,
        reading_cycle: ReadingCycle,
        effective_from_date: date,
        schedule_json: str,
        *,
        created_at: datetime,
    ) -> None: ...


class ReadingSessionReleaseOffsetsRepository(Protocol):
    def get_reading_session_release_offset(
        self, reading_session: ReadingSession, business_date: date
    ) -> ReadingSessionReleaseOffsetRecord | None: ...

    def list_reading_session_release_offsets(
        self, reading_session: ReadingSession
    ) -> list[ReadingSessionReleaseOffsetRecord]: ...

    def save_reading_session_release_offset(
        self,
        reading_session: ReadingSession,
        effective_from_date: date,
        offset_days: int,
        *,
        created_at: datetime,
    ) -> None: ...


class ScheduleSettingsRepository(Protocol):
    """アプリ全体の設定を読み書きする Repository 契約。"""

    def get_setting(self, key: str) -> str | None: ...

    def upsert_setting(self, key: str, value: str) -> None: ...


class TimezoneSettingsRepository(Protocol):
    """業務タイムゾーン設定と予定枠の有無を照会する契約。"""

    def get_setting(self, key: str) -> str | None: ...

    def upsert_setting(self, key: str, value: str) -> None: ...

    def has_planned_readings(self) -> bool: ...


class MeterMasterWriteRepository(Protocol):
    def get_meter_master(self, meter_id: str) -> MeterMasterRecord | None: ...

    def create_meter_master(self, request: MeterMasterCreateRequest) -> MeterMasterRecord: ...

    def update_meter_master(
        self, meter_id: str, request: MeterMasterUpdateRequest
    ) -> MeterMasterRecord: ...

    def set_meter_master_active(self, meter_id: str, is_active: bool) -> None: ...

    def get_meter_master_usage(self, meter_id: str) -> MeterMasterUsageRecord: ...


class MeasurementDefinitionWriteRepository(Protocol):
    def create_measurement_definition(
        self, request: MeasurementDefinitionCreateRequest
    ) -> None: ...

    def update_measurement_definition(
        self,
        measurement_definition_id: str,
        request: MeasurementDefinitionUpdateRequest,
    ) -> None: ...

    def set_measurement_definition_active(
        self,
        measurement_definition_id: str,
        is_active: bool,
        *,
        generation_start_date: date | None = None,
    ) -> None: ...


class MeterAssignmentRepository(Protocol):
    def get_meter_assignment(
        self, measurement_definition_id: str, at: datetime
    ) -> MeterAssignmentRecord | None: ...

    def list_meter_assignments(
        self, measurement_definition_id: str
    ) -> list[MeterAssignmentRecord]: ...

    def set_initial_meter_assignment(
        self, measurement_definition_id: str, meter_id: str, effective_from: datetime
    ) -> None: ...


class StaffRepository(Protocol):
    def list_staff(self, *, include_inactive: bool = False) -> list[StaffRecord]: ...

    def get_staff(self, staff_id: str) -> StaffRecord | None: ...

    def create_staff(self, request: StaffCreateRequest) -> StaffRecord: ...

    def update_staff(self, staff_id: str, request: StaffUpdateRequest) -> StaffRecord: ...

    def set_staff_active(self, staff_id: str, is_active: bool) -> None: ...


class PlannedReadingLogicalSlotRepository(Protocol):
    def list_planned_reading_logical_slots(
        self,
        measurement_definition_id: str,
        from_business_date: date,
        through_business_date: date,
    ) -> list[PlannedReadingLogicalSlotRecord]: ...


class MeterMasterQueryRepository(Protocol):
    """work package download 用の物理メーターマスター取得境界。"""

    def list_meter_master_data(self) -> list[MeterMasterData]:
        """Return all physical meters required by the Android client."""
        ...


class MeasurementDefinitionQueryRepository(Protocol):
    """work package download 用の measurement definition 取得境界。"""

    def list_measurement_definition_data(self) -> list[MeasurementDefinitionData]:
        """Return all measurement definitions required by the Android client."""
        ...


class PlannedReadingTaskQueryRepository(Protocol):
    """work package download 用の open planned reading 一覧取得境界。"""

    def list_open_planned_reading_tasks_for_release_date(
        self, release_date: date
    ) -> list[PlannedReadingTask]:
        """Return open tasks whose release_date is on or before the specified date."""
        ...

    def list_open_planned_reading_tasks_for_business_date(
        self, business_date: date
    ) -> list[PlannedReadingTask]:
        """Return open tasks for the specified business_date."""
        ...

    def list_open_planned_reading_tasks(self, business_date: date) -> list[PlannedReadingTask]:
        """Deprecated alias for business_date-specific queries."""
        ...


class ReleaseDateCalculator(Protocol):
    """planned reading の配布可能日を計算する境界。"""

    def compute_release_date(self, business_date: date, reading_session: ReadingSession) -> date:
        """Return the release date for the given planned reading slot."""
        ...


class PlannedReadingLifecycleRepository(Protocol):
    """planned reading の lifecycle reconciliation を扱う repository。"""

    def auto_close_open_planned_readings(
        self,
        through_business_date: date,
        *,
        closed_at: datetime,
        close_reason: PlannedReadingCloseReason,
    ) -> int:
        """Auto-close open planned readings up to the provided business_date inclusive."""
        ...

    def list_planned_reading_recomposition_candidates(
        self,
        measurement_definition_id: str,
        from_business_date: date,
    ) -> list[PlannedReadingRecompositionRecord]:
        """Return planned readings that may need future recomposition."""
        ...

    def supersede_planned_readings(
        self, planned_reading_ids: list[str], *, superseded_at: datetime
    ) -> int:
        """Mark the provided planned readings as superseded when they are no longer valid."""
        ...


class PlannedReadingRecompositionRepository(Protocol):
    """schedule 変更時の future planned reading 再構成を扱う repository。"""

    def recompose_future_planned_readings(
        self,
        measurement_definition_id: str,
        reading_cycle: ReadingCycle,
        schedule: ScheduleSettings,
        *,
        from_business_date: date | None = None,
    ) -> tuple[int, int]:
        """Return `(superseded_count, generated_count)` after recomposition."""
        ...


class ReadingUpsertRepository(Protocol):
    """planned_reading_id を自然キーとして検針結果を保存する repository。"""

    def upsert_reading(self, request: ReadingUpsertRequest) -> UpsertOutcome:
        """Persist a resolved reading submission with conflict resolution handled internally."""
        ...


class HistoricalMeasuredReadingLookupRepository(Protocol):
    """usage_delta 計算用に直近 measured 読値を返す repository。"""

    def get_latest_measured_reading(
        self,
        measurement_definition_id: str,
        meter_id: str,
        *,
        before_scheduled_at: datetime,
        before_planned_reading_id: str | None = None,
        excluding_planned_reading_id: str | None = None,
    ) -> HistoricalMeasuredReadingRecord | None:
        """Return the latest prior measured reading with a decimal raw_reading."""
        ...

    def list_following_measured_readings(
        self,
        measurement_definition_id: str,
        meter_id: str,
        *,
        after_scheduled_at: datetime,
        after_planned_reading_id: str | None = None,
        excluding_planned_reading_id: str | None = None,
    ) -> list[HistoricalMeasuredReadingRecord]:
        """Return later measured readings in deterministic schedule order."""
        ...


class ReadingPersistenceRepository(Protocol):
    """計算済み reading request を永続化する low-level repository。"""

    def upsert_reading(self, request: ReadingUpsertRequest) -> UpsertOutcome:
        """Persist a calculated reading submission with conflict resolution handled internally."""
        ...

    def save_calculated_usage(self, update: CalculatedUsageUpdate) -> None:
        """Persist calculated usage fields without changing audit metadata."""
        ...


class ReplacementEventUpsertRepository(Protocol):
    """replacement_event_id を自然キーとして交換イベントを保存する repository。"""

    def upsert_replacement(self, request: ReplacementUpsertRequest) -> UpsertOutcome:
        """Persist a replacement event with conflict resolution handled internally."""
        ...


class ReplacementReflectionRepository(Protocol):
    """accepted replacement を業務状態へ反映する repository。"""

    def reflect_replacement(self, request: ReplacementUpsertRequest) -> None:
        """Apply accepted replacement side effects to assignments and planned readings."""
        ...


class ReplacementBoundaryLookupRepository(Protocol):
    """新メーター初回 measured の計算基準を返す repository。"""

    def get_replacement_boundary_for_new_meter_first_reading(
        self,
        measurement_definition_id: str,
        meter_id: str,
        *,
        before_scheduled_at: datetime,
        before_planned_reading_id: str | None = None,
        excluding_planned_reading_id: str | None = None,
    ) -> ReplacementBoundaryRecord | None:
        """Return the latest unconsumed boundary, including one without an initial reading."""
        ...


__all__ = [
    "CalculatedUsageUpdate",
    "DailySchedule",
    "EveryTwoMonthsSchedule",
    "MeasurementDefinitionCreateRequest",
    "MeasurementDefinitionData",
    "MeasurementDefinitionLookupRepository",
    "MeasurementDefinitionQueryRepository",
    "MeasurementDefinitionRecord",
    "MeasurementDefinitionScheduleRecord",
    "HistoricalMeasuredReadingLookupRepository",
    "HistoricalMeasuredReadingRecord",
    "MeasurementDefinitionScheduleRepository",
    "MeasurementDefinitionUpdateRequest",
    "MeasurementDefinitionWithoutActiveAssignmentRecord",
    "MeasurementDefinitionWriteRepository",
    "MeterAssignmentRecord",
    "MeterAssignmentRepository",
    "MeterMasterCreateRequest",
    "MeterMasterData",
    "MeterMasterQueryRepository",
    "MeterMasterRecord",
    "MeterMasterUpdateRequest",
    "MeterMasterUsageRecord",
    "MeterMasterWriteRepository",
    "MonthlySchedule",
    "MonthlyScheduleSelectorType",
    "MonthParity",
    "PC_DIRECT_DEVICE_ID",
    "PlannedReadingCloseReason",
    "PlannedReadingCreateRequest",
    "PlannedReadingGenerationDefinitionRecord",
    "PlannedReadingGenerationRepository",
    "PlannedReadingLogicalSlotRecord",
    "PlannedReadingLogicalSlotRepository",
    "PlannedReadingLookupRepository",
    "PlannedReadingRecompositionRecord",
    "PlannedReadingRecord",
    "PlannedReadingLifecycleRepository",
    "PlannedReadingRecompositionRepository",
    "PlannedReadingTask",
    "PlannedReadingTaskState",
    "PlannedReadingTaskQueryRepository",
    "ReadingConfirmationContext",
    "ReadingCycleScheduleDefaultRecord",
    "ReadingCycleScheduleDefaultsRepository",
    "ReleaseDateCalculator",
    "ReadingPersistenceRepository",
    "ReadingSessionReleaseOffsetRecord",
    "ReadingSessionReleaseOffsetsRepository",
    "ReadingUpsertRepository",
    "ReadingUpsertRequest",
    "ReplacementBoundaryLookupRepository",
    "ReplacementBoundaryRecord",
    "ReplacementReflectionRepository",
    "ReplacementEventUpsertRepository",
    "ReplacementUpsertRequest",
    "ScheduleItem",
    "ScheduleSettings",
    "ScheduleSettingsRepository",
    "StaffCreateRequest",
    "StaffRecord",
    "StaffRepository",
    "StaffUpdateRequest",
    "TimezoneSettingsRepository",
    "WeeklySchedule",
    "UpsertDisposition",
    "UpsertOutcome",
]
