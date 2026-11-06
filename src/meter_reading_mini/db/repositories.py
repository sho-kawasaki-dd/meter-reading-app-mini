"""標準 sqlite3 を使う Repository 実装。"""

from __future__ import annotations

import calendar
import json
import sqlite3
from collections.abc import Callable, Collection
from datetime import date, datetime, time, timedelta
from decimal import Decimal, InvalidOperation
from typing import Any, cast
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

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
from meter_reading_mini.core.errors import (
    InactiveMasterError,
    InvalidTimezoneError,
    MasterInUseError,
    MeterAssignmentResolutionError,
    ScheduleHistoryResolutionError,
    SupersededPlannedReadingError,
    ValidationError,
)
from meter_reading_mini.core.meter_reading_sync_repositories import (
    AssignmentCalculationCorrectionRecord,
    AssignmentCalculationTerms,
    AssignmentMeasuredReadingRecord,
    AssignmentReviewFlagRecord,
    CalculatedUsageUpdate,
    DailySchedule,
    EveryTwoMonthsSchedule,
    HistoricalMeasuredReadingRecord,
    MeasurementDefinitionCreateRequest,
    MeasurementDefinitionRecord,
    MeasurementDefinitionScheduleRecord,
    MeasurementDefinitionUpdateRequest,
    MeasurementDefinitionWithoutActiveAssignmentRecord,
    MeterAssignmentRecord,
    MeterLocationRecord,
    MeterMasterCreateRequest,
    MeterMasterRecord,
    MeterMasterUpdateRequest,
    MeterMasterUsageRecord,
    MobileReadingImportResultRecord,
    MobileReadingImportTargetRecord,
    MonthlySchedule,
    MonthlyScheduleSelectorType,
    MonthParity,
    PlannedReadingCloseReason,
    PlannedReadingCreateRequest,
    PlannedReadingGenerationDefinitionRecord,
    PlannedReadingLogicalSlotRecord,
    PlannedReadingRecompositionRecord,
    PlannedReadingRecord,
    PlannedReadingTaskState,
    ReadingCycleScheduleDefaultRecord,
    ReadingOverviewRecord,
    ReadingSessionReleaseOffsetRecord,
    ReadingUpsertRequest,
    ReplacementAffectedSlot,
    ReplacementBoundaryCalculationRecord,
    ReplacementBoundaryCalculationUpdate,
    ReplacementBoundaryRecord,
    ReplacementDefinitionReading,
    ReplacementEventRecord,
    ReplacementUpsertRequest,
    ResultSource,
    ScheduleItem,
    ScheduleSettings,
    SheetPlannedReadingRecord,
    StaffCreateRequest,
    StaffRecord,
    StaffUpdateRequest,
    UpsertDisposition,
    UpsertOutcome,
    WeeklySchedule,
)
from meter_reading_mini.core.reading_comparison import (
    ComparableReadingContent,
    ComparableReadingHeader,
    normalize_stored_structured_payload,
    reading_content_matches,
    serialize_structured_payload,
)
from meter_reading_mini.core.timeutil import parse_utc, utc_now, utc_now_str

_TIMEZONE_SETTING = "app.timezone.v1"
_DEFAULT_TIMEZONE = "Asia/Tokyo"


def _date_text(value: date) -> str:
    return value.isoformat()


def _read_date(value: str) -> date:
    parsed = date.fromisoformat(value)
    if parsed.isoformat() != value:
        raise ValueError(f"日付は YYYY-MM-DD 形式で指定してください: {value}")
    return parsed


def _decimal_text(value: Decimal | None) -> str | None:
    if value is None:
        return None
    if not value.is_finite():
        raise ValidationError("Decimal は有限値で指定してください。")
    return format(value, "f")


def _read_decimal(value: str | None) -> Decimal | None:
    if value is None:
        return None
    try:
        parsed = Decimal(value)
    except InvalidOperation as exc:
        raise ValidationError(f"DB の Decimal 値が不正です: {value}") from exc
    if not parsed.is_finite() or format(parsed, "f") != value:
        raise ValidationError(f"DB の Decimal 値が plain decimal 形式ではありません: {value}")
    return parsed


def _replacement_reading_map(
    request: ReplacementUpsertRequest, definition_ids: set[str]
) -> dict[str, ReplacementDefinitionReading]:
    if request.definition_readings:
        readings = {
            reading.measurement_definition_id: reading for reading in request.definition_readings
        }
        if len(readings) != len(request.definition_readings) or set(readings) != definition_ids:
            raise ValidationError("測定定義別読値が交換対象と一致しません。")
        if request.old_final_reading is not None or request.new_initial_reading is not None:
            raise ValidationError("測定定義別読値と共通読値は同時に指定できません。")
        return readings
    if request.old_final_reading is not None or request.new_initial_reading is not None:
        if len(definition_ids) != 1:
            raise ValidationError(
                "複数の測定定義がある交換では、測定定義ごとの読値を指定してください。"
            )
        definition_id = next(iter(definition_ids))
        return {
            definition_id: ReplacementDefinitionReading(
                definition_id,
                request.old_final_reading,
                request.new_initial_reading,
            )
        }
    return {
        definition_id: ReplacementDefinitionReading(definition_id, None, None)
        for definition_id in definition_ids
    }


_ID_CHUNK_SIZE = 500

_REPLACEMENT_BOUNDARY_CALCULATION_SELECT = (
    "SELECT replacement_event_id, measurement_definition_id, old_meter_id, new_meter_id, "
    "replaced_at, old_final_reading, new_initial_reading, old_assignment_effective_from, "
    "new_assignment_effective_from, old_meter_usage_delta, previous_measured_reading, "
    "previous_measured_planned_reading_id FROM replacement_boundaries "
)


def _id_chunks(values: Collection[str]) -> list[tuple[str, ...]]:
    unique = sorted(set(values))
    return [
        tuple(unique[start : start + _ID_CHUNK_SIZE])
        for start in range(0, len(unique), _ID_CHUNK_SIZE)
    ]


def _json_text(value: dict[str, Any] | None) -> str | None:
    return serialize_structured_payload(value)


def _deserialize_schedule(cycle: ReadingCycle, raw: str) -> ScheduleSettings:
    """DB の履歴 JSON を契約上のスケジュール値へ変換する。"""
    try:
        parsed: object = json.loads(raw)
        if not isinstance(parsed, dict):
            raise ValueError("JSON の最上位は object である必要があります")
        data = cast(dict[str, Any], parsed)
        if cycle in {
            ReadingCycle.DAILY_1X,
            ReadingCycle.DAILY_2X,
            ReadingCycle.DAILY_3X,
        }:
            items: object = data["reading_sessions"]
            if not isinstance(items, list):
                raise ValueError("reading_sessions は array である必要があります")
            schedule_items: list[ScheduleItem] = []
            for item_value in cast(list[object], items):
                if not isinstance(item_value, dict):
                    raise ValueError("reading_sessions の要素は object である必要があります")
                item = cast(dict[str, Any], item_value)
                schedule_items.append(
                    ScheduleItem(
                        ReadingSession(item["reading_session"]),
                        _schedule_integer(item, "day_offset", 0),
                    )
                )
            return DailySchedule(tuple(schedule_items))
        if cycle is ReadingCycle.WEEKLY:
            weekdays = {
                "monday": 0,
                "tuesday": 1,
                "wednesday": 2,
                "thursday": 3,
                "friday": 4,
                "saturday": 5,
                "sunday": 6,
            }
            return WeeklySchedule(
                weekday=weekdays[data["weekday"]],
                reading_session=ReadingSession(data["reading_session"]),
                day_offset=_schedule_integer(data, "day_offset", 0),
            )
        selector = MonthlyScheduleSelectorType(data["selector_type"])
        reading_session = ReadingSession(data.get("reading_session", "08:00"))
        day_offset = _schedule_integer(data, "day_offset", 0)
        day_of_month = _optional_schedule_integer(data, "day_of_month")
        nth = _optional_schedule_integer(data, "nth")
        edge = data.get("edge")
        if edge is not None and not isinstance(edge, str):
            raise ValueError("edge は文字列である必要があります")
        weekday_value = data.get("nth_weekday")
        nth_weekday: int | None
        if selector is MonthlyScheduleSelectorType.NTH_WEEKDAY:
            weekday_names = (
                "monday",
                "tuesday",
                "wednesday",
                "thursday",
                "friday",
                "saturday",
                "sunday",
            )
            if isinstance(weekday_value, str):
                nth_weekday = weekday_names.index(weekday_value)
            elif type(weekday_value) is int:
                nth_weekday = weekday_value
            else:
                raise ValueError("nth_weekday は曜日名または整数で指定してください")
        else:
            nth_weekday = None
        if cycle is ReadingCycle.MONTHLY:
            return MonthlySchedule(
                selector_type=selector,
                day_of_month=day_of_month,
                nth=nth,
                nth_weekday=nth_weekday,
                edge=edge,
                reading_session=reading_session,
                day_offset=day_offset,
            )
        return EveryTwoMonthsSchedule(
            month_parity=MonthParity(data["month_parity"]),
            selector_type=selector,
            day_of_month=day_of_month,
            nth=nth,
            nth_weekday=nth_weekday,
            edge=edge,
            reading_session=reading_session,
            day_offset=day_offset,
        )
    except (KeyError, TypeError, ValueError, json.JSONDecodeError) as exc:
        raise ValidationError(f"スケジュール履歴の JSON が不正です: {exc}") from exc


def _schedule_integer(data: dict[str, Any], key: str, default: int) -> int:
    value = data.get(key, default)
    if type(value) is not int:
        raise ValueError(f"{key} は整数である必要があります")
    return value


def _optional_schedule_integer(data: dict[str, Any], key: str) -> int | None:
    value = data.get(key)
    if value is None:
        return None
    if type(value) is not int:
        raise ValueError(f"{key} は整数である必要があります")
    return value


class SQLiteRepository:
    """接続を共有し、複数の Repository Protocol を実装する。"""

    def __init__(
        self,
        conn: sqlite3.Connection,
        *,
        clock: Callable[[], datetime] = utc_now,
        business_date_provider: Callable[[], date] | None = None,
    ) -> None:
        self._conn = conn
        self._clock = clock
        self._business_date_provider = business_date_provider

    def _execute(self, sql: str, parameters: tuple[object, ...] = ()) -> sqlite3.Cursor:
        try:
            return self._conn.execute(sql, parameters)
        except sqlite3.IntegrityError as exc:
            raise ValidationError(f"データベース制約に違反しました: {exc}") from exc

    def _now(self) -> datetime:
        value = self._clock()
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValidationError("注入時計はタイムゾーン付き日時を返す必要があります。")
        return value

    def _business_date(self) -> date:
        if self._business_date_provider is not None:
            return self._business_date_provider()
        return self._now().astimezone(self._business_timezone()).date()

    def _midnight_utc(self, value: date) -> str:
        local_midnight = datetime.combine(value, time.min, tzinfo=self._business_timezone())
        return utc_now_str(local_midnight)

    def _business_timezone(self) -> ZoneInfo:
        timezone_name = self.get_setting(_TIMEZONE_SETTING) or _DEFAULT_TIMEZONE
        try:
            return ZoneInfo(timezone_name)
        except ZoneInfoNotFoundError as exc:
            raise InvalidTimezoneError(
                f"DB に無効な業務タイムゾーンが保存されています: {timezone_name}"
            ) from exc

    @staticmethod
    def _record_schedule(row: tuple[Any, ...]) -> MeasurementDefinitionScheduleRecord:
        return MeasurementDefinitionScheduleRecord(
            measurement_definition_id=str(row[0]),
            effective_from_date=_read_date(str(row[1])),
            schedule_json=None if row[2] is None else str(row[2]),
            created_at=parse_utc(str(row[3])),
        )

    def get_planned_reading(self, planned_reading_id: str) -> PlannedReadingRecord | None:
        row = self._conn.execute(
            "SELECT planned_reading_id, measurement_definition_id, meter_id, scheduled_at, "
            "business_date, reading_session, task_state, close_reason, "
            "superseded_by_planned_reading_id FROM planned_readings "
            "WHERE planned_reading_id = ?",
            (planned_reading_id,),
        ).fetchone()
        if row is None:
            return None
        return PlannedReadingRecord(
            planned_reading_id=str(row[0]),
            measurement_definition_id=str(row[1]),
            meter_id=str(row[2]),
            scheduled_at=parse_utc(str(row[3])),
            business_date=_read_date(str(row[4])),
            reading_session=ReadingSession(str(row[5])),
            task_state=PlannedReadingTaskState(str(row[6])),
            close_reason=PlannedReadingCloseReason(str(row[7])) if row[7] else None,
            superseded_by_planned_reading_id=str(row[8]) if row[8] else None,
        )

    def get_mobile_reading_import_targets(
        self, planned_reading_ids: Collection[str]
    ) -> dict[str, MobileReadingImportTargetRecord]:
        targets: dict[str, MobileReadingImportTargetRecord] = {}
        for chunk in _id_chunks(planned_reading_ids):
            placeholders = ", ".join("?" for _ in chunk)
            rows = self._conn.execute(
                "SELECT p.planned_reading_id, p.measurement_definition_id, p.meter_id, "
                "p.location_id, p.business_date, p.reading_session, p.scheduled_at, "
                "p.task_state, p.close_reason, p.closed_at, p.superseded_by_planned_reading_id, "
                "m.location_name, m.meter_name, m.is_active, "
                "d.display_name, d.category, d.unit, d.value_schema, d.calculation_method, "
                "d.decimal_places, d.warning_threshold, d.reading_cycle, d.purpose, d.is_active, "
                "r.result_status, r.raw_reading, r.structured_payload_json, r.note, "
                "r.recorded_at, r.client_updated_at, r.result_source, r.device_id, r.updated_by, "
                "r.signature, r.last_reading, r.usage_delta, r.arrival_sequence "
                "FROM planned_readings AS p "
                "LEFT JOIN physical_meters AS m ON m.meter_id = p.meter_id "
                "LEFT JOIN measurement_definitions AS d "
                "ON d.measurement_definition_id = p.measurement_definition_id "
                "LEFT JOIN reading_transactions AS r "
                "ON r.planned_reading_id = p.planned_reading_id "
                f"WHERE p.planned_reading_id IN ({placeholders})",
                chunk,
            ).fetchall()
            for row in rows:
                if row[11] is None:
                    raise ValidationError(f"予定枠に対応する物理メーターが見つかりません: {row[0]}")
                if row[14] is None:
                    raise ValidationError(f"予定枠に対応する測定定義が見つかりません: {row[0]}")
                existing_result = None
                if row[24] is not None:
                    existing_result = MobileReadingImportResultRecord(
                        result_status=ResultStatus(str(row[24])),
                        raw_reading=_read_decimal(str(row[25]) if row[25] is not None else None),
                        structured_payload_json=str(row[26]) if row[26] is not None else None,
                        note=str(row[27]) if row[27] is not None else None,
                        recorded_at=parse_utc(str(row[28])),
                        client_updated_at=parse_utc(str(row[29])),
                        result_source=ResultSource(str(row[30])),
                        device_id=str(row[31]),
                        updated_by=str(row[32]),
                        signature=str(row[33]),
                        last_reading=_read_decimal(str(row[34]) if row[34] is not None else None),
                        usage_delta=_read_decimal(str(row[35]) if row[35] is not None else None),
                        arrival_sequence=int(row[36]),
                    )
                target = MobileReadingImportTargetRecord(
                    planned_reading_id=str(row[0]),
                    measurement_definition_id=str(row[1]),
                    meter_id=str(row[2]),
                    location_id=str(row[3]),
                    business_date=_read_date(str(row[4])),
                    reading_session=ReadingSession(str(row[5])),
                    scheduled_at=parse_utc(str(row[6])),
                    task_state=PlannedReadingTaskState(str(row[7])),
                    close_reason=(
                        PlannedReadingCloseReason(str(row[8])) if row[8] is not None else None
                    ),
                    closed_at=parse_utc(str(row[9])) if row[9] is not None else None,
                    superseded_by_planned_reading_id=(
                        str(row[10]) if row[10] is not None else None
                    ),
                    location_name=str(row[11]),
                    meter_name=str(row[12]),
                    meter_is_active=bool(row[13]),
                    display_name=str(row[14]),
                    category=Category(str(row[15])),
                    unit=str(row[16]),
                    value_schema=ValueSchema(str(row[17])),
                    calculation_method=CalculationMethod(str(row[18])),
                    decimal_places=int(row[19]) if row[19] is not None else None,
                    warning_threshold=_read_decimal(str(row[20]) if row[20] is not None else None),
                    reading_cycle=ReadingCycle(str(row[21])),
                    purpose=Purpose(str(row[22])),
                    definition_is_active=bool(row[23]),
                    existing_result=existing_result,
                )
                targets[target.planned_reading_id] = target
        return targets

    def get_measurement_definition(
        self, measurement_definition_id: str
    ) -> MeasurementDefinitionRecord | None:
        row = self._conn.execute(
            "SELECT measurement_definition_id, value_schema, calculation_method, multiplier, "
            "decimal_places, rollover_value, location_id, reading_cycle, is_active, "
            "generation_start_date, display_name, category, unit, purpose, warning_threshold "
            "FROM measurement_definitions "
            "WHERE measurement_definition_id = ?",
            (measurement_definition_id,),
        ).fetchone()
        if row is None:
            return None
        multiplier = _read_decimal(str(row[3]))
        if multiplier is None:
            raise ValidationError("測定定義の倍率が NULL です。")
        assignment = self.get_meter_assignment(measurement_definition_id, self._now())
        return MeasurementDefinitionRecord(
            measurement_definition_id=str(row[0]),
            value_schema=ValueSchema(str(row[1])),
            calculation_method=CalculationMethod(str(row[2])),
            multiplier=assignment.multiplier if assignment is not None else multiplier,
            decimal_places=int(row[4]) if row[4] is not None else None,
            rollover_value=(
                assignment.rollover_value
                if assignment is not None
                else _read_decimal(str(row[5]) if row[5] is not None else None)
            ),
            location_id=str(row[6]),
            reading_cycle=ReadingCycle(str(row[7])),
            is_active=bool(row[8]),
            generation_start_date=_read_date(str(row[9])),
            display_name=str(row[10]),
            category=Category(str(row[11])),
            unit=str(row[12]),
            purpose=Purpose(str(row[13])),
            warning_threshold=_read_decimal(str(row[14]) if row[14] is not None else None),
            meter_id=assignment.meter_id if assignment is not None else None,
        )

    def list_measurement_definitions(
        self, *, show_inactive: bool = False
    ) -> list[MeasurementDefinitionRecord]:
        rows = self._conn.execute(
            "SELECT measurement_definition_id FROM measurement_definitions "
            "WHERE (? = 1 OR is_active = 1) "
            "ORDER BY location_id, display_name, measurement_definition_id",
            (int(show_inactive),),
        ).fetchall()
        definitions: list[MeasurementDefinitionRecord] = []
        for row in rows:
            definition = self.get_measurement_definition(str(row[0]))
            if definition is None:
                raise ValidationError(f"測定定義が見つかりません: {row[0]}")
            definitions.append(definition)
        return definitions

    def list_planned_reading_generation_definitions(
        self,
    ) -> list[PlannedReadingGenerationDefinitionRecord]:
        rows = self._conn.execute(
            "SELECT d.measurement_definition_id, d.location_id, d.reading_cycle, "
            "d.generation_start_date FROM measurement_definitions AS d "
            "WHERE d.is_active = 1 ORDER BY d.location_id, d.measurement_definition_id"
        ).fetchall()
        definitions: list[PlannedReadingGenerationDefinitionRecord] = []
        for row in rows:
            definitions.append(
                PlannedReadingGenerationDefinitionRecord(
                    measurement_definition_id=str(row[0]),
                    location_id=str(row[1]),
                    meter_id=None,
                    reading_cycle=ReadingCycle(str(row[2])),
                    generation_start_date=_read_date(str(row[3])),
                )
            )
        return definitions

    def get_planned_reading_generation_schedule(
        self,
        measurement_definition_id: str,
        reading_cycle: ReadingCycle,
        business_date: date,
    ) -> ScheduleSettings:
        definition_schedule = self.get_measurement_definition_schedule_for_date(
            measurement_definition_id, business_date
        )
        raw_schedule = (
            definition_schedule.schedule_json if definition_schedule is not None else None
        )
        if raw_schedule is None:
            default = self.get_reading_cycle_schedule_default(reading_cycle, business_date)
            raw_schedule = default.schedule_json if default is not None else None
        if raw_schedule is None:
            raise ScheduleHistoryResolutionError(
                measurement_definition_id,
                business_date.isoformat(),
                business_date.isoformat(),
                "適用可能なスケジュール履歴がありません",
            )
        try:
            return _deserialize_schedule(reading_cycle, raw_schedule)
        except ValidationError as error:
            raise ScheduleHistoryResolutionError(
                measurement_definition_id,
                (
                    definition_schedule.effective_from_date.isoformat()
                    if definition_schedule is not None
                    else business_date.isoformat()
                ),
                business_date.isoformat(),
                f"スケジュール履歴が不正です: {error}",
            ) from error

    def get_meter_id_for_planned_reading(
        self, measurement_definition_id: str, scheduled_at: datetime
    ) -> str:
        normalized_at = utc_now_str(scheduled_at)
        rows = self._conn.execute(
            "SELECT a.meter_id, m.is_active FROM meter_assignments AS a "
            "JOIN physical_meters AS m ON m.meter_id = a.meter_id "
            "WHERE a.measurement_definition_id = ? AND a.effective_from <= ? "
            "AND (a.effective_to IS NULL OR a.effective_to > ?) "
            "ORDER BY a.effective_from, a.meter_id",
            (measurement_definition_id, normalized_at, normalized_at),
        ).fetchall()
        scheduled_date = parse_utc(normalized_at).astimezone(self._business_timezone()).date()
        if not rows:
            raise MeterAssignmentResolutionError(
                measurement_definition_id,
                scheduled_date.isoformat(),
                scheduled_date.isoformat(),
                "予定日時に有効な物理メーター割当がありません",
            )
        if len(rows) != 1:
            raise MeterAssignmentResolutionError(
                measurement_definition_id,
                scheduled_date.isoformat(),
                scheduled_date.isoformat(),
                "予定日時に有効な物理メーター割当が複数あります",
            )
        if not bool(rows[0][1]):
            raise InactiveMasterError(
                f"無効な物理メーターを予定枠へ割り当てられません: {rows[0][0]}"
            )
        return str(rows[0][0])

    def list_measurement_definitions_without_active_assignment(
        self,
    ) -> list[MeasurementDefinitionWithoutActiveAssignmentRecord]:
        now = utc_now_str(self._now())
        rows = self._conn.execute(
            "SELECT d.measurement_definition_id FROM measurement_definitions AS d "
            "WHERE d.is_active = 1 AND NOT EXISTS (SELECT 1 FROM meter_assignments AS a "
            "JOIN physical_meters AS m ON m.meter_id = a.meter_id AND m.is_active = 1 "
            "WHERE a.measurement_definition_id = d.measurement_definition_id "
            "AND a.effective_from <= ? AND (a.effective_to IS NULL OR a.effective_to > ?)) "
            "ORDER BY d.location_id, d.measurement_definition_id",
            (now, now),
        ).fetchall()
        return [MeasurementDefinitionWithoutActiveAssignmentRecord(str(row[0])) for row in rows]

    def create_planned_reading_if_missing(self, request: PlannedReadingCreateRequest) -> bool:
        return self.create_planned_readings_if_missing([request]) == 1

    def create_planned_readings_if_missing(
        self, requests: list[PlannedReadingCreateRequest]
    ) -> int:
        inserted = 0
        seen: set[str] = set()
        for request in requests:
            if request.planned_reading_id in seen:
                continue
            seen.add(request.planned_reading_id)
            existing = self._conn.execute(
                "SELECT 1 FROM planned_readings WHERE planned_reading_id = ?",
                (request.planned_reading_id,),
            ).fetchone()
            if existing is not None:
                continue
            meter = self.get_meter_master(request.meter_id)
            if meter is None or not meter.is_active:
                raise InactiveMasterError(
                    f"有効な物理メーターを予定枠へ指定してください: {request.meter_id}"
                )
            definition = self._conn.execute(
                "SELECT is_active FROM measurement_definitions WHERE measurement_definition_id = ?",
                (request.measurement_definition_id,),
            ).fetchone()
            if definition is not None and not bool(definition[0]):
                raise InactiveMasterError(
                    "無効な測定定義の新しい予定枠は生成できません: "
                    f"{request.measurement_definition_id}"
                )
            cursor = self._execute(
                "INSERT INTO planned_readings (planned_reading_id, measurement_definition_id, "
                "meter_id, location_id, business_date, release_date, "
                "reading_session, scheduled_at, "
                "task_state) VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'open') "
                "ON CONFLICT(planned_reading_id) DO NOTHING",
                (
                    request.planned_reading_id,
                    request.measurement_definition_id,
                    request.meter_id,
                    request.location_id,
                    _date_text(request.business_date),
                    _date_text(request.release_date),
                    request.reading_session.value,
                    utc_now_str(request.scheduled_at),
                ),
            )
            inserted += cursor.rowcount
        return inserted

    def get_measurement_definition_schedule(
        self, measurement_definition_id: str, business_date: date | None = None
    ) -> str | None:
        effective_date = business_date or self._business_date()
        row = self._conn.execute(
            "SELECT schedule_json FROM measurement_definition_schedules "
            "WHERE measurement_definition_id = ? AND effective_from_date <= ? "
            "ORDER BY effective_from_date DESC LIMIT 1",
            (measurement_definition_id, _date_text(effective_date)),
        ).fetchone()
        return str(row[0]) if row is not None and row[0] is not None else None

    def upsert_measurement_definition_schedule(
        self,
        measurement_definition_id: str,
        schedule_json: str | None,
        *,
        effective_from_date: date,
        created_at: datetime,
    ) -> None:
        self._execute(
            "INSERT INTO measurement_definition_schedules "
            "(measurement_definition_id, effective_from_date, schedule_json, created_at) "
            "VALUES (?, ?, ?, ?) ON CONFLICT(measurement_definition_id, effective_from_date) "
            "DO UPDATE SET schedule_json = excluded.schedule_json, "
            "created_at = excluded.created_at",
            (
                measurement_definition_id,
                _date_text(effective_from_date),
                schedule_json,
                utc_now_str(created_at),
            ),
        )

    def list_measurement_definition_schedule_history(
        self, measurement_definition_id: str
    ) -> list[MeasurementDefinitionScheduleRecord]:
        rows = self._conn.execute(
            "SELECT measurement_definition_id, effective_from_date, schedule_json, created_at "
            "FROM measurement_definition_schedules WHERE measurement_definition_id = ? "
            "ORDER BY effective_from_date",
            (measurement_definition_id,),
        ).fetchall()
        return [self._record_schedule(row) for row in rows]

    def get_measurement_definition_schedule_for_date(
        self, measurement_definition_id: str, business_date: date
    ) -> MeasurementDefinitionScheduleRecord | None:
        row = self._conn.execute(
            "SELECT measurement_definition_id, effective_from_date, schedule_json, created_at "
            "FROM measurement_definition_schedules WHERE measurement_definition_id = ? "
            "AND effective_from_date <= ? ORDER BY effective_from_date DESC LIMIT 1",
            (measurement_definition_id, _date_text(business_date)),
        ).fetchone()
        return self._record_schedule(row) if row is not None else None

    def get_reading_cycle_schedule_default(
        self, reading_cycle: ReadingCycle, business_date: date
    ) -> ReadingCycleScheduleDefaultRecord | None:
        row = self._conn.execute(
            "SELECT reading_cycle, effective_from_date, schedule_json, created_at "
            "FROM reading_cycle_schedule_defaults WHERE reading_cycle = ? "
            "AND effective_from_date <= ? ORDER BY effective_from_date DESC LIMIT 1",
            (reading_cycle.value, _date_text(business_date)),
        ).fetchone()
        return (
            ReadingCycleScheduleDefaultRecord(
                ReadingCycle(str(row[0])),
                _read_date(str(row[1])),
                str(row[2]),
                parse_utc(str(row[3])),
            )
            if row is not None
            else None
        )

    def list_reading_cycle_schedule_defaults(
        self, reading_cycle: ReadingCycle
    ) -> list[ReadingCycleScheduleDefaultRecord]:
        rows = self._conn.execute(
            "SELECT reading_cycle, effective_from_date, schedule_json, created_at "
            "FROM reading_cycle_schedule_defaults WHERE reading_cycle = ? "
            "ORDER BY effective_from_date",
            (reading_cycle.value,),
        ).fetchall()
        return [
            ReadingCycleScheduleDefaultRecord(
                ReadingCycle(str(row[0])),
                _read_date(str(row[1])),
                str(row[2]),
                parse_utc(str(row[3])),
            )
            for row in rows
        ]

    def save_reading_cycle_schedule_default(
        self,
        reading_cycle: ReadingCycle,
        effective_from_date: date,
        schedule_json: str,
        *,
        created_at: datetime,
    ) -> None:
        self._execute(
            "INSERT INTO reading_cycle_schedule_defaults "
            "(reading_cycle, effective_from_date, schedule_json, created_at) VALUES (?, ?, ?, ?) "
            "ON CONFLICT(reading_cycle, effective_from_date) DO UPDATE SET "
            "schedule_json = excluded.schedule_json, created_at = excluded.created_at",
            (
                reading_cycle.value,
                _date_text(effective_from_date),
                schedule_json,
                utc_now_str(created_at),
            ),
        )

    def list_measurement_definitions_using_cycle_default(
        self, reading_cycle: ReadingCycle, from_business_date: date
    ) -> list[str]:
        rows = self._conn.execute(
            "SELECT d.measurement_definition_id FROM measurement_definitions AS d "
            "WHERE d.reading_cycle = ? AND d.is_active = 1 AND EXISTS ("
            "SELECT 1 FROM planned_readings AS p "
            "WHERE p.measurement_definition_id = d.measurement_definition_id "
            "AND p.business_date >= ? AND ("
            "SELECT s.schedule_json FROM measurement_definition_schedules AS s "
            "WHERE s.measurement_definition_id = d.measurement_definition_id "
            "AND s.effective_from_date <= p.business_date "
            "ORDER BY s.effective_from_date DESC LIMIT 1"
            ") IS NULL) ORDER BY d.measurement_definition_id",
            (reading_cycle.value, _date_text(from_business_date)),
        ).fetchall()
        return [str(row[0]) for row in rows]

    def get_reading_session_release_offset(
        self, reading_session: ReadingSession, business_date: date
    ) -> ReadingSessionReleaseOffsetRecord | None:
        row = self._conn.execute(
            "SELECT reading_session, effective_from_date, offset_days, created_at "
            "FROM reading_session_release_offsets WHERE reading_session = ? "
            "AND effective_from_date <= ? ORDER BY effective_from_date DESC LIMIT 1",
            (reading_session.value, _date_text(business_date)),
        ).fetchone()
        return (
            ReadingSessionReleaseOffsetRecord(
                ReadingSession(str(row[0])),
                _read_date(str(row[1])),
                int(row[2]),
                parse_utc(str(row[3])),
            )
            if row is not None
            else None
        )

    def list_reading_session_release_offsets(
        self, reading_session: ReadingSession
    ) -> list[ReadingSessionReleaseOffsetRecord]:
        rows = self._conn.execute(
            "SELECT reading_session, effective_from_date, offset_days, created_at "
            "FROM reading_session_release_offsets WHERE reading_session = ? "
            "ORDER BY effective_from_date",
            (reading_session.value,),
        ).fetchall()
        return [
            ReadingSessionReleaseOffsetRecord(
                ReadingSession(str(row[0])),
                _read_date(str(row[1])),
                int(row[2]),
                parse_utc(str(row[3])),
            )
            for row in rows
        ]

    def save_reading_session_release_offset(
        self,
        reading_session: ReadingSession,
        effective_from_date: date,
        offset_days: int,
        *,
        created_at: datetime,
    ) -> None:
        self._execute(
            "INSERT INTO reading_session_release_offsets "
            "(reading_session, effective_from_date, offset_days, created_at) VALUES (?, ?, ?, ?) "
            "ON CONFLICT(reading_session, effective_from_date) DO UPDATE SET "
            "offset_days = excluded.offset_days, created_at = excluded.created_at",
            (
                reading_session.value,
                _date_text(effective_from_date),
                offset_days,
                utc_now_str(created_at),
            ),
        )

    def get_setting(self, key: str) -> str | None:
        row = self._conn.execute(
            "SELECT setting_value FROM meter_app_settings WHERE setting_key = ?", (key,)
        ).fetchone()
        return str(row[0]) if row else None

    def upsert_setting(self, key: str, value: str) -> None:
        self._execute(
            "INSERT INTO meter_app_settings (setting_key, setting_value, updated_at) "
            "VALUES (?, ?, ?) ON CONFLICT(setting_key) DO UPDATE SET "
            "setting_value = excluded.setting_value, updated_at = excluded.updated_at",
            (key, value, utc_now_str(self._now())),
        )

    def has_planned_readings(self) -> bool:
        row = self._conn.execute("SELECT EXISTS(SELECT 1 FROM planned_readings)").fetchone()
        return bool(row and row[0])

    def get_meter_master(self, meter_id: str) -> MeterMasterRecord | None:
        row = self._conn.execute(
            "SELECT meter_id, location_id, location_name, meter_name, category, meter_serial, "
            "is_active FROM physical_meters WHERE meter_id = ?",
            (meter_id,),
        ).fetchone()
        return self._meter_record(row) if row is not None else None

    def list_meter_masters(self, *, include_inactive: bool = False) -> list[MeterMasterRecord]:
        sql = (
            "SELECT meter_id, location_id, location_name, meter_name, category, meter_serial, "
            "is_active FROM physical_meters"
        )
        if not include_inactive:
            sql += " WHERE is_active = 1"
        sql += " ORDER BY location_id, meter_id"
        return [self._meter_record(row) for row in self._conn.execute(sql).fetchall()]

    @staticmethod
    def _location_records(rows: list[tuple[Any, ...]]) -> list[MeterLocationRecord]:
        grouped: dict[str, set[str]] = {}
        for location_id, location_name in rows:
            grouped.setdefault(str(location_id), set()).add(str(location_name))
        return [
            MeterLocationRecord(
                location_id=location_id,
                location_name=sorted(names)[0],
                location_names=tuple(sorted(names)),
            )
            for location_id, names in sorted(grouped.items())
        ]

    def list_meter_locations(self) -> list[MeterLocationRecord]:
        rows = self._conn.execute(
            "SELECT location_id, location_name FROM physical_meters "
            "GROUP BY location_id, location_name ORDER BY location_id, location_name"
        ).fetchall()
        return self._location_records(rows)

    def get_meter_location(self, location_id: str) -> MeterLocationRecord | None:
        rows = self._conn.execute(
            "SELECT location_id, location_name FROM physical_meters WHERE location_id = ? "
            "GROUP BY location_id, location_name ORDER BY location_name",
            (location_id,),
        ).fetchall()
        records = self._location_records(rows)
        return records[0] if records else None

    def rename_meter_location(self, location_id: str, location_name: str) -> None:
        cursor = self._execute(
            "UPDATE physical_meters SET location_name = ? WHERE location_id = ?",
            (location_name, location_id),
        )
        if cursor.rowcount == 0:
            raise ValidationError(f"場所が見つかりません: {location_id}")

    @staticmethod
    def _meter_record(row: tuple[Any, ...]) -> MeterMasterRecord:
        return MeterMasterRecord(
            meter_id=str(row[0]),
            location_id=str(row[1]),
            location_name=str(row[2]),
            meter_name=str(row[3]),
            category=Category(str(row[4])),
            meter_serial=str(row[5]) if row[5] is not None else None,
            is_active=bool(row[6]),
        )

    def create_meter_master(self, request: MeterMasterCreateRequest) -> MeterMasterRecord:
        location = self.get_meter_location(request.location_id)
        if location is not None and location.location_name != request.location_name:
            raise ValidationError(f"同一場所の名称が一致しません: {request.location_id}")
        self._execute(
            "INSERT INTO physical_meters "
            "(meter_id, location_id, location_name, meter_name, category, meter_serial, is_active) "
            "VALUES (?, ?, ?, ?, ?, ?, 1)",
            (
                request.meter_id,
                request.location_id,
                request.location_name,
                request.meter_name,
                request.category.value,
                request.meter_serial,
            ),
        )
        result = self.get_meter_master(request.meter_id)
        if result is None:
            raise ValidationError("登録した物理メーターを取得できません。")
        return result

    def update_meter_master(
        self, meter_id: str, request: MeterMasterUpdateRequest
    ) -> MeterMasterRecord:
        cursor = self._execute(
            "UPDATE physical_meters SET meter_name = ?, category = ?, meter_serial = ? "
            "WHERE meter_id = ?",
            (
                request.meter_name,
                request.category.value,
                request.meter_serial,
                meter_id,
            ),
        )
        if cursor.rowcount == 0:
            raise ValidationError(f"物理メーターが見つかりません: {meter_id}")
        result = self.get_meter_master(meter_id)
        if result is None:
            raise ValidationError(f"更新した物理メーターを取得できません: {meter_id}")
        return result

    def get_meter_master_usage(self, meter_id: str) -> MeterMasterUsageRecord:
        now = utc_now_str(self._now())
        has_active_assignment = self._conn.execute(
            "SELECT EXISTS(SELECT 1 FROM measurement_definitions AS d "
            "JOIN meter_assignments AS a "
            "ON a.measurement_definition_id = d.measurement_definition_id "
            "WHERE d.is_active = 1 AND a.meter_id = ? AND a.effective_from <= ? "
            "AND (a.effective_to IS NULL OR a.effective_to > ?))",
            (meter_id, now, now),
        ).fetchone()
        has_open = self._conn.execute(
            "SELECT EXISTS(SELECT 1 FROM planned_readings "
            "WHERE meter_id = ? AND task_state = 'open')",
            (meter_id,),
        ).fetchone()
        return MeterMasterUsageRecord(
            has_active_assignment=bool(has_active_assignment and has_active_assignment[0]),
            has_open_planned_readings=bool(has_open and has_open[0]),
        )

    def set_meter_master_active(self, meter_id: str, is_active: bool) -> None:
        if not is_active:
            usage = self.get_meter_master_usage(meter_id)
            if usage.has_active_assignment or usage.has_open_planned_readings:
                raise MasterInUseError(f"使用中の物理メーターは無効化できません: {meter_id}")
        cursor = self._execute(
            "UPDATE physical_meters SET is_active = ? WHERE meter_id = ?",
            (int(is_active), meter_id),
        )
        if cursor.rowcount == 0:
            raise ValidationError(f"物理メーターが見つかりません: {meter_id}")

    def create_measurement_definition(self, request: MeasurementDefinitionCreateRequest) -> None:
        if request.initial_meter_id is not None:
            meter = self.get_meter_master(request.initial_meter_id)
            if meter is None or not meter.is_active:
                raise InactiveMasterError(
                    f"有効な物理メーターを指定してください: {request.initial_meter_id}"
                )
            if meter.location_id != request.location_id:
                raise ValidationError("測定定義と物理メーターの集計場所が一致しません。")

        self._execute(
            "INSERT INTO measurement_definitions "
            "(measurement_definition_id, location_id, meter_id, display_name, category, unit, "
            "value_schema, multiplier, decimal_places, warning_threshold, rollover_value, "
            "calculation_method, reading_cycle, purpose, generation_start_date, is_active) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 1)",
            (
                request.measurement_definition_id,
                request.location_id,
                request.initial_meter_id,
                request.display_name,
                request.category.value,
                request.unit,
                request.value_schema.value,
                _decimal_text(request.multiplier),
                request.decimal_places,
                _decimal_text(request.warning_threshold),
                _decimal_text(request.rollover_value),
                request.calculation_method.value,
                request.reading_cycle.value,
                request.purpose.value,
                _date_text(request.generation_start_date),
            ),
        )
        created_at = utc_now_str(self._now())
        self._execute(
            "INSERT INTO measurement_definition_schedules "
            "(measurement_definition_id, effective_from_date, schedule_json, created_at) "
            "VALUES (?, ?, ?, ?)",
            (
                request.measurement_definition_id,
                _date_text(request.generation_start_date),
                request.initial_schedule_json,
                created_at,
            ),
        )
        if request.initial_meter_id is not None:
            self._execute(
                "INSERT INTO meter_assignments "
                "(measurement_definition_id, meter_id, effective_from, effective_to, "
                "multiplier, rollover_value) VALUES (?, ?, ?, NULL, ?, ?)",
                (
                    request.measurement_definition_id,
                    request.initial_meter_id,
                    self._midnight_utc(request.generation_start_date),
                    _decimal_text(request.multiplier),
                    _decimal_text(request.rollover_value),
                ),
            )

    def update_measurement_definition(
        self,
        measurement_definition_id: str,
        request: MeasurementDefinitionUpdateRequest,
    ) -> None:
        immutable = self._conn.execute(
            "SELECT location_id, reading_cycle, value_schema, calculation_method "
            "FROM measurement_definitions WHERE measurement_definition_id = ?",
            (measurement_definition_id,),
        ).fetchone()
        if immutable is None:
            raise ValidationError(f"測定定義が見つかりません: {measurement_definition_id}")
        protected_values = (
            (request.location_id, str(immutable[0]), "集計場所"),
            (
                request.reading_cycle.value if request.reading_cycle is not None else None,
                str(immutable[1]),
                "検針周期",
            ),
            (
                request.value_schema.value if request.value_schema is not None else None,
                str(immutable[2]),
                "値形式",
            ),
            (
                request.calculation_method.value
                if request.calculation_method is not None
                else None,
                str(immutable[3]),
                "計算方法",
            ),
        )
        for requested, stored, label in protected_values:
            if requested is not None and requested != stored:
                raise ValidationError(f"作成後に{label}は変更できません。")

        try:
            purpose = Purpose(request.purpose).value
        except (TypeError, ValueError) as exc:
            raise ValidationError(
                "集計目的は operational / billing / audit のいずれかです。"
            ) from exc
        cursor = self._execute(
            "UPDATE measurement_definitions SET display_name = ?, category = ?, unit = ?, "
            "decimal_places = ?, warning_threshold = ?, "
            "purpose = ? WHERE measurement_definition_id = ?",
            (
                request.display_name,
                request.category.value,
                request.unit,
                request.decimal_places,
                _decimal_text(request.warning_threshold),
                purpose,
                measurement_definition_id,
            ),
        )
        if cursor.rowcount == 0:
            raise ValidationError(f"測定定義が見つかりません: {measurement_definition_id}")

    def set_measurement_definition_active(
        self,
        measurement_definition_id: str,
        is_active: bool,
        *,
        generation_start_date: date | None = None,
    ) -> None:
        row = self._conn.execute(
            "SELECT generation_start_date, is_active FROM measurement_definitions "
            "WHERE measurement_definition_id = ?",
            (measurement_definition_id,),
        ).fetchone()
        if row is None:
            raise ValidationError(f"測定定義が見つかりません: {measurement_definition_id}")
        now = self._now()
        if is_active:
            current_start = _read_date(str(row[0]))
            requested_start = generation_start_date or self._business_date()
            next_start = max(current_start, requested_start, self._business_date())
            self._execute(
                "UPDATE measurement_definitions SET is_active = 1, generation_start_date = ? "
                "WHERE measurement_definition_id = ?",
                (_date_text(next_start), measurement_definition_id),
            )
        else:
            self._execute(
                "UPDATE measurement_definitions SET is_active = 0 "
                "WHERE measurement_definition_id = ?",
                (measurement_definition_id,),
            )
            self._execute(
                "UPDATE planned_readings SET task_state = 'superseded', closed_at = ?, "
                "close_reason = 'definition_deactivated' WHERE measurement_definition_id = ? "
                "AND task_state = 'open' AND business_date >= ?",
                (
                    utc_now_str(now),
                    measurement_definition_id,
                    _date_text(self._business_date()),
                ),
            )

    def get_meter_assignment(
        self, measurement_definition_id: str, at: datetime
    ) -> MeterAssignmentRecord | None:
        timestamp = utc_now_str(at)
        rows = self._conn.execute(
            "SELECT measurement_definition_id, meter_id, effective_from, effective_to, "
            "multiplier, rollover_value, EXISTS (SELECT 1 FROM assignment_review_flags AS f "
            "WHERE f.measurement_definition_id = meter_assignments.measurement_definition_id "
            "AND f.meter_id = meter_assignments.meter_id "
            "AND f.assignment_effective_from = meter_assignments.effective_from) "
            "FROM meter_assignments WHERE measurement_definition_id = ? "
            "AND effective_from <= ? AND (effective_to IS NULL OR effective_to > ?) "
            "ORDER BY effective_from, meter_id",
            (measurement_definition_id, timestamp, timestamp),
        ).fetchall()
        if len(rows) > 1:
            local_date = at.astimezone(self._business_timezone()).date().isoformat()
            raise MeterAssignmentResolutionError(
                measurement_definition_id,
                local_date,
                local_date,
                "対象日時のメーター割当が複数あります",
            )
        return self._assignment_record(rows[0]) if rows else None

    @staticmethod
    def _assignment_record(row: tuple[Any, ...]) -> MeterAssignmentRecord:
        multiplier = _read_decimal(str(row[4]))
        rollover_value = _read_decimal(str(row[5]) if row[5] is not None else None)
        if multiplier is None or multiplier <= 0:
            raise ValidationError("割当の倍率は有限の正数である必要があります。")
        if rollover_value is not None and rollover_value <= 0:
            raise ValidationError("割当のロールオーバー値は有限の正数である必要があります。")
        return MeterAssignmentRecord(
            measurement_definition_id=str(row[0]),
            meter_id=str(row[1]),
            effective_from=parse_utc(str(row[2])),
            effective_to=parse_utc(str(row[3])) if row[3] is not None else None,
            multiplier=multiplier,
            rollover_value=rollover_value,
            needs_review=bool(row[6]) if len(row) > 6 else False,
        )

    def get_meter_assignment_by_key(
        self, measurement_definition_id: str, meter_id: str, effective_from: datetime
    ) -> MeterAssignmentRecord | None:
        row = self._conn.execute(
            "SELECT measurement_definition_id, meter_id, effective_from, effective_to, "
            "multiplier, rollover_value, EXISTS (SELECT 1 FROM assignment_review_flags AS f "
            "WHERE f.measurement_definition_id = meter_assignments.measurement_definition_id "
            "AND f.meter_id = meter_assignments.meter_id "
            "AND f.assignment_effective_from = meter_assignments.effective_from) "
            "FROM meter_assignments "
            "WHERE measurement_definition_id = ? AND meter_id = ? AND effective_from = ?",
            (measurement_definition_id, meter_id, utc_now_str(effective_from)),
        ).fetchone()
        return self._assignment_record(row) if row is not None else None

    def list_meter_assignments(self, measurement_definition_id: str) -> list[MeterAssignmentRecord]:
        rows = self._conn.execute(
            "SELECT measurement_definition_id, meter_id, effective_from, effective_to, "
            "multiplier, rollover_value, EXISTS (SELECT 1 FROM assignment_review_flags AS f "
            "WHERE f.measurement_definition_id = meter_assignments.measurement_definition_id "
            "AND f.meter_id = meter_assignments.meter_id "
            "AND f.assignment_effective_from = meter_assignments.effective_from) "
            "FROM meter_assignments WHERE measurement_definition_id = ? ORDER BY effective_from",
            (measurement_definition_id,),
        ).fetchall()
        return [self._assignment_record(row) for row in rows]

    def list_assignment_review_flags(self) -> list[AssignmentReviewFlagRecord]:
        rows = self._conn.execute(
            "SELECT measurement_definition_id, meter_id, assignment_effective_from, "
            "reason, created_at FROM assignment_review_flags "
            "ORDER BY measurement_definition_id, assignment_effective_from"
        ).fetchall()
        return [
            AssignmentReviewFlagRecord(
                measurement_definition_id=str(row[0]),
                meter_id=str(row[1]),
                assignment_effective_from=parse_utc(str(row[2])),
                reason=str(row[3]),
                created_at=parse_utc(str(row[4])),
            )
            for row in rows
        ]

    def clear_assignment_review_flag(self, assignment: MeterAssignmentRecord) -> None:
        self._execute(
            "DELETE FROM assignment_review_flags WHERE measurement_definition_id = ? "
            "AND meter_id = ? AND assignment_effective_from = ?",
            (
                assignment.measurement_definition_id,
                assignment.meter_id,
                utc_now_str(assignment.effective_from),
            ),
        )

    def list_assignment_measured_readings(
        self, assignment: MeterAssignmentRecord
    ) -> list[AssignmentMeasuredReadingRecord]:
        until = (
            utc_now_str(assignment.effective_to) if assignment.effective_to is not None else None
        )
        rows = self._conn.execute(
            "SELECT p.planned_reading_id, r.raw_reading, r.last_reading, r.usage_delta "
            "FROM planned_readings p JOIN reading_transactions r USING (planned_reading_id) "
            "WHERE p.measurement_definition_id = ? AND p.meter_id = ? "
            "AND p.scheduled_at >= ? AND (? IS NULL OR p.scheduled_at < ?) "
            "AND r.result_status = 'measured' ORDER BY p.scheduled_at, p.planned_reading_id",
            (
                assignment.measurement_definition_id,
                assignment.meter_id,
                utc_now_str(assignment.effective_from),
                until,
                until,
            ),
        ).fetchall()
        return [
            AssignmentMeasuredReadingRecord(
                planned_reading_id=str(row[0]),
                raw_reading=_read_decimal(str(row[1]) if row[1] is not None else None),
                last_reading=_read_decimal(str(row[2]) if row[2] is not None else None),
                usage_delta=_read_decimal(str(row[3]) if row[3] is not None else None),
            )
            for row in rows
        ]

    def update_assignment_calculation(self, assignment: MeterAssignmentRecord) -> None:
        cursor = self._execute(
            "UPDATE meter_assignments SET multiplier = ?, rollover_value = ? "
            "WHERE measurement_definition_id = ? AND meter_id = ? AND effective_from = ?",
            (
                _decimal_text(assignment.multiplier),
                _decimal_text(assignment.rollover_value),
                assignment.measurement_definition_id,
                assignment.meter_id,
                utc_now_str(assignment.effective_from),
            ),
        )
        if cursor.rowcount != 1:
            raise ValidationError("訂正対象のメーター割当が見つかりません。")

    def save_assignment_calculation_correction(
        self, correction: AssignmentCalculationCorrectionRecord
    ) -> None:
        self._execute(
            "INSERT INTO assignment_calculation_corrections "
            "(correction_id, measurement_definition_id, meter_id, assignment_effective_from, "
            "old_multiplier, old_rollover_value, new_multiplier, new_rollover_value, "
            "staff_id, staff_name, corrected_at, reason) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                correction.correction_id,
                correction.measurement_definition_id,
                correction.meter_id,
                utc_now_str(correction.assignment_effective_from),
                _decimal_text(correction.old_multiplier),
                _decimal_text(correction.old_rollover_value),
                _decimal_text(correction.new_multiplier),
                _decimal_text(correction.new_rollover_value),
                correction.staff_id,
                correction.staff_name,
                utc_now_str(correction.corrected_at),
                correction.reason,
            ),
        )

    def list_assignment_calculation_corrections(
        self, assignment: MeterAssignmentRecord
    ) -> list[AssignmentCalculationCorrectionRecord]:
        rows = self._conn.execute(
            "SELECT correction_id, measurement_definition_id, meter_id, assignment_effective_from, "
            "old_multiplier, old_rollover_value, new_multiplier, new_rollover_value, "
            "staff_id, staff_name, corrected_at, reason FROM assignment_calculation_corrections "
            "WHERE measurement_definition_id = ? AND meter_id = ? "
            "AND assignment_effective_from = ? "
            "ORDER BY corrected_at, correction_id",
            (
                assignment.measurement_definition_id,
                assignment.meter_id,
                utc_now_str(assignment.effective_from),
            ),
        ).fetchall()
        result: list[AssignmentCalculationCorrectionRecord] = []
        for row in rows:
            old_multiplier = _read_decimal(str(row[4]))
            new_multiplier = _read_decimal(str(row[6]))
            if old_multiplier is None or new_multiplier is None:
                raise ValidationError("計算条件の訂正履歴に倍率がありません。")
            result.append(
                AssignmentCalculationCorrectionRecord(
                    correction_id=str(row[0]),
                    measurement_definition_id=str(row[1]),
                    meter_id=str(row[2]),
                    assignment_effective_from=parse_utc(str(row[3])),
                    old_multiplier=old_multiplier,
                    old_rollover_value=_read_decimal(str(row[5]) if row[5] is not None else None),
                    new_multiplier=new_multiplier,
                    new_rollover_value=_read_decimal(str(row[7]) if row[7] is not None else None),
                    staff_id=str(row[8]),
                    staff_name=str(row[9]),
                    corrected_at=parse_utc(str(row[10])),
                    reason=str(row[11]),
                )
            )
        return result

    def set_initial_meter_assignment(
        self, measurement_definition_id: str, meter_id: str, effective_from: datetime
    ) -> None:
        meter = self.get_meter_master(meter_id)
        if meter is None or not meter.is_active:
            raise InactiveMasterError(f"有効な物理メーターを指定してください: {meter_id}")
        definition = self._conn.execute(
            "SELECT location_id, multiplier, rollover_value FROM measurement_definitions "
            "WHERE measurement_definition_id = ? AND is_active = 1",
            (measurement_definition_id,),
        ).fetchone()
        if definition is None:
            raise ValidationError(f"有効な測定定義が見つかりません: {measurement_definition_id}")
        if str(definition[0]) != meter.location_id:
            raise ValidationError("測定定義と物理メーターの集計場所が一致しません。")
        if self._conn.execute(
            "SELECT EXISTS(SELECT 1 FROM meter_assignments WHERE measurement_definition_id = ?)",
            (measurement_definition_id,),
        ).fetchone()[0]:
            raise ValidationError("初期割当は既存割当がない測定定義にのみ設定できます。")
        self._execute(
            "INSERT INTO meter_assignments "
            "(measurement_definition_id, meter_id, effective_from, effective_to, "
            "multiplier, rollover_value) VALUES (?, ?, ?, NULL, ?, ?)",
            (
                measurement_definition_id,
                meter_id,
                utc_now_str(effective_from),
                str(definition[1]),
                str(definition[2]) if definition[2] is not None else None,
            ),
        )
        self._execute(
            "UPDATE measurement_definitions SET meter_id = ? WHERE measurement_definition_id = ?",
            (meter_id, measurement_definition_id),
        )

    @staticmethod
    def _staff_numeric_id(staff_id: str) -> int:
        try:
            return int(staff_id)
        except ValueError as exc:
            raise ValidationError("担当者 ID は整数で指定してください。") from exc

    @staticmethod
    def _staff_record(row: tuple[Any, ...]) -> StaffRecord:
        return StaffRecord(str(row[0]), str(row[1]), bool(row[2]))

    def list_staff(self, *, include_inactive: bool = False) -> list[StaffRecord]:
        sql = "SELECT id, name, is_active FROM staff"
        if not include_inactive:
            sql += " WHERE is_active = 1"
        sql += " ORDER BY name, id"
        return [self._staff_record(row) for row in self._conn.execute(sql).fetchall()]

    def get_staff(self, staff_id: str) -> StaffRecord | None:
        row = self._conn.execute(
            "SELECT id, name, is_active FROM staff WHERE id = ?",
            (self._staff_numeric_id(staff_id),),
        ).fetchone()
        return self._staff_record(row) if row is not None else None

    def create_staff(self, request: StaffCreateRequest) -> StaffRecord:
        cursor = self._execute(
            "INSERT INTO staff (name, is_active) VALUES (?, 1)", (request.display_name,)
        )
        if cursor.lastrowid is None or cursor.lastrowid <= 0:
            raise ValidationError("担当者 ID を採番できません。")
        result = self.get_staff(str(cursor.lastrowid))
        if result is None:
            raise ValidationError("登録した担当者を取得できません。")
        return result

    def update_staff(self, staff_id: str, request: StaffUpdateRequest) -> StaffRecord:
        cursor = self._execute(
            "UPDATE staff SET name = ? WHERE id = ?",
            (request.display_name, self._staff_numeric_id(staff_id)),
        )
        if cursor.rowcount == 0:
            raise ValidationError(f"担当者が見つかりません: {staff_id}")
        result = self.get_staff(staff_id)
        if result is None:
            raise ValidationError(f"更新した担当者を取得できません: {staff_id}")
        return result

    def set_staff_active(self, staff_id: str, is_active: bool) -> None:
        cursor = self._execute(
            "UPDATE staff SET is_active = ? WHERE id = ?",
            (int(is_active), self._staff_numeric_id(staff_id)),
        )
        if cursor.rowcount == 0:
            raise ValidationError(f"担当者が見つかりません: {staff_id}")

    def list_meter_master_data(self) -> list[MeterMasterData]:
        rows = self._conn.execute(
            "SELECT meter_id, location_id, location_name, meter_name, category, meter_serial "
            "FROM physical_meters WHERE is_active = 1 ORDER BY location_id, meter_id"
        ).fetchall()

        return [
            MeterMasterData(
                meter_id=str(row[0]),
                location_id=str(row[1]),
                location_name=str(row[2]),
                meter_name=str(row[3]),
                category=Category(str(row[4])),
                meter_serial=str(row[5]) if row[5] is not None else None,
            )
            for row in rows
        ]

    def list_measurement_definition_data(self) -> list[MeasurementDefinitionData]:
        rows = self._conn.execute(
            "SELECT d.measurement_definition_id, d.location_id, d.display_name, d.category, "
            "d.unit, d.value_schema, "
            "(SELECT a.multiplier FROM meter_assignments AS a "
            "WHERE a.measurement_definition_id = d.measurement_definition_id "
            "AND a.effective_from <= ? AND (a.effective_to IS NULL OR a.effective_to > ?) "
            "ORDER BY a.effective_from DESC LIMIT 1), "
            "d.decimal_places, d.warning_threshold, "
            "(SELECT a.rollover_value FROM meter_assignments AS a "
            "WHERE a.measurement_definition_id = d.measurement_definition_id "
            "AND a.effective_from <= ? AND (a.effective_to IS NULL OR a.effective_to > ?) "
            "ORDER BY a.effective_from DESC LIMIT 1), "
            "d.calculation_method, d.reading_cycle, d.purpose, "
            "(SELECT a.meter_id FROM meter_assignments AS a "
            "JOIN physical_meters AS m ON m.meter_id = a.meter_id AND m.is_active = 1 "
            "WHERE a.measurement_definition_id = d.measurement_definition_id "
            "AND a.effective_from <= ? AND (a.effective_to IS NULL OR a.effective_to > ?) "
            "ORDER BY a.effective_from DESC LIMIT 1), "
            "(SELECT rt.raw_reading FROM reading_transactions AS rt "
            "JOIN planned_readings AS p ON p.planned_reading_id = rt.planned_reading_id "
            "WHERE p.measurement_definition_id = d.measurement_definition_id "
            "AND p.meter_id = (SELECT a.meter_id FROM meter_assignments AS a "
            "WHERE a.measurement_definition_id = d.measurement_definition_id "
            "AND a.effective_from <= ? AND (a.effective_to IS NULL OR a.effective_to > ?) "
            "ORDER BY a.effective_from DESC LIMIT 1) "
            "AND rt.result_status = 'measured' AND rt.raw_reading IS NOT NULL "
            "ORDER BY p.scheduled_at DESC, p.planned_reading_id DESC LIMIT 1) "
            ", d.multiplier, d.rollover_value "
            "FROM measurement_definitions AS d WHERE d.is_active = 1 "
            "ORDER BY d.location_id, d.measurement_definition_id",
            tuple(utc_now_str(self._now()) for _ in range(8)),
        ).fetchall()
        result: list[MeasurementDefinitionData] = []
        for row in rows:
            has_active_assignment = row[13] is not None
            multiplier_value = row[6] if has_active_assignment else row[15]
            rollover_value = row[9] if has_active_assignment else row[16]
            multiplier = _read_decimal(str(multiplier_value))
            if multiplier is None:
                raise ValidationError("測定定義の倍率が NULL です。")
            result.append(
                MeasurementDefinitionData(
                    measurement_definition_id=str(row[0]),
                    location_id=str(row[1]),
                    meter_id=str(row[13]) if row[13] is not None else None,
                    display_name=str(row[2]),
                    category=Category(str(row[3])),
                    unit=str(row[4]),
                    value_schema=ValueSchema(str(row[5])),
                    last_reading=_read_decimal(str(row[14]) if row[14] is not None else None),
                    multiplier=multiplier,
                    decimal_places=int(row[7]) if row[7] is not None else None,
                    warning_threshold=_read_decimal(str(row[8]) if row[8] is not None else None),
                    rollover_value=_read_decimal(
                        str(rollover_value) if rollover_value is not None else None
                    ),
                    calculation_method=CalculationMethod(str(row[10])),
                    reading_cycle=ReadingCycle(str(row[11])),
                    purpose=Purpose(str(row[12])),
                )
            )
        return result

    def list_planned_reading_logical_slots(
        self,
        measurement_definition_id: str,
        from_business_date: date,
        through_business_date: date,
    ) -> list[PlannedReadingLogicalSlotRecord]:
        rows = self._conn.execute(
            "SELECT measurement_definition_id, business_date, reading_session, "
            "planned_reading_id, task_state, close_reason, superseded_by_planned_reading_id "
            "FROM planned_readings WHERE measurement_definition_id = ? "
            "AND business_date >= ? AND business_date <= ? "
            "ORDER BY business_date, reading_session, planned_reading_id",
            (
                measurement_definition_id,
                _date_text(from_business_date),
                _date_text(through_business_date),
            ),
        ).fetchall()
        return [
            PlannedReadingLogicalSlotRecord(
                measurement_definition_id=str(row[0]),
                business_date=_read_date(str(row[1])),
                reading_session=ReadingSession(str(row[2])),
                planned_reading_id=str(row[3]),
                task_state=PlannedReadingTaskState(str(row[4])),
                close_reason=PlannedReadingCloseReason(str(row[5])) if row[5] else None,
                superseded_by_planned_reading_id=str(row[6]) if row[6] else None,
            )
            for row in rows
        ]

    def list_reading_overview(self, business_date: date) -> list[ReadingOverviewRecord]:
        rows = self._conn.execute(
            "SELECT p.planned_reading_id, p.measurement_definition_id, p.meter_id, "
            "p.location_id, m.location_name, m.meter_name, d.display_name, d.category, "
            "d.reading_cycle, d.unit, p.business_date, p.reading_session, p.scheduled_at, "
            "p.task_state, p.close_reason, p.superseded_by_planned_reading_id, "
            "r.result_status, r.raw_reading, r.structured_payload_json, r.last_reading, "
            "r.usage_delta, d.warning_threshold, r.recorded_at, r.note "
            "FROM planned_readings AS p "
            "JOIN physical_meters AS m ON m.meter_id = p.meter_id "
            "JOIN measurement_definitions AS d "
            "ON d.measurement_definition_id = p.measurement_definition_id "
            "LEFT JOIN reading_transactions AS r ON r.planned_reading_id = p.planned_reading_id "
            "WHERE p.business_date = ? ORDER BY p.scheduled_at, p.planned_reading_id",
            (_date_text(business_date),),
        ).fetchall()
        return [
            ReadingOverviewRecord(
                planned_reading_id=str(row[0]),
                measurement_definition_id=str(row[1]),
                meter_id=str(row[2]),
                location_id=str(row[3]),
                location_name=str(row[4]),
                meter_name=str(row[5]),
                display_name=str(row[6]),
                category=Category(str(row[7])),
                reading_cycle=ReadingCycle(str(row[8])),
                unit=str(row[9]),
                business_date=_read_date(str(row[10])),
                reading_session=ReadingSession(str(row[11])),
                scheduled_at=parse_utc(str(row[12])),
                task_state=PlannedReadingTaskState(str(row[13])),
                close_reason=PlannedReadingCloseReason(str(row[14])) if row[14] else None,
                superseded_by_planned_reading_id=str(row[15]) if row[15] is not None else None,
                result_status=ResultStatus(str(row[16])) if row[16] is not None else None,
                raw_reading=_read_decimal(str(row[17]) if row[17] is not None else None),
                structured_payload_json=str(row[18]) if row[18] is not None else None,
                last_reading=_read_decimal(str(row[19]) if row[19] is not None else None),
                usage_delta=_read_decimal(str(row[20]) if row[20] is not None else None),
                warning_threshold=_read_decimal(str(row[21]) if row[21] is not None else None),
                recorded_at=parse_utc(str(row[22])) if row[22] is not None else None,
                note=str(row[23]) if row[23] is not None else None,
            )
            for row in rows
        ]

    @staticmethod
    def _task_record(row: tuple[Any, ...]) -> PlannedReadingTask:
        return PlannedReadingTask(
            planned_reading_id=str(row[0]),
            measurement_definition_id=str(row[1]),
            meter_id=str(row[2]),
            location_id=str(row[3]),
            business_date=_read_date(str(row[4])),
            release_date=_read_date(str(row[5])),
            reading_session=ReadingSession(str(row[6])),
            scheduled_at=parse_utc(str(row[7])),
            reading_cycle=ReadingCycle(str(row[8])),
            required=True,
        )

    def _list_open_tasks(self, where_sql: str, value: str) -> list[PlannedReadingTask]:
        rows = self._conn.execute(
            "SELECT p.planned_reading_id, p.measurement_definition_id, p.meter_id, p.location_id, "
            "p.business_date, p.release_date, p.reading_session, p.scheduled_at, d.reading_cycle "
            "FROM planned_readings AS p JOIN measurement_definitions AS d "
            "ON d.measurement_definition_id = p.measurement_definition_id "
            f"WHERE {where_sql} AND p.task_state = 'open' "
            "ORDER BY p.release_date, p.business_date, p.reading_session, p.location_id, "
            "p.measurement_definition_id, p.planned_reading_id",
            (value,),
        ).fetchall()
        return [self._task_record(row) for row in rows]

    def list_open_planned_reading_tasks_for_release_date(
        self, release_date: date
    ) -> list[PlannedReadingTask]:
        return self._list_open_tasks("p.release_date <= ?", _date_text(release_date))

    def list_open_planned_reading_tasks_for_business_date(
        self, business_date: date
    ) -> list[PlannedReadingTask]:
        return self._list_open_tasks("p.business_date = ?", _date_text(business_date))

    def list_open_planned_reading_tasks(self, business_date: date) -> list[PlannedReadingTask]:
        return self.list_open_planned_reading_tasks_for_business_date(business_date)

    def list_planned_reading_tasks_for_sheet(
        self,
        business_date: date,
        task_states: Collection[PlannedReadingTaskState],
    ) -> list[SheetPlannedReadingRecord]:
        states = sorted({state.value for state in task_states})
        if not states:
            return []
        placeholders = ", ".join("?" for _ in states)
        rows = self._conn.execute(
            "SELECT p.planned_reading_id, p.measurement_definition_id, p.meter_id, p.location_id, "
            "p.business_date, p.reading_session, p.scheduled_at, d.reading_cycle, p.task_state "
            "FROM planned_readings AS p JOIN measurement_definitions AS d "
            "ON d.measurement_definition_id = p.measurement_definition_id "
            f"WHERE p.business_date = ? AND p.task_state IN ({placeholders}) "
            "ORDER BY p.release_date, p.business_date, p.reading_session, p.location_id, "
            "p.measurement_definition_id, p.planned_reading_id",
            (_date_text(business_date), *states),
        ).fetchall()
        return [
            SheetPlannedReadingRecord(
                planned_reading_id=str(row[0]),
                measurement_definition_id=str(row[1]),
                meter_id=str(row[2]),
                location_id=str(row[3]),
                business_date=_read_date(str(row[4])),
                reading_session=ReadingSession(str(row[5])),
                scheduled_at=parse_utc(str(row[6])),
                reading_cycle=ReadingCycle(str(row[7])),
                task_state=PlannedReadingTaskState(str(row[8])),
            )
            for row in rows
        ]

    def list_sheet_meters(self, meter_ids: Collection[str]) -> list[MeterMasterData]:
        result: list[MeterMasterData] = []
        for chunk in _id_chunks(meter_ids):
            placeholders = ", ".join("?" for _ in chunk)
            rows = self._conn.execute(
                "SELECT meter_id, location_id, location_name, meter_name, category, meter_serial "
                f"FROM physical_meters WHERE meter_id IN ({placeholders}) "
                "ORDER BY location_id, meter_id",
                chunk,
            ).fetchall()
            result.extend(
                MeterMasterData(
                    meter_id=str(row[0]),
                    location_id=str(row[1]),
                    location_name=str(row[2]),
                    meter_name=str(row[3]),
                    category=Category(str(row[4])),
                    meter_serial=str(row[5]) if row[5] is not None else None,
                )
                for row in rows
            )
        return sorted(result, key=lambda meter: (meter.location_id, meter.meter_id))

    def list_sheet_measurement_definitions(
        self, measurement_definition_ids: Collection[str]
    ) -> list[MeasurementDefinitionData]:
        result: list[MeasurementDefinitionData] = []
        for chunk in _id_chunks(measurement_definition_ids):
            placeholders = ", ".join("?" for _ in chunk)
            rows = self._conn.execute(
                "SELECT measurement_definition_id, location_id, display_name, category, unit, "
                "value_schema, multiplier, decimal_places, warning_threshold, rollover_value, "
                "calculation_method, reading_cycle, purpose FROM measurement_definitions "
                f"WHERE measurement_definition_id IN ({placeholders})",
                chunk,
            ).fetchall()
            for row in rows:
                multiplier = _read_decimal(str(row[6]))
                if multiplier is None:
                    raise ValidationError("測定定義の倍率が NULL です。")
                result.append(
                    MeasurementDefinitionData(
                        measurement_definition_id=str(row[0]),
                        location_id=str(row[1]),
                        display_name=str(row[2]),
                        category=Category(str(row[3])),
                        unit=str(row[4]),
                        value_schema=ValueSchema(str(row[5])),
                        multiplier=multiplier,
                        decimal_places=int(row[7]) if row[7] is not None else None,
                        warning_threshold=_read_decimal(
                            str(row[8]) if row[8] is not None else None
                        ),
                        rollover_value=_read_decimal(str(row[9]) if row[9] is not None else None),
                        calculation_method=CalculationMethod(str(row[10])),
                        reading_cycle=ReadingCycle(str(row[11])),
                        purpose=Purpose(str(row[12])),
                    )
                )
        return sorted(
            result,
            key=lambda definition: (definition.location_id, definition.measurement_definition_id),
        )

    def auto_close_open_planned_readings(
        self,
        through_business_date: date,
        *,
        closed_at: datetime,
        close_reason: PlannedReadingCloseReason,
    ) -> int:
        cursor = self._execute(
            "UPDATE planned_readings SET task_state = 'auto_closed', "
            "closed_at = ?, close_reason = ? "
            "WHERE task_state = 'open' AND business_date <= ?",
            (utc_now_str(closed_at), close_reason.value, _date_text(through_business_date)),
        )
        return cursor.rowcount

    def list_planned_reading_recomposition_candidates(
        self,
        measurement_definition_id: str,
        from_business_date: date,
    ) -> list[PlannedReadingRecompositionRecord]:
        rows = self._conn.execute(
            "SELECT planned_reading_id, measurement_definition_id, meter_id, location_id, "
            "business_date, release_date, reading_session, scheduled_at, task_state, close_reason, "
            "superseded_by_planned_reading_id FROM planned_readings "
            "WHERE measurement_definition_id = ? AND business_date >= ? "
            "ORDER BY business_date, reading_session, planned_reading_id",
            (measurement_definition_id, _date_text(from_business_date)),
        ).fetchall()
        return [
            PlannedReadingRecompositionRecord(
                planned_reading_id=str(row[0]),
                measurement_definition_id=str(row[1]),
                meter_id=str(row[2]),
                location_id=str(row[3]),
                business_date=_read_date(str(row[4])),
                release_date=_read_date(str(row[5])),
                reading_session=ReadingSession(str(row[6])),
                scheduled_at=parse_utc(str(row[7])),
                task_state=PlannedReadingTaskState(str(row[8])),
                close_reason=PlannedReadingCloseReason(str(row[9])) if row[9] else None,
                superseded_by_planned_reading_id=str(row[10]) if row[10] else None,
            )
            for row in rows
        ]

    def supersede_planned_readings(
        self, planned_reading_ids: list[str], *, superseded_at: datetime
    ) -> int:
        if not planned_reading_ids:
            return 0
        placeholders = ",".join("?" for _ in planned_reading_ids)
        cursor = self._execute(
            "UPDATE planned_readings SET task_state = 'superseded', closed_at = ?, "
            "close_reason = 'schedule_recomposed' WHERE task_state = 'open' "
            f"AND planned_reading_id IN ({placeholders})",
            (utc_now_str(superseded_at), *planned_reading_ids),
        )
        return cursor.rowcount

    def get_latest_measured_reading(
        self,
        measurement_definition_id: str,
        meter_id: str,
        *,
        before_scheduled_at: datetime,
        before_planned_reading_id: str | None = None,
        excluding_planned_reading_id: str | None = None,
    ) -> HistoricalMeasuredReadingRecord | None:
        boundary = utc_now_str(before_scheduled_at)
        comparison_sql = "p.scheduled_at < ?"
        parameters: list[object] = [measurement_definition_id, meter_id, boundary]
        if before_planned_reading_id is not None:
            comparison_sql = (
                "(p.scheduled_at < ? OR (p.scheduled_at = ? AND p.planned_reading_id < ?))"
            )
            parameters.extend([boundary, before_planned_reading_id])
        if excluding_planned_reading_id is not None:
            parameters.append(excluding_planned_reading_id)
        exclude_sql = (
            " AND p.planned_reading_id != ?" if excluding_planned_reading_id is not None else ""
        )
        row = self._conn.execute(
            "SELECT p.planned_reading_id, p.meter_id, p.scheduled_at, "
            "rt.raw_reading, rt.recorded_at "
            "FROM planned_readings AS p JOIN reading_transactions AS rt "
            "ON rt.planned_reading_id = p.planned_reading_id "
            "WHERE p.measurement_definition_id = ? AND p.meter_id = ? "
            "AND rt.result_status = 'measured' AND rt.raw_reading IS NOT NULL AND "
            + comparison_sql
            + exclude_sql
            + " ORDER BY p.scheduled_at DESC, p.planned_reading_id DESC LIMIT 1",
            tuple(parameters),
        ).fetchone()
        if row is None:
            return None
        raw_reading = _read_decimal(str(row[3]))
        if raw_reading is None:
            return None
        return HistoricalMeasuredReadingRecord(
            planned_reading_id=str(row[0]),
            raw_reading=raw_reading,
            recorded_at=parse_utc(str(row[4])),
            meter_id=str(row[1]),
            scheduled_at=parse_utc(str(row[2])),
        )

    def list_following_measured_readings(
        self,
        measurement_definition_id: str,
        meter_id: str,
        *,
        after_scheduled_at: datetime,
        after_planned_reading_id: str | None = None,
        excluding_planned_reading_id: str | None = None,
    ) -> list[HistoricalMeasuredReadingRecord]:
        boundary = utc_now_str(after_scheduled_at)
        comparison_sql = "p.scheduled_at > ?"
        parameters: list[object] = [measurement_definition_id, meter_id, boundary]
        if after_planned_reading_id is not None:
            comparison_sql = (
                "(p.scheduled_at > ? OR (p.scheduled_at = ? AND p.planned_reading_id > ?))"
            )
            parameters.extend([boundary, after_planned_reading_id])
        exclude_sql = ""
        if excluding_planned_reading_id is not None:
            exclude_sql = " AND p.planned_reading_id != ?"
            parameters.append(excluding_planned_reading_id)
        rows = self._conn.execute(
            "SELECT p.planned_reading_id, p.meter_id, p.scheduled_at, "
            "rt.raw_reading, rt.recorded_at "
            "FROM planned_readings AS p JOIN reading_transactions AS rt "
            "ON rt.planned_reading_id = p.planned_reading_id "
            "WHERE p.measurement_definition_id = ? AND p.meter_id = ? "
            "AND rt.result_status = 'measured' AND rt.raw_reading IS NOT NULL AND "
            + comparison_sql
            + exclude_sql
            + " ORDER BY p.scheduled_at, p.planned_reading_id",
            tuple(parameters),
        ).fetchall()
        result: list[HistoricalMeasuredReadingRecord] = []
        for row in rows:
            value = _read_decimal(str(row[3]))
            if value is not None:
                result.append(
                    HistoricalMeasuredReadingRecord(
                        planned_reading_id=str(row[0]),
                        raw_reading=value,
                        recorded_at=parse_utc(str(row[4])),
                        meter_id=str(row[1]),
                        scheduled_at=parse_utc(str(row[2])),
                    )
                )
        return result

    def list_measured_readings_on_or_after(
        self,
        measurement_definition_id: str,
        meter_id: str,
        *,
        from_scheduled_at: datetime,
    ) -> list[HistoricalMeasuredReadingRecord]:
        rows = self._conn.execute(
            "SELECT p.planned_reading_id, p.meter_id, p.scheduled_at, "
            "rt.raw_reading, rt.recorded_at "
            "FROM planned_readings AS p JOIN reading_transactions AS rt "
            "ON rt.planned_reading_id = p.planned_reading_id "
            "WHERE p.measurement_definition_id = ? AND p.meter_id = ? "
            "AND rt.result_status = 'measured' AND rt.raw_reading IS NOT NULL "
            "AND p.scheduled_at >= ? "
            "ORDER BY p.scheduled_at, p.planned_reading_id",
            (
                measurement_definition_id,
                meter_id,
                utc_now_str(from_scheduled_at),
            ),
        ).fetchall()
        result: list[HistoricalMeasuredReadingRecord] = []
        for row in rows:
            raw_reading = _read_decimal(str(row[3]))
            if raw_reading is not None:
                result.append(
                    HistoricalMeasuredReadingRecord(
                        planned_reading_id=str(row[0]),
                        raw_reading=raw_reading,
                        recorded_at=parse_utc(str(row[4])),
                        meter_id=str(row[1]),
                        scheduled_at=parse_utc(str(row[2])),
                    )
                )
        return result

    @staticmethod
    def _reading_content_matches(row: tuple[Any, ...], request: ReadingUpsertRequest) -> bool:
        stored_payload = normalize_stored_structured_payload(
            str(row[4]) if row[4] is not None else None
        )
        existing_header = ComparableReadingHeader(
            result_status=str(row[0]),
            recorded_at=str(row[1]),
            client_updated_at=str(row[2]),
        )
        incoming_header = ComparableReadingHeader(
            result_status=request.result_status.value,
            recorded_at=utc_now_str(request.recorded_at),
            client_updated_at=utc_now_str(request.client_updated_at),
        )
        # ヘッダー不一致時は既存読値をデコードしない従来の判定順を保つ。
        if existing_header != incoming_header:
            return False
        return reading_content_matches(
            ComparableReadingContent(
                header=existing_header,
                raw_reading=_read_decimal(str(row[3]) if row[3] is not None else None),
                structured_payload_json=stored_payload,
                note=str(row[5]) if row[5] is not None else None,
            ),
            ComparableReadingContent(
                header=incoming_header,
                raw_reading=request.raw_reading,
                structured_payload_json=_json_text(request.structured_payload_json),
                note=request.note,
            ),
        )

    def _mark_planned_reading_completed(self, planned_reading_id: str, closed_at: datetime) -> None:
        self._execute(
            "UPDATE planned_readings SET task_state = 'completed', "
            "closed_at = ?, close_reason = NULL "
            "WHERE planned_reading_id = ? AND task_state IN ('open', 'auto_closed')",
            (utc_now_str(closed_at), planned_reading_id),
        )

    def upsert_reading(self, request: ReadingUpsertRequest) -> UpsertOutcome:
        task_state = self._conn.execute(
            "SELECT task_state FROM planned_readings WHERE planned_reading_id = ?",
            (request.planned_reading_id,),
        ).fetchone()
        if task_state is not None and str(task_state[0]) == PlannedReadingTaskState.SUPERSEDED:
            raise SupersededPlannedReadingError(request.planned_reading_id)
        recorded_at = utc_now_str(request.recorded_at)
        client_updated_at = utc_now_str(request.client_updated_at)
        raw_reading = _decimal_text(request.raw_reading)
        last_reading = _decimal_text(request.last_reading)
        usage_delta = _decimal_text(request.usage_delta)
        structured_json = _json_text(request.structured_payload_json)
        existing = self._conn.execute(
            "SELECT result_status, recorded_at, client_updated_at, raw_reading, "
            "structured_payload_json, note, arrival_sequence FROM reading_transactions "
            "WHERE planned_reading_id = ?",
            (request.planned_reading_id,),
        ).fetchone()
        if existing is None:
            self._execute(
                "INSERT INTO reading_transactions (planned_reading_id, result_status, recorded_at, "
                "client_updated_at, raw_reading, last_reading, usage_delta, "
                "structured_payload_json, "
                "device_id, updated_by, signature, note, result_source, arrival_sequence) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 1)",
                (
                    request.planned_reading_id,
                    request.result_status.value,
                    recorded_at,
                    client_updated_at,
                    raw_reading,
                    last_reading,
                    usage_delta,
                    structured_json,
                    request.device_id,
                    request.updated_by,
                    request.signature,
                    request.note,
                    request.result_source.value,
                ),
            )
            self._mark_planned_reading_completed(request.planned_reading_id, request.recorded_at)
            return UpsertOutcome(UpsertDisposition.INSERTED)

        is_mobile = request.result_source.value == "mobile_html"
        if is_mobile and self._reading_content_matches(existing, request):
            return UpsertOutcome(
                UpsertDisposition.IGNORED_DUPLICATE,
                detail="同一内容の検針結果を再受信しました。",
            )
        if is_mobile and client_updated_at < str(existing[2]):
            return UpsertOutcome(
                UpsertDisposition.IGNORED_STALE,
                detail="古い client_updated_at の入力を無視しました。",
            )
        same_client_time = client_updated_at == str(existing[2])
        disposition = (
            UpsertDisposition.REPLACED_ON_TIE
            if is_mobile and same_client_time
            else UpsertDisposition.UPDATED
        )
        self._execute(
            "UPDATE reading_transactions SET result_status = ?, recorded_at = ?, "
            "client_updated_at = ?, "
            "raw_reading = ?, last_reading = ?, usage_delta = ?, structured_payload_json = ?, "
            "device_id = ?, updated_by = ?, signature = ?, note = ?, result_source = ?, "
            "arrival_sequence = ? WHERE planned_reading_id = ?",
            (
                request.result_status.value,
                recorded_at,
                client_updated_at,
                raw_reading,
                last_reading,
                usage_delta,
                structured_json,
                request.device_id,
                request.updated_by,
                request.signature,
                request.note,
                request.result_source.value,
                int(existing[6]) + 1,
                request.planned_reading_id,
            ),
        )
        self._mark_planned_reading_completed(request.planned_reading_id, request.recorded_at)
        return UpsertOutcome(disposition)

    def save_calculated_usage(self, update: CalculatedUsageUpdate) -> None:
        cursor = self._execute(
            "UPDATE reading_transactions SET last_reading = ?, usage_delta = ? "
            "WHERE planned_reading_id = ?",
            (
                _decimal_text(update.last_reading),
                _decimal_text(update.usage_delta),
                update.planned_reading_id,
            ),
        )
        if cursor.rowcount == 0:
            raise ValidationError(
                f"再計算対象の検針結果が見つかりません: {update.planned_reading_id}"
            )

    def get_reading_client_updated_at(self, planned_reading_id: str) -> datetime | None:
        row = self._conn.execute(
            "SELECT client_updated_at FROM reading_transactions WHERE planned_reading_id = ?",
            (planned_reading_id,),
        ).fetchone()
        return parse_utc(str(row[0])) if row is not None else None

    def list_assignments_spanning_meter(
        self, meter_id: str, replaced_at: datetime
    ) -> list[MeterAssignmentRecord]:
        at = utc_now_str(replaced_at)
        rows = self._conn.execute(
            "SELECT measurement_definition_id, meter_id, effective_from, effective_to, "
            "multiplier, rollover_value, EXISTS (SELECT 1 FROM assignment_review_flags AS f "
            "WHERE f.measurement_definition_id = meter_assignments.measurement_definition_id "
            "AND f.meter_id = meter_assignments.meter_id "
            "AND f.assignment_effective_from = meter_assignments.effective_from) "
            "FROM meter_assignments "
            "WHERE meter_id = ? AND effective_from < ? "
            "AND (effective_to IS NULL OR effective_to > ?) "
            "ORDER BY measurement_definition_id, effective_from",
            (meter_id, at, at),
        ).fetchall()
        return [self._assignment_record(row) for row in rows]

    def has_meter_assignment_on_or_after(self, meter_id: str, at: datetime) -> bool:
        row = self._conn.execute(
            "SELECT EXISTS(SELECT 1 FROM meter_assignments "
            "WHERE meter_id = ? AND (effective_to IS NULL OR effective_to > ?))",
            (meter_id, utc_now_str(at)),
        ).fetchone()
        return bool(row and row[0])

    def has_later_assignment_or_replacement(
        self, measurement_definition_id: str, replaced_at: datetime
    ) -> bool:
        at = utc_now_str(replaced_at)
        row = self._conn.execute(
            "SELECT EXISTS(SELECT 1 FROM meter_assignments "
            "WHERE measurement_definition_id = ? AND effective_from > ?) OR "
            "EXISTS(SELECT 1 FROM replacement_boundaries "
            "WHERE measurement_definition_id = ? AND replaced_at > ?)",
            (measurement_definition_id, at, measurement_definition_id, at),
        ).fetchone()
        return bool(row and row[0])

    def list_replacement_affected_slots(
        self, measurement_definition_id: str, meter_id: str, replaced_at: datetime
    ) -> list[ReplacementAffectedSlot]:
        rows = self._conn.execute(
            "SELECT p.planned_reading_id, p.task_state, r.result_status, r.raw_reading, "
            "r.last_reading, r.usage_delta, r.structured_payload_json "
            "FROM planned_readings p LEFT JOIN reading_transactions r USING (planned_reading_id) "
            "WHERE p.measurement_definition_id = ? AND p.meter_id = ? AND p.scheduled_at >= ? "
            "ORDER BY p.scheduled_at, p.planned_reading_id",
            (measurement_definition_id, meter_id, utc_now_str(replaced_at)),
        ).fetchall()
        return [
            ReplacementAffectedSlot(
                planned_reading_id=str(row[0]),
                task_state=PlannedReadingTaskState(str(row[1])),
                result_status=ResultStatus(str(row[2])) if row[2] is not None else None,
                raw_reading=_read_decimal(str(row[3]) if row[3] is not None else None),
                last_reading=_read_decimal(str(row[4]) if row[4] is not None else None),
                usage_delta=_read_decimal(str(row[5]) if row[5] is not None else None),
                structured_payload_json=str(row[6]) if row[6] is not None else None,
            )
            for row in rows
        ]

    @staticmethod
    def _replacement_event_record(row: tuple[Any, ...]) -> ReplacementEventRecord:
        return ReplacementEventRecord(
            replacement_event_id=str(row[0]),
            location_id=str(row[1]),
            old_meter_id=str(row[2]),
            new_meter_id=str(row[3]),
            replaced_at=parse_utc(str(row[4])),
            old_final_reading=_read_decimal(str(row[5]) if row[5] is not None else None),
            new_initial_reading=_read_decimal(str(row[6]) if row[6] is not None else None),
            client_updated_at=parse_utc(str(row[7])),
            updated_by=str(row[8]),
            note=str(row[9]) if row[9] is not None else None,
            arrival_sequence=int(row[10]),
        )

    def get_replacement_event(self, replacement_event_id: str) -> ReplacementEventRecord | None:
        row = self._conn.execute(
            "SELECT replacement_event_id, location_id, old_meter_id, new_meter_id, replaced_at, "
            "old_final_reading, new_initial_reading, client_updated_at, updated_by, note, "
            "arrival_sequence FROM replacement_events WHERE replacement_event_id = ?",
            (replacement_event_id,),
        ).fetchone()
        return self._replacement_event_record(row) if row is not None else None

    def list_replacement_events(self, location_id: str) -> list[ReplacementEventRecord]:
        rows = self._conn.execute(
            "SELECT replacement_event_id, location_id, old_meter_id, new_meter_id, replaced_at, "
            "old_final_reading, new_initial_reading, client_updated_at, updated_by, note, "
            "arrival_sequence FROM replacement_events WHERE location_id = ? "
            "ORDER BY replaced_at DESC, replacement_event_id DESC",
            (location_id,),
        ).fetchall()
        return [self._replacement_event_record(row) for row in rows]

    def correct_replacement_event(self, request: ReplacementUpsertRequest) -> None:
        cursor = self._execute(
            "UPDATE replacement_events SET old_final_reading = NULL, new_initial_reading = NULL, "
            "client_updated_at = ?, updated_by = ?, note = ?, "
            "arrival_sequence = arrival_sequence + 1 "
            "WHERE replacement_event_id = ? AND location_id = ? AND old_meter_id = ? "
            "AND new_meter_id = ? AND replaced_at = ?",
            (
                utc_now_str(request.client_updated_at),
                request.updated_by,
                request.note,
                request.replacement_event_id,
                request.location_id,
                request.old_meter_id,
                request.new_meter_id,
                utc_now_str(request.replaced_at),
            ),
        )
        if cursor.rowcount != 1:
            raise ValidationError("訂正対象の交換イベントが見つかりません。")
        rows = self._conn.execute(
            "SELECT measurement_definition_id FROM replacement_boundaries "
            "WHERE replacement_event_id = ?",
            (request.replacement_event_id,),
        ).fetchall()
        definition_ids = {str(row[0]) for row in rows}
        readings = _replacement_reading_map(request, definition_ids)
        if not rows:
            raise ValidationError("訂正対象の交換境界がありません。")
        for definition_id, reading in readings.items():
            self._execute(
                "UPDATE replacement_boundaries SET old_final_reading = ?, "
                "new_initial_reading = ?, updated_at = ? "
                "WHERE replacement_event_id = ? AND measurement_definition_id = ?",
                (
                    _decimal_text(reading.old_final_reading),
                    _decimal_text(reading.new_initial_reading),
                    utc_now_str(self._now()),
                    request.replacement_event_id,
                    definition_id,
                ),
            )

    def upsert_replacement(self, request: ReplacementUpsertRequest) -> UpsertOutcome:
        client_updated_at = utc_now_str(request.client_updated_at)
        existing = self._conn.execute(
            "SELECT client_updated_at, arrival_sequence, location_id, old_meter_id, "
            "new_meter_id, replaced_at, note FROM replacement_events "
            "WHERE replacement_event_id = ?",
            (request.replacement_event_id,),
        ).fetchone()
        if existing is None:
            self._execute(
                "INSERT INTO replacement_events (replacement_event_id, location_id, old_meter_id, "
                "new_meter_id, replaced_at, old_final_reading, new_initial_reading, "
                "client_updated_at, updated_by, note, arrival_sequence) "
                "VALUES (?, ?, ?, ?, ?, NULL, NULL, ?, ?, ?, 1)",
                (
                    request.replacement_event_id,
                    request.location_id,
                    request.old_meter_id,
                    request.new_meter_id,
                    utc_now_str(request.replaced_at),
                    client_updated_at,
                    request.updated_by,
                    request.note,
                ),
            )
            return UpsertOutcome(UpsertDisposition.INSERTED)
        if client_updated_at < str(existing[0]):
            return UpsertOutcome(UpsertDisposition.IGNORED_STALE)
        if tuple(str(value) for value in existing[2:6]) != (
            request.location_id,
            request.old_meter_id,
            request.new_meter_id,
            utc_now_str(request.replaced_at),
        ):
            raise ValidationError("既存の交換イベントでは交換日時・旧新メーターを変更できません。")

        boundaries = self.list_replacement_boundaries_for_event(request.replacement_event_id)
        if not boundaries:
            raise ValidationError("訂正対象の交換境界がありません。")
        for boundary in boundaries:
            if (
                boundary.old_meter_id != request.old_meter_id
                or boundary.new_meter_id != request.new_meter_id
                or boundary.replaced_at != request.replaced_at
            ):
                raise ValidationError("交換イベントと交換境界の内容が一致しません。")
        readings = _replacement_reading_map(
            request, {boundary.measurement_definition_id for boundary in boundaries}
        )
        if (
            client_updated_at == str(existing[0])
            and request.note == existing[6]
            and all(
                readings[boundary.measurement_definition_id].old_final_reading
                == boundary.old_final_reading
                and readings[boundary.measurement_definition_id].new_initial_reading
                == boundary.new_initial_reading
                for boundary in boundaries
            )
        ):
            return UpsertOutcome(UpsertDisposition.IGNORED_DUPLICATE)

        disposition = (
            UpsertDisposition.REPLACED_ON_TIE
            if client_updated_at == str(existing[0])
            else UpsertDisposition.UPDATED
        )
        self._execute(
            "UPDATE replacement_events SET client_updated_at = ?, updated_by = ?, note = ?, "
            "arrival_sequence = ? WHERE replacement_event_id = ?",
            (
                client_updated_at,
                request.updated_by,
                request.note,
                int(existing[1]) + 1,
                request.replacement_event_id,
            ),
        )
        for definition_id, reading in readings.items():
            self._execute(
                "UPDATE replacement_boundaries SET old_final_reading = ?, "
                "new_initial_reading = ?, updated_at = ? "
                "WHERE replacement_event_id = ? AND measurement_definition_id = ?",
                (
                    _decimal_text(reading.old_final_reading),
                    _decimal_text(reading.new_initial_reading),
                    utc_now_str(self._now()),
                    request.replacement_event_id,
                    definition_id,
                ),
            )
        return UpsertOutcome(disposition)

    def get_replacement_boundary_for_new_meter_first_reading(
        self,
        measurement_definition_id: str,
        meter_id: str,
        *,
        before_scheduled_at: datetime,
        before_planned_reading_id: str | None = None,
        excluding_planned_reading_id: str | None = None,
    ) -> ReplacementBoundaryRecord | None:
        scheduled_at = utc_now_str(before_scheduled_at)
        target_order = (
            "(p.scheduled_at < ? OR (p.scheduled_at = ? AND p.planned_reading_id < ?))"
            if before_planned_reading_id is not None
            else "p.scheduled_at < ?"
        )
        params: list[object] = [measurement_definition_id, meter_id, scheduled_at]
        if before_planned_reading_id is not None:
            params.extend([scheduled_at, scheduled_at, before_planned_reading_id])
        else:
            params.append(scheduled_at)
        exclude_sql = ""
        if excluding_planned_reading_id is not None:
            exclude_sql = " AND p.planned_reading_id != ?"
            params.append(excluding_planned_reading_id)
        row = self._conn.execute(
            "SELECT b.replacement_event_id, b.measurement_definition_id, b.new_initial_reading, "
            "b.new_meter_id, b.replaced_at FROM replacement_boundaries AS b "
            "WHERE b.measurement_definition_id = ? AND b.new_meter_id = ? "
            "AND b.replaced_at <= ? AND NOT EXISTS ("
            "SELECT 1 FROM reading_transactions AS rt JOIN planned_readings AS p "
            "ON p.planned_reading_id = rt.planned_reading_id "
            "WHERE p.measurement_definition_id = b.measurement_definition_id "
            "AND p.meter_id = b.new_meter_id AND rt.result_status = 'measured' "
            "AND rt.raw_reading IS NOT NULL AND p.scheduled_at >= b.replaced_at AND "
            + target_order
            + exclude_sql
            + ") ORDER BY b.replaced_at DESC, b.replacement_event_id DESC LIMIT 1",
            tuple(params),
        ).fetchone()
        if row is None:
            return None
        return ReplacementBoundaryRecord(
            replacement_event_id=str(row[0]),
            measurement_definition_id=str(row[1]),
            new_initial_reading=_read_decimal(str(row[2]) if row[2] is not None else None),
            new_meter_id=str(row[3]),
            replaced_at=parse_utc(str(row[4])),
        )

    @staticmethod
    def _replacement_boundary_calculation_record(
        row: tuple[Any, ...],
    ) -> ReplacementBoundaryCalculationRecord:
        return ReplacementBoundaryCalculationRecord(
            replacement_event_id=str(row[0]),
            measurement_definition_id=str(row[1]),
            old_meter_id=str(row[2]),
            new_meter_id=str(row[3]),
            replaced_at=parse_utc(str(row[4])),
            old_final_reading=_read_decimal(str(row[5]) if row[5] is not None else None),
            new_initial_reading=_read_decimal(str(row[6]) if row[6] is not None else None),
            old_assignment_effective_from=parse_utc(str(row[7])),
            new_assignment_effective_from=parse_utc(str(row[8])),
            old_meter_usage_delta=_read_decimal(str(row[9]) if row[9] is not None else None),
            previous_measured_reading=_read_decimal(str(row[10]) if row[10] is not None else None),
            previous_measured_planned_reading_id=str(row[11]) if row[11] is not None else None,
        )

    def list_replacement_boundaries_for_event(
        self, replacement_event_id: str
    ) -> list[ReplacementBoundaryCalculationRecord]:
        rows = self._conn.execute(
            _REPLACEMENT_BOUNDARY_CALCULATION_SELECT + "WHERE replacement_event_id = ? "
            "ORDER BY measurement_definition_id",
            (replacement_event_id,),
        ).fetchall()
        return [self._replacement_boundary_calculation_record(row) for row in rows]

    def list_replacement_boundaries_for_old_meter(
        self, measurement_definition_id: str, old_meter_id: str
    ) -> list[ReplacementBoundaryCalculationRecord]:
        rows = self._conn.execute(
            _REPLACEMENT_BOUNDARY_CALCULATION_SELECT
            + "WHERE measurement_definition_id = ? AND old_meter_id = ? "
            "ORDER BY replaced_at, replacement_event_id",
            (measurement_definition_id, old_meter_id),
        ).fetchall()
        return [self._replacement_boundary_calculation_record(row) for row in rows]

    def list_replacement_boundaries_for_new_meter(
        self,
        measurement_definition_id: str,
        new_meter_id: str,
        *,
        before_scheduled_at: datetime,
    ) -> list[ReplacementBoundaryCalculationRecord]:
        rows = self._conn.execute(
            _REPLACEMENT_BOUNDARY_CALCULATION_SELECT
            + "WHERE measurement_definition_id = ? AND new_meter_id = ? AND replaced_at <= ? "
            "ORDER BY replaced_at DESC, replacement_event_id DESC",
            (measurement_definition_id, new_meter_id, utc_now_str(before_scheduled_at)),
        ).fetchall()
        return [self._replacement_boundary_calculation_record(row) for row in rows]

    def list_assignment_boundaries(
        self, assignment: MeterAssignmentRecord
    ) -> list[ReplacementBoundaryCalculationRecord]:
        rows = self._conn.execute(
            _REPLACEMENT_BOUNDARY_CALCULATION_SELECT + "WHERE measurement_definition_id = ? AND "
            "((old_meter_id = ? AND old_assignment_effective_from = ?) OR "
            "(new_meter_id = ? AND new_assignment_effective_from = ?)) "
            "ORDER BY replaced_at, replacement_event_id",
            (
                assignment.measurement_definition_id,
                assignment.meter_id,
                utc_now_str(assignment.effective_from),
                assignment.meter_id,
                utc_now_str(assignment.effective_from),
            ),
        ).fetchall()
        return [self._replacement_boundary_calculation_record(row) for row in rows]

    def update_replacement_boundary_calculation(
        self, update: ReplacementBoundaryCalculationUpdate
    ) -> None:
        cursor = self._execute(
            "UPDATE replacement_boundaries SET previous_measured_reading = ?, "
            "previous_measured_planned_reading_id = ?, old_meter_usage_delta = ?, updated_at = ? "
            "WHERE replacement_event_id = ? AND measurement_definition_id = ?",
            (
                _decimal_text(update.previous_measured_reading),
                update.previous_measured_planned_reading_id,
                _decimal_text(update.old_meter_usage_delta),
                utc_now_str(self._now()),
                update.replacement_event_id,
                update.measurement_definition_id,
            ),
        )
        if cursor.rowcount == 0:
            raise ValidationError(
                "更新対象の交換境界が見つかりません: "
                f"{update.replacement_event_id}/{update.measurement_definition_id}"
            )

    def reflect_replacement(self, request: ReplacementUpsertRequest) -> None:
        self._reflect_replacement(request, terms=None)

    def reflect_verified_replacement(
        self, request: ReplacementUpsertRequest, terms: dict[str, AssignmentCalculationTerms]
    ) -> None:
        self._reflect_replacement(request, terms=terms)

    def _reflect_replacement(
        self, request: ReplacementUpsertRequest, terms: dict[str, AssignmentCalculationTerms] | None
    ) -> None:
        new_meter = self.get_meter_master(request.new_meter_id)
        if new_meter is None or not new_meter.is_active:
            raise InactiveMasterError(
                f"有効な交換先物理メーターを指定してください: {request.new_meter_id}"
            )
        if new_meter.location_id != request.location_id:
            raise ValidationError("交換イベントと交換先メーターの集計場所が一致しません。")

        replaced_at = utc_now_str(request.replaced_at)
        definitions = self._conn.execute(
            "SELECT d.measurement_definition_id, d.location_id, d.reading_cycle, "
            "d.calculation_method, d.value_schema, a.effective_from, "
            "a.multiplier, a.rollover_value FROM measurement_definitions AS d "
            "JOIN meter_assignments AS a "
            "ON a.measurement_definition_id = d.measurement_definition_id "
            "WHERE "
            + ("d.is_active = 1 AND " if terms is None else "")
            + "d.location_id = ? AND a.meter_id = ? "
            "AND a.effective_from < ? AND (a.effective_to IS NULL OR a.effective_to > ?) "
            "ORDER BY d.measurement_definition_id",
            (request.location_id, request.old_meter_id, replaced_at, replaced_at),
        ).fetchall()
        definition_ids = {str(definition[0]) for definition in definitions}
        definition_readings = _replacement_reading_map(request, definition_ids)
        if terms is not None and set(terms) != {str(definition[0]) for definition in definitions}:
            raise ValidationError("交換対象と新しい計算条件が一致しません。")
        for definition in definitions:
            definition_id = str(definition[0])
            reading = definition_readings[definition_id]
            new_multiplier = (
                _decimal_text(terms[definition_id].multiplier)
                if terms is not None
                else definition[6]
            )
            new_rollover = (
                _decimal_text(terms[definition_id].rollover_value)
                if terms is not None
                else definition[7]
            )
            self._execute(
                "UPDATE meter_assignments SET effective_to = ? "
                "WHERE measurement_definition_id = ? AND meter_id = ? "
                "AND effective_from < ? AND (effective_to IS NULL OR effective_to > ?)",
                (replaced_at, definition_id, request.old_meter_id, replaced_at, replaced_at),
            )
            self._execute(
                "INSERT INTO meter_assignments "
                "(measurement_definition_id, meter_id, effective_from, effective_to, "
                "multiplier, rollover_value) VALUES (?, ?, ?, NULL, ?, ?) "
                "ON CONFLICT(measurement_definition_id, meter_id, effective_from) "
                "DO NOTHING",
                (definition_id, request.new_meter_id, replaced_at, new_multiplier, new_rollover),
            )
            self._execute(
                "UPDATE measurement_definitions SET meter_id = ? "
                "WHERE measurement_definition_id = ?",
                (request.new_meter_id, definition_id),
            )

            previous = self.get_latest_measured_reading(
                definition_id,
                request.old_meter_id,
                before_scheduled_at=request.replaced_at,
            )
            boundary_updated_at = utc_now_str(self._now())
            self._execute(
                "INSERT INTO replacement_boundaries "
                "(replacement_event_id, measurement_definition_id, location_id, business_date, "
                "old_meter_id, new_meter_id, replaced_at, old_assignment_effective_from, "
                "new_assignment_effective_from, previous_measured_reading, "
                "previous_measured_planned_reading_id, old_final_reading, old_meter_usage_delta, "
                "new_initial_reading, created_at, updated_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, "
                "NULL, ?, ?, ?) ON CONFLICT(replacement_event_id, measurement_definition_id) "
                "DO UPDATE SET location_id = excluded.location_id, "
                "business_date = excluded.business_date, "
                "old_meter_id = excluded.old_meter_id, new_meter_id = excluded.new_meter_id, "
                "replaced_at = excluded.replaced_at, "
                "previous_measured_reading = excluded.previous_measured_reading, "
                "previous_measured_planned_reading_id = "
                "excluded.previous_measured_planned_reading_id, "
                "old_final_reading = excluded.old_final_reading, "
                "new_initial_reading = excluded.new_initial_reading, "
                "updated_at = excluded.updated_at",
                (
                    request.replacement_event_id,
                    definition_id,
                    request.location_id,
                    _date_text(parse_utc(replaced_at).astimezone(self._business_timezone()).date()),
                    request.old_meter_id,
                    request.new_meter_id,
                    replaced_at,
                    str(definition[5]),
                    replaced_at,
                    _decimal_text(previous.raw_reading) if previous is not None else None,
                    previous.planned_reading_id if previous is not None else None,
                    _decimal_text(reading.old_final_reading),
                    _decimal_text(reading.new_initial_reading),
                    boundary_updated_at,
                    boundary_updated_at,
                ),
            )

            affected = self._conn.execute(
                "SELECT planned_reading_id, location_id, business_date, release_date, "
                "reading_session, scheduled_at, task_state FROM planned_readings "
                "WHERE measurement_definition_id = ? AND meter_id = ? AND scheduled_at >= ? "
                + (
                    "AND task_state IN ('open', 'auto_closed') "
                    if terms is None
                    else "AND task_state IN ('open', 'auto_closed', 'completed') "
                )
                + "ORDER BY scheduled_at, planned_reading_id",
                (definition_id, request.old_meter_id, replaced_at),
            ).fetchall()
            for planned in affected:
                old_id = str(planned[0])
                new_id = f"{old_id}-replacement-{request.replacement_event_id}"
                self._execute(
                    "INSERT INTO planned_readings (planned_reading_id, measurement_definition_id, "
                    "meter_id, location_id, business_date, release_date, "
                    "reading_session, scheduled_at, "
                    "task_state) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?) "
                    "ON CONFLICT(planned_reading_id) DO NOTHING",
                    (
                        new_id,
                        definition_id,
                        request.new_meter_id,
                        str(planned[1]),
                        str(planned[2]),
                        str(planned[3]),
                        str(planned[4]),
                        str(planned[5]),
                        (
                            "auto_closed"
                            if str(planned[6]) == PlannedReadingTaskState.AUTO_CLOSED.value
                            else "open"
                        ),
                    ),
                )
                successor = self._conn.execute(
                    "SELECT measurement_definition_id, meter_id, location_id, business_date, "
                    "release_date, reading_session, scheduled_at FROM planned_readings "
                    "WHERE planned_reading_id = ?",
                    (new_id,),
                ).fetchone()
                if successor is None or tuple(str(value) for value in successor) != (
                    definition_id,
                    request.new_meter_id,
                    str(planned[1]),
                    str(planned[2]),
                    str(planned[3]),
                    str(planned[4]),
                    str(planned[5]),
                ):
                    raise ValidationError(f"交換後の予定枠を一意に確定できません: {new_id}")
                self._execute(
                    "UPDATE planned_readings SET task_state = 'superseded', closed_at = ?, "
                    "close_reason = 'meter_replaced', superseded_by_planned_reading_id = ? "
                    "WHERE planned_reading_id = ? "
                    + (
                        "AND task_state IN ('open', 'auto_closed')"
                        if terms is None
                        else "AND task_state IN ('open', 'auto_closed', 'completed')"
                    ),
                    (utc_now_str(self._now()), new_id, old_id),
                )

    @staticmethod
    def _schedule_items(
        cycle: ReadingCycle, business_date: date, schedule: ScheduleSettings
    ) -> tuple[tuple[ReadingSession, int], ...]:
        if isinstance(schedule, DailySchedule):
            return tuple((item.reading_session, item.day_offset) for item in schedule.reading_items)
        if isinstance(schedule, WeeklySchedule):
            return (
                ((schedule.reading_session, schedule.day_offset),)
                if business_date.weekday() == schedule.weekday
                else ()
            )
        if isinstance(schedule, EveryTwoMonthsSchedule):
            if (business_date.month % 2 == 1) != (schedule.month_parity is MonthParity.ODD):
                return ()
            selector = MonthlySchedule(
                selector_type=schedule.selector_type,
                day_of_month=schedule.day_of_month,
                nth=schedule.nth,
                nth_weekday=schedule.nth_weekday,
                edge=schedule.edge,
                reading_session=schedule.reading_session,
                day_offset=schedule.day_offset,
            )
        else:
            selector = schedule

        if selector.selector_type is MonthlyScheduleSelectorType.DAY_OF_MONTH:
            if selector.day_of_month is None:
                raise ValidationError("月次スケジュールの日付がありません。")
            last_day = calendar.monthrange(business_date.year, business_date.month)[1]
            matches = business_date.day == min(selector.day_of_month, last_day)
        elif selector.selector_type is MonthlyScheduleSelectorType.NTH_WEEKDAY:
            if selector.nth is None or selector.nth_weekday is None:
                raise ValidationError("月次スケジュールの曜日指定がありません。")
            matches = (
                business_date.weekday() == selector.nth_weekday
                and (business_date.day - 1) // 7 + 1 == selector.nth
            )
        else:
            last_day = calendar.monthrange(business_date.year, business_date.month)[1]
            matches = (
                business_date.day == 1
                if selector.edge == "first"
                else business_date.day == last_day
            )
        return ((selector.reading_session, selector.day_offset),) if matches else ()

    def _scheduled_at(self, business_date: date, session: ReadingSession, day_offset: int) -> str:
        local_value = datetime.combine(
            business_date + timedelta(days=day_offset),
            time.fromisoformat(session.value),
            tzinfo=self._business_timezone(),
        )
        return utc_now_str(local_value)

    def _release_date(self, business_date: date, session: ReadingSession) -> date:
        offset = self.get_reading_session_release_offset(session, business_date)
        if offset is None:
            raise ScheduleHistoryResolutionError(
                session.value,
                business_date.isoformat(),
                business_date.isoformat(),
                "適用可能な配信オフセット履歴がありません",
            )
        return business_date + timedelta(days=offset.offset_days)

    def recompose_release_dates(
        self, reading_session: ReadingSession, from_business_date: date
    ) -> int:
        rows = self._conn.execute(
            "SELECT planned_reading_id, business_date, scheduled_at, release_date "
            "FROM planned_readings WHERE reading_session = ? AND task_state = 'open' "
            "ORDER BY business_date, planned_reading_id",
            (reading_session.value,),
        ).fetchall()
        changed_count = 0
        for row in rows:
            scheduled_at = parse_utc(str(row[2]))
            effective_business_date = scheduled_at.astimezone(self._business_timezone()).date()
            if effective_business_date < from_business_date:
                continue
            release_date = self._release_date(effective_business_date, reading_session)
            release_date_text = _date_text(release_date)
            if str(row[3]) == release_date_text:
                continue
            cursor = self._execute(
                "UPDATE planned_readings SET release_date = ? "
                "WHERE planned_reading_id = ? AND task_state = 'open'",
                (release_date_text, str(row[0])),
            )
            changed_count += cursor.rowcount
        return changed_count

    def recompose_future_planned_readings(
        self,
        measurement_definition_id: str,
        *,
        from_business_date: date,
    ) -> tuple[int, int]:
        definition = self._conn.execute(
            "SELECT location_id, reading_cycle, generation_start_date, is_active "
            "FROM measurement_definitions WHERE measurement_definition_id = ?",
            (measurement_definition_id,),
        ).fetchone()
        if definition is None:
            raise ValidationError(f"測定定義が見つかりません: {measurement_definition_id}")
        if not bool(definition[3]):
            return (0, 0)

        candidates = self.list_planned_reading_recomposition_candidates(
            measurement_definition_id, from_business_date
        )
        if not candidates:
            return (0, 0)
        through = max(candidate.business_date for candidate in candidates)
        start = max(from_business_date, _read_date(str(definition[2])))
        if start > through:
            return (0, 0)
        reading_cycle = ReadingCycle(str(definition[1]))
        location_id = str(definition[0])

        slots: dict[tuple[date, ReadingSession], list[PlannedReadingRecompositionRecord]] = {}
        for candidate in candidates:
            slots.setdefault((candidate.business_date, candidate.reading_session), []).append(
                candidate
            )

        desired: dict[tuple[date, ReadingSession], int] = {}
        day = start
        while day <= through:
            schedule = self.get_planned_reading_generation_schedule(
                measurement_definition_id, reading_cycle, day
            )
            for session, day_offset in self._schedule_items(reading_cycle, day, schedule):
                key = day, session
                if key in desired:
                    raise ScheduleHistoryResolutionError(
                        measurement_definition_id,
                        day.isoformat(),
                        day.isoformat(),
                        f"{session.value} が重複するスケジュールです",
                    )
                desired[key] = day_offset
            day += timedelta(days=1)

        now = utc_now_str(self._now())
        superseded = 0
        generated = 0
        for key in sorted(set(slots) | set(desired), key=lambda value: (value[0], value[1].value)):
            records = slots.get(key, [])
            current = self._resolve_current_logical_slot(records)
            day_offset = desired.get(key)

            if day_offset is not None:
                can_reopen = (
                    current is not None
                    and current.task_state is PlannedReadingTaskState.SUPERSEDED
                    and current.close_reason is PlannedReadingCloseReason.SCHEDULE_RECOMPOSED
                    and current.superseded_by_planned_reading_id is None
                )
                if (
                    current is not None
                    and current.task_state is not PlannedReadingTaskState.OPEN
                    and not can_reopen
                ):
                    continue
                scheduled = self._scheduled_at(key[0], key[1], day_offset)
                meter_id = self.get_meter_id_for_planned_reading(
                    measurement_definition_id, parse_utc(scheduled)
                )
                release_date = _date_text(
                    self._release_date(key[0] + timedelta(days=day_offset), key[1])
                )
                if current is None:
                    stable_id = (
                        f"plan-{measurement_definition_id}-{key[0].isoformat()}-"
                        f"{key[1].value.replace(':', '')}"
                    )
                    self._execute(
                        "INSERT INTO planned_readings "
                        "(planned_reading_id, measurement_definition_id, meter_id, location_id, "
                        "business_date, release_date, reading_session, scheduled_at, task_state) "
                        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'open')",
                        (
                            stable_id,
                            measurement_definition_id,
                            meter_id,
                            location_id,
                            _date_text(key[0]),
                            release_date,
                            key[1].value,
                            scheduled,
                        ),
                    )
                    generated += 1
                    continue
                if (
                    current.meter_id != meter_id
                    or current.location_id != location_id
                    or utc_now_str(current.scheduled_at) != scheduled
                    or current.release_date != date.fromisoformat(release_date)
                    or current.task_state is not PlannedReadingTaskState.OPEN
                    or current.close_reason is not None
                    or current.superseded_by_planned_reading_id is not None
                ):
                    self._execute(
                        "UPDATE planned_readings SET meter_id = ?, location_id = ?, "
                        "release_date = ?, scheduled_at = ?, task_state = 'open', "
                        "closed_at = NULL, "
                        "close_reason = NULL, superseded_by_planned_reading_id = NULL "
                        "WHERE planned_reading_id = ?",
                        (
                            meter_id,
                            location_id,
                            release_date,
                            scheduled,
                            current.planned_reading_id,
                        ),
                    )
                    generated += 1
                continue

            if current is not None and current.task_state is PlannedReadingTaskState.OPEN:
                cursor = self._execute(
                    "UPDATE planned_readings SET task_state = 'superseded', closed_at = ?, "
                    "close_reason = 'schedule_recomposed' WHERE planned_reading_id = ? "
                    "AND task_state = 'open'",
                    (now, current.planned_reading_id),
                )
                superseded += cursor.rowcount
        return superseded, generated

    @staticmethod
    def _resolve_current_logical_slot(
        records: list[PlannedReadingRecompositionRecord],
    ) -> PlannedReadingRecompositionRecord | None:
        if not records:
            return None
        by_id = {record.planned_reading_id: record for record in records}
        referenced_ids = {
            record.superseded_by_planned_reading_id
            for record in records
            if record.superseded_by_planned_reading_id is not None
        }
        roots = [record for record in records if record.planned_reading_id not in referenced_ids]
        if len(roots) != 1:
            raise ValidationError("論理予定枠の後継関係が不正です。")

        current = roots[0]
        visited: set[str] = set()
        while current.superseded_by_planned_reading_id is not None:
            if current.planned_reading_id in visited:
                raise ValidationError("論理予定枠の後継関係に循環があります。")
            visited.add(current.planned_reading_id)
            successor = by_id.get(current.superseded_by_planned_reading_id)
            if successor is None:
                raise ValidationError(
                    f"後継予定枠が見つかりません: {current.superseded_by_planned_reading_id}"
                )
            if (
                successor.business_date != current.business_date
                or successor.reading_session is not current.reading_session
                or successor.measurement_definition_id != current.measurement_definition_id
            ):
                raise ValidationError("論理予定枠の後継が異なる枠を指しています。")
            current = successor
        return current


__all__ = ["SQLiteRepository"]
