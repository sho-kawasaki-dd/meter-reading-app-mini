"""標準 sqlite3 を使う Repository 実装。"""

from __future__ import annotations

import calendar
import json
import sqlite3
from collections.abc import Callable
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
    ValueSchema,
)
from meter_reading_mini.core.errors import (
    InactiveMasterError,
    InvalidTimezoneError,
    MasterInUseError,
    ScheduleHistoryResolutionError,
    ValidationError,
)
from meter_reading_mini.core.meter_reading_sync_repositories import (
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
    MeterMasterCreateRequest,
    MeterMasterRecord,
    MeterMasterUpdateRequest,
    MeterMasterUsageRecord,
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
    ReadingSessionReleaseOffsetRecord,
    ReadingUpsertRequest,
    ReplacementBoundaryRecord,
    ReplacementUpsertRequest,
    ScheduleItem,
    ScheduleSettings,
    StaffCreateRequest,
    StaffRecord,
    StaffUpdateRequest,
    UpsertDisposition,
    UpsertOutcome,
    WeeklySchedule,
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


def _json_text(value: dict[str, Any] | None) -> str | None:
    if value is None:
        return None
    try:
        return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    except (TypeError, ValueError) as exc:
        raise ValidationError(f"構造化入力を JSON 化できません: {exc}") from exc


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

    def get_measurement_definition(
        self, measurement_definition_id: str
    ) -> MeasurementDefinitionRecord | None:
        row = self._conn.execute(
            "SELECT measurement_definition_id, value_schema, calculation_method, multiplier, "
            "decimal_places, rollover_value FROM measurement_definitions "
            "WHERE measurement_definition_id = ?",
            (measurement_definition_id,),
        ).fetchone()
        if row is None:
            return None
        multiplier = _read_decimal(str(row[3]))
        if multiplier is None:
            raise ValidationError("測定定義の倍率が NULL です。")
        return MeasurementDefinitionRecord(
            measurement_definition_id=str(row[0]),
            value_schema=ValueSchema(str(row[1])),
            calculation_method=CalculationMethod(str(row[2])),
            multiplier=multiplier,
            decimal_places=int(row[4]) if row[4] is not None else None,
            rollover_value=_read_decimal(str(row[5]) if row[5] is not None else None),
        )

    def get_latest_planned_business_date(self) -> date | None:
        row = self._conn.execute("SELECT MAX(business_date) FROM planned_readings").fetchone()
        return _read_date(str(row[0])) if row and row[0] is not None else None

    def list_planned_reading_generation_definitions(
        self,
    ) -> list[PlannedReadingGenerationDefinitionRecord]:
        now = utc_now_str(self._now())
        rows = self._conn.execute(
            "SELECT d.measurement_definition_id, d.location_id, a.meter_id, d.reading_cycle, "
            "d.generation_start_date FROM measurement_definitions AS d "
            "JOIN meter_assignments AS a "
            "ON a.measurement_definition_id = d.measurement_definition_id "
            "AND a.effective_from <= ? AND (a.effective_to IS NULL OR a.effective_to > ?) "
            "JOIN physical_meters AS m ON m.meter_id = a.meter_id AND m.is_active = 1 "
            "WHERE d.is_active = 1 ORDER BY d.location_id, d.measurement_definition_id",
            (now, now),
        ).fetchall()
        definitions: list[PlannedReadingGenerationDefinitionRecord] = []
        current_date = self._business_date()
        for row in rows:
            definition_id = str(row[0])
            cycle = ReadingCycle(str(row[3]))
            definition_schedule = self.get_measurement_definition_schedule_for_date(
                definition_id, current_date
            )
            raw_schedule = definition_schedule.schedule_json if definition_schedule else None
            if raw_schedule is None:
                default = self.get_reading_cycle_schedule_default(cycle, current_date)
                raw_schedule = default.schedule_json if default else None
            if raw_schedule is None:
                raise ScheduleHistoryResolutionError(
                    definition_id,
                    str(row[4]),
                    current_date.isoformat(),
                    "適用可能なスケジュール履歴がありません",
                )
            definitions.append(
                PlannedReadingGenerationDefinitionRecord(
                    measurement_definition_id=definition_id,
                    location_id=str(row[1]),
                    meter_id=str(row[2]),
                    reading_cycle=cycle,
                    generation_start_date=_read_date(str(row[4])),
                    schedule=_deserialize_schedule(cycle, raw_schedule),
                )
            )
        return definitions

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

    def get_measurement_definition_schedule(self, measurement_definition_id: str) -> str | None:
        row = self._conn.execute(
            "SELECT schedule_json FROM measurement_definition_schedules "
            "WHERE measurement_definition_id = ? ORDER BY effective_from_date DESC LIMIT 1",
            (measurement_definition_id,),
        ).fetchone()
        return str(row[0]) if row is not None and row[0] is not None else None

    def upsert_measurement_definition_schedule(
        self,
        measurement_definition_id: str,
        schedule_json: str,
        *,
        effective_from_date: date | None = None,
        created_at: datetime | None = None,
    ) -> None:
        self._execute(
            "INSERT INTO measurement_definition_schedules "
            "(measurement_definition_id, effective_from_date, schedule_json, created_at) "
            "VALUES (?, ?, ?, ?) ON CONFLICT(measurement_definition_id, effective_from_date) "
            "DO UPDATE SET schedule_json = excluded.schedule_json, "
            "created_at = excluded.created_at",
            (
                measurement_definition_id,
                _date_text(effective_from_date or self._business_date()),
                schedule_json,
                utc_now_str(created_at or self._now()),
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
            "UPDATE physical_meters SET location_name = ?, meter_name = ?, category = ?, "
            "meter_serial = ? WHERE meter_id = ?",
            (
                request.location_name,
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
                "(measurement_definition_id, meter_id, effective_from, effective_to) "
                "VALUES (?, ?, ?, NULL)",
                (
                    request.measurement_definition_id,
                    request.initial_meter_id,
                    self._midnight_utc(request.generation_start_date),
                ),
            )

    def update_measurement_definition(
        self,
        measurement_definition_id: str,
        request: MeasurementDefinitionUpdateRequest,
    ) -> None:
        cursor = self._execute(
            "UPDATE measurement_definitions SET display_name = ?, category = ?, unit = ?, "
            "multiplier = ?, decimal_places = ?, warning_threshold = ?, rollover_value = ?, "
            "purpose = ? WHERE measurement_definition_id = ?",
            (
                request.display_name,
                request.category.value,
                request.unit,
                _decimal_text(request.multiplier),
                request.decimal_places,
                _decimal_text(request.warning_threshold),
                _decimal_text(request.rollover_value),
                request.purpose,
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
        row = self._conn.execute(
            "SELECT measurement_definition_id, meter_id, effective_from, effective_to "
            "FROM meter_assignments WHERE measurement_definition_id = ? "
            "AND effective_from <= ? AND (effective_to IS NULL OR effective_to > ?) "
            "ORDER BY effective_from DESC LIMIT 1",
            (measurement_definition_id, timestamp, timestamp),
        ).fetchone()
        return self._assignment_record(row) if row is not None else None

    @staticmethod
    def _assignment_record(row: tuple[Any, ...]) -> MeterAssignmentRecord:
        return MeterAssignmentRecord(
            measurement_definition_id=str(row[0]),
            meter_id=str(row[1]),
            effective_from=parse_utc(str(row[2])),
            effective_to=parse_utc(str(row[3])) if row[3] is not None else None,
        )

    def list_meter_assignments(self, measurement_definition_id: str) -> list[MeterAssignmentRecord]:
        rows = self._conn.execute(
            "SELECT measurement_definition_id, meter_id, effective_from, effective_to "
            "FROM meter_assignments WHERE measurement_definition_id = ? ORDER BY effective_from",
            (measurement_definition_id,),
        ).fetchall()
        return [self._assignment_record(row) for row in rows]

    def set_initial_meter_assignment(
        self, measurement_definition_id: str, meter_id: str, effective_from: datetime
    ) -> None:
        meter = self.get_meter_master(meter_id)
        if meter is None or not meter.is_active:
            raise InactiveMasterError(f"有効な物理メーターを指定してください: {meter_id}")
        definition = self._conn.execute(
            "SELECT location_id FROM measurement_definitions "
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
            "(measurement_definition_id, meter_id, effective_from, effective_to) "
            "VALUES (?, ?, ?, NULL)",
            (measurement_definition_id, meter_id, utc_now_str(effective_from)),
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
        self._execute(
            "INSERT INTO staff (id, name, is_active) VALUES (?, ?, 1)",
            (self._staff_numeric_id(request.staff_id), request.display_name),
        )
        result = self.get_staff(request.staff_id)
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
            "d.unit, d.value_schema, d.multiplier, d.decimal_places, d.warning_threshold, "
            "d.rollover_value, d.calculation_method, d.reading_cycle, d.purpose, "
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
            "FROM measurement_definitions AS d WHERE d.is_active = 1 "
            "ORDER BY d.location_id, d.measurement_definition_id",
            tuple(utc_now_str(self._now()) for _ in range(4)),
        ).fetchall()
        result: list[MeasurementDefinitionData] = []
        for row in rows:
            multiplier = _read_decimal(str(row[6]))
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
                    rollover_value=_read_decimal(str(row[9]) if row[9] is not None else None),
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
            "business_date, reading_session, scheduled_at, task_state, close_reason, "
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
                reading_session=ReadingSession(str(row[5])),
                scheduled_at=parse_utc(str(row[6])),
                task_state=PlannedReadingTaskState(str(row[7])),
                close_reason=PlannedReadingCloseReason(str(row[8])) if row[8] else None,
                superseded_by_planned_reading_id=str(row[9]) if row[9] else None,
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

    @staticmethod
    def _reading_content_matches(row: tuple[Any, ...], request: ReadingUpsertRequest) -> bool:
        stored_payload: str | None = None
        if row[4] is not None:
            try:
                parsed_payload = json.loads(str(row[4]))
            except json.JSONDecodeError as exc:
                raise ValidationError("保存済み構造化入力の JSON が不正です。") from exc
            if not isinstance(parsed_payload, dict):
                raise ValidationError("保存済み構造化入力は JSON object である必要があります。")
            stored_payload = _json_text(cast(dict[str, Any], parsed_payload))
        return (
            str(row[0]) == request.result_status.value
            and str(row[1]) == utc_now_str(request.recorded_at)
            and str(row[2]) == utc_now_str(request.client_updated_at)
            and _read_decimal(str(row[3]) if row[3] is not None else None) == request.raw_reading
            and stored_payload == _json_text(request.structured_payload_json)
            and (str(row[5]) if row[5] is not None else None) == request.note
        )

    def _mark_planned_reading_completed(self, planned_reading_id: str, closed_at: datetime) -> None:
        self._execute(
            "UPDATE planned_readings SET task_state = 'completed', "
            "closed_at = ?, close_reason = NULL "
            "WHERE planned_reading_id = ? AND task_state IN ('open', 'auto_closed')",
            (utc_now_str(closed_at), planned_reading_id),
        )

    def upsert_reading(self, request: ReadingUpsertRequest) -> UpsertOutcome:
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

    def upsert_replacement(self, request: ReplacementUpsertRequest) -> UpsertOutcome:
        client_updated_at = utc_now_str(request.client_updated_at)
        existing = self._conn.execute(
            "SELECT client_updated_at, arrival_sequence FROM replacement_events "
            "WHERE replacement_event_id = ?",
            (request.replacement_event_id,),
        ).fetchone()
        if existing is None:
            self._execute(
                "INSERT INTO replacement_events (replacement_event_id, location_id, old_meter_id, "
                "new_meter_id, replaced_at, old_final_reading, new_initial_reading, "
                "client_updated_at, "
                "updated_by, note, arrival_sequence) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 1)",
                (
                    request.replacement_event_id,
                    request.location_id,
                    request.old_meter_id,
                    request.new_meter_id,
                    utc_now_str(request.replaced_at),
                    _decimal_text(request.old_final_reading),
                    _decimal_text(request.new_initial_reading),
                    client_updated_at,
                    request.updated_by,
                    request.note,
                ),
            )
            return UpsertOutcome(UpsertDisposition.INSERTED)
        if client_updated_at < str(existing[0]):
            return UpsertOutcome(UpsertDisposition.IGNORED_STALE)
        disposition = (
            UpsertDisposition.REPLACED_ON_TIE
            if client_updated_at == str(existing[0])
            else UpsertDisposition.UPDATED
        )
        self._execute(
            "UPDATE replacement_events SET location_id = ?, old_meter_id = ?, new_meter_id = ?, "
            "replaced_at = ?, old_final_reading = ?, new_initial_reading = ?, "
            "client_updated_at = ?, "
            "updated_by = ?, note = ?, arrival_sequence = ? WHERE replacement_event_id = ?",
            (
                request.location_id,
                request.old_meter_id,
                request.new_meter_id,
                utc_now_str(request.replaced_at),
                _decimal_text(request.old_final_reading),
                _decimal_text(request.new_initial_reading),
                client_updated_at,
                request.updated_by,
                request.note,
                int(existing[1]) + 1,
                request.replacement_event_id,
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
            "p.scheduled_at < ? OR (p.scheduled_at = ? AND p.planned_reading_id < ?)"
            if before_planned_reading_id is not None
            else "p.scheduled_at < ?"
        )
        params: list[object] = [measurement_definition_id, meter_id, scheduled_at]
        if before_planned_reading_id is not None:
            params.extend([scheduled_at, scheduled_at, before_planned_reading_id])
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

    def reflect_replacement(self, request: ReplacementUpsertRequest) -> None:
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
            "d.calculation_method, d.value_schema FROM measurement_definitions AS d "
            "JOIN meter_assignments AS a "
            "ON a.measurement_definition_id = d.measurement_definition_id "
            "WHERE d.is_active = 1 AND d.location_id = ? AND a.meter_id = ? "
            "AND a.effective_from <= ? AND (a.effective_to IS NULL OR a.effective_to > ?) "
            "ORDER BY d.measurement_definition_id",
            (request.location_id, request.old_meter_id, replaced_at, replaced_at),
        ).fetchall()
        for definition in definitions:
            definition_id = str(definition[0])
            self._execute(
                "UPDATE meter_assignments SET effective_to = ? "
                "WHERE measurement_definition_id = ? AND meter_id = ? "
                "AND effective_from <= ? AND (effective_to IS NULL OR effective_to > ?)",
                (replaced_at, definition_id, request.old_meter_id, replaced_at, replaced_at),
            )
            self._execute(
                "INSERT INTO meter_assignments "
                "(measurement_definition_id, meter_id, effective_from, effective_to) "
                "VALUES (?, ?, ?, NULL) "
                "ON CONFLICT(measurement_definition_id, meter_id, effective_from) "
                "DO NOTHING",
                (definition_id, request.new_meter_id, replaced_at),
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
                "old_meter_id, new_meter_id, replaced_at, previous_measured_reading, "
                "previous_measured_planned_reading_id, old_final_reading, old_meter_usage_delta, "
                "new_initial_reading, created_at, updated_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, "
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
                    _date_text(request.replaced_at.date()),
                    request.old_meter_id,
                    request.new_meter_id,
                    replaced_at,
                    _decimal_text(previous.raw_reading) if previous is not None else None,
                    previous.planned_reading_id if previous is not None else None,
                    _decimal_text(request.old_final_reading),
                    _decimal_text(request.new_initial_reading),
                    boundary_updated_at,
                    boundary_updated_at,
                ),
            )

            affected = self._conn.execute(
                "SELECT planned_reading_id, location_id, business_date, release_date, "
                "reading_session, scheduled_at, task_state FROM planned_readings "
                "WHERE measurement_definition_id = ? AND meter_id = ? AND scheduled_at >= ? "
                "AND task_state IN ('open', 'auto_closed') "
                "ORDER BY scheduled_at, planned_reading_id",
                (definition_id, request.old_meter_id, replaced_at),
            ).fetchall()
            for planned in affected:
                old_id = str(planned[0])
                new_id = f"{old_id}-replacement-{request.replacement_event_id}"
                self._execute(
                    "INSERT INTO planned_readings (planned_reading_id, measurement_definition_id, "
                    "meter_id, location_id, business_date, release_date, "
                    "reading_session, scheduled_at, "
                    "task_state) VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'open') "
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
                    ),
                )
                self._execute(
                    "UPDATE planned_readings SET task_state = 'superseded', closed_at = ?, "
                    "close_reason = 'meter_replaced', superseded_by_planned_reading_id = ? "
                    "WHERE planned_reading_id = ? AND task_state IN ('open', 'auto_closed')",
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
        return business_date + timedelta(days=offset.offset_days if offset is not None else 0)

    def recompose_future_planned_readings(
        self,
        measurement_definition_id: str,
        reading_cycle: ReadingCycle,
        schedule: ScheduleSettings,
        *,
        from_business_date: date | None = None,
    ) -> tuple[int, int]:
        start = from_business_date or self._business_date()
        latest = self._conn.execute(
            "SELECT MAX(business_date) FROM planned_readings WHERE measurement_definition_id = ?",
            (measurement_definition_id,),
        ).fetchone()
        if latest is None or latest[0] is None or start > _read_date(str(latest[0])):
            return (0, 0)
        through = _read_date(str(latest[0]))
        definition = self._conn.execute(
            "SELECT location_id FROM measurement_definitions WHERE measurement_definition_id = ?",
            (measurement_definition_id,),
        ).fetchone()
        if definition is None:
            raise ValidationError(f"測定定義が見つかりません: {measurement_definition_id}")

        desired: set[tuple[date, ReadingSession]] = set()
        generated = 0
        day = start
        while day <= through:
            for session, day_offset in self._schedule_items(reading_cycle, day, schedule):
                desired.add((day, session))
                scheduled = self._scheduled_at(day, session, day_offset)
                assignment = self.get_meter_assignment(
                    measurement_definition_id, parse_utc(scheduled)
                )
                if assignment is None:
                    raise ScheduleHistoryResolutionError(
                        measurement_definition_id,
                        day.isoformat(),
                        day.isoformat(),
                        "予定日時に有効な物理メーター割当がありません",
                    )
                stable_id = (
                    f"plan-{measurement_definition_id}-{day.isoformat()}-"
                    f"{session.value.replace(':', '')}"
                )
                existing = self._conn.execute(
                    "SELECT task_state, close_reason, meter_id, location_id, release_date, "
                    "scheduled_at FROM planned_readings WHERE planned_reading_id = ?",
                    (stable_id,),
                ).fetchone()
                if existing is None:
                    self._execute(
                        "INSERT INTO planned_readings "
                        "(planned_reading_id, measurement_definition_id, "
                        "meter_id, location_id, business_date, release_date, "
                        "reading_session, scheduled_at, "
                        "task_state) VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'open')",
                        (
                            stable_id,
                            measurement_definition_id,
                            assignment.meter_id,
                            str(definition[0]),
                            _date_text(day),
                            _date_text(
                                self._release_date(day + timedelta(days=day_offset), session)
                            ),
                            session.value,
                            scheduled,
                        ),
                    )
                    generated += 1
                elif str(existing[0]) == "open" or (
                    str(existing[0]) == "superseded"
                    and str(existing[1]) == PlannedReadingCloseReason.SCHEDULE_RECOMPOSED.value
                ):
                    changed = (
                        str(existing[2]) != assignment.meter_id
                        or str(existing[3]) != str(definition[0])
                        or str(existing[4])
                        != _date_text(self._release_date(day + timedelta(days=day_offset), session))
                        or str(existing[5]) != scheduled
                        or str(existing[0]) != "open"
                    )
                    if changed:
                        self._execute(
                            "UPDATE planned_readings SET meter_id = ?, location_id = ?, "
                            "release_date = ?, scheduled_at = ?, task_state = 'open', "
                            "closed_at = NULL, close_reason = NULL, "
                            "superseded_by_planned_reading_id = NULL WHERE planned_reading_id = ?",
                            (
                                assignment.meter_id,
                                str(definition[0]),
                                _date_text(
                                    self._release_date(day + timedelta(days=day_offset), session)
                                ),
                                scheduled,
                                stable_id,
                            ),
                        )
                        generated += 1
            day += timedelta(days=1)

        candidates = self.list_planned_reading_recomposition_candidates(
            measurement_definition_id, start
        )
        now = utc_now_str(self._now())
        superseded = 0
        for candidate in candidates:
            if (
                candidate.task_state is PlannedReadingTaskState.OPEN
                and (
                    candidate.business_date,
                    candidate.reading_session,
                )
                not in desired
            ):
                cursor = self._execute(
                    "UPDATE planned_readings SET task_state = 'superseded', closed_at = ?, "
                    "close_reason = 'schedule_recomposed' WHERE planned_reading_id = ? "
                    "AND task_state = 'open'",
                    (now, candidate.planned_reading_id),
                )
                superseded += cursor.rowcount
        return superseded, generated


__all__ = ["SQLiteRepository"]
