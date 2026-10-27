"""開発環境・初回起動用の既定設定投入。"""

from __future__ import annotations

import sqlite3
from collections.abc import Callable
from dataclasses import dataclass
from datetime import date, datetime

from meter_reading_mini.core.contract_models import ReadingCycle, ReadingSession
from meter_reading_mini.core.errors import ValidationError
from meter_reading_mini.core.timeutil import utc_now, utc_now_str

_INITIAL_EFFECTIVE_DATE = date(1, 1, 1)
_TIMEZONE_SETTING = "app.timezone.v1"
_DEFAULT_TIMEZONE = "Asia/Tokyo"


@dataclass(frozen=True, slots=True)
class BootstrapSeedResult:
    inserted_settings: int
    inserted_schedule_defaults: int
    inserted_release_offsets: int


_DEFAULT_SCHEDULE_JSON: dict[ReadingCycle, str] = {
    ReadingCycle.DAILY_1X: '{"reading_sessions":[{"reading_session":"08:00","day_offset":0}]}',
    ReadingCycle.DAILY_2X: (
        '{"reading_sessions":[{"reading_session":"08:00","day_offset":0},'
        '{"reading_session":"22:00","day_offset":0}]}'
    ),
    ReadingCycle.DAILY_3X: (
        '{"reading_sessions":[{"reading_session":"06:00","day_offset":0},'
        '{"reading_session":"14:00","day_offset":0},'
        '{"reading_session":"22:00","day_offset":0}]}'
    ),
    ReadingCycle.WEEKLY: '{"weekday":"sunday","reading_session":"08:00","day_offset":0}',
    ReadingCycle.MONTHLY: (
        '{"selector_type":"day_of_month","reading_session":"08:00","day_offset":0,"day_of_month":1}'
    ),
    ReadingCycle.EVERY_TWO_MONTHS: (
        '{"selector_type":"day_of_month","month_parity":"odd",'
        '"reading_session":"08:00","day_offset":0,"day_of_month":1}'
    ),
}

_DEFAULT_RELEASE_OFFSETS: dict[ReadingSession, int] = {
    session: (-1 if session is ReadingSession.SESSION_0000 else 0) for session in ReadingSession
}


def bootstrap_default_settings(
    conn: sqlite3.Connection,
    *,
    clock: Callable[[], datetime] = utc_now,
) -> BootstrapSeedResult:
    """設定・適用履歴の初版だけを冪等に投入する。呼出側がトランザクションを所有する。"""
    created_at = utc_now_str(clock())
    settings = (
        ("auto_close.cutoff_time.v1", "07:00"),
        (_TIMEZONE_SETTING, _DEFAULT_TIMEZONE),
    )
    inserted_settings = 0
    for key, value in settings:
        try:
            cursor = conn.execute(
                "INSERT INTO meter_app_settings (setting_key, setting_value, updated_at) "
                "VALUES (?, ?, ?) ON CONFLICT(setting_key) DO NOTHING",
                (key, value, created_at),
            )
        except sqlite3.IntegrityError as exc:
            raise ValidationError(f"既定設定を登録できませんでした: {exc}") from exc
        inserted_settings += cursor.rowcount

    inserted_defaults = 0
    for cycle, schedule_json in _DEFAULT_SCHEDULE_JSON.items():
        try:
            cursor = conn.execute(
                "INSERT INTO reading_cycle_schedule_defaults "
                "(reading_cycle, effective_from_date, schedule_json, created_at) "
                "VALUES (?, ?, ?, ?) "
                "ON CONFLICT(reading_cycle, effective_from_date) DO NOTHING",
                (cycle.value, _INITIAL_EFFECTIVE_DATE.isoformat(), schedule_json, created_at),
            )
        except sqlite3.IntegrityError as exc:
            raise ValidationError(f"既定スケジュールを登録できませんでした: {exc}") from exc
        inserted_defaults += cursor.rowcount

    inserted_offsets = 0
    for session, offset in _DEFAULT_RELEASE_OFFSETS.items():
        try:
            cursor = conn.execute(
                "INSERT INTO reading_session_release_offsets "
                "(reading_session, effective_from_date, offset_days, created_at) "
                "VALUES (?, ?, ?, ?) "
                "ON CONFLICT(reading_session, effective_from_date) DO NOTHING",
                (session.value, _INITIAL_EFFECTIVE_DATE.isoformat(), offset, created_at),
            )
        except sqlite3.IntegrityError as exc:
            raise ValidationError(f"既定配信オフセットを登録できませんでした: {exc}") from exc
        inserted_offsets += cursor.rowcount

    return BootstrapSeedResult(inserted_settings, inserted_defaults, inserted_offsets)


__all__ = ["BootstrapSeedResult", "bootstrap_default_settings"]
