"""app_settings からアプリ固定タイムゾーンを読み書きする service。"""

from __future__ import annotations

import logging
from collections.abc import Callable
from datetime import UTC, date, datetime, time, timedelta, tzinfo
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from meter_reading_mini.core.errors import InvalidTimezoneError, TimezoneChangeError
from meter_reading_mini.core.meter_reading_sync_repositories import (
    TimezoneSettingsRepository,
)
from meter_reading_mini.core.timeutil import utc_now

logger = logging.getLogger(__name__)

APP_TIMEZONE_SETTINGS_KEY = "app.timezone.v1"
DEFAULT_APP_TIMEZONE_NAME = "Asia/Tokyo"
DEFAULT_APP_TIMEZONE = ZoneInfo(DEFAULT_APP_TIMEZONE_NAME)


def _normalize_timezone_name(timezone_name: str | ZoneInfo) -> str:
    """保存前の timezone 名を検証し、canonical 文字列へ正規化する。"""

    normalized = timezone_name.key if isinstance(timezone_name, ZoneInfo) else timezone_name.strip()

    if not normalized:
        raise InvalidTimezoneError("timezone_name must not be blank.")

    try:
        ZoneInfo(normalized)
    except ZoneInfoNotFoundError as exc:  # pragma: no cover - validated by callers
        raise InvalidTimezoneError(f"Invalid timezone: {normalized}") from exc

    return normalized


def build_current_datetime_provider(
    timezone_provider: Callable[[], tzinfo] | None = None,
) -> Callable[[], datetime]:
    """timezone-aware な現在時刻 provider を組み立てる。"""

    resolved_timezone_provider = timezone_provider or (lambda: DEFAULT_APP_TIMEZONE)
    return lambda: utc_now().astimezone(resolved_timezone_provider())


def build_business_date_provider(
    timezone_provider: Callable[[], tzinfo] | None = None,
) -> Callable[[], date]:
    """timezone-aware な business_date provider を組み立てる。"""

    resolved_timezone_provider = timezone_provider or (lambda: DEFAULT_APP_TIMEZONE)
    return lambda: utc_now().astimezone(resolved_timezone_provider()).date()


def normalize_to_utc(value: datetime) -> datetime:
    """タイムゾーン付き日時を UTC へ正規化する。"""

    if value.tzinfo is None or value.utcoffset() is None:
        raise InvalidTimezoneError("日時はタイムゾーン付きで指定してください。")
    return value.astimezone(UTC)


def build_scheduled_at(
    business_date: date,
    reading_session: str,
    *,
    day_offset: int = 0,
    tzinfo: tzinfo | None = None,
) -> datetime:
    """業務日と検針時間から UTC の予定日時を構築する。"""

    effective_timezone = tzinfo or DEFAULT_APP_TIMEZONE
    local_datetime = datetime.combine(
        business_date + timedelta(days=day_offset),
        time.fromisoformat(reading_session),
        tzinfo=effective_timezone,
    )
    return normalize_to_utc(local_datetime)


class TimezoneSettingsService:
    """app timezone を読み書きする service。"""

    def __init__(self, repository: TimezoneSettingsRepository) -> None:
        self._repository = repository

    def get_timezone(self) -> ZoneInfo:
        """現在の app timezone を返す。未設定なら Asia/Tokyo。"""

        raw = self._repository.get_setting(APP_TIMEZONE_SETTINGS_KEY)
        if raw is None:
            return DEFAULT_APP_TIMEZONE

        try:
            return ZoneInfo(raw)
        except ZoneInfoNotFoundError as exc:
            raise InvalidTimezoneError(f"Invalid timezone stored in app_settings: {raw}") from exc

    def save_timezone(self, timezone_name: str | ZoneInfo) -> ZoneInfo:
        """app timezone を保存し、canonical ZoneInfo を返す。"""

        normalized = _normalize_timezone_name(timezone_name)
        if normalized != self.get_timezone().key and self._repository.has_planned_readings():
            raise TimezoneChangeError("予定枠の生成後は業務タイムゾーンを変更できません。")
        self._repository.upsert_setting(APP_TIMEZONE_SETTINGS_KEY, normalized)
        logger.info("Saved app timezone: %s", normalized)
        return ZoneInfo(normalized)


__all__ = [
    "APP_TIMEZONE_SETTINGS_KEY",
    "DEFAULT_APP_TIMEZONE",
    "DEFAULT_APP_TIMEZONE_NAME",
    "TimezoneSettingsService",
    "build_scheduled_at",
    "build_business_date_provider",
    "build_current_datetime_provider",
    "normalize_to_utc",
]
