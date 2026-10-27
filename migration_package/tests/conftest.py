from __future__ import annotations

import json
import os
import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, date, datetime, time
from decimal import Decimal
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest
from PySide6.QtGui import QFont
from PySide6.QtWidgets import QApplication

from meter_reading_mini.core.contract_models import (
    CalculationMethod,
    Category,
    Purpose,
    ReadingCycle,
    ReadingSession,
    ValueSchema,
)
from meter_reading_mini.core.meter_reading_sync_repositories import (
    MeasurementDefinitionCreateRequest,
    MeterMasterCreateRequest,
    PlannedReadingCloseReason,
    PlannedReadingCreateRequest,
)
from meter_reading_mini.db.bootstrap_seeds import bootstrap_default_settings
from meter_reading_mini.db.connection import connect, connect_memory, transaction
from meter_reading_mini.db.migrations import create_schema
from meter_reading_mini.db.repositories import SQLiteRepository
from meter_reading_mini.ui import display_settings

WORK_AREA_WIDTH = 1366
WORK_AREA_HEIGHT = 768 - 40


@pytest.fixture(scope="session")
def qapp_args(tmp_path_factory: pytest.TempPathFactory) -> list[str]:
    if os.environ.get("QT_QPA_PLATFORM") != "offscreen":
        return []
    config_path = tmp_path_factory.mktemp("qt-offscreen") / "config.json"
    config_path.write_text(
        json.dumps({"screens": [{"width": WORK_AREA_WIDTH, "height": WORK_AREA_HEIGHT}]}),
        encoding="utf-8",
    )
    return ["pytest", "-platform", f"offscreen:configfile={config_path}"]


@pytest.fixture
def restore_app_font(qapp: QApplication, monkeypatch: pytest.MonkeyPatch) -> Iterator[QFont]:
    original = QFont(qapp.font())
    monkeypatch.setattr(display_settings, "_base_font", None)
    yield original
    QApplication.setFont(original)


@contextmanager
def unchecked_constraints(conn: sqlite3.Connection) -> Iterator[None]:
    conn.execute("PRAGMA ignore_check_constraints = ON")
    try:
        yield
    finally:
        conn.execute("PRAGMA ignore_check_constraints = OFF")


@pytest.fixture
def memory_conn() -> Iterator[sqlite3.Connection]:
    conn = connect_memory()
    create_schema(conn)
    try:
        yield conn
    finally:
        conn.close()


@pytest.fixture
def fixed_clock() -> datetime:
    return datetime(2026, 2, 4, 3, 0, 0, tzinfo=UTC)


@pytest.fixture
def sqlite_repository(memory_conn: sqlite3.Connection, fixed_clock: datetime) -> SQLiteRepository:
    def clock() -> datetime:
        return fixed_clock

    def business_date() -> date:
        return date(2026, 2, 4)

    bootstrap_default_settings(memory_conn, clock=clock)
    return SQLiteRepository(
        memory_conn,
        clock=clock,
        business_date_provider=business_date,
    )


@pytest.fixture
def file_database_repository(
    tmp_path: Path, fixed_clock: datetime
) -> Iterator[tuple[sqlite3.Connection, SQLiteRepository]]:
    conn = connect(tmp_path / "meter-reading.db")
    create_schema(conn)

    def clock() -> datetime:
        return fixed_clock

    def business_date() -> date:
        return fixed_clock.astimezone(ZoneInfo("Asia/Tokyo")).date()

    with transaction(conn):
        bootstrap_default_settings(conn, clock=clock)
    repository = SQLiteRepository(
        conn,
        clock=clock,
        business_date_provider=business_date,
    )
    try:
        yield conn, repository
    finally:
        conn.close()


@pytest.fixture
def sample_master(sqlite_repository: SQLiteRepository) -> tuple[str, str]:
    meter_id = "meter-test-001"
    definition_id = "definition-test-001"
    sqlite_repository.create_meter_master(
        MeterMasterCreateRequest(
            meter_id=meter_id,
            location_id="location-test-001",
            location_name="架空の試験棟",
            meter_name="試験用電力量計",
            category=Category.ELECTRICITY,
            meter_serial="TEST-001",
        )
    )
    sqlite_repository.create_measurement_definition(
        MeasurementDefinitionCreateRequest(
            measurement_definition_id=definition_id,
            location_id="location-test-001",
            display_name="試験用積算電力量",
            category=Category.ELECTRICITY,
            unit="kWh",
            value_schema=ValueSchema.DECIMAL_STRING,
            multiplier=Decimal("1.00"),
            decimal_places=2,
            warning_threshold=Decimal("900.00"),
            rollover_value=Decimal("1000"),
            calculation_method=CalculationMethod.DELTA,
            reading_cycle=ReadingCycle.DAILY_1X,
            purpose=Purpose.OPERATIONAL,
            generation_start_date=date(2026, 2, 1),
            initial_schedule_json='{"reading_sessions":[{"reading_session":"08:00","day_offset":0}]}',
            initial_meter_id=meter_id,
        )
    )
    return meter_id, definition_id


@pytest.fixture
def superseded_planned_reading_ids(
    memory_conn: sqlite3.Connection,
    sqlite_repository: SQLiteRepository,
    sample_master: tuple[str, str],
) -> tuple[str, str, str]:
    meter_id, definition_id = sample_master
    replaced_id = "fixture-replaced-original"
    replacement_id = "fixture-replacement-successor"
    deactivated_id = "fixture-deactivated-slot"
    with transaction(memory_conn):
        sqlite_repository.create_planned_readings_if_missing(
            [
                PlannedReadingCreateRequest(
                    planned_reading_id=replaced_id,
                    measurement_definition_id=definition_id,
                    meter_id=meter_id,
                    location_id="location-test-001",
                    business_date=date(2026, 2, 4),
                    release_date=date(2026, 2, 4),
                    reading_session=ReadingSession.SESSION_0800,
                    scheduled_at=fixed_schedule_datetime(date(2026, 2, 4)),
                ),
                PlannedReadingCreateRequest(
                    planned_reading_id=replacement_id,
                    measurement_definition_id=definition_id,
                    meter_id=meter_id,
                    location_id="location-test-001",
                    business_date=date(2026, 2, 4),
                    release_date=date(2026, 2, 4),
                    reading_session=ReadingSession.SESSION_0800,
                    scheduled_at=fixed_schedule_datetime(date(2026, 2, 4)),
                ),
                PlannedReadingCreateRequest(
                    planned_reading_id=deactivated_id,
                    measurement_definition_id=definition_id,
                    meter_id=meter_id,
                    location_id="location-test-001",
                    business_date=date(2026, 2, 5),
                    release_date=date(2026, 2, 5),
                    reading_session=ReadingSession.SESSION_0800,
                    scheduled_at=fixed_schedule_datetime(date(2026, 2, 5)),
                ),
            ]
        )
        memory_conn.execute(
            "UPDATE planned_readings SET task_state = 'superseded', closed_at = ?, "
            "close_reason = ?, superseded_by_planned_reading_id = ? "
            "WHERE planned_reading_id = ?",
            (
                "2026-02-04 03:00:00",
                PlannedReadingCloseReason.METER_REPLACED.value,
                replacement_id,
                replaced_id,
            ),
        )
        memory_conn.execute(
            "UPDATE planned_readings SET task_state = 'superseded', closed_at = ?, "
            "close_reason = ? WHERE planned_reading_id = ?",
            (
                "2026-02-04 03:00:00",
                PlannedReadingCloseReason.DEFINITION_DEACTIVATED.value,
                deactivated_id,
            ),
        )
    return replaced_id, replacement_id, deactivated_id


def fixed_schedule_datetime(business_date: date) -> datetime:
    return datetime.combine(
        business_date,
        time(8),
        tzinfo=ZoneInfo("Asia/Tokyo"),
    ).astimezone(UTC)
