from __future__ import annotations

import sqlite3
from dataclasses import replace
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from pathlib import Path

import pytest

from meter_reading_mini.core.contract_models import (
    CalculationMethod,
    Category,
    Purpose,
    ReadingCycle,
    ReadingSession,
    ResultStatus,
    ValueSchema,
)
from meter_reading_mini.core.errors import (
    InactiveMasterError,
    InvalidTimezoneError,
    MasterInUseError,
    TimezoneChangeError,
    ValidationError,
)
from meter_reading_mini.core.meter_reading_sync_repositories import (
    PC_DIRECT_DEVICE_ID,
    CalculatedUsageUpdate,
    DailySchedule,
    EveryTwoMonthsSchedule,
    MeasurementDefinitionCreateRequest,
    MeasurementDefinitionUpdateRequest,
    MeterMasterCreateRequest,
    MeterMasterUpdateRequest,
    MonthlySchedule,
    MonthlyScheduleSelectorType,
    MonthParity,
    PlannedReadingCloseReason,
    PlannedReadingCreateRequest,
    PlannedReadingTaskState,
    ReadingCycleScheduleDefaultRecord,
    ReadingSessionReleaseOffsetRecord,
    ReadingUpsertRequest,
    ReplacementUpsertRequest,
    ResultSource,
    ScheduleItem,
    StaffCreateRequest,
    StaffUpdateRequest,
    UpsertDisposition,
    WeeklySchedule,
)
from meter_reading_mini.core.timezone_settings import TimezoneSettingsService
from meter_reading_mini.db.bootstrap_seeds import bootstrap_default_settings
from meter_reading_mini.db.connection import connect, connect_readonly, transaction
from meter_reading_mini.db.migrations import create_schema
from meter_reading_mini.db.repositories import (
    SQLiteRepository,
    _date_text,
    _decimal_text,
    _deserialize_schedule,
    _json_text,
    _read_date,
    _read_decimal,
)

READ_AT = datetime(2026, 2, 4, 8, 0, tzinfo=UTC)


def _planned(
    planned_reading_id: str,
    definition_id: str,
    meter_id: str,
    *,
    scheduled_at: datetime = READ_AT,
) -> PlannedReadingCreateRequest:
    return PlannedReadingCreateRequest(
        planned_reading_id=planned_reading_id,
        measurement_definition_id=definition_id,
        meter_id=meter_id,
        location_id="location-test-001",
        business_date=date(2026, 2, 4),
        release_date=date(2026, 2, 4),
        reading_session=ReadingSession.SESSION_0800,
        scheduled_at=scheduled_at,
    )


def _reading(
    planned_reading_id: str,
    *,
    raw: Decimal,
    recorded_at: datetime = READ_AT,
    client_updated_at: datetime = READ_AT,
    note: str | None = "確認済み",
) -> ReadingUpsertRequest:
    return ReadingUpsertRequest(
        planned_reading_id=planned_reading_id,
        measurement_definition_id="definition-test-001",
        result_status=ResultStatus.MEASURED,
        recorded_at=recorded_at,
        client_updated_at=client_updated_at,
        raw_reading=raw,
        structured_payload_json={"z": "値", "a": 1},
        updated_by="試験担当",
        signature="test-signature",
        note=note,
        device_id="0123456789abcdef0123456789abcdef",
        result_source=ResultSource.MOBILE_HTML,
    )


def test_default_settings_are_idempotent_and_use_history_tables(
    memory_conn: sqlite3.Connection, fixed_clock: datetime
) -> None:
    create_result = bootstrap_default_settings(memory_conn, clock=lambda: fixed_clock)
    repeat_result = bootstrap_default_settings(memory_conn, clock=lambda: fixed_clock)

    assert create_result.inserted_settings == 2
    assert create_result.inserted_schedule_defaults == len(ReadingCycle)
    assert create_result.inserted_release_offsets == len(ReadingSession)
    assert repeat_result == type(repeat_result)(0, 0, 0)
    assert memory_conn.execute(
        "SELECT setting_value FROM meter_app_settings "
        "WHERE setting_key = 'auto_close.cutoff_time.v1'"
    ).fetchone() == ("07:00",)
    assert memory_conn.execute(
        "SELECT setting_value FROM meter_app_settings WHERE setting_key = 'app.timezone.v1'"
    ).fetchone() == ("Asia/Tokyo",)
    assert memory_conn.execute(
        "SELECT COUNT(*) FROM reading_cycle_schedule_defaults "
        "WHERE effective_from_date = '0001-01-01'"
    ).fetchone() == (len(ReadingCycle),)
    assert memory_conn.execute(
        "SELECT offset_days FROM reading_session_release_offsets "
        "WHERE reading_session = '00:00' AND effective_from_date = '0001-01-01'"
    ).fetchone() == (-1,)
    assert memory_conn.execute(
        "SELECT COUNT(*) FROM meter_app_settings "
        "WHERE setting_key LIKE 'planned_reading_schedule.%'"
    ).fetchone() == (0,)


@pytest.mark.parametrize(
    ("table", "message"),
    [
        ("meter_app_settings", "既定設定"),
        ("reading_cycle_schedule_defaults", "既定スケジュール"),
        ("reading_session_release_offsets", "既定配信オフセット"),
    ],
)
def test_default_seed_surfaces_integrity_errors_and_caller_rolls_back(
    memory_conn: sqlite3.Connection,
    fixed_clock: datetime,
    table: str,
    message: str,
) -> None:
    memory_conn.execute(
        f"CREATE TRIGGER reject_seed BEFORE INSERT ON {table} "
        "BEGIN SELECT RAISE(ABORT, 'injected seed failure'); END"
    )
    with pytest.raises(ValidationError, match=message), transaction(memory_conn):
        bootstrap_default_settings(memory_conn, clock=lambda: fixed_clock)

    assert memory_conn.execute("SELECT COUNT(*) FROM meter_app_settings").fetchone() == (0,)
    assert memory_conn.execute(
        "SELECT COUNT(*) FROM reading_cycle_schedule_defaults"
    ).fetchone() == (0,)
    assert memory_conn.execute(
        "SELECT COUNT(*) FROM reading_session_release_offsets"
    ).fetchone() == (0,)


def test_repository_persists_file_database_with_wal(tmp_path: Path, fixed_clock: datetime) -> None:
    path = tmp_path / "repository-test.db"
    conn = connect(path)
    try:
        create_schema(conn)
        repository = SQLiteRepository(conn, clock=lambda: fixed_clock)
        with transaction(conn):
            bootstrap_default_settings(conn, clock=lambda: fixed_clock)
            repository.create_meter_master(
                MeterMasterCreateRequest(
                    meter_id="meter-persist",
                    location_id="location-persist",
                    location_name="架空の保存確認場所",
                    meter_name="保存確認計器",
                    category=Category.WATER,
                )
            )
        assert conn.execute("PRAGMA journal_mode").fetchone() == ("wal",)
    finally:
        conn.close()

    readonly = connect_readonly(path)
    try:
        persisted = SQLiteRepository(readonly).get_meter_master("meter-persist")
        assert persisted is not None
        assert persisted.location_name == "架空の保存確認場所"
        assert readonly.execute(
            "SELECT setting_value FROM meter_app_settings WHERE setting_key = 'app.timezone.v1'"
        ).fetchone() == ("Asia/Tokyo",)
    finally:
        readonly.close()


def test_generation_and_schedule_history_are_idempotent(
    sqlite_repository: SQLiteRepository, sample_master: tuple[str, str]
) -> None:
    meter_id, definition_id = sample_master
    definitions = sqlite_repository.list_planned_reading_generation_definitions()
    assert len(definitions) == 1
    assert definitions[0].generation_start_date == date(2026, 2, 1)
    assert definitions[0].meter_id is None
    assert (
        sqlite_repository.get_meter_assignment(
            definition_id, datetime(2026, 1, 31, 14, 59, tzinfo=UTC)
        )
        is None
    )
    assignment = sqlite_repository.get_meter_assignment(
        definition_id, datetime(2026, 1, 31, 15, 0, tzinfo=UTC)
    )
    assert assignment is not None
    assert assignment.meter_id == meter_id
    assert (
        sqlite_repository.get_measurement_definition_schedule_for_date(
            definition_id, date(2026, 2, 2)
        )
        is not None
    )

    request = _planned("plan-a", definition_id, meter_id)
    assert sqlite_repository.create_planned_readings_if_missing([request, request]) == 1
    assert sqlite_repository.create_planned_reading_if_missing(request) is False
    planned = sqlite_repository.get_planned_reading("plan-a")
    assert planned is not None
    assert planned.scheduled_at == READ_AT
    assert planned.task_state is not None
    assert planned.task_state.value == "open"

    assert (
        sqlite_repository.list_planned_reading_logical_slots(
            definition_id, date(2026, 2, 4), date(2026, 2, 4)
        )[0].planned_reading_id
        == "plan-a"
    )


def test_schedule_default_and_release_offset_histories_use_effective_dates(
    sqlite_repository: SQLiteRepository,
) -> None:
    sqlite_repository.save_reading_cycle_schedule_default(
        ReadingCycle.DAILY_1X,
        date(2026, 2, 5),
        '{"reading_sessions":[{"reading_session":"06:00","day_offset":0}]}',
        created_at=READ_AT,
    )
    sqlite_repository.save_reading_cycle_schedule_default(
        ReadingCycle.DAILY_1X,
        date(2026, 2, 5),
        '{"reading_sessions":[{"reading_session":"07:00","day_offset":0}]}',
        created_at=READ_AT + timedelta(seconds=1),
    )
    before = sqlite_repository.get_reading_cycle_schedule_default(
        ReadingCycle.DAILY_1X, date(2026, 2, 4)
    )
    after = sqlite_repository.get_reading_cycle_schedule_default(
        ReadingCycle.DAILY_1X, date(2026, 2, 5)
    )
    assert before is not None and after is not None
    assert '"08:00"' in before.schedule_json
    assert '"07:00"' in after.schedule_json
    assert isinstance(
        sqlite_repository.list_reading_cycle_schedule_defaults(ReadingCycle.DAILY_1X)[-1],
        ReadingCycleScheduleDefaultRecord,
    )
    sqlite_repository.save_reading_session_release_offset(
        ReadingSession.SESSION_0000, date(2026, 2, 5), -2, created_at=READ_AT
    )
    sqlite_repository.save_reading_session_release_offset(
        ReadingSession.SESSION_0000,
        date(2026, 2, 5),
        -3,
        created_at=READ_AT + timedelta(seconds=1),
    )
    assert (
        len(sqlite_repository.list_reading_session_release_offsets(ReadingSession.SESSION_0000))
        == 2
    )
    offset = sqlite_repository.get_reading_session_release_offset(
        ReadingSession.SESSION_0000, date(2026, 2, 5)
    )
    assert offset is not None and isinstance(offset, ReadingSessionReleaseOffsetRecord)
    assert offset.offset_days == -3
    before_offset = sqlite_repository.get_reading_session_release_offset(
        ReadingSession.SESSION_0000, date(2026, 2, 4)
    )
    assert before_offset is not None
    assert before_offset.offset_days == -1


def test_repository_value_converters_and_schedule_decoders() -> None:
    assert _date_text(date(2026, 2, 4)) == "2026-02-04"
    assert _read_date("2026-02-04") == date(2026, 2, 4)
    with pytest.raises(ValueError):
        _read_date("2026-2-4")
    assert _decimal_text(None) is None
    assert _decimal_text(Decimal("1E+3")) == "1000"
    assert _read_decimal(None) is None
    assert _read_decimal("1000") == Decimal("1000")
    with pytest.raises(ValidationError, match="有限"):
        _decimal_text(Decimal("Infinity"))
    with pytest.raises(ValidationError, match="plain decimal"):
        _read_decimal("1e3")
    assert _json_text(None) is None
    with pytest.raises(ValidationError, match="不正です"):
        _read_decimal("not-a-decimal")
    with pytest.raises(ValidationError, match="JSON 化"):
        _json_text({"invalid": object()})

    daily = _deserialize_schedule(
        ReadingCycle.DAILY_1X,
        '{"reading_sessions":[{"reading_session":"08:00","day_offset":1}]}',
    )
    assert isinstance(daily, DailySchedule)
    assert daily.reading_items == (ScheduleItem(ReadingSession.SESSION_0800, 1),)
    weekly = _deserialize_schedule(
        ReadingCycle.WEEKLY,
        '{"weekday":"sunday","reading_session":"08:00","day_offset":0}',
    )
    assert isinstance(weekly, WeeklySchedule)
    assert weekly.weekday == 6
    monthly = _deserialize_schedule(
        ReadingCycle.MONTHLY,
        '{"selector_type":"nth_weekday","nth":2,"nth_weekday":"monday","reading_session":"08:00"}',
    )
    assert isinstance(monthly, MonthlySchedule)
    assert monthly.selector_type is MonthlyScheduleSelectorType.NTH_WEEKDAY
    assert monthly.nth_weekday == 0
    every_two_months = _deserialize_schedule(
        ReadingCycle.EVERY_TWO_MONTHS,
        '{"selector_type":"day_of_month","month_parity":"even","day_of_month":1,'
        '"reading_session":"08:00"}',
    )
    assert isinstance(every_two_months, EveryTwoMonthsSchedule)
    assert every_two_months.month_parity is MonthParity.EVEN
    with pytest.raises(ValidationError, match="JSON"):
        _deserialize_schedule(ReadingCycle.WEEKLY, "[]")
    with pytest.raises(ValidationError, match="JSON"):
        _deserialize_schedule(ReadingCycle.MONTHLY, '{"selector_type":"unknown"}')
    with pytest.raises(ValidationError, match="JSON"):
        _deserialize_schedule(
            ReadingCycle.DAILY_1X,
            '{"reading_sessions":[{"reading_session":"08:00","day_offset":"tomorrow"}]}',
        )


def test_schedule_item_matching_for_weekly_monthly_and_bimonthly(
    memory_conn: sqlite3.Connection,
) -> None:
    repository = SQLiteRepository(memory_conn)
    weekly = WeeklySchedule(weekday=0, reading_session=ReadingSession.SESSION_0800)
    assert repository._schedule_items(ReadingCycle.WEEKLY, date(2026, 2, 2), weekly) == (
        (ReadingSession.SESSION_0800, 0),
    )
    assert repository._schedule_items(ReadingCycle.WEEKLY, date(2026, 2, 3), weekly) == ()

    month_end = MonthlySchedule(
        selector_type=MonthlyScheduleSelectorType.DAY_OF_MONTH,
        day_of_month=31,
        reading_session=ReadingSession.SESSION_0800,
    )
    assert repository._schedule_items(ReadingCycle.MONTHLY, date(2026, 2, 28), month_end) == (
        (ReadingSession.SESSION_0800, 0),
    )
    second_monday = MonthlySchedule(
        selector_type=MonthlyScheduleSelectorType.NTH_WEEKDAY,
        nth=2,
        nth_weekday=0,
    )
    assert repository._schedule_items(ReadingCycle.MONTHLY, date(2026, 2, 9), second_monday)
    month_edge = MonthlySchedule(
        selector_type=MonthlyScheduleSelectorType.MONTH_EDGE,
        edge="last",
    )
    assert repository._schedule_items(ReadingCycle.MONTHLY, date(2026, 2, 28), month_edge)

    odd_month = EveryTwoMonthsSchedule(
        selector_type=MonthlyScheduleSelectorType.DAY_OF_MONTH,
        month_parity=MonthParity.ODD,
        day_of_month=1,
    )
    assert repository._schedule_items(ReadingCycle.EVERY_TWO_MONTHS, date(2026, 3, 1), odd_month)
    assert (
        repository._schedule_items(ReadingCycle.EVERY_TWO_MONTHS, date(2026, 2, 1), odd_month) == ()
    )
    with pytest.raises(ValidationError, match="日付"):
        repository._schedule_items(
            ReadingCycle.MONTHLY,
            date(2026, 2, 1),
            MonthlySchedule(selector_type=MonthlyScheduleSelectorType.DAY_OF_MONTH),
        )
    with pytest.raises(ValidationError, match="曜日"):
        repository._schedule_items(
            ReadingCycle.MONTHLY,
            date(2026, 2, 1),
            MonthlySchedule(selector_type=MonthlyScheduleSelectorType.NTH_WEEKDAY),
        )


def test_master_assignment_settings_and_query_repositories(
    sqlite_repository: SQLiteRepository,
    sample_master: tuple[str, str],
) -> None:
    old_meter_id, _ = sample_master
    new_meter_id = "meter-test-003"
    sqlite_repository.create_meter_master(
        MeterMasterCreateRequest(
            meter_id=new_meter_id,
            location_id="location-test-001",
            location_name="架空の試験棟",
            meter_name="試験用水量計",
            category=Category.WATER,
        )
    )
    updated_meter = sqlite_repository.update_meter_master(
        new_meter_id,
        MeterMasterUpdateRequest(
            location_name="架空の試験棟二号",
            meter_name="更新後の水量計",
            category=Category.WATER,
            meter_serial="TEST-003",
        ),
    )
    assert updated_meter.location_name == "架空の試験棟二号"

    definition_id = "definition-test-003"
    sqlite_repository.create_measurement_definition(
        MeasurementDefinitionCreateRequest(
            measurement_definition_id=definition_id,
            location_id="location-test-001",
            display_name="試験用流量",
            category=Category.WATER,
            unit="m3",
            value_schema=ValueSchema.DECIMAL_STRING,
            multiplier=Decimal("1"),
            decimal_places=3,
            warning_threshold=None,
            rollover_value=None,
            calculation_method=CalculationMethod.RAW_ONLY,
            reading_cycle=ReadingCycle.WEEKLY,
            purpose=Purpose.BILLING,
            generation_start_date=date(2026, 2, 4),
            initial_schedule_json=None,
            initial_meter_id=None,
        )
    )
    assert sqlite_repository.get_measurement_definition(definition_id) is not None
    without_assignment = sqlite_repository.list_measurement_definitions_without_active_assignment()
    assert [row.measurement_definition_id for row in without_assignment] == [definition_id]
    with pytest.raises(InactiveMasterError):
        sqlite_repository.set_initial_meter_assignment(definition_id, "missing-meter", READ_AT)
    sqlite_repository.set_initial_meter_assignment(
        definition_id, new_meter_id, READ_AT - timedelta(days=1)
    )
    active_assignment = sqlite_repository.get_meter_assignment(definition_id, READ_AT)
    assert active_assignment is not None
    assert active_assignment.meter_id == new_meter_id
    assert sqlite_repository.list_meter_assignments(definition_id)[0].meter_id == new_meter_id
    assert sqlite_repository.list_measurement_definitions_without_active_assignment() == []

    sqlite_repository.update_measurement_definition(
        definition_id,
        MeasurementDefinitionUpdateRequest(
            display_name="更新後の流量",
            category=Category.WATER,
            unit="m3",
            multiplier=Decimal("1.25"),
            decimal_places=3,
            warning_threshold=Decimal("500"),
            rollover_value=Decimal("1000"),
            purpose="audit",
        ),
    )
    sqlite_repository.upsert_measurement_definition_schedule(
        definition_id,
        '{"weekday":"monday","reading_session":"08:00"}',
        effective_from_date=date(2026, 2, 5),
        created_at=READ_AT,
    )
    assert (
        sqlite_repository.get_measurement_definition_schedule(definition_id, date(2026, 2, 5))
        is not None
    )
    assert len(sqlite_repository.list_measurement_definition_schedule_history(definition_id)) == 2
    assert (
        sqlite_repository.get_measurement_definition_schedule_for_date(
            definition_id, date(2026, 2, 3)
        )
        is None
    )
    initial_schedule = sqlite_repository.get_measurement_definition_schedule_for_date(
        definition_id, date(2026, 2, 4)
    )
    later_schedule = sqlite_repository.get_measurement_definition_schedule_for_date(
        definition_id, date(2026, 2, 5)
    )
    assert initial_schedule is not None and initial_schedule.schedule_json is None
    assert later_schedule is not None and later_schedule.schedule_json is not None

    sqlite_repository.upsert_setting("test.setting", "value")
    assert sqlite_repository.get_setting("test.setting") == "value"
    assert old_meter_id in {meter.meter_id for meter in sqlite_repository.list_meter_master_data()}
    data = {
        item.measurement_definition_id: item
        for item in sqlite_repository.list_measurement_definition_data()
    }
    assert data[definition_id].meter_id == new_meter_id
    assert data[definition_id].purpose is Purpose.AUDIT

    sqlite_repository.set_measurement_definition_active(definition_id, False)
    sqlite_repository.set_meter_master_active(new_meter_id, False)
    assert new_meter_id not in {
        meter.meter_id for meter in sqlite_repository.list_meter_master_data()
    }


def test_superseded_fixtures_cover_replacement_and_definition_deactivation(
    sqlite_repository: SQLiteRepository,
    superseded_planned_reading_ids: tuple[str, str, str],
) -> None:
    replaced_id, replacement_id, deactivated_id = superseded_planned_reading_ids

    replaced = sqlite_repository.get_planned_reading(replaced_id)
    replacement = sqlite_repository.get_planned_reading(replacement_id)
    deactivated = sqlite_repository.get_planned_reading(deactivated_id)

    assert replaced is not None
    assert replaced.task_state is PlannedReadingTaskState.SUPERSEDED
    assert replaced.close_reason is PlannedReadingCloseReason.METER_REPLACED
    assert replaced.superseded_by_planned_reading_id == replacement_id
    assert replacement is not None and replacement.task_state is PlannedReadingTaskState.OPEN
    assert deactivated is not None
    assert deactivated.task_state is PlannedReadingTaskState.SUPERSEDED
    assert deactivated.close_reason is PlannedReadingCloseReason.DEFINITION_DEACTIVATED


def test_open_task_queries_and_recomposition(
    sqlite_repository: SQLiteRepository,
    sample_master: tuple[str, str],
) -> None:
    meter_id, definition_id = sample_master
    assert sqlite_repository.get_planned_reading("missing") is None
    sqlite_repository.create_planned_reading_if_missing(
        _planned("plan-recompose", definition_id, meter_id)
    )
    assert (
        len(sqlite_repository.list_open_planned_reading_tasks_for_release_date(date(2026, 2, 4)))
        == 1
    )
    assert (
        len(sqlite_repository.list_open_planned_reading_tasks_for_business_date(date(2026, 2, 4)))
        == 1
    )
    assert (
        sqlite_repository.list_open_planned_reading_tasks(date(2026, 2, 4))[0].reading_cycle
        is ReadingCycle.DAILY_1X
    )

    sqlite_repository.upsert_measurement_definition_schedule(
        definition_id,
        '{"reading_sessions":[{"reading_session":"06:00","day_offset":0}]}',
        effective_from_date=date(2026, 2, 4),
        created_at=READ_AT,
    )
    result = sqlite_repository.recompose_future_planned_readings(
        definition_id,
        from_business_date=date(2026, 2, 4),
    )
    assert result == (1, 1)
    candidates = sqlite_repository.list_planned_reading_recomposition_candidates(
        definition_id, date(2026, 2, 4)
    )
    assert {candidate.planned_reading_id for candidate in candidates} == {
        "plan-recompose",
        "plan-definition-test-001-2026-02-04-0600",
    }
    assert sqlite_repository.recompose_future_planned_readings(
        definition_id, from_business_date=date(2026, 2, 4)
    ) == (0, 0)
    superseded_count = sqlite_repository.supersede_planned_readings(
        ["plan-definition-test-001-2026-02-04-0600"],
        superseded_at=READ_AT,
    )
    assert superseded_count == 1
    assert sqlite_repository.recompose_future_planned_readings(
        definition_id, from_business_date=date(2026, 2, 4)
    ) == (0, 1)
    assert (
        sqlite_repository.supersede_planned_readings(
            ["plan-definition-test-001-2026-02-04-0600"],
            superseded_at=READ_AT,
        )
        == 1
    )
    assert sqlite_repository.supersede_planned_readings([], superseded_at=READ_AT) == 0


def test_repository_timezone_invalid_and_naive_clock_errors(
    memory_conn: sqlite3.Connection,
) -> None:
    repository = SQLiteRepository(memory_conn)
    with pytest.raises(ValidationError, match="タイムゾーン付き"):
        SQLiteRepository(memory_conn, clock=lambda: datetime(2026, 2, 4))._now()
    repository.upsert_setting("app.timezone.v1", "Invalid/Zone")
    with pytest.raises(InvalidTimezoneError):
        repository._business_date()


def test_timezone_change_uses_repository_planned_reading_guard(
    memory_conn: sqlite3.Connection,
    sqlite_repository: SQLiteRepository,
    sample_master: tuple[str, str],
) -> None:
    _, definition_id = sample_master
    timezone_service = TimezoneSettingsService(
        sqlite_repository,
        transaction_factory=lambda: transaction(memory_conn),
    )
    assert timezone_service.get_timezone().key == "Asia/Tokyo"
    assert timezone_service.save_timezone("Asia/Tokyo").key == "Asia/Tokyo"
    assert not sqlite_repository.has_planned_readings()

    sqlite_repository.create_planned_reading_if_missing(
        _planned("plan-timezone-guard", definition_id, "meter-test-001")
    )
    with pytest.raises(TimezoneChangeError):
        timezone_service.save_timezone("UTC")


def test_reading_upsert_duplicate_tie_stale_and_decimal_storage(
    memory_conn: sqlite3.Connection,
    sqlite_repository: SQLiteRepository,
    sample_master: tuple[str, str],
) -> None:
    meter_id, definition_id = sample_master
    sqlite_repository.create_planned_reading_if_missing(_planned("plan-a", definition_id, meter_id))

    first = _reading("plan-a", raw=Decimal("10.50"))
    assert sqlite_repository.upsert_reading(first).disposition is UpsertDisposition.INSERTED
    duplicate = sqlite_repository.upsert_reading(first)
    assert duplicate.disposition is UpsertDisposition.IGNORED_DUPLICATE
    assert memory_conn.execute(
        "SELECT raw_reading, arrival_sequence FROM reading_transactions "
        "WHERE planned_reading_id = 'plan-a'"
    ).fetchone() == ("10.50", 1)

    tie = sqlite_repository.upsert_reading(
        _reading("plan-a", raw=Decimal("11.25"), recorded_at=READ_AT + timedelta(seconds=1))
    )
    assert tie.disposition is UpsertDisposition.REPLACED_ON_TIE
    assert memory_conn.execute(
        "SELECT raw_reading, arrival_sequence FROM reading_transactions "
        "WHERE planned_reading_id = 'plan-a'"
    ).fetchone() == ("11.25", 2)

    stale = sqlite_repository.upsert_reading(
        _reading(
            "plan-a",
            raw=Decimal("99"),
            client_updated_at=READ_AT - timedelta(seconds=1),
        )
    )
    assert stale.disposition is UpsertDisposition.IGNORED_STALE
    assert memory_conn.execute(
        "SELECT raw_reading, arrival_sequence FROM reading_transactions "
        "WHERE planned_reading_id = 'plan-a'"
    ).fetchone() == ("11.25", 2)
    assert memory_conn.execute(
        "SELECT recorded_at, client_updated_at FROM reading_transactions "
        "WHERE planned_reading_id = 'plan-a'"
    ).fetchone() == ("2026-02-04 08:00:01", "2026-02-04 08:00:00")
    updated = sqlite_repository.upsert_reading(
        _reading(
            "plan-a",
            raw=Decimal("12"),
            recorded_at=READ_AT + timedelta(seconds=2),
            client_updated_at=READ_AT + timedelta(seconds=2),
        )
    )
    assert updated.disposition is UpsertDisposition.UPDATED
    sqlite_repository.save_calculated_usage(
        CalculatedUsageUpdate("plan-a", Decimal("11.25"), Decimal("0.75"))
    )
    assert memory_conn.execute(
        "SELECT last_reading, usage_delta FROM reading_transactions "
        "WHERE planned_reading_id = 'plan-a'"
    ).fetchone() == ("11.25", "0.75")

    sqlite_repository.create_planned_reading_if_missing(
        _planned("plan-manual", "definition-test-001", "meter-test-001")
    )
    mobile = _reading(
        "plan-manual",
        raw=Decimal("3"),
        client_updated_at=READ_AT + timedelta(seconds=5),
    )
    assert sqlite_repository.upsert_reading(mobile).disposition is UpsertDisposition.INSERTED
    manual = replace(
        mobile,
        client_updated_at=READ_AT - timedelta(seconds=5),
        updated_by="PC担当",
        device_id=PC_DIRECT_DEVICE_ID,
        result_source=ResultSource.MANUAL,
    )
    assert sqlite_repository.upsert_reading(manual).disposition is UpsertDisposition.UPDATED
    assert memory_conn.execute(
        "SELECT result_source, device_id, arrival_sequence FROM reading_transactions "
        "WHERE planned_reading_id = 'plan-manual'"
    ).fetchone() == ("manual", PC_DIRECT_DEVICE_ID, 2)


def test_measured_history_is_meter_and_sequence_scoped(
    sqlite_repository: SQLiteRepository, sample_master: tuple[str, str]
) -> None:
    meter_id, definition_id = sample_master
    earlier = _planned(
        "plan-a",
        definition_id,
        meter_id,
        scheduled_at=READ_AT - timedelta(hours=1),
    )
    current = _planned("plan-b", definition_id, meter_id)
    later = _planned(
        "plan-c",
        definition_id,
        meter_id,
        scheduled_at=READ_AT + timedelta(hours=1),
    )
    sqlite_repository.create_planned_readings_if_missing([earlier, current, later])
    sqlite_repository.upsert_reading(
        _reading("plan-a", raw=Decimal("10"), recorded_at=READ_AT - timedelta(hours=1))
    )
    sqlite_repository.upsert_reading(_reading("plan-c", raw=Decimal("30")))

    previous = sqlite_repository.get_latest_measured_reading(
        definition_id,
        meter_id,
        before_scheduled_at=READ_AT,
        before_planned_reading_id="plan-b",
    )
    assert previous is not None
    assert previous.planned_reading_id == "plan-a"
    following = sqlite_repository.list_following_measured_readings(
        definition_id,
        meter_id,
        after_scheduled_at=READ_AT,
        after_planned_reading_id="plan-b",
    )
    assert [reading.planned_reading_id for reading in following] == ["plan-c"]

    with pytest.raises(ValidationError, match="制約"):
        sqlite_repository.create_planned_reading_if_missing(
            _planned("missing-master", "not-a-definition", meter_id)
        )


def test_replacement_reflection_preserves_boundary_even_without_initial_value(
    sqlite_repository: SQLiteRepository,
    sample_master: tuple[str, str],
) -> None:
    from meter_reading_mini.core.contract_models import Category
    from meter_reading_mini.core.meter_reading_sync_repositories import MeterMasterCreateRequest

    old_meter_id, definition_id = sample_master
    new_meter_id = "meter-test-002"
    sqlite_repository.create_meter_master(
        MeterMasterCreateRequest(
            meter_id=new_meter_id,
            location_id="location-test-001",
            location_name="架空の試験棟",
            meter_name="交換後の試験計器",
            category=Category.ELECTRICITY,
        )
    )
    sqlite_repository.create_planned_reading_if_missing(
        _planned("plan-before-replacement", definition_id, old_meter_id)
    )
    sqlite_repository.create_planned_reading_if_missing(
        _planned(
            "plan-old-meter-history",
            definition_id,
            old_meter_id,
            scheduled_at=READ_AT - timedelta(hours=3),
        )
    )
    sqlite_repository.upsert_reading(
        _reading(
            "plan-old-meter-history",
            raw=Decimal("90"),
            recorded_at=READ_AT - timedelta(hours=3),
        )
    )
    replacement = ReplacementUpsertRequest(
        replacement_event_id="replacement-test-001",
        location_id="location-test-001",
        old_meter_id=old_meter_id,
        new_meter_id=new_meter_id,
        replaced_at=READ_AT - timedelta(hours=1),
        client_updated_at=READ_AT,
        old_final_reading=Decimal("100"),
        new_initial_reading=None,
        updated_by="試験担当",
        note=None,
    )
    assert (
        sqlite_repository.upsert_replacement(replacement).disposition is UpsertDisposition.INSERTED
    )
    stale_replacement = replace(replacement, client_updated_at=READ_AT - timedelta(seconds=1))
    assert (
        sqlite_repository.upsert_replacement(stale_replacement).disposition
        is UpsertDisposition.IGNORED_STALE
    )
    assert (
        sqlite_repository.upsert_replacement(replacement).disposition
        is UpsertDisposition.REPLACED_ON_TIE
    )
    sqlite_repository.reflect_replacement(replacement)

    old = sqlite_repository.get_planned_reading("plan-before-replacement")
    assert old is not None
    assert old.task_state is not None
    assert old.task_state.value == "superseded"
    assert old.close_reason is not None
    assert old.close_reason is PlannedReadingCloseReason.METER_REPLACED
    successor_id = old.superseded_by_planned_reading_id
    assert successor_id is not None
    assert (
        sqlite_repository.get_latest_measured_reading(
            definition_id,
            new_meter_id,
            before_scheduled_at=READ_AT,
            before_planned_reading_id=successor_id,
        )
        is None
    )
    sqlite_repository.upsert_reading(_reading(successor_id, raw=Decimal("5"), recorded_at=READ_AT))
    boundary = sqlite_repository.get_replacement_boundary_for_new_meter_first_reading(
        definition_id,
        new_meter_id,
        before_scheduled_at=READ_AT,
        before_planned_reading_id=successor_id,
        excluding_planned_reading_id=successor_id,
    )
    assert boundary is not None
    assert boundary.new_initial_reading is None
    assert boundary.new_meter_id == new_meter_id
    assert (
        sqlite_repository.get_replacement_boundary_for_new_meter_first_reading(
            definition_id,
            new_meter_id,
            before_scheduled_at=READ_AT + timedelta(days=1),
            before_planned_reading_id="zzzz",
        )
        is None
    )


def test_master_use_protection_and_staff_constraints(
    memory_conn: sqlite3.Connection,
    sqlite_repository: SQLiteRepository,
    sample_master: tuple[str, str],
) -> None:
    meter_id, definition_id = sample_master
    with pytest.raises(MasterInUseError):
        sqlite_repository.set_meter_master_active(meter_id, False)

    sqlite_repository.create_planned_reading_if_missing(
        _planned("plan-open", definition_id, meter_id)
    )
    with pytest.raises(MasterInUseError):
        sqlite_repository.set_meter_master_active(meter_id, False)
    sqlite_repository.auto_close_open_planned_readings(
        date(2026, 2, 4),
        closed_at=READ_AT,
        close_reason=PlannedReadingCloseReason.CUTOFF_ELAPSED,
    )
    usage_after_cutoff = sqlite_repository.get_meter_master_usage(meter_id)
    assert usage_after_cutoff.has_active_assignment
    assert not usage_after_cutoff.has_open_planned_readings
    sqlite_repository.create_planned_reading_if_missing(
        PlannedReadingCreateRequest(
            planned_reading_id="plan-future-open",
            measurement_definition_id=definition_id,
            meter_id=meter_id,
            location_id="location-test-001",
            business_date=date(2026, 2, 5),
            release_date=date(2026, 2, 5),
            reading_session=ReadingSession.SESSION_0800,
            scheduled_at=READ_AT + timedelta(days=1),
        )
    )
    sqlite_repository.create_planned_reading_if_missing(
        PlannedReadingCreateRequest(
            planned_reading_id="plan-completed",
            measurement_definition_id=definition_id,
            meter_id=meter_id,
            location_id="location-test-001",
            business_date=date(2026, 2, 5),
            release_date=date(2026, 2, 5),
            reading_session=ReadingSession.SESSION_1400,
            scheduled_at=READ_AT + timedelta(days=1, hours=6),
        )
    )
    sqlite_repository.upsert_reading(
        _reading(
            "plan-completed",
            raw=Decimal("100"),
            recorded_at=READ_AT + timedelta(days=1, hours=6),
            client_updated_at=READ_AT + timedelta(days=1, hours=6),
        )
    )
    sqlite_repository.set_measurement_definition_active(definition_id, False)
    assert sqlite_repository.get_meter_master_usage(meter_id).has_active_assignment is False
    assert sqlite_repository.get_meter_master_usage(meter_id).has_open_planned_readings is False
    auto_closed = sqlite_repository.get_planned_reading("plan-open")
    assert auto_closed is not None
    assert auto_closed.task_state is not None
    assert auto_closed.task_state.value == "auto_closed"
    future_open = sqlite_repository.get_planned_reading("plan-future-open")
    assert future_open is not None
    assert future_open.task_state is not None
    assert future_open.task_state.value == "superseded"
    assert future_open.close_reason is PlannedReadingCloseReason.DEFINITION_DEACTIVATED
    completed = sqlite_repository.get_planned_reading("plan-completed")
    assert completed is not None
    assert completed.task_state is not None
    assert completed.task_state.value == "completed"
    sqlite_repository.set_meter_master_active(meter_id, False)

    staff = sqlite_repository.create_staff(StaffCreateRequest("1", "試験担当"))
    assert staff.staff_id == "1"
    assert sqlite_repository.get_staff("1") == staff
    with pytest.raises(ValidationError, match="整数"):
        sqlite_repository.get_staff("staff-1")
    with pytest.raises(ValidationError, match="制約"):
        sqlite_repository.create_staff(StaffCreateRequest("2", "試験担当"))
    assert memory_conn.execute("SELECT COUNT(*) FROM staff WHERE name = '試験担当'").fetchone() == (
        1,
    )
    updated_staff = sqlite_repository.update_staff("1", StaffUpdateRequest("更新後の試験担当"))
    assert updated_staff.display_name == "更新後の試験担当"
    with pytest.raises(ValidationError, match="見つかりません"):
        sqlite_repository.update_staff("99", StaffUpdateRequest("存在しない担当者"))
    sqlite_repository.set_staff_active("1", False)
    assert sqlite_repository.list_staff() == []
    assert sqlite_repository.list_staff(include_inactive=True)[0].is_active is False
    with pytest.raises(ValidationError, match="見つかりません"):
        sqlite_repository.set_staff_active("99", True)


def test_schema_checks_result_source_and_close_reason(
    memory_conn: sqlite3.Connection,
    sqlite_repository: SQLiteRepository,
    sample_master: tuple[str, str],
) -> None:
    meter_id, definition_id = sample_master
    sqlite_repository.create_planned_reading_if_missing(
        _planned("plan-check", definition_id, meter_id)
    )
    with pytest.raises(ValidationError, match="制約"):
        sqlite_repository._execute(
            "INSERT INTO reading_transactions "
            "(planned_reading_id, result_status, recorded_at, client_updated_at, device_id, "
            "updated_by, signature, result_source) "
            "VALUES ('plan-check', 'measured', '2026-02-04 08:00:00', "
            "'2026-02-04 08:00:00', 'device', 'staff', 'sig', 'system')"
        )
    with pytest.raises(ValidationError, match="制約"):
        sqlite_repository._execute(
            "UPDATE planned_readings SET close_reason = 'not-a-reason' "
            "WHERE planned_reading_id = 'plan-check'"
        )
