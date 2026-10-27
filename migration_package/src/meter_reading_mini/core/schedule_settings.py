"""`app_settings` から planned reading スケジュール設定を読み書きする service。

各 reading_cycle の具体的な検針時刻や曜日・日付指定は measurement definition ではなく
PC 全体の運用設定として `app_settings` テーブルに JSON 文字列で保持する。
"""

from __future__ import annotations

import json
import logging
import re
from collections.abc import Callable
from contextlib import AbstractContextManager
from datetime import date, datetime, time, timedelta
from typing import TypeGuard

from meter_reading_mini.core.contract_models import ReadingCycle, ReadingSession
from meter_reading_mini.core.errors import ScheduleHistoryResolutionError, ScheduleValidationError
from meter_reading_mini.core.meter_reading_sync_repositories import (
    DailySchedule,
    EveryTwoMonthsSchedule,
    MeasurementDefinitionScheduleRepository,
    MonthlySchedule,
    MonthlyScheduleSelectorType,
    MonthParity,
    PlannedReadingRecompositionRepository,
    ReadingCycleScheduleDefaultsRepository,
    ReadingSessionReleaseOffsetsRepository,
    ReleaseDateCalculator,
    ReleaseOffsetRecompositionRepository,
    ScheduleItem,
    ScheduleSettings,
    ScheduleSettingsRepository,
    WeeklySchedule,
)
from meter_reading_mini.core.timeutil import utc_now
from meter_reading_mini.core.timezone_settings import build_business_date_provider

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# app settings keys
# ---------------------------------------------------------------------------

# ---------------------------------------------------------------------------
# JSON serialization / deserialization
# ---------------------------------------------------------------------------

_WEEKDAY_NAMES = ("monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday")
_WEEKDAY_NAME_TO_INT = {name: i for i, name in enumerate(_WEEKDAY_NAMES)}


def _serialize_schedule(schedule: ScheduleSettings) -> str:
    """ScheduleSettings → JSON 文字列。"""

    if isinstance(schedule, DailySchedule):
        return json.dumps(
            {
                "reading_sessions": [
                    {
                        "reading_session": item.reading_session.value,
                        "day_offset": item.day_offset,
                    }
                    for item in schedule.reading_items
                ],
            }
        )

    if isinstance(schedule, WeeklySchedule):
        return json.dumps(
            {
                "weekday": _WEEKDAY_NAMES[schedule.weekday],
                "reading_session": schedule.reading_session.value,
                "day_offset": schedule.day_offset,
            }
        )

    if isinstance(schedule, MonthlySchedule):
        obj: dict[str, object] = {
            "selector_type": schedule.selector_type.value,
            "reading_session": schedule.reading_session.value,
            "day_offset": schedule.day_offset,
        }
        _add_selector_fields(obj, schedule)
        return json.dumps(obj)

    obj2: dict[str, object] = {
        "selector_type": schedule.selector_type.value,
        "month_parity": schedule.month_parity.value,
        "reading_session": schedule.reading_session.value,
        "day_offset": schedule.day_offset,
    }
    _add_selector_fields(obj2, schedule)
    return json.dumps(obj2)

    raise TypeError(f"Unsupported schedule type: {type(schedule)}")  # pragma: no cover


def _add_selector_fields(
    obj: dict[str, object], schedule: MonthlySchedule | EveryTwoMonthsSchedule
) -> None:
    if schedule.selector_type is MonthlyScheduleSelectorType.DAY_OF_MONTH:
        obj["day_of_month"] = schedule.day_of_month
    elif schedule.selector_type is MonthlyScheduleSelectorType.NTH_WEEKDAY:
        obj["nth"] = schedule.nth
        obj["nth_weekday"] = (
            _WEEKDAY_NAMES[schedule.nth_weekday] if schedule.nth_weekday is not None else None
        )
    elif schedule.selector_type is MonthlyScheduleSelectorType.MONTH_EDGE:
        obj["edge"] = schedule.edge


def _deserialize_schedule(reading_cycle: ReadingCycle, raw: str) -> ScheduleSettings:
    """JSON 文字列 → ScheduleSettings。"""

    parsed = json.loads(raw)
    if not _is_json_object(parsed):
        raise ValueError("Schedule JSON must contain an object.")
    data = parsed

    if reading_cycle in _DAILY_CYCLES:
        reading_sessions = data.get("reading_sessions")
        if not _is_json_array(reading_sessions):
            raise ValueError("Daily schedule JSON must contain a reading_sessions array.")
        return DailySchedule(
            reading_items=tuple(_parse_daily_schedule_item(item) for item in reading_sessions),
        )

    if reading_cycle is ReadingCycle.WEEKLY:
        weekday_name = _required_string(data, "weekday")
        weekday = _WEEKDAY_NAME_TO_INT.get(weekday_name)
        if weekday is None:
            raise ValueError(f"Unknown weekday: {weekday_name}")
        return WeeklySchedule(
            weekday=weekday,
            reading_session=ReadingSession(_required_string(data, "reading_session")),
            day_offset=_optional_integer(data, "day_offset", 0),
        )

    if reading_cycle is ReadingCycle.MONTHLY:
        return _parse_monthly_schedule(data)

    if reading_cycle is ReadingCycle.EVERY_TWO_MONTHS:
        monthly = _parse_monthly_schedule(data)
        return EveryTwoMonthsSchedule(
            selector_type=monthly.selector_type,
            month_parity=MonthParity(_required_string(data, "month_parity")),
            day_of_month=monthly.day_of_month,
            nth=monthly.nth,
            nth_weekday=monthly.nth_weekday,
            edge=monthly.edge,
            reading_session=monthly.reading_session,
            day_offset=monthly.day_offset,
        )

    raise ValueError(f"Unsupported reading_cycle: {reading_cycle}")  # pragma: no cover


def serialize_schedule(schedule: ScheduleSettings) -> str:
    """ScheduleSettings を JSON 文字列へ変換する public helper。"""

    return _serialize_schedule(schedule)


def deserialize_schedule(reading_cycle: ReadingCycle, raw: str) -> ScheduleSettings:
    """JSON 文字列を reading_cycle に応じた ScheduleSettings へ変換する public helper。"""

    return _deserialize_schedule(reading_cycle, raw)


def validate_schedule_type_for_reading_cycle(
    reading_cycle: ReadingCycle, schedule: ScheduleSettings
) -> None:
    """reading_cycle と schedule 型の対応を検証する。"""

    if reading_cycle in _DAILY_CYCLES and not isinstance(schedule, DailySchedule):
        raise ScheduleValidationError([f"{reading_cycle.value} には日次スケジュールが必要です。"])
    if reading_cycle is ReadingCycle.WEEKLY and not isinstance(schedule, WeeklySchedule):
        raise ScheduleValidationError(["weekly には週次スケジュールが必要です。"])
    if reading_cycle is ReadingCycle.MONTHLY and not isinstance(schedule, MonthlySchedule):
        raise ScheduleValidationError(["monthly には月次スケジュールが必要です。"])
    if reading_cycle is ReadingCycle.EVERY_TWO_MONTHS and not isinstance(
        schedule, EveryTwoMonthsSchedule
    ):
        raise ScheduleValidationError(["every_two_months には隔月スケジュールが必要です。"])


def _parse_monthly_schedule(data: dict[str, object]) -> MonthlySchedule:
    selector_type = MonthlyScheduleSelectorType(_required_string(data, "selector_type"))
    reading_session = ReadingSession(_optional_string(data, "reading_session", "08:00"))
    day_offset = _optional_integer(data, "day_offset", 0)
    if selector_type is MonthlyScheduleSelectorType.DAY_OF_MONTH:
        return MonthlySchedule(
            selector_type=selector_type,
            day_of_month=_required_integer(data, "day_of_month"),
            reading_session=reading_session,
            day_offset=day_offset,
        )
    if selector_type is MonthlyScheduleSelectorType.NTH_WEEKDAY:
        return MonthlySchedule(
            selector_type=selector_type,
            nth=_required_integer(data, "nth"),
            nth_weekday=_required_weekday(data, "nth_weekday"),
            reading_session=reading_session,
            day_offset=day_offset,
        )
    if selector_type is MonthlyScheduleSelectorType.MONTH_EDGE:
        return MonthlySchedule(
            selector_type=selector_type,
            edge=_required_string(data, "edge"),
            reading_session=reading_session,
            day_offset=day_offset,
        )
    raise ValueError(f"Unsupported selector_type: {selector_type}")  # pragma: no cover


def _parse_daily_schedule_item(item: object) -> ScheduleItem:
    if isinstance(item, str):
        return ScheduleItem(reading_session=ReadingSession(item), day_offset=0)
    if not _is_json_object(item):
        raise TypeError("Daily schedule items must be strings or objects.")
    return ScheduleItem(
        reading_session=ReadingSession(_required_string(item, "reading_session")),
        day_offset=_optional_integer(item, "day_offset", 0),
    )


def _is_json_object(value: object) -> TypeGuard[dict[str, object]]:
    return isinstance(value, dict)


def _is_json_array(value: object) -> TypeGuard[list[object]]:
    return isinstance(value, list)


def _required_string(data: dict[str, object], key: str) -> str:
    value = data.get(key)
    if not isinstance(value, str):
        raise ValueError(f"Schedule field {key!r} must be a string.")
    return value


def _optional_string(data: dict[str, object], key: str, default: str) -> str:
    value = data.get(key, default)
    if not isinstance(value, str):
        raise ValueError(f"Schedule field {key!r} must be a string.")
    return value


def _required_integer(data: dict[str, object], key: str) -> int:
    value = data.get(key)
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValueError(f"Schedule field {key!r} must be an integer.")
    return value


def _optional_integer(data: dict[str, object], key: str, default: int) -> int:
    value = data.get(key, default)
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValueError(f"Schedule field {key!r} must be an integer.")
    return value


def _required_weekday(data: dict[str, object], key: str) -> int:
    weekday_name = _required_string(data, key)
    weekday = _WEEKDAY_NAME_TO_INT.get(weekday_name)
    if weekday is None:
        raise ValueError(f"Unknown weekday: {weekday_name}")
    return weekday


def _serialize_release_offset_days(offset_days: int) -> str:
    """release offset を app_settings 向けの文字列へ変換する。"""

    return str(int(offset_days))


_DAILY_CYCLE_RE = re.compile(r"^daily_(\d+)x$")
_DAILY_CYCLES = frozenset({cycle for cycle in ReadingCycle if _DAILY_CYCLE_RE.match(cycle.value)})


def expected_daily_count(reading_cycle: ReadingCycle) -> int:
    """指定された `reading_cycle` の日次期待件数を返す。

    日次以外の cycle では 0 を返す。
    日次系は `daily_Nx` の形式をパースして N を返す設計とすることで
    将来の `daily_4x` 等を容易に扱えるようにする。
    """

    m = _DAILY_CYCLE_RE.match(reading_cycle.value)
    if not m:
        return 0
    return int(m.group(1))


def validate_expected_daily_count(reading_cycle: ReadingCycle, schedule: ScheduleSettings) -> None:
    """日次スケジュールの件数不足・超過・重複を検証する。

    違反があれば `ScheduleValidationError` を投げる。
    """

    # 日次系に関係ない場合は何もしない
    if reading_cycle not in _DAILY_CYCLES:
        return

    if not isinstance(schedule, DailySchedule):
        # 型整合性は別の validator でチェック済みのはずなのでここでは無視する
        return

    errors: list[str] = []
    expected = expected_daily_count(reading_cycle)
    current = len(schedule.reading_items)

    if current < expected:
        errors.append(
            f"{reading_cycle.value} は {expected} 件の検針時刻を選択する必要があります"
            f"（現在 {current} 件）。"
        )
    if current > expected:
        errors.append(
            f"{reading_cycle.value} は {expected} 件の検針時刻のみ許可されています"
            f"（現在 {current} 件）。"
        )

    # 重複検査
    sessions = [item.reading_session for item in schedule.reading_items]
    duplicates = sorted({s for s in sessions if sessions.count(s) > 1})
    if duplicates:
        errors.append(
            "同一の検針時刻が重複しています: " + ", ".join(s.value for s in duplicates) + "。"
        )

    if errors:
        raise ScheduleValidationError(errors)


# ---------------------------------------------------------------------------
# default schedules
# ---------------------------------------------------------------------------

# Ordered pool of sessions used to pick fallback defaults when new daily_Nx
# variants are added. Keep the well-known defaults for 1x/2x/3x explicit
# to preserve existing behavior.
_ORDERED_DAILY_SESSIONS = (
    ReadingSession.SESSION_0600,
    ReadingSession.SESSION_0800,
    ReadingSession.SESSION_1400,
    ReadingSession.SESSION_1700,
    ReadingSession.SESSION_2200,
    ReadingSession.SESSION_0000,
)

_DEFAULT_DAILY_PROFILE: dict[int, tuple[ReadingSession, ...]] = {
    1: (ReadingSession.SESSION_0800,),
    2: (ReadingSession.SESSION_0800, ReadingSession.SESSION_2200),
    3: (ReadingSession.SESSION_0600, ReadingSession.SESSION_1400, ReadingSession.SESSION_2200),
}


def _default_daily_schedule_for_count(n: int) -> DailySchedule:
    if n in _DEFAULT_DAILY_PROFILE:
        sessions = _DEFAULT_DAILY_PROFILE[n]
    else:
        if n > len(_ORDERED_DAILY_SESSIONS):
            raise ValueError(
                f"Cannot construct default daily schedule for {n} sessions; not enough slots."
            )
        sessions = _ORDERED_DAILY_SESSIONS[:n]
    return DailySchedule(reading_items=tuple(ScheduleItem(s) for s in sessions))


DEFAULT_SCHEDULES: dict[ReadingCycle, ScheduleSettings] = {}

# populate dynamic daily defaults (handles future daily_Nx variants)
for _cycle in ReadingCycle:
    expected = expected_daily_count(_cycle)
    if expected > 0:
        DEFAULT_SCHEDULES[_cycle] = _default_daily_schedule_for_count(expected)

# non-daily defaults
DEFAULT_SCHEDULES[ReadingCycle.WEEKLY] = WeeklySchedule(
    weekday=6, reading_session=ReadingSession.SESSION_0800, day_offset=0
)
DEFAULT_SCHEDULES[ReadingCycle.MONTHLY] = MonthlySchedule(
    selector_type=MonthlyScheduleSelectorType.DAY_OF_MONTH,
    day_of_month=1,
    reading_session=ReadingSession.SESSION_0800,
    day_offset=0,
)
DEFAULT_SCHEDULES[ReadingCycle.EVERY_TWO_MONTHS] = EveryTwoMonthsSchedule(
    selector_type=MonthlyScheduleSelectorType.DAY_OF_MONTH,
    month_parity=MonthParity.ODD,
    day_of_month=1,
    reading_session=ReadingSession.SESSION_0800,
    day_offset=0,
)

DEFAULT_RELEASE_OFFSETS: dict[ReadingSession, int] = {
    ReadingSession.SESSION_0000: -1,
    ReadingSession.SESSION_0600: 0,
    ReadingSession.SESSION_0800: 0,
    ReadingSession.SESSION_1400: 0,
    ReadingSession.SESSION_1700: 0,
    ReadingSession.SESSION_2200: 0,
}


# ---------------------------------------------------------------------------
# service
# ---------------------------------------------------------------------------

TransactionFactory = Callable[[], AbstractContextManager[object]]


def _resolve_effective_date(
    effective_from_date: date | None, current_business_date_provider: Callable[[], date]
) -> date:
    current_business_date = current_business_date_provider()
    resolved_date = effective_from_date or current_business_date
    if resolved_date < current_business_date:
        raise ScheduleValidationError(["適用開始日に過去の日付は指定できません。"])
    return resolved_date


def _deserialize_default_history(
    repository: ReadingCycleScheduleDefaultsRepository,
    reading_cycle: ReadingCycle,
    business_date: date,
) -> ScheduleSettings:
    record = repository.get_reading_cycle_schedule_default(reading_cycle, business_date)
    if record is None:
        raise ScheduleHistoryResolutionError(
            reading_cycle.value,
            business_date.isoformat(),
            business_date.isoformat(),
            "適用可能な周期別既定スケジュール履歴がありません",
        )
    try:
        return deserialize_schedule(reading_cycle, record.schedule_json)
    except (TypeError, ValueError) as error:
        raise ScheduleHistoryResolutionError(
            reading_cycle.value,
            record.effective_from_date.isoformat(),
            business_date.isoformat(),
            f"スケジュール履歴が不正です: {error}",
        ) from error


class ScheduleSettingsService:
    """周期別既定スケジュールを履歴管理する service。"""

    def __init__(
        self,
        repository: ReadingCycleScheduleDefaultsRepository,
        *,
        recomposition_repository: PlannedReadingRecompositionRepository,
        transaction_factory: TransactionFactory,
        current_business_date_provider: Callable[[], date] | None = None,
        clock: Callable[[], datetime] = utc_now,
    ) -> None:
        self._repository = repository
        self._recomposition_repository = recomposition_repository
        self._transaction_factory = transaction_factory
        self._current_business_date_provider = (
            current_business_date_provider or build_business_date_provider()
        )
        self._clock = clock

    def get_schedule(
        self, reading_cycle: ReadingCycle, business_date: date | None = None
    ) -> ScheduleSettings:
        """指定日に有効な周期別既定スケジュールを返す。"""

        effective_date = business_date or self._current_business_date_provider()
        return _deserialize_default_history(self._repository, reading_cycle, effective_date)

    def save_schedule(
        self,
        reading_cycle: ReadingCycle,
        schedule: ScheduleSettings,
        *,
        effective_from_date: date | None = None,
    ) -> None:
        """適用履歴を保存し、既定値を使う有効定義の枠を再構成する。"""

        validate_schedule_type_for_reading_cycle(reading_cycle, schedule)
        validate_expected_daily_count(reading_cycle, schedule)
        effective_date = _resolve_effective_date(
            effective_from_date, self._current_business_date_provider
        )
        with self._transaction_factory():
            self._repository.save_reading_cycle_schedule_default(
                reading_cycle,
                effective_date,
                serialize_schedule(schedule),
                created_at=self._clock(),
            )
            for (
                measurement_definition_id
            ) in self._repository.list_measurement_definitions_using_cycle_default(
                reading_cycle, effective_date
            ):
                superseded_count, generated_count = (
                    self._recomposition_repository.recompose_future_planned_readings(
                        measurement_definition_id,
                        from_business_date=effective_date,
                    )
                )
                logger.info(
                    "周期別既定スケジュールを再構成しました: definition=%s "
                    "superseded=%d generated=%d",
                    measurement_definition_id,
                    superseded_count,
                    generated_count,
                )
        logger.info("Saved schedule for %s", reading_cycle.value)

    @classmethod
    def serialize_default(cls, reading_cycle: ReadingCycle) -> str:
        """指定 reading_cycle のデフォルトスケジュールを JSON 文字列で返す。

        bootstrap.py から利用するための public アクセスポイント。
        """

        return serialize_schedule(DEFAULT_SCHEDULES[reading_cycle])


class MeasurementDefinitionScheduleService:
    """測定定義別スケジュールを適用履歴として管理する service。"""

    def __init__(
        self,
        repository: MeasurementDefinitionScheduleRepository,
        *,
        schedule_defaults_repository: ReadingCycleScheduleDefaultsRepository,
        recomposition_repository: PlannedReadingRecompositionRepository,
        transaction_factory: TransactionFactory,
        current_business_date_provider: Callable[[], date] | None = None,
        clock: Callable[[], datetime] = utc_now,
    ) -> None:
        self._repository = repository
        self._schedule_defaults_repository = schedule_defaults_repository
        self._recomposition_repository = recomposition_repository
        self._transaction_factory = transaction_factory
        self._current_business_date_provider = (
            current_business_date_provider or build_business_date_provider()
        )
        self._clock = clock

    def get_schedule(
        self,
        measurement_definition_id: str,
        reading_cycle: ReadingCycle,
        business_date: date | None = None,
    ) -> ScheduleSettings:
        """指定日に有効な定義別設定を返し、NULL 履歴は周期別既定へ委譲する。"""

        effective_date = business_date or self._current_business_date_provider()
        record = self._repository.get_measurement_definition_schedule_for_date(
            measurement_definition_id, effective_date
        )
        if record is None or record.schedule_json is None:
            return _deserialize_default_history(
                self._schedule_defaults_repository, reading_cycle, effective_date
            )
        try:
            return deserialize_schedule(reading_cycle, record.schedule_json)
        except (TypeError, ValueError) as error:
            raise ScheduleHistoryResolutionError(
                measurement_definition_id,
                record.effective_from_date.isoformat(),
                effective_date.isoformat(),
                f"測定定義別スケジュール履歴が不正です: {error}",
            ) from error

    def save_schedule(
        self,
        measurement_definition_id: str,
        reading_cycle: ReadingCycle,
        schedule: ScheduleSettings,
        *,
        effective_from_date: date | None = None,
    ) -> None:
        """適用履歴を保存し、同じトランザクションで予定枠を再構成する。"""

        validate_schedule_type_for_reading_cycle(reading_cycle, schedule)
        validate_expected_daily_count(reading_cycle, schedule)
        effective_date = _resolve_effective_date(
            effective_from_date, self._current_business_date_provider
        )
        with self._transaction_factory():
            self._repository.upsert_measurement_definition_schedule(
                measurement_definition_id,
                serialize_schedule(schedule),
                effective_from_date=effective_date,
                created_at=self._clock(),
            )
            superseded_count, generated_count = (
                self._recomposition_repository.recompose_future_planned_readings(
                    measurement_definition_id,
                    from_business_date=effective_date,
                )
            )
            logger.info(
                "予定枠を再構成しました: definition=%s cycle=%s from=%s superseded=%d generated=%d",
                measurement_definition_id,
                reading_cycle.value,
                effective_date.isoformat(),
                superseded_count,
                generated_count,
            )

    def ensure_default_schedule(
        self,
        measurement_definition_id: str,
        reading_cycle: ReadingCycle,
        *,
        effective_from_date: date | None = None,
    ) -> ScheduleSettings:
        """未作成なら NULL の適用履歴を作り、周期別既定を返す。"""

        date_for_history = _resolve_effective_date(
            effective_from_date, self._current_business_date_provider
        )
        current = self._repository.get_measurement_definition_schedule_for_date(
            measurement_definition_id, date_for_history
        )
        if current is None:
            with self._transaction_factory():
                self._repository.upsert_measurement_definition_schedule(
                    measurement_definition_id,
                    None,
                    effective_from_date=date_for_history,
                    created_at=self._clock(),
                )
                self._recomposition_repository.recompose_future_planned_readings(
                    measurement_definition_id,
                    from_business_date=date_for_history,
                )
        return self.get_schedule(measurement_definition_id, reading_cycle, date_for_history)

    @classmethod
    def serialize_default(cls, reading_cycle: ReadingCycle) -> str:
        """指定 reading_cycle の default schedule を JSON 文字列で返す。"""

        return serialize_schedule(DEFAULT_SCHEDULES[reading_cycle])


class ReleaseOffsetSettingsService(ReleaseDateCalculator):
    """検針時間ごとの配信オフセットを適用履歴として管理する service。"""

    def __init__(
        self,
        repository: ReadingSessionReleaseOffsetsRepository,
        *,
        recomposition_repository: ReleaseOffsetRecompositionRepository,
        transaction_factory: TransactionFactory,
        current_business_date_provider: Callable[[], date] | None = None,
        clock: Callable[[], datetime] = utc_now,
    ) -> None:
        self._repository = repository
        self._recomposition_repository = recomposition_repository
        self._transaction_factory = transaction_factory
        self._current_business_date_provider = (
            current_business_date_provider or build_business_date_provider()
        )
        self._clock = clock

    def get_release_offset_days(
        self, reading_session: ReadingSession, business_date: date | None = None
    ) -> int:
        """指定日に有効な配信オフセットを返す。"""

        effective_date = business_date or self._current_business_date_provider()
        record = self._repository.get_reading_session_release_offset(
            reading_session, effective_date
        )
        if record is None:
            raise ScheduleHistoryResolutionError(
                reading_session.value,
                effective_date.isoformat(),
                effective_date.isoformat(),
                "適用可能な配信オフセット履歴がありません",
            )
        return record.offset_days

    def save_release_offset_days(
        self,
        reading_session: ReadingSession,
        offset_days: int,
        *,
        effective_from_date: date | None = None,
    ) -> None:
        """配信オフセット履歴を保存し、open 枠の配信日を同時に再構成する。"""

        effective_date = _resolve_effective_date(
            effective_from_date, self._current_business_date_provider
        )
        with self._transaction_factory():
            self._repository.save_reading_session_release_offset(
                reading_session,
                effective_date,
                offset_days,
                created_at=self._clock(),
            )
            changed_count = self._recomposition_repository.recompose_release_dates(
                reading_session, effective_date
            )
        logger.info("Saved release offset for %s: %s", reading_session.value, offset_days)
        logger.info(
            "配信日を再構成しました: session=%s from=%s changed=%d",
            reading_session.value,
            effective_date.isoformat(),
            changed_count,
        )

    def compute_release_date(self, business_date: date, reading_session: ReadingSession) -> date:
        """business_date と session から配布可能日を計算する。"""

        return business_date + timedelta(
            days=self.get_release_offset_days(reading_session, business_date)
        )

    @classmethod
    def serialize_default(cls, reading_session: ReadingSession) -> str:
        """指定 session のデフォルト offset を文字列で返す。"""

        return _serialize_release_offset_days(DEFAULT_RELEASE_OFFSETS.get(reading_session, 0))


# ---------------------------------------------------------------------------
# cutoff settings
# ---------------------------------------------------------------------------

CUTOFF_SETTINGS_KEY = "auto_close.cutoff_time.v1"
DEFAULT_CUTOFF_TIME = time(hour=7, minute=0)


class CutoffSettingsService:
    """auto-close の cutoff 時刻を読み書きする service。"""

    def __init__(self, repository: ScheduleSettingsRepository) -> None:
        self._repository = repository

    def get_cutoff_time(self) -> time:
        """cutoff 時刻を返す。DB 未設定時は DEFAULT_CUTOFF_TIME。"""

        raw = self._repository.get_setting(CUTOFF_SETTINGS_KEY)
        if raw is None:
            return DEFAULT_CUTOFF_TIME
        return time.fromisoformat(raw)

    def save_cutoff_time(self, cutoff: time) -> None:
        """cutoff 時刻を保存する。"""

        self._repository.upsert_setting(CUTOFF_SETTINGS_KEY, cutoff.strftime("%H:%M"))
        logger.info("Saved cutoff time: %s", cutoff.strftime("%H:%M"))


# ---------------------------------------------------------------------------
# provider type for work_package_download
# ---------------------------------------------------------------------------

ScheduleSettingsProvider = ScheduleSettingsService


__all__ = [
    "CUTOFF_SETTINGS_KEY",
    "CutoffSettingsService",
    "DEFAULT_CUTOFF_TIME",
    "DEFAULT_RELEASE_OFFSETS",
    "DEFAULT_SCHEDULES",
    "ReleaseOffsetSettingsService",
    "MeasurementDefinitionScheduleService",
    "deserialize_schedule",
    "ScheduleSettingsProvider",
    "ScheduleSettingsService",
    "serialize_schedule",
    "validate_schedule_type_for_reading_cycle",
    "ScheduleValidationError",
    "expected_daily_count",
    "validate_expected_daily_count",
]
