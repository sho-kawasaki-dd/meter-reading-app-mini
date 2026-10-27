from __future__ import annotations

import sqlite3
from datetime import UTC, date, datetime, time
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
from meter_reading_mini.core.errors import (
    MeterAssignmentResolutionError,
    ScheduleHistoryResolutionError,
)
from meter_reading_mini.core.meter_reading_sync_repositories import (
    MeasurementDefinitionCreateRequest,
    MeterMasterCreateRequest,
    PlannedReadingCloseReason,
    PlannedReadingCreateRequest,
)
from meter_reading_mini.core.schedule_settings import ReleaseOffsetSettingsService
from meter_reading_mini.core.timezone_settings import build_scheduled_at
from meter_reading_mini.core.work_package_download import (
    FutureBusinessDateError,
    PlannedReadingGeneratorService,
    PlannedReadingLifecycleService,
    WorkPackageDownloadService,
    _resolve_auto_close_through_business_date,
)
from meter_reading_mini.db.connection import transaction
from meter_reading_mini.db.repositories import SQLiteRepository

_CURRENT_TIMEZONE = ZoneInfo("Asia/Tokyo")
_CURRENT_BUSINESS_DATE = date(2026, 2, 4)
_FIXED_CLOCK = datetime(2026, 2, 4, 3, 0, tzinfo=UTC)


def _create_definition(
    conn: sqlite3.Connection,
    repository: SQLiteRepository,
    *,
    definition_id: str,
    cycle: ReadingCycle = ReadingCycle.DAILY_1X,
    generation_start_date: date = date(2026, 2, 5),
    schedule_json: str | None = '{"reading_sessions":[{"reading_session":"08:00","day_offset":0}]}',
) -> str:
    meter_id = f"meter-{definition_id}"
    with transaction(conn):
        repository.create_meter_master(
            MeterMasterCreateRequest(
                meter_id=meter_id,
                location_id="location-work-package",
                location_name="架空の試験棟",
                meter_name="試験用メーター",
                category=Category.ELECTRICITY,
            )
        )
        repository.create_measurement_definition(
            MeasurementDefinitionCreateRequest(
                measurement_definition_id=definition_id,
                location_id="location-work-package",
                display_name="試験用積算電力量",
                category=Category.ELECTRICITY,
                unit="kWh",
                value_schema=ValueSchema.DECIMAL_STRING,
                multiplier=Decimal("1"),
                decimal_places=2,
                warning_threshold=None,
                rollover_value=None,
                calculation_method=CalculationMethod.DELTA,
                reading_cycle=cycle,
                purpose=Purpose.OPERATIONAL,
                generation_start_date=generation_start_date,
                initial_schedule_json=schedule_json,
                initial_meter_id=meter_id,
            )
        )
    return meter_id


def _build_download_service(
    conn: sqlite3.Connection,
    repository: SQLiteRepository,
    *,
    current_datetime: datetime,
    cutoff_time: time = time(7),
) -> WorkPackageDownloadService:
    def transaction_factory():
        return transaction(conn)

    def date_provider() -> date:
        return current_datetime.astimezone(_CURRENT_TIMEZONE).date()

    def timezone_provider() -> ZoneInfo:
        return _CURRENT_TIMEZONE

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
        scheduled_timezone_provider=timezone_provider,
    )
    lifecycle = PlannedReadingLifecycleService(
        repository,
        current_datetime_provider=lambda: current_datetime.astimezone(_CURRENT_TIMEZONE),
        cutoff_time_provider=lambda: cutoff_time,
    )
    return WorkPackageDownloadService(
        generator,
        repository,
        repository,
        repository,
        planned_reading_lifecycle_service=lifecycle,
        business_date_provider=date_provider,
        transaction_factory=transaction_factory,
    )


def _build_generator(
    conn: sqlite3.Connection, repository: SQLiteRepository
) -> PlannedReadingGeneratorService:
    def transaction_factory():
        return transaction(conn)

    def business_date_provider() -> date:
        return _CURRENT_BUSINESS_DATE

    release_offset_service = ReleaseOffsetSettingsService(
        repository,
        recomposition_repository=repository,
        transaction_factory=transaction_factory,
        current_business_date_provider=business_date_provider,
        clock=lambda: _FIXED_CLOCK,
    )
    return PlannedReadingGeneratorService(
        repository,
        release_date_calculator=release_offset_service,
        scheduled_timezone_provider=lambda: _CURRENT_TIMEZONE,
    )


def test_backfill_completes_in_fourteen_day_chunks_and_is_idempotent(
    file_database_repository: tuple[sqlite3.Connection, SQLiteRepository],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    conn, repository = file_database_repository
    _create_definition(conn, repository, definition_id="definition-backfill")
    service = _build_download_service(
        conn,
        repository,
        current_datetime=datetime(2026, 2, 25, 8, 0, tzinfo=_CURRENT_TIMEZONE),
    )
    create_batch = repository.create_planned_readings_if_missing
    batch_sizes: list[int] = []

    def track_batches(requests: list[PlannedReadingCreateRequest]) -> int:
        batch_sizes.append(len(requests))
        return create_batch(requests)

    monkeypatch.setattr(repository, "create_planned_readings_if_missing", track_batches)
    service.download_for_business_date(date(2026, 2, 25))

    assert batch_sizes == [14, 7]
    assert conn.execute(
        "SELECT COUNT(*) FROM planned_readings WHERE measurement_definition_id = ?",
        ("definition-backfill",),
    ).fetchone() == (21,)
    batch_sizes.clear()
    service.download_for_business_date(date(2026, 2, 25))
    assert batch_sizes == []


def test_new_definition_backfills_from_its_own_start_despite_other_existing_rows(
    file_database_repository: tuple[sqlite3.Connection, SQLiteRepository],
) -> None:
    conn, repository = file_database_repository
    _create_definition(conn, repository, definition_id="definition-already-running")
    service = _build_download_service(
        conn,
        repository,
        current_datetime=datetime(2026, 2, 12, 8, 0, tzinfo=_CURRENT_TIMEZONE),
    )
    service.download_for_business_date(date(2026, 2, 12))

    _create_definition(
        conn,
        repository,
        definition_id="definition-added-later",
        generation_start_date=date(2026, 2, 10),
    )
    service.download_for_business_date(date(2026, 2, 12))

    assert {
        row[0]
        for row in conn.execute(
            "SELECT business_date FROM planned_readings WHERE measurement_definition_id = ?",
            ("definition-added-later",),
        )
    } == {"2026-02-10", "2026-02-11", "2026-02-12"}


def test_backfill_fills_partial_daily_gaps_without_generating_before_start(
    file_database_repository: tuple[sqlite3.Connection, SQLiteRepository],
) -> None:
    conn, repository = file_database_repository
    _create_definition(
        conn,
        repository,
        definition_id="definition-partial",
        cycle=ReadingCycle.DAILY_2X,
        schedule_json=(
            '{"reading_sessions":[{"reading_session":"08:00","day_offset":0},'
            '{"reading_session":"22:00","day_offset":0}]}'
        ),
    )
    generator = _build_generator(conn, repository)
    with transaction(conn):
        generator.ensure_generated_through(date(2026, 2, 6))
    conn.execute(
        "DELETE FROM planned_readings WHERE measurement_definition_id = ? "
        "AND business_date = ? AND reading_session = ?",
        ("definition-partial", "2026-02-05", "22:00"),
    )
    with transaction(conn):
        generator.ensure_generated_through(date(2026, 2, 6))

    rows = conn.execute(
        "SELECT business_date, reading_session FROM planned_readings "
        "WHERE measurement_definition_id = ? ORDER BY business_date, reading_session",
        ("definition-partial",),
    ).fetchall()
    assert rows == [
        ("2026-02-05", "08:00"),
        ("2026-02-05", "22:00"),
        ("2026-02-06", "08:00"),
        ("2026-02-06", "22:00"),
    ]


def test_inactive_measurement_definitions_are_not_backfilled(
    file_database_repository: tuple[sqlite3.Connection, SQLiteRepository],
) -> None:
    conn, repository = file_database_repository
    _create_definition(conn, repository, definition_id="definition-inactive")
    conn.execute(
        "UPDATE measurement_definitions SET is_active = 0 WHERE measurement_definition_id = ?",
        ("definition-inactive",),
    )

    with transaction(conn):
        _build_generator(conn, repository).ensure_generated_through(date(2026, 2, 6))

    assert conn.execute(
        "SELECT COUNT(*) FROM planned_readings WHERE measurement_definition_id = ?",
        ("definition-inactive",),
    ).fetchone() == (0,)


def test_work_package_groups_daily_weekly_monthly_and_every_two_month_slots(
    file_database_repository: tuple[sqlite3.Connection, SQLiteRepository],
) -> None:
    conn, repository = file_database_repository
    schedules = (
        (
            "definition-daily",
            ReadingCycle.DAILY_1X,
            '{"reading_sessions":[{"reading_session":"08:00"}]}',
        ),
        (
            "definition-weekly",
            ReadingCycle.WEEKLY,
            '{"weekday":"sunday","reading_session":"08:00","day_offset":0}',
        ),
        (
            "definition-monthly",
            ReadingCycle.MONTHLY,
            '{"selector_type":"day_of_month","day_of_month":1,"reading_session":"08:00","day_offset":0}',
        ),
        (
            "definition-two-month",
            ReadingCycle.EVERY_TWO_MONTHS,
            '{"selector_type":"day_of_month","month_parity":"odd","day_of_month":1,'
            '"reading_session":"08:00","day_offset":0}',
        ),
    )
    for definition_id, cycle, schedule_json in schedules:
        _create_definition(
            conn,
            repository,
            definition_id=definition_id,
            cycle=cycle,
            schedule_json=schedule_json,
        )
    service = _build_download_service(
        conn,
        repository,
        current_datetime=datetime(2026, 3, 1, 8, 0, tzinfo=_CURRENT_TIMEZONE),
    )

    response = service.download_for_business_date(date(2026, 3, 1))

    assert [task.measurement_definition_id for task in response.work_package.daily_tasks] == [
        "definition-daily"
    ]
    assert [task.measurement_definition_id for task in response.work_package.weekly_tasks] == [
        "definition-weekly"
    ]
    assert [task.measurement_definition_id for task in response.work_package.monthly_tasks] == [
        "definition-monthly"
    ]
    assert [
        task.measurement_definition_id for task in response.work_package.every_two_months_tasks
    ] == ["definition-two-month"]


def test_backfill_fills_older_gaps_even_when_a_later_slot_already_exists(
    file_database_repository: tuple[sqlite3.Connection, SQLiteRepository],
) -> None:
    conn, repository = file_database_repository
    meter_id = _create_definition(conn, repository, definition_id="definition-gap")
    target = date(2026, 2, 20)
    with transaction(conn):
        repository.create_planned_reading_if_missing(
            PlannedReadingCreateRequest(
                planned_reading_id="plan-definition-gap-2026-02-20-0800",
                measurement_definition_id="definition-gap",
                meter_id=meter_id,
                location_id="location-work-package",
                business_date=target,
                release_date=target,
                reading_session=ReadingSession.SESSION_0800,
                scheduled_at=build_scheduled_at(target, "08:00"),
            )
        )
        _build_generator(conn, repository).ensure_generated_through(target)

    dates = {
        row[0]
        for row in conn.execute(
            "SELECT business_date FROM planned_readings WHERE measurement_definition_id = ?",
            ("definition-gap",),
        )
    }
    assert dates == {date(2026, 2, day).isoformat() for day in range(5, 21)}


def test_backfill_requires_schedule_time_assignment_and_rejects_missing_history(
    file_database_repository: tuple[sqlite3.Connection, SQLiteRepository],
) -> None:
    conn, repository = file_database_repository
    meter_id = _create_definition(
        conn,
        repository,
        definition_id="definition-no-assignment",
        generation_start_date=date(2026, 2, 5),
    )
    conn.execute(
        "UPDATE meter_assignments SET effective_from = ? WHERE measurement_definition_id = ?",
        ("2026-02-06 00:00:00", "definition-no-assignment"),
    )
    with pytest.raises(MeterAssignmentResolutionError), transaction(conn):
        _build_generator(conn, repository).ensure_generated_through(date(2026, 2, 5))

    with transaction(conn):
        conn.execute(
            "UPDATE meter_assignments SET effective_from = ? WHERE measurement_definition_id = ?",
            ("2026-02-01 00:00:00", "definition-no-assignment"),
        )
        _build_generator(conn, repository).ensure_generated_through(date(2026, 2, 5))

    assert conn.execute(
        "SELECT COUNT(*) FROM planned_readings WHERE meter_id = ?",
        (meter_id,),
    ).fetchone() == (1,)

    conn.execute(
        "DELETE FROM measurement_definition_schedules WHERE measurement_definition_id = ?",
        ("definition-no-assignment",),
    )
    conn.execute(
        "DELETE FROM reading_cycle_schedule_defaults WHERE reading_cycle = ?",
        (ReadingCycle.DAILY_1X.value,),
    )
    with pytest.raises(ScheduleHistoryResolutionError), transaction(conn):
        _build_generator(conn, repository).ensure_generated_through(date(2026, 2, 6))


def test_cutoff_uses_current_business_datetime_not_requested_work_package_date(
    file_database_repository: tuple[sqlite3.Connection, SQLiteRepository],
) -> None:
    conn, repository = file_database_repository
    _create_definition(conn, repository, definition_id="definition-cutoff")
    service = _build_download_service(
        conn,
        repository,
        current_datetime=datetime(2026, 2, 10, 8, 0, tzinfo=_CURRENT_TIMEZONE),
    )

    service.download_for_business_date(date(2026, 2, 5))

    assert conn.execute(
        "SELECT task_state, close_reason FROM planned_readings "
        "WHERE measurement_definition_id = ? AND business_date = ?",
        ("definition-cutoff", "2026-02-05"),
    ).fetchone() == ("auto_closed", PlannedReadingCloseReason.CUTOFF_ELAPSED.value)


@pytest.mark.parametrize(
    ("current_datetime", "expected_through"),
    [
        (
            datetime(2026, 3, 8, 6, 59, 59, tzinfo=ZoneInfo("America/New_York")),
            date(2026, 3, 6),
        ),
        (
            datetime(2026, 3, 8, 7, 0, tzinfo=ZoneInfo("America/New_York")),
            date(2026, 3, 7),
        ),
    ],
)
def test_cutoff_boundary_uses_local_time_across_daylight_saving_transition(
    current_datetime: datetime, expected_through: date
) -> None:
    assert (
        _resolve_auto_close_through_business_date(
            current_datetime=current_datetime,
            cutoff_time=time(7),
        )
        == expected_through
    )


def test_work_package_generation_and_auto_close_roll_back_after_late_chunk_failure(
    file_database_repository: tuple[sqlite3.Connection, SQLiteRepository],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    conn, repository = file_database_repository
    _create_definition(conn, repository, definition_id="definition-rollback")
    service = _build_download_service(
        conn,
        repository,
        current_datetime=datetime(2026, 2, 25, 8, 0, tzinfo=_CURRENT_TIMEZONE),
    )
    create_batch = repository.create_planned_readings_if_missing
    batch_count = 0

    def fail_after_second_batch(requests: list[PlannedReadingCreateRequest]) -> int:
        nonlocal batch_count
        batch_count += 1
        created = create_batch(requests)
        if batch_count == 2:
            raise RuntimeError("later backfill chunk failed")
        return created

    monkeypatch.setattr(repository, "create_planned_readings_if_missing", fail_after_second_batch)
    with pytest.raises(RuntimeError, match="later backfill chunk failed"):
        service.download_for_business_date(date(2026, 2, 25))

    assert conn.execute("SELECT COUNT(*) FROM planned_readings").fetchone() == (0,)


def test_future_work_package_date_is_rejected_before_starting_transaction(
    file_database_repository: tuple[sqlite3.Connection, SQLiteRepository],
) -> None:
    conn, repository = file_database_repository
    service = _build_download_service(
        conn,
        repository,
        current_datetime=datetime(2026, 2, 4, 12, 0, tzinfo=_CURRENT_TIMEZONE),
    )

    with pytest.raises(FutureBusinessDateError):
        service.download_for_business_date(date(2026, 2, 5))

    assert not conn.in_transaction
