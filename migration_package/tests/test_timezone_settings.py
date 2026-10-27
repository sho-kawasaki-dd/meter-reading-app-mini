from __future__ import annotations

from datetime import UTC, date, datetime
from zoneinfo import ZoneInfo

import pytest

from meter_reading_mini.core.errors import InvalidTimezoneError
from meter_reading_mini.core.timeutil import DB_DATETIME_FORMAT
from meter_reading_mini.core.timezone_settings import (
    DEFAULT_APP_TIMEZONE,
    build_business_date_provider,
    build_current_datetime_provider,
    build_scheduled_at,
    normalize_to_utc,
)


def test_normalize_to_utc_converts_an_aware_datetime() -> None:
    value = datetime.fromisoformat("2026-04-01T08:00:00+09:00")

    assert normalize_to_utc(value) == datetime(2026, 3, 31, 23, 0, tzinfo=UTC)


def test_normalize_to_utc_rejects_a_naive_datetime() -> None:
    with pytest.raises(InvalidTimezoneError):
        normalize_to_utc(datetime(2026, 4, 1, 8, 0))


def test_build_scheduled_at_uses_the_configured_business_timezone() -> None:
    assert build_scheduled_at(date(2026, 4, 1), "08:00") == datetime(2026, 3, 31, 23, 0, tzinfo=UTC)


def test_build_scheduled_at_applies_day_offset_before_combining_time() -> None:
    assert build_scheduled_at(date(2026, 4, 1), "00:00", day_offset=1) == datetime(
        2026, 4, 1, 15, 0, tzinfo=UTC
    )


def test_build_scheduled_at_accepts_an_explicit_timezone() -> None:
    assert build_scheduled_at(
        date(2026, 4, 1),
        "08:00",
        tzinfo=ZoneInfo("UTC"),
    ) == datetime(2026, 4, 1, 8, 0, tzinfo=UTC)


def test_current_datetime_provider_converts_utc_now_to_business_timezone(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from meter_reading_mini.core import timezone_settings

    monkeypatch.setattr(
        timezone_settings,
        "utc_now",
        lambda: datetime(2026, 4, 1, 1, 0, tzinfo=UTC),
    )

    now = build_current_datetime_provider(lambda: DEFAULT_APP_TIMEZONE)()

    assert now == datetime(2026, 4, 1, 10, 0, tzinfo=DEFAULT_APP_TIMEZONE)


def test_business_date_provider_uses_the_converted_utc_date(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from meter_reading_mini.core import timezone_settings

    monkeypatch.setattr(
        timezone_settings,
        "utc_now",
        lambda: datetime(2026, 3, 31, 16, 0, tzinfo=UTC),
    )

    assert build_business_date_provider()() == date(2026, 4, 1)


def test_signature_datetime_format_is_utc_second_precision() -> None:
    assert DB_DATETIME_FORMAT == "%Y-%m-%d %H:%M:%S"
