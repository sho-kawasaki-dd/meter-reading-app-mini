"""`app_settings` から planned reading スケジュール設定を読み書きする service。

各 reading_cycle の具体的な検針時刻や曜日・日付指定は measurement definition ではなく
PC 全体の運用設定として `app_settings` テーブルに JSON 文字列で保持する。
"""

from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass
from datetime import date, time, timedelta
from enum import StrEnum
from typing import Callable, Protocol
from uuid import UUID

from api.models import ReadingCycle, ReadingSession
from application.meter_reading_sync_repositories import PlannedReadingRecompositionRepository, ReleaseDateCalculator
from application.timezone_settings import build_business_date_provider

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# app_settings key naming
# ---------------------------------------------------------------------------

_SCHEDULE_KEY_PREFIX = "planned_reading_schedule"
_SCHEDULE_KEY_VERSION = "v1"

SCHEDULE_SETTINGS_KEYS: dict[ReadingCycle, str] = {
	cycle: f"{_SCHEDULE_KEY_PREFIX}.{cycle.value}.{_SCHEDULE_KEY_VERSION}"
	for cycle in ReadingCycle
}

_RELEASE_OFFSET_KEY_PREFIX = "planned_reading_release_offset"
_RELEASE_OFFSET_KEY_VERSION = "v1"

RELEASE_OFFSET_SETTINGS_KEYS: dict[ReadingSession, str] = {
	session: f"{_RELEASE_OFFSET_KEY_PREFIX}.{session.value}.{_RELEASE_OFFSET_KEY_VERSION}"
	for session in ReadingSession
}


# ---------------------------------------------------------------------------
# monthly / every_two_months selector types
# ---------------------------------------------------------------------------

class MonthlyScheduleSelectorType(StrEnum):
	DAY_OF_MONTH = "day_of_month"
	NTH_WEEKDAY = "nth_weekday"
	MONTH_EDGE = "month_edge"


class MonthParity(StrEnum):
	ODD = "odd"
	EVEN = "even"


@dataclass(frozen=True, slots=True)
class ScheduleItem:
	"""schedule の最小構成要素。"""

	reading_session: ReadingSession
	day_offset: int = 0


# ---------------------------------------------------------------------------
# schedule value dataclasses
# ---------------------------------------------------------------------------

@dataclass(frozen=True, slots=True)
class DailySchedule:
	"""daily_1x / daily_2x / daily_3x 用: schedule item 配列。"""

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
	"""weekly 用: 曜日 (0=Mon … 6=Sun) と reading_session。"""

	weekday: int  # 0=Monday … 6=Sunday
	reading_session: ReadingSession
	day_offset: int = 0


@dataclass(frozen=True, slots=True)
class MonthlySchedule:
	"""monthly 用: selector_type と対応する値。"""

	selector_type: MonthlyScheduleSelectorType
	day_of_month: int | None = None       # selector_type == day_of_month
	nth: int | None = None                 # selector_type == nth_weekday (例: 2 → 第2)
	nth_weekday: int | None = None         # 0=Mon … 6=Sun
	edge: str | None = None                # "first" | "last"
	reading_session: ReadingSession = ReadingSession.SESSION_0800
	day_offset: int = 0


@dataclass(frozen=True, slots=True)
class EveryTwoMonthsSchedule:
	"""every_two_months 用: monthly schedule + month_parity。"""

	selector_type: MonthlyScheduleSelectorType
	month_parity: MonthParity
	day_of_month: int | None = None
	nth: int | None = None
	nth_weekday: int | None = None
	edge: str | None = None
	reading_session: ReadingSession = ReadingSession.SESSION_0800
	day_offset: int = 0


# ---------------------------------------------------------------------------
# union type
# ---------------------------------------------------------------------------

ScheduleSettings = DailySchedule | WeeklySchedule | MonthlySchedule | EveryTwoMonthsSchedule


# ---------------------------------------------------------------------------
# JSON serialization / deserialization
# ---------------------------------------------------------------------------

_WEEKDAY_NAMES = ("monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday")
_WEEKDAY_NAME_TO_INT = {name: i for i, name in enumerate(_WEEKDAY_NAMES)}


def _serialize_schedule(schedule: ScheduleSettings) -> str:
	"""ScheduleSettings → JSON 文字列。"""

	if isinstance(schedule, DailySchedule):
		return json.dumps({
			"reading_sessions": [
				{
					"reading_session": item.reading_session.value,
					"day_offset": item.day_offset,
				}
				for item in schedule.reading_items
			],
		})

	if isinstance(schedule, WeeklySchedule):
		return json.dumps({
			"weekday": _WEEKDAY_NAMES[schedule.weekday],
			"reading_session": schedule.reading_session.value,
			"day_offset": schedule.day_offset,
		})

	if isinstance(schedule, MonthlySchedule):
		obj: dict[str, object] = {
			"selector_type": schedule.selector_type.value,
			"reading_session": schedule.reading_session.value,
			"day_offset": schedule.day_offset,
		}
		_add_selector_fields(obj, schedule)
		return json.dumps(obj)

	if isinstance(schedule, EveryTwoMonthsSchedule):
		obj2: dict[str, object] = {
			"selector_type": schedule.selector_type.value,
			"month_parity": schedule.month_parity.value,
			"reading_session": schedule.reading_session.value,
			"day_offset": schedule.day_offset,
		}
		_add_selector_fields(obj2, schedule)
		return json.dumps(obj2)

	raise TypeError(f"Unsupported schedule type: {type(schedule)}")  # pragma: no cover


def _add_selector_fields(obj: dict[str, object], schedule: MonthlySchedule | EveryTwoMonthsSchedule) -> None:
	if schedule.selector_type is MonthlyScheduleSelectorType.DAY_OF_MONTH:
		obj["day_of_month"] = schedule.day_of_month
	elif schedule.selector_type is MonthlyScheduleSelectorType.NTH_WEEKDAY:
		obj["nth"] = schedule.nth
		obj["nth_weekday"] = _WEEKDAY_NAMES[schedule.nth_weekday] if schedule.nth_weekday is not None else None
	elif schedule.selector_type is MonthlyScheduleSelectorType.MONTH_EDGE:
		obj["edge"] = schedule.edge


def _deserialize_schedule(reading_cycle: ReadingCycle, raw: str) -> ScheduleSettings:
	"""JSON 文字列 → ScheduleSettings。"""

	data = json.loads(raw)

	if reading_cycle in _DAILY_CYCLES:
		return DailySchedule(
			reading_items=tuple(_parse_daily_schedule_item(item) for item in data["reading_sessions"]),
		)

	if reading_cycle is ReadingCycle.WEEKLY:
		return WeeklySchedule(
			weekday=_WEEKDAY_NAME_TO_INT[data["weekday"]],
			reading_session=ReadingSession(data["reading_session"]),
			day_offset=int(data.get("day_offset", 0)),
		)

	if reading_cycle is ReadingCycle.MONTHLY:
		return _parse_monthly_schedule(data)

	if reading_cycle is ReadingCycle.EVERY_TWO_MONTHS:
		monthly = _parse_monthly_schedule(data)
		return EveryTwoMonthsSchedule(
			selector_type=monthly.selector_type,
			month_parity=MonthParity(data["month_parity"]),
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


def validate_schedule_type_for_reading_cycle(reading_cycle: ReadingCycle, schedule: ScheduleSettings) -> None:
	"""reading_cycle と schedule 型の対応を検証する。"""

	if reading_cycle in _DAILY_CYCLES and not isinstance(schedule, DailySchedule):
		raise ValueError(f"{reading_cycle.value} requires DailySchedule.")
	if reading_cycle is ReadingCycle.WEEKLY and not isinstance(schedule, WeeklySchedule):
		raise ValueError("weekly requires WeeklySchedule.")
	if reading_cycle is ReadingCycle.MONTHLY and not isinstance(schedule, MonthlySchedule):
		raise ValueError("monthly requires MonthlySchedule.")
	if reading_cycle is ReadingCycle.EVERY_TWO_MONTHS and not isinstance(schedule, EveryTwoMonthsSchedule):
		raise ValueError("every_two_months requires EveryTwoMonthsSchedule.")


def _parse_monthly_schedule(data: dict) -> MonthlySchedule:
	selector_type = MonthlyScheduleSelectorType(data["selector_type"])
	reading_session = ReadingSession(data.get("reading_session", "08:00"))
	day_offset = int(data.get("day_offset", 0))
	if selector_type is MonthlyScheduleSelectorType.DAY_OF_MONTH:
		return MonthlySchedule(
			selector_type=selector_type,
			day_of_month=data["day_of_month"],
			reading_session=reading_session,
			day_offset=day_offset,
		)
	if selector_type is MonthlyScheduleSelectorType.NTH_WEEKDAY:
		return MonthlySchedule(
			selector_type=selector_type,
			nth=data["nth"],
			nth_weekday=_WEEKDAY_NAME_TO_INT[data["nth_weekday"]],
			reading_session=reading_session,
			day_offset=day_offset,
		)
	if selector_type is MonthlyScheduleSelectorType.MONTH_EDGE:
		return MonthlySchedule(
			selector_type=selector_type,
			edge=data["edge"],
			reading_session=reading_session,
			day_offset=day_offset,
		)
	raise ValueError(f"Unsupported selector_type: {selector_type}")  # pragma: no cover


def _parse_daily_schedule_item(item: object) -> ScheduleItem:
	if isinstance(item, str):
		return ScheduleItem(reading_session=ReadingSession(item), day_offset=0)
	if not isinstance(item, dict):
		raise TypeError("Daily schedule items must be strings or objects.")
	return ScheduleItem(
		reading_session=ReadingSession(item["reading_session"]),
		day_offset=int(item.get("day_offset", 0)),
	)


def _serialize_release_offset_days(offset_days: int) -> str:
	"""release offset を app_settings 向けの文字列へ変換する。"""

	return str(int(offset_days))


def _deserialize_release_offset_days(raw: str) -> int:
	"""app_settings の release offset 文字列を整数へ変換する。"""

	return int(raw)


_DAILY_CYCLE_RE = re.compile(r"^daily_(\d+)x$")
_DAILY_CYCLES = frozenset({cycle for cycle in ReadingCycle if _DAILY_CYCLE_RE.match(cycle.value)})


class ScheduleValidationError(ValueError):
	"""スケジュール保存時の業務ルール違反を表す例外。

	UI 層で複数のエラーメッセージを扱えるように `errors` リストを保持する。
	"""

	def __init__(self, errors: list[str]) -> None:
		super().__init__("Invalid schedule data.")
		self.errors = errors


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
	"""日次スケジュールの件数不足・超過・重複を検証し、違反があれば `ScheduleValidationError` を投げる。"""

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
		errors.append(f"{reading_cycle.value} は {expected} 件の検針時刻を選択する必要があります（現在 {current} 件）。")
	if current > expected:
		errors.append(f"{reading_cycle.value} は {expected} 件の検針時刻のみ許可されています（現在 {current} 件）。")

	# 重複検査
	sessions = [item.reading_session for item in schedule.reading_items]
	duplicates = sorted({s for s in sessions if sessions.count(s) > 1})
	if duplicates:
		errors.append("同一の検針時刻が重複しています: " + ", ".join(s.value for s in duplicates) + "。")

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
			raise ValueError(f"Cannot construct default daily schedule for {n} sessions; not enough slots.")
		sessions = _ORDERED_DAILY_SESSIONS[:n]
	return DailySchedule(reading_items=tuple(ScheduleItem(s) for s in sessions))


DEFAULT_SCHEDULES: dict[ReadingCycle, ScheduleSettings] = {}

# populate dynamic daily defaults (handles future daily_Nx variants)
for _cycle in ReadingCycle:
	expected = expected_daily_count(_cycle)
	if expected > 0:
		DEFAULT_SCHEDULES[_cycle] = _default_daily_schedule_for_count(expected)

# non-daily defaults
DEFAULT_SCHEDULES[ReadingCycle.WEEKLY] = WeeklySchedule(weekday=6, reading_session=ReadingSession.SESSION_0800, day_offset=0)
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
# repository protocol
# ---------------------------------------------------------------------------

class ScheduleSettingsRepository(Protocol):
	"""app_settings からスケジュール設定を読み書きする repository。"""

	def get_setting(self, key: str) -> str | None: ...
	def upsert_setting(self, key: str, value: str) -> None: ...


class MeasurementDefinitionScheduleRepository(Protocol):
	"""measurement_definition_schedules を読み書きする repository。"""

	def get_measurement_definition_schedule(self, measurement_definition_id: str) -> str | None: ...
	def upsert_measurement_definition_schedule(self, measurement_definition_id: str, schedule_json: str) -> None: ...


# ---------------------------------------------------------------------------
# service
# ---------------------------------------------------------------------------

class ScheduleSettingsService:
	"""reading_cycle ごとのスケジュール設定を読み書きする service。"""

	def __init__(self, repository: ScheduleSettingsRepository) -> None:
		self._repository = repository

	def get_schedule(self, reading_cycle: ReadingCycle) -> ScheduleSettings:
		"""指定 reading_cycle のスケジュール設定を返す。未設定ならデフォルト。"""

		key = SCHEDULE_SETTINGS_KEYS[reading_cycle]
		raw = self._repository.get_setting(key)
		if raw is None:
			return DEFAULT_SCHEDULES[reading_cycle]
		return deserialize_schedule(reading_cycle, raw)

	def save_schedule(self, reading_cycle: ReadingCycle, schedule: ScheduleSettings) -> None:
		"""指定 reading_cycle のスケジュール設定を保存する。"""

		validate_schedule_type_for_reading_cycle(reading_cycle, schedule)
		# 期待日次件数等の業務ルール検証を必須実行
		validate_expected_daily_count(reading_cycle, schedule)
		key = SCHEDULE_SETTINGS_KEYS[reading_cycle]
		self._repository.upsert_setting(key, serialize_schedule(schedule))
		logger.info("Saved schedule for %s", reading_cycle.value)

	@classmethod
	def serialize_default(cls, reading_cycle: ReadingCycle) -> str:
		"""指定 reading_cycle のデフォルトスケジュールを JSON 文字列で返す。

		bootstrap.py から利用するための public アクセスポイント。
		"""

		return serialize_schedule(DEFAULT_SCHEDULES[reading_cycle])


class MeasurementDefinitionScheduleService:
	"""measurement_definition 単位の planned reading スケジュールを読み書きする service。"""

	def __init__(
		self,
		repository: MeasurementDefinitionScheduleRepository,
		*,
		recomposition_repository: PlannedReadingRecompositionRepository | None = None,
		current_business_date_provider: Callable[[], date] | None = None,
	) -> None:
		self._repository = repository
		self._recomposition_repository = recomposition_repository
		self._current_business_date_provider = current_business_date_provider or build_business_date_provider()

	def get_schedule(self, measurement_definition_id: str, reading_cycle: ReadingCycle) -> ScheduleSettings:
		"""指定 measurement_definition_id のスケジュールを返す。未設定なら default を返す。"""

		raw = self._repository.get_measurement_definition_schedule(measurement_definition_id)
		if raw is None:
			return DEFAULT_SCHEDULES[reading_cycle]
		return deserialize_schedule(reading_cycle, raw)

	def save_schedule(self, measurement_definition_id: str, reading_cycle: ReadingCycle, schedule: ScheduleSettings) -> None:
		"""measurement_definition 単位のスケジュールを保存する。"""

		validate_schedule_type_for_reading_cycle(reading_cycle, schedule)
		# 期待日次件数等の業務ルール検証を必須実行
		validate_expected_daily_count(reading_cycle, schedule)
		self._repository.upsert_measurement_definition_schedule(measurement_definition_id, serialize_schedule(schedule))
		logger.info("Saved measurement definition schedule for %s (%s)", measurement_definition_id, reading_cycle.value)
		if self._recomposition_repository is not None:
			from_business_date = self._current_business_date_provider()
			superseded_count, generated_count = self._recomposition_repository.recompose_future_planned_readings(
				measurement_definition_id,
				reading_cycle,
				schedule,
				from_business_date=from_business_date,
			)
			logger.info(
				"Recomposed future planned readings for %s (%s) from %s: superseded=%d generated=%d",
				measurement_definition_id,
				reading_cycle.value,
				from_business_date.isoformat(),
				superseded_count,
				generated_count,
			)

	def ensure_default_schedule(self, measurement_definition_id: str, reading_cycle: ReadingCycle) -> ScheduleSettings:
		"""未作成なら default schedule を作成し、現在値を返す。"""

		current = self._repository.get_measurement_definition_schedule(measurement_definition_id)
		if current is not None:
			return deserialize_schedule(reading_cycle, current)

		default_schedule = DEFAULT_SCHEDULES[reading_cycle]
		self._repository.upsert_measurement_definition_schedule(measurement_definition_id, serialize_schedule(default_schedule))
		logger.info("Created default measurement definition schedule for %s (%s)", measurement_definition_id, reading_cycle.value)
		return default_schedule

	@classmethod
	def serialize_default(cls, reading_cycle: ReadingCycle) -> str:
		"""指定 reading_cycle の default schedule を JSON 文字列で返す。"""

		return serialize_schedule(DEFAULT_SCHEDULES[reading_cycle])


class ReleaseOffsetSettingsService(ReleaseDateCalculator):
	"""reading_session ごとの release offset を読み書きする service。"""

	def __init__(self, repository: ScheduleSettingsRepository) -> None:
		self._repository = repository

	def get_release_offset_days(self, reading_session: ReadingSession) -> int:
		"""指定 session の release offset を返す。未設定ならデフォルトを使う。"""

		key = RELEASE_OFFSET_SETTINGS_KEYS[reading_session]
		raw = self._repository.get_setting(key)
		if raw is None:
			return DEFAULT_RELEASE_OFFSETS.get(reading_session, 0)
		return _deserialize_release_offset_days(raw)

	def save_release_offset_days(self, reading_session: ReadingSession, offset_days: int) -> None:
		"""指定 session の release offset を保存する。"""

		key = RELEASE_OFFSET_SETTINGS_KEYS[reading_session]
		self._repository.upsert_setting(key, _serialize_release_offset_days(offset_days))
		logger.info("Saved release offset for %s: %s", reading_session.value, offset_days)

	def compute_release_date(self, business_date: date, reading_session: ReadingSession) -> date:
		"""business_date と session から配布可能日を計算する。"""

		return business_date + timedelta(days=self.get_release_offset_days(reading_session))

	@classmethod
	def serialize_default(cls, reading_session: ReadingSession) -> str:
		"""指定 session のデフォルト offset を文字列で返す。"""

		return _serialize_release_offset_days(DEFAULT_RELEASE_OFFSETS.get(reading_session, 0))


# ---------------------------------------------------------------------------
# cutoff settings
# ---------------------------------------------------------------------------

CUTOFF_SETTINGS_KEY = "auto_close.cutoff_time.v1"
DEFAULT_CUTOFF_TIME = time(hour=7, minute=0)
ACTIVE_DEVICE_ID_SETTINGS_KEY = "device_binding.active_device_id.v1"


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


def normalize_device_id(value: str) -> str:
	"""device_id を UUID として検証し、canonical 文字列へ正規化する。"""

	stripped = value.strip()
	if not stripped:
		raise ValueError("device_id must not be blank.")
	try:
		return str(UUID(stripped))
	except ValueError as exc:  # pragma: no cover - exercised via callers
		raise ValueError("device_id must be a valid UUID.") from exc


class DeviceBindingRepository(Protocol):
	"""主端末制に必要な active device と候補一覧を扱う repository。"""

	def get_setting(self, key: str) -> str | None: ...
	def upsert_setting(self, key: str, value: str) -> None: ...
	def delete_setting(self, key: str) -> None: ...
	def list_recent_device_ids(self, *, limit: int) -> list[str]: ...


class ActiveDeviceBindingService:
	"""主端末の取得・更新・解除と候補一覧を扱う service。"""

	def __init__(self, repository: DeviceBindingRepository) -> None:
		self._repository = repository

	def get_active_device_id(self) -> str | None:
		"""現在の主端末 device_id を返す。未設定なら None。"""

		raw = self._repository.get_setting(ACTIVE_DEVICE_ID_SETTINGS_KEY)
		if raw is None:
			return None
		return normalize_device_id(raw)

	def bind_active_device(self, device_id: str) -> str:
		"""主端末 device_id を保存し、canonical 値を返す。"""

		normalized = normalize_device_id(device_id)
		self._repository.upsert_setting(ACTIVE_DEVICE_ID_SETTINGS_KEY, normalized)
		logger.info("Saved active device binding: %s", normalized)
		return normalized

	def clear_active_device_id(self) -> None:
		"""主端末 device_id を未設定状態へ戻す。"""

		self._repository.delete_setting(ACTIVE_DEVICE_ID_SETTINGS_KEY)
		logger.info("Cleared active device binding")

	def list_recent_device_candidates(self, *, limit: int = 5) -> list[str]:
		"""最近 reading transaction に出現した device_id 候補を返す。"""

		if limit <= 0:
			return []
		return [
			normalize_device_id(device_id)
			for device_id in self._repository.list_recent_device_ids(limit=limit)
		]


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
	"DailySchedule",
	"EveryTwoMonthsSchedule",
	"ReleaseDateCalculator",
	"ReleaseOffsetSettingsService",
	"ScheduleItem",
	"MonthlySchedule",
	"MonthlyScheduleSelectorType",
	"MonthParity",
	"MeasurementDefinitionScheduleRepository",
	"MeasurementDefinitionScheduleService",
	"RELEASE_OFFSET_SETTINGS_KEYS",
	"SCHEDULE_SETTINGS_KEYS",
	"deserialize_schedule",
	"ScheduleSettings",
	"ScheduleSettingsProvider",
	"ScheduleSettingsRepository",
	"ScheduleSettingsService",
	"serialize_schedule",
	"validate_schedule_type_for_reading_cycle",
	"ScheduleValidationError",
	"expected_daily_count",
	"validate_expected_daily_count",
	"WeeklySchedule",
]
