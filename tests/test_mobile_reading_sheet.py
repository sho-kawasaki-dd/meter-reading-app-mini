from __future__ import annotations

import json
import sqlite3
from datetime import UTC, date, datetime, time
from decimal import Decimal
from functools import partial
from typing import Any
from zoneinfo import ZoneInfo

import pytest
from reading_service_helpers import (
    DEFINITION_ID,
    LOCATION_ID,
    METER_ID,
    SHEET_ID,
    build_services,
    confirmation_context,
    create_plan,
    mobile_submission,
    sync_request,
)

from meter_reading_mini.core.contract_models import (
    CalculationMethod,
    Category,
    MeterReadingSyncRequest,
    MeterReplacementSubmission,
    Purpose,
    ReadingCycle,
    ResultStatus,
    SyncItemRejectCode,
    ValueSchema,
)
from meter_reading_mini.core.errors import (
    SupersededPlannedReadingError,
    ValidationError,
)
from meter_reading_mini.core.meter_reading_sync import MeterReadingSyncService
from meter_reading_mini.core.meter_reading_sync_repositories import (
    ManualReadingInput,
    MeasurementDefinitionCreateRequest,
    MeterMasterCreateRequest,
    PlannedReadingTaskState,
    ReadingUpsertRequest,
    ResultSource,
)
from meter_reading_mini.core.mobile_reading import (
    RESULT_KIND,
    SheetFilter,
    encode_mr1_envelope,
    parse_mobile_reading_result,
)
from meter_reading_mini.core.mobile_reading_sheet import (
    EmptySheetError,
    FutureSheetBusinessDateError,
    MobileReadingSheetService,
    build_sheet_filename,
)
from meter_reading_mini.core.reading_signature import verify_meter_reading_signature
from meter_reading_mini.core.schedule_settings import ReleaseOffsetSettingsService
from meter_reading_mini.core.timeutil import parse_utc
from meter_reading_mini.core.work_package_download import (
    PlannedReadingGeneratorService,
    PlannedReadingLifecycleService,
)
from meter_reading_mini.db.connection import transaction
from meter_reading_mini.db.repositories import SQLiteRepository

JST = ZoneInfo("Asia/Tokyo")
NOW = datetime(2026, 2, 4, 3, 0, tzinfo=UTC)
DAILY_0800 = '{"reading_sessions":[{"reading_session":"08:00","day_offset":0}]}'
DAILY_TWICE = (
    '{"reading_sessions":[{"reading_session":"08:00","day_offset":0},'
    '{"reading_session":"22:00","day_offset":0}]}'
)
WEEKLY_WEDNESDAY = '{"weekday":"wednesday","reading_session":"14:00"}'


def _extract_data(html: str) -> dict[str, Any]:
    text = html.split('id="mrm-sheet-data">', 1)[1].split("</script>", 1)[0]
    return json.loads(text)


def _build_sheet_service(
    conn: sqlite3.Connection,
    repository: SQLiteRepository,
    *,
    now: datetime = NOW,
) -> MobileReadingSheetService:
    transaction_factory = partial(transaction, conn)
    release_offsets = ReleaseOffsetSettingsService(
        repository,
        recomposition_repository=repository,
        transaction_factory=transaction_factory,
        current_business_date_provider=lambda: now.astimezone(JST).date(),
        clock=lambda: now,
    )
    return MobileReadingSheetService(
        planned_reading_generator=PlannedReadingGeneratorService(
            repository,
            release_date_calculator=release_offsets,
            scheduled_timezone_provider=lambda: JST,
        ),
        planned_reading_lifecycle_service=PlannedReadingLifecycleService(
            repository,
            current_datetime_provider=lambda: now.astimezone(JST),
            cutoff_time_provider=lambda: time(7),
        ),
        task_repository=repository,
        master_repository=repository,
        historical_reading_repository=repository,
        boundary_repository=repository,
        transaction_factory=transaction_factory,
        timezone_provider=lambda: JST,
        clock=lambda: now,
        sheet_id_factory=lambda: SHEET_ID,
    )


def _add_definition(
    conn: sqlite3.Connection,
    repository: SQLiteRepository,
    definition_id: str,
    *,
    cycle: ReadingCycle,
    schedule_json: str,
) -> str:
    meter_id = f"meter-{definition_id}"
    with transaction(conn):
        repository.create_meter_master(
            MeterMasterCreateRequest(
                meter_id=meter_id,
                location_id=LOCATION_ID,
                location_name="試験場所",
                meter_name=f"メーター {definition_id}",
                category=Category.WATER,
            )
        )
        repository.create_measurement_definition(
            MeasurementDefinitionCreateRequest(
                measurement_definition_id=definition_id,
                location_id=LOCATION_ID,
                display_name=f"測定 {definition_id}",
                category=Category.WATER,
                unit="m3",
                value_schema=ValueSchema.DECIMAL_STRING,
                multiplier=Decimal("1"),
                decimal_places=2,
                warning_threshold=None,
                rollover_value=None,
                calculation_method=CalculationMethod.DELTA,
                reading_cycle=cycle,
                purpose=Purpose.OPERATIONAL,
                generation_start_date=date(2026, 2, 1),
                initial_schedule_json=schedule_json,
                initial_meter_id=meter_id,
            )
        )
    return meter_id


def _slot_id(definition_id: str, day: str, session: str) -> str:
    return f"plan-{definition_id}-{day}-{session}"


def _task_state(conn: sqlite3.Connection, planned_reading_id: str) -> str:
    row = conn.execute(
        "SELECT task_state FROM planned_readings WHERE planned_reading_id = ?",
        (planned_reading_id,),
    ).fetchone()
    return str(row[0])


def test_create_sheet_embeds_auto_closed_slot_and_inactive_masters(
    file_database_repository: tuple[sqlite3.Connection, SQLiteRepository],
) -> None:
    conn, repository = file_database_repository
    build_services(conn, repository)
    service = _build_sheet_service(conn, repository)
    slot_id = _slot_id(DEFINITION_ID, "2026-02-03", "0800")

    sheet = service.create_sheet(date(2026, 2, 3))

    assert (sheet.sheet_id, sheet.slot_count) == (SHEET_ID, 1)
    assert sheet.filename == "meter_reading_20260203_120000.html"
    data = _extract_data(sheet.html)
    assert data["kind"] == "mrm-meter-reading-sheet"
    assert data["v"] == 1
    assert data["sheet_id"] == SHEET_ID
    assert data["business_date"] == "2026-02-03"
    assert data["generated_at"] == "2026-02-04 03:00:00"
    assert data["generated_at_local"] == "2026-02-04 12:00:00"
    assert data["filter"] == {"reading_sessions": None, "cycle_groups": None}
    assert data["locations"] == [{"location_id": LOCATION_ID, "location_name": "試験場所"}]
    assert data["slots"] == [
        {
            "planned_reading_id": slot_id,
            "measurement_definition_id": DEFINITION_ID,
            "meter_id": METER_ID,
            "reading_session": "08:00",
            "scheduled_at": "2026-02-02 23:00:00",
            "reading_cycle": "daily_1x",
            "task_state": "auto_closed",
            "initial_last_reading": None,
            "initial_previous_scheduled_at": None,
            "initial_previous_planned_reading_id": None,
            "previous_source": "none",
        }
    ]
    assert _task_state(conn, slot_id) == "auto_closed"

    conn.execute("UPDATE measurement_definitions SET is_active = 0")
    conn.execute("UPDATE physical_meters SET is_active = 0")
    inactive = _extract_data(service.create_sheet(date(2026, 2, 3)).html)

    assert inactive["meters"] == [
        {
            "meter_id": METER_ID,
            "location_id": LOCATION_ID,
            "location_name": "試験場所",
            "meter_name": "試験メーター",
            "category": "water",
            "meter_serial": None,
        }
    ]
    assert inactive["definitions"] == [
        {
            "measurement_definition_id": DEFINITION_ID,
            "location_id": LOCATION_ID,
            "display_name": "試験測定対象",
            "category": "water",
            "unit": "m3",
            "value_schema": "decimal_string",
            "multiplier": "1",
            "decimal_places": 2,
            "warning_threshold": None,
            "rollover_value": None,
            "calculation_method": "delta",
        }
    ]
    assert [slot["planned_reading_id"] for slot in inactive["slots"]] == [slot_id]


def test_sheet_excludes_completed_and_superseded_slots_and_their_masters(
    file_database_repository: tuple[sqlite3.Connection, SQLiteRepository],
) -> None:
    conn, repository = file_database_repository
    writer, sync_service = build_services(conn, repository)
    completed_meter = METER_ID
    superseded_meter = _add_definition(
        conn, repository, "def-superseded", cycle=ReadingCycle.DAILY_1X, schedule_json=DAILY_0800
    )
    open_meter = _add_definition(
        conn, repository, "def-open", cycle=ReadingCycle.DAILY_1X, schedule_json=DAILY_0800
    )
    service = _build_sheet_service(conn, repository)
    service.create_sheet(date(2026, 2, 3))
    sync_service.sync(
        sync_request(mobile_submission(_slot_id(DEFINITION_ID, "2026-02-03", "0800"), "10")),
        confirmation_context(),
    )
    conn.execute(
        "UPDATE planned_readings SET task_state = 'superseded' WHERE planned_reading_id = ?",
        (_slot_id("def-superseded", "2026-02-03", "0800"),),
    )

    data = _extract_data(service.create_sheet(date(2026, 2, 3)).html)

    assert [slot["planned_reading_id"] for slot in data["slots"]] == [
        _slot_id("def-open", "2026-02-03", "0800")
    ]
    assert [meter["meter_id"] for meter in data["meters"]] == [open_meter]
    assert [d["measurement_definition_id"] for d in data["definitions"]] == ["def-open"]
    assert completed_meter != open_meter != superseded_meter
    assert writer is not None


def test_sheet_filters_and_filename_follow_the_filter_conditions(
    file_database_repository: tuple[sqlite3.Connection, SQLiteRepository],
) -> None:
    conn, repository = file_database_repository
    build_services(conn, repository)
    _add_definition(
        conn, repository, "def-twice", cycle=ReadingCycle.DAILY_2X, schedule_json=DAILY_TWICE
    )
    _add_definition(
        conn, repository, "def-weekly", cycle=ReadingCycle.WEEKLY, schedule_json=WEEKLY_WEDNESDAY
    )
    service = _build_sheet_service(conn, repository)
    today = date(2026, 2, 4)

    everything = service.create_sheet(today)
    assert everything.filename == "meter_reading_20260204_120000.html"
    assert [
        (slot["reading_session"], slot["measurement_definition_id"], slot["task_state"])
        for slot in _extract_data(everything.html)["slots"]
    ] == [
        ("08:00", "def-twice", "open"),
        ("08:00", DEFINITION_ID, "open"),
        ("14:00", "def-weekly", "open"),
        ("22:00", "def-twice", "open"),
    ]
    assert everything.slot_count == 4

    morning = service.create_sheet(today, reading_sessions=["08:00"])
    assert morning.filename == "meter_reading_20260204_0800_120000.html"
    assert morning.slot_count == 2
    assert _extract_data(morning.html)["filter"] == {
        "reading_sessions": ["08:00"],
        "cycle_groups": None,
    }

    weekly = service.create_sheet(today, cycle_groups=["weekly"])
    assert weekly.filename == "meter_reading_20260204_weekly_120000.html"
    assert weekly.slot_count == 1

    combined = service.create_sheet(today, reading_sessions=["22:00"], cycle_groups=["daily"])
    assert combined.filename == "meter_reading_20260204_filtered_120000.html"
    assert combined.slot_count == 1

    several = service.create_sheet(today, reading_sessions=["22:00", "08:00"])
    assert several.filename == "meter_reading_20260204_filtered_120000.html"
    assert several.slot_count == 3

    with pytest.raises(EmptySheetError):
        service.create_sheet(today, cycle_groups=["monthly"])
    with pytest.raises(ValidationError):
        service.create_sheet(today, reading_sessions=["12:34"])


def test_build_sheet_filename_covers_every_filter_shape() -> None:
    day = date(2026, 2, 4)

    assert (
        build_sheet_filename(day, "120000", SheetFilter()) == "meter_reading_20260204_120000.html"
    )
    assert (
        build_sheet_filename(day, "120000", SheetFilter.create(["14:00"]))
        == "meter_reading_20260204_1400_120000.html"
    )
    assert (
        build_sheet_filename(day, "120000", SheetFilter.create(None, ["every_two_months"]))
        == "meter_reading_20260204_every_two_months_120000.html"
    )
    assert (
        build_sheet_filename(day, "120000", SheetFilter.create(["14:00"], ["daily"]))
        == "meter_reading_20260204_filtered_120000.html"
    )


def test_future_and_empty_sheets_are_rejected(
    file_database_repository: tuple[sqlite3.Connection, SQLiteRepository],
) -> None:
    conn, repository = file_database_repository
    build_services(conn, repository)
    service = _build_sheet_service(conn, repository)

    with pytest.raises(FutureSheetBusinessDateError):
        service.create_sheet(date(2026, 2, 5))
    with pytest.raises(EmptySheetError):
        service.create_sheet(date(2026, 1, 31))
    assert conn.execute("SELECT COUNT(*) FROM planned_readings").fetchone() == (0,)


def test_sheet_creation_is_one_transaction_that_rolls_back_on_failure(
    file_database_repository: tuple[sqlite3.Connection, SQLiteRepository],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    conn, repository = file_database_repository
    build_services(conn, repository)
    service = _build_sheet_service(conn, repository)

    def fail(*args: object, **kwargs: object) -> list[Any]:
        raise RuntimeError("masters unavailable")

    monkeypatch.setattr(repository, "list_sheet_measurement_definitions", fail)

    with pytest.raises(RuntimeError):
        service.create_sheet(date(2026, 2, 3))
    assert conn.execute("SELECT COUNT(*) FROM planned_readings").fetchone() == (0,)
    assert not conn.in_transaction


@pytest.mark.parametrize("missing", ["meters", "definitions", "location"])
def test_sheet_rejects_missing_master_data_instead_of_dropping_it(
    file_database_repository: tuple[sqlite3.Connection, SQLiteRepository],
    monkeypatch: pytest.MonkeyPatch,
    missing: str,
) -> None:
    conn, repository = file_database_repository
    build_services(conn, repository)
    service = _build_sheet_service(conn, repository)
    original_meters = repository.list_sheet_meters

    if missing == "meters":
        monkeypatch.setattr(repository, "list_sheet_meters", lambda ids: [])
    elif missing == "definitions":
        monkeypatch.setattr(repository, "list_sheet_measurement_definitions", lambda ids: [])
    else:
        monkeypatch.setattr(
            repository,
            "list_sheet_meters",
            lambda ids: [
                meter.model_copy(update={"location_id": "elsewhere"})
                for meter in original_meters(ids)
            ],
        )

    with pytest.raises(ValidationError):
        service.create_sheet(date(2026, 2, 3))


def test_sheet_master_queries_handle_empty_states_and_large_id_lists(
    file_database_repository: tuple[sqlite3.Connection, SQLiteRepository],
) -> None:
    conn, repository = file_database_repository
    build_services(conn, repository)
    many_ids = [f"missing-{index}" for index in range(1200)]

    assert repository.list_planned_reading_tasks_for_sheet(date(2026, 2, 4), []) == []
    assert repository.list_sheet_meters([*many_ids, METER_ID])[0].meter_id == METER_ID
    assert (
        repository.list_sheet_measurement_definitions([*many_ids, DEFINITION_ID])[
            0
        ].measurement_definition_id
        == DEFINITION_ID
    )
    assert repository.list_sheet_meters(many_ids) == []


def test_initial_previous_reading_uses_measured_slot_from_the_same_day(
    file_database_repository: tuple[sqlite3.Connection, SQLiteRepository],
) -> None:
    conn, repository = file_database_repository
    _, sync_service = build_services(conn, repository)
    _add_definition(
        conn, repository, "def-twice", cycle=ReadingCycle.DAILY_2X, schedule_json=DAILY_TWICE
    )
    service = _build_sheet_service(conn, repository)
    service.create_sheet(date(2026, 2, 4))
    morning_id = _slot_id("def-twice", "2026-02-04", "0800")
    previous_day_id = _slot_id(DEFINITION_ID, "2026-02-03", "0800")
    response = sync_service.sync(
        sync_request(mobile_submission(previous_day_id, "100.50")),
        confirmation_context(),
    )
    assert response.accepted_reading_items == 1
    assert _task_state(conn, previous_day_id) == "completed"
    conn.execute(
        "INSERT INTO reading_transactions (planned_reading_id, result_status, recorded_at, "
        "client_updated_at, raw_reading, device_id, updated_by, signature, result_source) "
        "VALUES (?, 'measured', '2026-02-03 23:00:00', '2026-02-03 23:00:00', '40.0', "
        "'pc-direct', 'x', 'x', 'manual')",
        (morning_id,),
    )
    conn.execute(
        "UPDATE planned_readings SET task_state = 'completed' WHERE planned_reading_id = ?",
        (morning_id,),
    )

    data = _extract_data(service.create_sheet(date(2026, 2, 4), reading_sessions=["22:00"]).html)

    twice = next(s for s in data["slots"] if s["measurement_definition_id"] == "def-twice")
    assert twice["planned_reading_id"] == _slot_id("def-twice", "2026-02-04", "2200")
    assert twice["previous_source"] == "measured"
    assert twice["initial_last_reading"] == "40.0"
    assert twice["initial_previous_scheduled_at"] == "2026-02-03 23:00:00"
    assert twice["initial_previous_planned_reading_id"] == morning_id
    assert all(s["measurement_definition_id"] != DEFINITION_ID for s in data["slots"])

    next_day = _extract_data(
        service.create_sheet(date(2026, 2, 4), reading_sessions=["08:00"]).html
    )
    carried = next(s for s in next_day["slots"] if s["measurement_definition_id"] == DEFINITION_ID)
    assert carried["previous_source"] == "measured"
    assert carried["initial_last_reading"] == "100.50"
    assert carried["initial_previous_scheduled_at"] == "2026-02-02 23:00:00"
    assert carried["initial_previous_planned_reading_id"] == previous_day_id


def test_mobile_result_round_trip_completes_auto_closed_slot_with_audit_data(
    file_database_repository: tuple[sqlite3.Connection, SQLiteRepository],
) -> None:
    conn, repository = file_database_repository
    _, sync_service = build_services(conn, repository)
    service = _build_sheet_service(conn, repository)
    sheet = service.create_sheet(date(2026, 2, 3))
    slot_id = _slot_id(DEFINITION_ID, "2026-02-03", "0800")
    assert _task_state(conn, slot_id) == "auto_closed"
    result_code = encode_mr1_envelope(
        {
            "kind": RESULT_KIND,
            "v": 1,
            "sheet_id": sheet.sheet_id,
            "business_date": "2026-02-03",
            "entries": [
                {
                    "planned_reading_id": slot_id,
                    "result_status": "measured",
                    "raw_reading": "123.45",
                    "recorded_at": "2026-02-04T09:00:00.500+09:00",
                    "client_updated_at": "2026-02-04T09:01:00+09:00",
                    "note": "後日入力",
                }
            ],
        }
    )

    result = parse_mobile_reading_result(f"お疲れさまです\n> {result_code}\n")
    response = sync_service.sync(
        result.to_sync_request(),
        result.to_confirmation_context(staff_id="1", staff_name="入力側の名前"),
    )

    assert response.accepted_reading_items == 1
    assert _task_state(conn, slot_id) == "completed"
    stored = conn.execute(
        "SELECT raw_reading, usage_delta, device_id, result_source, updated_by, recorded_at, "
        "signature, note FROM reading_transactions WHERE planned_reading_id = ?",
        (slot_id,),
    ).fetchone()
    assert stored[:6] == (
        "123.45",
        "0",
        sheet.sheet_id,
        "mobile_html",
        "試験担当",
        "2026-02-04 00:00:00",
    )
    assert stored[7] == "後日入力"
    verify_meter_reading_signature(
        planned_reading_id=slot_id,
        result_status=ResultStatus.MEASURED,
        raw_reading=Decimal("123.45"),
        structured_payload_json=None,
        recorded_at=parse_utc(stored[5]),
        device_id=sheet.sheet_id,
        signature=stored[6],
    )


def _replace_meter(
    conn: sqlite3.Connection,
    repository: SQLiteRepository,
    sync_service: MeterReadingSyncService,
    *,
    new_initial: str | None,
) -> str:
    new_meter_id = "replacement-meter"
    with transaction(conn):
        repository.create_meter_master(
            MeterMasterCreateRequest(
                meter_id=new_meter_id,
                location_id=LOCATION_ID,
                location_name="試験場所",
                meter_name="交換後メーター",
                category=Category.WATER,
            )
        )
    scheduled = [datetime(2026, 2, 4, hour, 0, tzinfo=UTC) for hour in (4, 6, 7)]
    create_plan(repository, "plan-old-before", scheduled[0])
    create_plan(repository, "plan-old-slot", scheduled[1])
    create_plan(repository, "plan-new-late", scheduled[2], meter_id=new_meter_id)
    sync_service.sync(
        sync_request(mobile_submission("plan-old-before", "90")), confirmation_context()
    )
    replacement = MeterReplacementSubmission.model_validate(
        {
            "replacement_event_id": "replace-event",
            "location_id": LOCATION_ID,
            "old_meter_id": METER_ID,
            "new_meter_id": new_meter_id,
            "replaced_at": "2026-02-04T05:30:00+00:00",
            "client_updated_at": "2026-02-04T05:31:00+00:00",
            "old_final_reading": None,
            "new_initial_reading": new_initial,
            "updated_by": "入力側の担当者",
        }
    )
    response = sync_service.sync(
        MeterReadingSyncRequest(reading_items=[], replacement_items=[replacement]),
        confirmation_context(),
    )
    assert response.accepted_replacement_items == 1
    successor = repository.get_planned_reading("plan-old-slot")
    assert successor is not None
    assert successor.superseded_by_planned_reading_id is not None
    return successor.superseded_by_planned_reading_id


@pytest.mark.parametrize(
    ("new_initial", "source", "reading"),
    [("95", "replacement_initial", "95"), (None, "replacement_no_initial", None)],
)
def test_replacement_boundary_sets_initial_previous_reading_and_supersedes_old_slot(
    file_database_repository: tuple[sqlite3.Connection, SQLiteRepository],
    new_initial: str | None,
    source: str,
    reading: str | None,
) -> None:
    conn, repository = file_database_repository
    writer, sync_service = build_services(conn, repository)
    successor_id = _replace_meter(conn, repository, sync_service, new_initial=new_initial)
    service = _build_sheet_service(conn, repository)

    data = _extract_data(service.create_sheet(date(2026, 2, 4)).html)

    slots = {slot["planned_reading_id"]: slot for slot in data["slots"]}
    assert "plan-old-before" not in slots
    assert "plan-old-slot" not in slots
    assert slots[successor_id]["meter_id"] == "replacement-meter"
    for planned_reading_id in (successor_id, "plan-new-late"):
        slot = slots[planned_reading_id]
        assert slot["previous_source"] == source
        assert slot["initial_last_reading"] == reading
        assert slot["initial_previous_scheduled_at"] is None
        assert slot["initial_previous_planned_reading_id"] is None
    assert [meter["meter_id"] for meter in data["meters"]] == ["replacement-meter"]

    response = sync_service.sync(
        sync_request(mobile_submission("plan-old-slot", "99")), confirmation_context()
    )
    assert response.accepted_reading_items == 0
    assert response.rejected_reading_items[0].planned_reading_id == "plan-old-slot"
    assert response.rejected_reading_items[0].code is SyncItemRejectCode.INVALID_READING_SUBMISSION
    assert "置き換え済み" in response.rejected_reading_items[0].detail
    assert conn.execute(
        "SELECT COUNT(*) FROM reading_transactions WHERE planned_reading_id = 'plan-old-slot'"
    ).fetchone() == (0,)
    with pytest.raises(SupersededPlannedReadingError):
        writer.save_manual_reading(
            ManualReadingInput("plan-old-slot", ResultStatus.MEASURED, NOW, Decimal("99"), None),
            staff_id="1",
        )


def test_repository_rejects_results_for_superseded_slots_but_not_other_states(
    sqlite_repository: SQLiteRepository,
    superseded_planned_reading_ids: tuple[str, str, str],
) -> None:
    replaced_id, replacement_id, _ = superseded_planned_reading_ids
    _, definition_id = "meter-test-001", "definition-test-001"

    def request(planned_reading_id: str) -> ReadingUpsertRequest:
        return ReadingUpsertRequest(
            planned_reading_id=planned_reading_id,
            measurement_definition_id=definition_id,
            result_status=ResultStatus.SKIPPED,
            recorded_at=NOW,
            client_updated_at=NOW,
            raw_reading=None,
            structured_payload_json=None,
            updated_by="試験担当",
            signature="signature",
            note=None,
            device_id=SHEET_ID,
            result_source=ResultSource.MOBILE_HTML,
        )

    with pytest.raises(SupersededPlannedReadingError):
        sqlite_repository.upsert_reading(request(replaced_id))
    assert sqlite_repository.upsert_reading(request(replacement_id)).accepted
    stored = sqlite_repository.get_planned_reading(replacement_id)
    assert stored is not None
    assert stored.task_state is PlannedReadingTaskState.COMPLETED
