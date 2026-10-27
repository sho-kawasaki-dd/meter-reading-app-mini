from __future__ import annotations

import sqlite3
from datetime import UTC, date, datetime
from decimal import Decimal
from zoneinfo import ZoneInfo

import pytest

from meter_reading_mini.core.contract_models import (
    CalculationMethod,
    Category,
    Purpose,
    ReadingCycle,
    ReadingSession,
    ValueSchema,
)
from meter_reading_mini.core.errors import InvalidTimezoneError, TimezoneChangeError
from meter_reading_mini.core.meter_reading_sync_repositories import (
    MeasurementDefinitionCreateRequest,
    MeterMasterCreateRequest,
    PlannedReadingCreateRequest,
)
from meter_reading_mini.core.timeutil import DB_DATETIME_FORMAT
from meter_reading_mini.core.timezone_settings import (
    DEFAULT_APP_TIMEZONE,
    TimezoneSettingsService,
    build_business_date_provider,
    build_current_datetime_provider,
    build_scheduled_at,
    normalize_to_utc,
)
from meter_reading_mini.db.connection import transaction
from meter_reading_mini.db.repositories import SQLiteRepository


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


def _insert_planned_reading(conn: sqlite3.Connection, repository: SQLiteRepository) -> None:
    with transaction(conn):
        repository.create_meter_master(
            MeterMasterCreateRequest(
                meter_id="timezone-meter",
                location_id="timezone-location",
                location_name="架空の試験棟",
                meter_name="試験用メーター",
                category=Category.ELECTRICITY,
            )
        )
        repository.create_measurement_definition(
            MeasurementDefinitionCreateRequest(
                measurement_definition_id="timezone-definition",
                location_id="timezone-location",
                display_name="試験用測定定義",
                category=Category.ELECTRICITY,
                unit="kWh",
                value_schema=ValueSchema.DECIMAL_STRING,
                multiplier=Decimal("1"),
                decimal_places=2,
                warning_threshold=None,
                rollover_value=None,
                calculation_method=CalculationMethod.DELTA,
                reading_cycle=ReadingCycle.DAILY_1X,
                purpose=Purpose.OPERATIONAL,
                generation_start_date=date(2026, 2, 5),
                initial_schedule_json='{"reading_sessions":[{"reading_session":"08:00"}]}',
                initial_meter_id="timezone-meter",
            )
        )
        repository.create_planned_reading_if_missing(
            PlannedReadingCreateRequest(
                planned_reading_id="timezone-planned",
                measurement_definition_id="timezone-definition",
                meter_id="timezone-meter",
                location_id="timezone-location",
                business_date=date(2026, 2, 5),
                release_date=date(2026, 2, 5),
                reading_session=ReadingSession.SESSION_0800,
                scheduled_at=build_scheduled_at(date(2026, 2, 5), "08:00"),
            )
        )


def test_timezone_change_is_allowed_before_use_and_same_value_is_allowed_after_use(
    file_database_repository: tuple[sqlite3.Connection, SQLiteRepository],
) -> None:
    conn, repository = file_database_repository
    service = TimezoneSettingsService(
        repository,
        transaction_factory=lambda: transaction(conn),
    )

    assert service.save_timezone("UTC") == ZoneInfo("UTC")
    _insert_planned_reading(conn, repository)
    assert service.save_timezone("UTC") == ZoneInfo("UTC")

    with pytest.raises(TimezoneChangeError):
        service.save_timezone("Asia/Tokyo")
    assert service.get_timezone() == ZoneInfo("UTC")


def test_initial_default_timezone_can_be_saved_when_planned_rows_exist(
    file_database_repository: tuple[sqlite3.Connection, SQLiteRepository],
) -> None:
    conn, repository = file_database_repository
    conn.execute("DELETE FROM meter_app_settings WHERE setting_key = 'app.timezone.v1'")
    _insert_planned_reading(conn, repository)
    service = TimezoneSettingsService(
        repository,
        transaction_factory=lambda: transaction(conn),
    )

    assert service.save_timezone("Asia/Tokyo") == ZoneInfo("Asia/Tokyo")
