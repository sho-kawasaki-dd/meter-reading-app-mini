from __future__ import annotations

from datetime import UTC, date, datetime, time, tzinfo

DB_DATETIME_FORMAT = "%Y-%m-%d %H:%M:%S"


def utc_now() -> datetime:
    return datetime.now(UTC)


def _require_aware(value: datetime) -> None:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("タイムゾーン付きの日時を指定してください")


def utc_now_str(now: datetime | None = None) -> str:
    value = utc_now() if now is None else now
    _require_aware(value)
    return value.astimezone(UTC).strftime(DB_DATETIME_FORMAT)


def parse_utc(utc_str: str) -> datetime:
    value = datetime.strptime(utc_str, DB_DATETIME_FORMAT)
    if value.strftime(DB_DATETIME_FORMAT) != utc_str:
        raise ValueError("日時は YYYY-MM-DD HH:MM:SS 形式で指定してください")
    return value.replace(tzinfo=UTC)


def is_before_utc(value: str, reference: str) -> bool:
    return parse_utc(value) < parse_utc(reference)


def to_local(utc_str: str, tz: tzinfo | None = None) -> datetime:
    value = parse_utc(utc_str)
    return value.astimezone(tz) if tz is not None else value.astimezone()


def format_local(utc_str: str, tz: tzinfo | None = None) -> str:
    return to_local(utc_str, tz).strftime(DB_DATETIME_FORMAT)


def local_date(utc_str: str, tz: tzinfo | None = None) -> date:
    return to_local(utc_str, tz).date()


def _local_midnight_as_utc(value: date, tz: tzinfo | None) -> datetime:
    local_midnight = datetime.combine(value, time.min)
    if tz is None:
        return local_midnight.astimezone(UTC)
    return local_midnight.replace(tzinfo=tz).astimezone(UTC)


def local_range_to_utc(start: date, end: date, tz: tzinfo | None = None) -> tuple[str, str]:
    if start >= end:
        raise ValueError("開始日は終了日より前でなければなりません")
    return (
        _local_midnight_as_utc(start, tz).strftime(DB_DATETIME_FORMAT),
        _local_midnight_as_utc(end, tz).strftime(DB_DATETIME_FORMAT),
    )


def _as_local(now: datetime, tz: tzinfo | None) -> datetime:
    _require_aware(now)
    return now.astimezone(tz) if tz is not None else now.astimezone()


def local_today(now: datetime | None = None, tz: tzinfo | None = None) -> date:
    value = utc_now() if now is None else now
    return _as_local(value, tz).date()


def local_timestamp_for_filename(now: datetime | None = None, tz: tzinfo | None = None) -> str:
    value = utc_now() if now is None else now
    return _as_local(value, tz).strftime("%Y%m%d_%H%M%S")
