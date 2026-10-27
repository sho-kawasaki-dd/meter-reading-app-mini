from __future__ import annotations

import sqlite3
from datetime import UTC, date, datetime, time
from decimal import Decimal
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

from meter_reading_mini.core.contract_models import (
    CalculationMethod,
    Category,
    Purpose,
    ReadingCycle,
    ValueSchema,
)
from meter_reading_mini.core.meter_reading_sync_repositories import (
    MeasurementDefinitionCreateRequest,
    MeterMasterCreateRequest,
)
from meter_reading_mini.core.schedule_settings import ReleaseOffsetSettingsService
from meter_reading_mini.core.startup_preparation import StartupPreparationService
from meter_reading_mini.core.work_package_download import (
    PlannedReadingGeneratorService,
    PlannedReadingLifecycleService,
)
from meter_reading_mini.db.bootstrap_seeds import bootstrap_default_settings
from meter_reading_mini.db.connection import transaction
from meter_reading_mini.db.migrations import create_schema
from meter_reading_mini.db.repositories import SQLiteRepository

_TOKYO = ZoneInfo("Asia/Tokyo")
_FIXED_CLOCK = datetime(2026, 2, 4, 3, 0, tzinfo=UTC)


def _create_definition(conn: sqlite3.Connection, repository: SQLiteRepository) -> None:
    with transaction(conn):
        repository.create_meter_master(
            MeterMasterCreateRequest(
                meter_id="meter-startup",
                location_id="location-startup",
                location_name="架空の試験棟",
                meter_name="試験用メーター",
                category=Category.ELECTRICITY,
            )
        )
        repository.create_measurement_definition(
            MeasurementDefinitionCreateRequest(
                measurement_definition_id="definition-startup",
                location_id="location-startup",
                display_name="試験用積算電力量",
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
                initial_schedule_json=(
                    '{"reading_sessions":[{"reading_session":"08:00","day_offset":0}]}'
                ),
                initial_meter_id="meter-startup",
            )
        )


def _build_startup_service(
    conn: sqlite3.Connection,
    repository: SQLiteRepository,
    *,
    bootstrapper,
    current_datetime: datetime,
) -> tuple[StartupPreparationService, PlannedReadingGeneratorService]:
    def transaction_factory():
        return transaction(conn)

    def date_provider() -> date:
        return current_datetime.astimezone(_TOKYO).date()

    release_offset_service = ReleaseOffsetSettingsService(
        repository,
        recomposition_repository=repository,
        transaction_factory=transaction_factory,
        current_business_date_provider=date_provider,
        clock=lambda: _FIXED_CLOCK,
    )
    generator = PlannedReadingGeneratorService(
        repository,
        release_date_calculator=release_offset_service,
        scheduled_timezone_provider=lambda: _TOKYO,
    )
    lifecycle = PlannedReadingLifecycleService(
        repository,
        current_datetime_provider=lambda: current_datetime.astimezone(_TOKYO),
        cutoff_time_provider=lambda: time(7),
    )
    return (
        StartupPreparationService(
            bootstrapper=bootstrapper,
            planned_reading_generator=generator,
            planned_reading_lifecycle_service=lifecycle,
            transaction_factory=transaction_factory,
            business_date_provider=date_provider,
        ),
        generator,
    )


def test_startup_preparation_seeds_backfills_and_closes_in_one_operation(
    file_database_repository: tuple[sqlite3.Connection, SQLiteRepository],
) -> None:
    conn, repository = file_database_repository
    _create_definition(conn, repository)
    startup_service, _ = _build_startup_service(
        conn,
        repository,
        bootstrapper=lambda: repository.upsert_setting("startup.probe", "seeded"),
        current_datetime=datetime(2026, 2, 6, 8, 0, tzinfo=_TOKYO),
    )

    startup_service.prepare()

    assert repository.get_setting("startup.probe") == "seeded"
    assert conn.execute(
        "SELECT business_date, task_state FROM planned_readings "
        "WHERE measurement_definition_id = ? ORDER BY business_date",
        ("definition-startup",),
    ).fetchall() == [
        ("2026-02-05", "auto_closed"),
        ("2026-02-06", "open"),
    ]


def test_startup_preparation_failure_rolls_back_seed_and_completed_chunks(
    file_database_repository: tuple[sqlite3.Connection, SQLiteRepository],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    conn, repository = file_database_repository
    _create_definition(conn, repository)
    startup_service, generator = _build_startup_service(
        conn,
        repository,
        bootstrapper=lambda: repository.upsert_setting("startup.probe", "partial"),
        current_datetime=datetime(2026, 2, 25, 8, 0, tzinfo=_TOKYO),
    )
    ensure_generated = generator.ensure_generated_through

    def generate_then_fail(target_business_date: date) -> None:
        ensure_generated(target_business_date)
        raise RuntimeError("startup preparation failed")

    monkeypatch.setattr(generator, "ensure_generated_through", generate_then_fail)
    with pytest.raises(RuntimeError, match="startup preparation failed"):
        startup_service.prepare()

    assert repository.get_setting("startup.probe") is None
    assert conn.execute("SELECT COUNT(*) FROM planned_readings").fetchone() == (0,)
    assert not conn.in_transaction


def test_startup_preparation_commit_failure_rolls_back_all_seed_rows(
    tmp_path: Path,
) -> None:
    class CommitFailingConnection(sqlite3.Connection):
        fail_commit = False

        def execute(self, sql: str, parameters=()):
            if self.fail_commit and sql.strip().upper() == "COMMIT":
                raise sqlite3.OperationalError("simulated commit failure")
            return super().execute(sql, parameters)

    conn = sqlite3.connect(
        tmp_path / "commit-failure.db",
        autocommit=True,
        factory=CommitFailingConnection,
    )
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA journal_mode = WAL")
    create_schema(conn)
    repository = SQLiteRepository(
        conn,
        clock=lambda: _FIXED_CLOCK,
        business_date_provider=lambda: date(2026, 2, 4),
    )

    def transaction_factory():
        return transaction(conn)

    def clock() -> datetime:
        return _FIXED_CLOCK

    release_offset_service = ReleaseOffsetSettingsService(
        repository,
        recomposition_repository=repository,
        transaction_factory=transaction_factory,
        current_business_date_provider=lambda: date(2026, 2, 4),
        clock=clock,
    )
    generator = PlannedReadingGeneratorService(
        repository,
        release_date_calculator=release_offset_service,
        scheduled_timezone_provider=lambda: _TOKYO,
    )
    lifecycle = PlannedReadingLifecycleService(
        repository,
        current_datetime_provider=lambda: datetime(2026, 2, 4, 12, 0, tzinfo=_TOKYO),
        cutoff_time_provider=lambda: time(7),
    )
    startup_service = StartupPreparationService(
        bootstrapper=lambda: bootstrap_default_settings(conn, clock=clock),
        planned_reading_generator=generator,
        planned_reading_lifecycle_service=lifecycle,
        transaction_factory=transaction_factory,
        business_date_provider=lambda: date(2026, 2, 4),
    )
    conn.fail_commit = True
    try:
        with pytest.raises(sqlite3.OperationalError, match="simulated commit failure"):
            startup_service.prepare()

        assert conn.execute("SELECT COUNT(*) FROM meter_app_settings").fetchone() == (0,)
        assert conn.execute("SELECT COUNT(*) FROM reading_cycle_schedule_defaults").fetchone() == (
            0,
        )
        assert conn.execute("SELECT COUNT(*) FROM reading_session_release_offsets").fetchone() == (
            0,
        )
        assert not conn.in_transaction
    finally:
        conn.close()
