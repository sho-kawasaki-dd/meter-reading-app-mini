from __future__ import annotations

import json
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
from meter_reading_mini.core.errors import ScheduleHistoryResolutionError, ScheduleValidationError
from meter_reading_mini.core.meter_reading_sync_repositories import (
    DailySchedule,
    EveryTwoMonthsSchedule,
    MeasurementDefinitionCreateRequest,
    MeterMasterCreateRequest,
    MonthlySchedule,
    MonthlyScheduleSelectorType,
    MonthParity,
    PlannedReadingCreateRequest,
    ScheduleItem,
    WeeklySchedule,
)
from meter_reading_mini.core.schedule_settings import (
    CUTOFF_SETTINGS_KEY,
    DEFAULT_SCHEDULES,
    CutoffSettingsService,
    MeasurementDefinitionScheduleService,
    ReleaseOffsetSettingsService,
    ScheduleSettingsService,
    _default_daily_schedule_for_count,
    deserialize_schedule,
    expected_daily_count,
    serialize_schedule,
    validate_expected_daily_count,
    validate_schedule_type_for_reading_cycle,
)
from meter_reading_mini.core.timezone_settings import build_scheduled_at
from meter_reading_mini.core.work_package_download import PlannedReadingGeneratorService
from meter_reading_mini.db.connection import transaction
from meter_reading_mini.db.repositories import SQLiteRepository

_CURRENT_BUSINESS_DATE = date(2026, 2, 4)
_FIXED_CLOCK = datetime(2026, 2, 4, 3, 0, tzinfo=UTC)
_CURRENT_TIMEZONE = ZoneInfo("Asia/Tokyo")


def _transaction_factory(conn: sqlite3.Connection):
    return lambda: transaction(conn)


def _create_definition(
    conn: sqlite3.Connection,
    repository: SQLiteRepository,
    *,
    definition_id: str,
    cycle: ReadingCycle = ReadingCycle.DAILY_1X,
    schedule_json: str | None = '{"reading_sessions":[{"reading_session":"08:00","day_offset":0}]}',
) -> str:
    meter_id = f"meter-{definition_id}"
    with transaction(conn):
        repository.create_meter_master(
            MeterMasterCreateRequest(
                meter_id=meter_id,
                location_id="location-test-001",
                location_name="架空の試験棟",
                meter_name="試験用メーター",
                category=Category.ELECTRICITY,
            )
        )
        repository.create_measurement_definition(
            MeasurementDefinitionCreateRequest(
                measurement_definition_id=definition_id,
                location_id="location-test-001",
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
                generation_start_date=date(2026, 2, 5),
                initial_schedule_json=schedule_json,
                initial_meter_id=meter_id,
            )
        )
    return meter_id


def _create_services(
    conn: sqlite3.Connection, repository: SQLiteRepository
) -> tuple[
    MeasurementDefinitionScheduleService,
    ScheduleSettingsService,
    ReleaseOffsetSettingsService,
]:
    transaction_factory = _transaction_factory(conn)

    def date_provider() -> date:
        return _CURRENT_BUSINESS_DATE

    def clock() -> datetime:
        return _FIXED_CLOCK

    return (
        MeasurementDefinitionScheduleService(
            repository,
            schedule_defaults_repository=repository,
            recomposition_repository=repository,
            transaction_factory=transaction_factory,
            current_business_date_provider=date_provider,
            clock=clock,
        ),
        ScheduleSettingsService(
            repository,
            recomposition_repository=repository,
            transaction_factory=transaction_factory,
            current_business_date_provider=date_provider,
            clock=clock,
        ),
        ReleaseOffsetSettingsService(
            repository,
            recomposition_repository=repository,
            transaction_factory=transaction_factory,
            current_business_date_provider=date_provider,
            clock=clock,
        ),
    )


def _generate_through(conn: sqlite3.Connection, repository: SQLiteRepository, target: date) -> None:
    release_offset_service = ReleaseOffsetSettingsService(
        repository,
        recomposition_repository=repository,
        transaction_factory=_transaction_factory(conn),
        current_business_date_provider=lambda: _CURRENT_BUSINESS_DATE,
        clock=lambda: _FIXED_CLOCK,
    )
    generator = PlannedReadingGeneratorService(
        repository,
        release_date_calculator=release_offset_service,
        scheduled_timezone_provider=lambda: _CURRENT_TIMEZONE,
    )
    with transaction(conn):
        generator.ensure_generated_through(target)


@pytest.mark.parametrize("cycle", list(ReadingCycle))
def test_default_schedule_histories_round_trip(
    file_database_repository: tuple[sqlite3.Connection, SQLiteRepository],
    cycle: ReadingCycle,
) -> None:
    _, repository = file_database_repository
    record = repository.get_reading_cycle_schedule_default(cycle, _CURRENT_BUSINESS_DATE)

    assert record is not None
    assert deserialize_schedule(cycle, record.schedule_json) == DEFAULT_SCHEDULES[cycle]
    assert (
        deserialize_schedule(cycle, serialize_schedule(DEFAULT_SCHEDULES[cycle]))
        == (DEFAULT_SCHEDULES[cycle])
    )


def test_definition_schedule_history_recomposes_only_open_slots_from_effective_date(
    file_database_repository: tuple[sqlite3.Connection, SQLiteRepository],
) -> None:
    conn, repository = file_database_repository
    _create_definition(conn, repository, definition_id="definition-custom")
    _generate_through(conn, repository, date(2026, 2, 10))
    definition_service, _, _ = _create_services(conn, repository)
    replacement = DailySchedule((ScheduleItem(ReadingSession.SESSION_2200),))

    definition_service.save_schedule(
        "definition-custom",
        ReadingCycle.DAILY_1X,
        replacement,
        effective_from_date=date(2026, 2, 8),
    )

    assert definition_service.get_schedule(
        "definition-custom", ReadingCycle.DAILY_1X, date(2026, 2, 7)
    ) == DailySchedule((ScheduleItem(ReadingSession.SESSION_0800),))
    assert (
        definition_service.get_schedule(
            "definition-custom", ReadingCycle.DAILY_1X, date(2026, 2, 8)
        )
        == replacement
    )
    rows = conn.execute(
        "SELECT business_date, reading_session, task_state, close_reason "
        "FROM planned_readings WHERE measurement_definition_id = ? "
        "ORDER BY business_date, reading_session",
        ("definition-custom",),
    ).fetchall()
    before = [row for row in rows if row[0] == "2026-02-07"]
    after = [row for row in rows if row[0] >= "2026-02-08"]
    assert before == [("2026-02-07", "08:00", "open", None)]
    assert ("2026-02-08", "08:00", "superseded", "schedule_recomposed") in after
    assert ("2026-02-08", "22:00", "open", None) in after


def test_recomposition_updates_the_current_replacement_successor_only(
    file_database_repository: tuple[sqlite3.Connection, SQLiteRepository],
) -> None:
    conn, repository = file_database_repository
    meter_id = "meter-definition-replaced"
    _create_definition(conn, repository, definition_id="definition-replaced")
    _generate_through(conn, repository, date(2026, 2, 9))
    old_id = "plan-definition-replaced-2026-02-07-0800"
    successor_id = "replacement-successor"
    with transaction(conn):
        repository.create_planned_reading_if_missing(
            PlannedReadingCreateRequest(
                planned_reading_id=successor_id,
                measurement_definition_id="definition-replaced",
                meter_id=meter_id,
                location_id="location-test-001",
                business_date=date(2026, 2, 7),
                release_date=date(2026, 2, 7),
                reading_session=ReadingSession.SESSION_0800,
                scheduled_at=build_scheduled_at(date(2026, 2, 7), "08:00"),
            )
        )
        conn.execute(
            "UPDATE planned_readings SET task_state = 'superseded', "
            "close_reason = 'meter_replaced', superseded_by_planned_reading_id = ? "
            "WHERE planned_reading_id = ?",
            (successor_id, old_id),
        )
    definition_service, _, _ = _create_services(conn, repository)

    definition_service.save_schedule(
        "definition-replaced",
        ReadingCycle.DAILY_1X,
        DailySchedule((ScheduleItem(ReadingSession.SESSION_2200),)),
        effective_from_date=date(2026, 2, 7),
    )

    assert conn.execute(
        "SELECT task_state, close_reason, superseded_by_planned_reading_id "
        "FROM planned_readings WHERE planned_reading_id = ?",
        (old_id,),
    ).fetchone() == ("superseded", "meter_replaced", successor_id)
    assert conn.execute(
        "SELECT task_state, close_reason FROM planned_readings WHERE planned_reading_id = ?",
        (successor_id,),
    ).fetchone() == ("superseded", "schedule_recomposed")
    assert conn.execute(
        "SELECT task_state FROM planned_readings "
        "WHERE measurement_definition_id = ? AND business_date = ? AND reading_session = ?",
        ("definition-replaced", "2026-02-07", "22:00"),
    ).fetchone() == ("open",)


def test_recomposition_does_not_resolve_or_reopen_closed_slots(
    file_database_repository: tuple[sqlite3.Connection, SQLiteRepository],
) -> None:
    conn, repository = file_database_repository
    meter_id = _create_definition(conn, repository, definition_id="definition-closed")
    _generate_through(conn, repository, date(2026, 2, 6))
    conn.execute(
        "UPDATE planned_readings SET task_state = 'auto_closed', "
        "close_reason = 'cutoff_elapsed' "
        "WHERE measurement_definition_id = ? AND business_date = ?",
        ("definition-closed", "2026-02-06"),
    )
    conn.execute("UPDATE physical_meters SET is_active = 0 WHERE meter_id = ?", (meter_id,))
    definition_service, _, _ = _create_services(conn, repository)

    definition_service.save_schedule(
        "definition-closed",
        ReadingCycle.DAILY_1X,
        DailySchedule((ScheduleItem(ReadingSession.SESSION_0800),)),
        effective_from_date=date(2026, 2, 6),
    )

    assert conn.execute(
        "SELECT task_state, close_reason FROM planned_readings "
        "WHERE measurement_definition_id = ? AND business_date = ?",
        ("definition-closed", "2026-02-06"),
    ).fetchone() == ("auto_closed", "cutoff_elapsed")


def test_cycle_default_change_recomposes_only_default_linked_definitions(
    file_database_repository: tuple[sqlite3.Connection, SQLiteRepository],
) -> None:
    conn, repository = file_database_repository
    _create_definition(
        conn,
        repository,
        definition_id="definition-default",
        schedule_json=None,
    )
    _create_definition(conn, repository, definition_id="definition-explicit")
    _generate_through(conn, repository, date(2026, 2, 9))
    _, settings_service, _ = _create_services(conn, repository)
    replacement = DailySchedule((ScheduleItem(ReadingSession.SESSION_0600),))

    settings_service.save_schedule(
        ReadingCycle.DAILY_1X,
        replacement,
        effective_from_date=date(2026, 2, 8),
    )

    default_rows = conn.execute(
        "SELECT reading_session, task_state FROM planned_readings "
        "WHERE measurement_definition_id = 'definition-default' AND business_date = '2026-02-08' "
        "ORDER BY reading_session"
    ).fetchall()
    explicit_rows = conn.execute(
        "SELECT reading_session, task_state FROM planned_readings "
        "WHERE measurement_definition_id = 'definition-explicit' AND business_date = '2026-02-08' "
        "ORDER BY reading_session"
    ).fetchall()
    assert default_rows == [("06:00", "open"), ("08:00", "superseded")]
    assert explicit_rows == [("08:00", "open")]


def test_effective_date_rejects_past_and_same_day_upsert_replaces_history(
    file_database_repository: tuple[sqlite3.Connection, SQLiteRepository],
) -> None:
    conn, repository = file_database_repository
    _create_definition(conn, repository, definition_id="definition-upsert")
    definition_service, _, _ = _create_services(conn, repository)

    with pytest.raises(ScheduleValidationError):
        definition_service.save_schedule(
            "definition-upsert",
            ReadingCycle.DAILY_1X,
            DailySchedule((ScheduleItem(ReadingSession.SESSION_2200),)),
            effective_from_date=date(2026, 2, 3),
        )

    definition_service.save_schedule(
        "definition-upsert",
        ReadingCycle.DAILY_1X,
        DailySchedule((ScheduleItem(ReadingSession.SESSION_2200),)),
        effective_from_date=date(2026, 2, 6),
    )
    definition_service.save_schedule(
        "definition-upsert",
        ReadingCycle.DAILY_1X,
        DailySchedule((ScheduleItem(ReadingSession.SESSION_1400),)),
        effective_from_date=date(2026, 2, 6),
    )

    history = repository.list_measurement_definition_schedule_history("definition-upsert")
    assert len(history) == 2
    assert definition_service.get_schedule(
        "definition-upsert", ReadingCycle.DAILY_1X, date(2026, 2, 6)
    ) == DailySchedule((ScheduleItem(ReadingSession.SESSION_1400),))


def test_schedule_save_rolls_back_history_when_recomposition_fails(
    file_database_repository: tuple[sqlite3.Connection, SQLiteRepository],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    conn, repository = file_database_repository
    _create_definition(conn, repository, definition_id="definition-rollback")
    definition_service, _, _ = _create_services(conn, repository)

    def fail_recomposition(*args, **kwargs) -> tuple[int, int]:
        raise RuntimeError("recomposition failed")

    monkeypatch.setattr(repository, "recompose_future_planned_readings", fail_recomposition)
    with pytest.raises(RuntimeError, match="recomposition failed"):
        definition_service.save_schedule(
            "definition-rollback",
            ReadingCycle.DAILY_1X,
            DailySchedule((ScheduleItem(ReadingSession.SESSION_2200),)),
            effective_from_date=date(2026, 2, 6),
        )

    history_dates = {
        record.effective_from_date
        for record in repository.list_measurement_definition_schedule_history("definition-rollback")
    }
    assert date(2026, 2, 6) not in history_dates


def test_schedule_type_mismatch_is_a_domain_validation_error() -> None:
    with pytest.raises(ScheduleValidationError):
        validate_schedule_type_for_reading_cycle(
            ReadingCycle.WEEKLY,
            DailySchedule((ScheduleItem(ReadingSession.SESSION_0800),)),
        )


def test_release_offset_change_reconstructs_open_release_dates_atomically(
    file_database_repository: tuple[sqlite3.Connection, SQLiteRepository],
) -> None:
    conn, repository = file_database_repository
    _create_definition(conn, repository, definition_id="definition-offset")
    _generate_through(conn, repository, date(2026, 2, 9))
    _, _, offset_service = _create_services(conn, repository)
    conn.execute(
        "UPDATE planned_readings SET task_state = 'auto_closed', close_reason = 'cutoff_elapsed' "
        "WHERE measurement_definition_id = ? AND business_date = ?",
        ("definition-offset", "2026-02-08"),
    )

    offset_service.save_release_offset_days(
        ReadingSession.SESSION_0800,
        -2,
        effective_from_date=date(2026, 2, 7),
    )

    rows = conn.execute(
        "SELECT business_date, release_date, task_state FROM planned_readings "
        "WHERE measurement_definition_id = ? AND business_date >= '2026-02-07' "
        "ORDER BY business_date",
        ("definition-offset",),
    ).fetchall()
    by_day = {row[0]: row[1:] for row in rows}
    assert by_day["2026-02-07"] == ("2026-02-05", "open")
    assert by_day["2026-02-08"] == ("2026-02-08", "auto_closed")
    assert by_day["2026-02-09"] == ("2026-02-07", "open")
    assert offset_service.compute_release_date(
        date(2026, 2, 6), ReadingSession.SESSION_0800
    ) == date(2026, 2, 6)
    offset_service.save_release_offset_days(
        ReadingSession.SESSION_0800,
        -3,
        effective_from_date=date(2026, 2, 7),
    )
    assert len(repository.list_reading_session_release_offsets(ReadingSession.SESSION_0800)) == 2
    assert conn.execute(
        "SELECT release_date FROM planned_readings "
        "WHERE measurement_definition_id = ? AND business_date = ?",
        ("definition-offset", "2026-02-07"),
    ).fetchone() == ("2026-02-04",)


def test_release_offset_history_upsert_and_failed_recomposition_roll_back(
    file_database_repository: tuple[sqlite3.Connection, SQLiteRepository],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    conn, repository = file_database_repository
    _, _, offset_service = _create_services(conn, repository)

    def fail_recomposition(*args, **kwargs) -> int:
        raise RuntimeError("release recomposition failed")

    monkeypatch.setattr(repository, "recompose_release_dates", fail_recomposition)
    with pytest.raises(RuntimeError, match="release recomposition failed"):
        offset_service.save_release_offset_days(
            ReadingSession.SESSION_0600,
            3,
            effective_from_date=date(2026, 2, 6),
        )
    assert date(2026, 2, 6) not in {
        record.effective_from_date
        for record in repository.list_reading_session_release_offsets(ReadingSession.SESSION_0600)
    }


def test_release_offset_effective_date_uses_scheduled_local_date(
    file_database_repository: tuple[sqlite3.Connection, SQLiteRepository],
) -> None:
    conn, repository = file_database_repository
    _create_definition(
        conn,
        repository,
        definition_id="definition-offset-day",
        schedule_json='{"reading_sessions":[{"reading_session":"08:00","day_offset":1}]}',
    )
    _generate_through(conn, repository, date(2026, 2, 7))
    _, _, offset_service = _create_services(conn, repository)

    offset_service.save_release_offset_days(
        ReadingSession.SESSION_0800,
        -2,
        effective_from_date=date(2026, 2, 7),
    )

    assert conn.execute(
        "SELECT release_date FROM planned_readings "
        "WHERE measurement_definition_id = ? AND business_date = ?",
        ("definition-offset-day", "2026-02-06"),
    ).fetchone() == ("2026-02-05",)


def test_generation_uses_date_specific_default_history_and_assignment_time(
    file_database_repository: tuple[sqlite3.Connection, SQLiteRepository],
) -> None:
    conn, repository = file_database_repository
    _create_definition(
        conn,
        repository,
        definition_id="definition-date-specific",
        schedule_json=None,
    )
    repository.save_reading_cycle_schedule_default(
        ReadingCycle.DAILY_1X,
        date(2026, 2, 8),
        json.dumps({"reading_sessions": [{"reading_session": "22:00", "day_offset": 0}]}),
        created_at=_FIXED_CLOCK,
    )
    _generate_through(conn, repository, date(2026, 2, 9))

    rows = conn.execute(
        "SELECT business_date, reading_session, scheduled_at FROM planned_readings "
        "WHERE measurement_definition_id = ? ORDER BY business_date",
        ("definition-date-specific",),
    ).fetchall()
    assert [(row[0], row[1]) for row in rows] == [
        ("2026-02-05", "08:00"),
        ("2026-02-06", "08:00"),
        ("2026-02-07", "08:00"),
        ("2026-02-08", "22:00"),
        ("2026-02-09", "22:00"),
    ]
    assert rows[-1][2] == "2026-02-09 13:00:00"
    assert build_scheduled_at(
        date(2026, 2, 9), ReadingSession.SESSION_2200.value, tzinfo=_CURRENT_TIMEZONE
    ) == datetime(2026, 2, 9, 13, 0, tzinfo=UTC)


@pytest.mark.parametrize(
    "schedule",
    [
        DailySchedule((ScheduleItem(ReadingSession.SESSION_0600, day_offset=1),)),
        WeeklySchedule(weekday=2, reading_session=ReadingSession.SESSION_1400, day_offset=-1),
        MonthlySchedule(
            selector_type=MonthlyScheduleSelectorType.NTH_WEEKDAY,
            nth=2,
            nth_weekday=1,
            reading_session=ReadingSession.SESSION_1700,
            day_offset=1,
        ),
        MonthlySchedule(
            selector_type=MonthlyScheduleSelectorType.MONTH_EDGE,
            edge="last",
            reading_session=ReadingSession.SESSION_2200,
        ),
        EveryTwoMonthsSchedule(
            selector_type=MonthlyScheduleSelectorType.NTH_WEEKDAY,
            month_parity=MonthParity.EVEN,
            nth=3,
            nth_weekday=4,
            reading_session=ReadingSession.SESSION_0600,
        ),
        EveryTwoMonthsSchedule(
            selector_type=MonthlyScheduleSelectorType.MONTH_EDGE,
            month_parity=MonthParity.ODD,
            edge="first",
            reading_session=ReadingSession.SESSION_0000,
        ),
    ],
)
def test_schedule_json_round_trips_all_selector_shapes(schedule) -> None:
    if isinstance(schedule, DailySchedule):
        cycle = ReadingCycle.DAILY_1X
    elif isinstance(schedule, WeeklySchedule):
        cycle = ReadingCycle.WEEKLY
    elif isinstance(schedule, EveryTwoMonthsSchedule):
        cycle = ReadingCycle.EVERY_TWO_MONTHS
    else:
        cycle = ReadingCycle.MONTHLY

    assert deserialize_schedule(cycle, serialize_schedule(schedule)) == schedule


@pytest.mark.parametrize(
    ("cycle", "raw", "error_type"),
    [
        (ReadingCycle.DAILY_1X, "{", ValueError),
        (ReadingCycle.DAILY_1X, "[]", ValueError),
        (ReadingCycle.DAILY_1X, '{"reading_sessions":"08:00"}', ValueError),
        (ReadingCycle.DAILY_1X, '{"reading_sessions":[1]}', TypeError),
        (ReadingCycle.WEEKLY, '{"weekday":"funday","reading_session":"08:00"}', ValueError),
        (ReadingCycle.WEEKLY, '{"weekday":"monday","reading_session":8}', ValueError),
        (
            ReadingCycle.WEEKLY,
            '{"weekday":"monday","reading_session":"08:00","day_offset":true}',
            ValueError,
        ),
        (
            ReadingCycle.MONTHLY,
            '{"selector_type":"day_of_month","day_of_month":true}',
            ValueError,
        ),
        (
            ReadingCycle.MONTHLY,
            '{"selector_type":"nth_weekday","nth":1,"nth_weekday":"funday"}',
            ValueError,
        ),
        (ReadingCycle.MONTHLY, '{"selector_type":"month_edge"}', ValueError),
        (
            ReadingCycle.EVERY_TWO_MONTHS,
            '{"selector_type":"day_of_month","day_of_month":1}',
            ValueError,
        ),
    ],
)
def test_invalid_schedule_json_is_rejected(
    cycle: ReadingCycle, raw: str, error_type: type[Exception]
) -> None:
    with pytest.raises(error_type):
        deserialize_schedule(cycle, raw)


@pytest.mark.parametrize(
    ("cycle", "schedule"),
    [
        (ReadingCycle.DAILY_1X, WeeklySchedule(1, ReadingSession.SESSION_0800)),
        (ReadingCycle.WEEKLY, DailySchedule((ScheduleItem(ReadingSession.SESSION_0800),))),
        (
            ReadingCycle.MONTHLY,
            DailySchedule((ScheduleItem(ReadingSession.SESSION_0800),)),
        ),
        (
            ReadingCycle.EVERY_TWO_MONTHS,
            MonthlySchedule(
                selector_type=MonthlyScheduleSelectorType.DAY_OF_MONTH,
                day_of_month=1,
            ),
        ),
    ],
)
def test_each_reading_cycle_rejects_a_mismatched_schedule_type(
    cycle: ReadingCycle, schedule
) -> None:
    with pytest.raises(ScheduleValidationError):
        validate_schedule_type_for_reading_cycle(cycle, schedule)


def test_daily_count_validation_and_dynamic_defaults() -> None:
    assert expected_daily_count(ReadingCycle.WEEKLY) == 0
    validate_expected_daily_count(
        ReadingCycle.DAILY_1X,
        DailySchedule((ScheduleItem(ReadingSession.SESSION_0800),)),
    )
    invalid_schedules = (
        (ReadingCycle.DAILY_1X, DailySchedule(())),
        (
            ReadingCycle.DAILY_2X,
            DailySchedule((ScheduleItem(ReadingSession.SESSION_0800),)),
        ),
        (
            ReadingCycle.DAILY_1X,
            DailySchedule(
                (
                    ScheduleItem(ReadingSession.SESSION_0800),
                    ScheduleItem(ReadingSession.SESSION_2200),
                )
            ),
        ),
        (
            ReadingCycle.DAILY_2X,
            DailySchedule(
                (
                    ScheduleItem(ReadingSession.SESSION_0800),
                    ScheduleItem(ReadingSession.SESSION_0800),
                )
            ),
        ),
    )
    for cycle, schedule in invalid_schedules:
        with pytest.raises(ScheduleValidationError):
            validate_expected_daily_count(cycle, schedule)

    assert len(_default_daily_schedule_for_count(4).reading_items) == 4
    with pytest.raises(ValueError, match="not enough slots"):
        _default_daily_schedule_for_count(7)


def test_history_resolution_errors_and_default_history_creation(
    file_database_repository: tuple[sqlite3.Connection, SQLiteRepository],
) -> None:
    conn, repository = file_database_repository
    definition_service, settings_service, offset_service = _create_services(conn, repository)
    _create_definition(conn, repository, definition_id="definition-missing-history")
    conn.execute(
        "DELETE FROM measurement_definition_schedules WHERE measurement_definition_id = ?",
        ("definition-missing-history",),
    )

    assert (
        definition_service.ensure_default_schedule(
            "definition-missing-history", ReadingCycle.DAILY_1X
        )
        == DEFAULT_SCHEDULES[ReadingCycle.DAILY_1X]
    )
    record = repository.get_measurement_definition_schedule_for_date(
        "definition-missing-history", _CURRENT_BUSINESS_DATE
    )
    assert record is not None and record.schedule_json is None

    conn.execute(
        "DELETE FROM reading_cycle_schedule_defaults WHERE reading_cycle = ?",
        (ReadingCycle.DAILY_1X.value,),
    )
    with pytest.raises(ScheduleHistoryResolutionError):
        settings_service.get_schedule(ReadingCycle.DAILY_1X, date(2026, 2, 5))

    with pytest.raises(ScheduleHistoryResolutionError):
        definition_service.get_schedule(
            "definition-missing-history", ReadingCycle.DAILY_1X, date(2026, 2, 5)
        )
    conn.execute(
        "DELETE FROM reading_session_release_offsets WHERE reading_session = ?",
        (ReadingSession.SESSION_0600.value,),
    )
    with pytest.raises(ScheduleHistoryResolutionError):
        offset_service.compute_release_date(date(2026, 2, 5), ReadingSession.SESSION_0600)


def test_default_history_malformed_values_are_reported_as_domain_errors(
    file_database_repository: tuple[sqlite3.Connection, SQLiteRepository],
) -> None:
    conn, repository = file_database_repository
    definition_service, settings_service, _ = _create_services(conn, repository)
    repository.save_reading_cycle_schedule_default(
        ReadingCycle.DAILY_1X,
        _CURRENT_BUSINESS_DATE,
        "{",
        created_at=_FIXED_CLOCK,
    )
    with pytest.raises(ScheduleHistoryResolutionError):
        settings_service.get_schedule(ReadingCycle.DAILY_1X, _CURRENT_BUSINESS_DATE)

    _create_definition(conn, repository, definition_id="definition-malformed-history")
    repository.upsert_measurement_definition_schedule(
        "definition-malformed-history",
        "{",
        effective_from_date=date(2026, 2, 5),
        created_at=_FIXED_CLOCK,
    )
    with pytest.raises(ScheduleHistoryResolutionError):
        definition_service.get_schedule(
            "definition-malformed-history", ReadingCycle.DAILY_1X, date(2026, 2, 5)
        )


def test_service_default_dates_serializers_and_cutoff_settings(
    file_database_repository: tuple[sqlite3.Connection, SQLiteRepository],
) -> None:
    conn, repository = file_database_repository
    _, settings_service, _ = _create_services(conn, repository)
    new_schedule = WeeklySchedule(weekday=1, reading_session=ReadingSession.SESSION_1400)
    settings_service.save_schedule(ReadingCycle.WEEKLY, new_schedule)
    assert (
        settings_service.get_schedule(ReadingCycle.WEEKLY, _CURRENT_BUSINESS_DATE) == new_schedule
    )
    history_record = repository.get_reading_cycle_schedule_default(
        ReadingCycle.WEEKLY, _CURRENT_BUSINESS_DATE
    )
    assert history_record is not None
    assert history_record.effective_from_date == _CURRENT_BUSINESS_DATE
    repeated_schedule = WeeklySchedule(
        weekday=4,
        reading_session=ReadingSession.SESSION_2200,
    )
    settings_service.save_schedule(
        ReadingCycle.WEEKLY,
        repeated_schedule,
        effective_from_date=_CURRENT_BUSINESS_DATE,
    )
    assert len(repository.list_reading_cycle_schedule_defaults(ReadingCycle.WEEKLY)) == 2
    assert (
        settings_service.get_schedule(ReadingCycle.WEEKLY, _CURRENT_BUSINESS_DATE)
        == repeated_schedule
    )

    assert ScheduleSettingsService.serialize_default(ReadingCycle.DAILY_1X)
    assert MeasurementDefinitionScheduleService.serialize_default(ReadingCycle.WEEKLY)
    assert ReleaseOffsetSettingsService.serialize_default(ReadingSession.SESSION_0000) == "-1"

    cutoff_service = CutoffSettingsService(repository)
    assert cutoff_service.get_cutoff_time() == time(7)
    cutoff_service.save_cutoff_time(time(6, 30))
    assert cutoff_service.get_cutoff_time() == time(6, 30)
    assert repository.get_setting(CUTOFF_SETTINGS_KEY) == "06:30"
