from __future__ import annotations

import sqlite3
from dataclasses import replace
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from functools import partial
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

from meter_reading_mini.app_context import create_app_context
from meter_reading_mini.core.contract_models import (
    CalculationMethod,
    Category,
    PreviousReadingSource,
    Purpose,
    ReadingSession,
    ResultStatus,
    ValueSchema,
)
from meter_reading_mini.core.errors import ValidationError
from meter_reading_mini.core.meter_reading_sync_repositories import (
    AssignmentReviewFlagRecord,
    MeterAssignmentRecord,
    PlannedReadingCloseReason,
    PlannedReadingTaskState,
    ReadingHistoryBoundaryRecord,
    ReadingHistoryReadingRecord,
)
from meter_reading_mini.core.reading_history import (
    ReadingHistoryService,
    _HistoryBuilder,
    default_history_start_date,
)
from meter_reading_mini.core.reading_history_contracts import (
    ReadingHistoryCode,
    ReadingHistoryDefinitionOption,
    ReadingHistoryQuery,
    ReadingHistoryRowKind,
)
from meter_reading_mini.db.connection import connect, read_transaction
from meter_reading_mini.db.repositories import SQLiteRepository

START = datetime(2026, 2, 1, tzinfo=UTC)
REPLACED = datetime(2026, 2, 4, tzinfo=UTC)
ZONE = ZoneInfo("Asia/Tokyo")
DEFINITION = ReadingHistoryDefinitionOption(
    "d",
    "積算",
    "l",
    "場所",
    Category.ELECTRICITY,
    "kWh",
    Purpose.OPERATIONAL,
    CalculationMethod.DELTA,
    ValueSchema.DECIMAL_STRING,
    True,
)
QUERY = ReadingHistoryQuery("d", date(2026, 2, 1), date(2026, 2, 5))


def _reading(
    id_: str,
    at: datetime,
    meter: str,
    *,
    raw: str | None = None,
    last: str | None = None,
    usage: str | None = None,
    state: PlannedReadingTaskState = PlannedReadingTaskState.COMPLETED,
    status: ResultStatus | None = ResultStatus.MEASURED,
    business_date: date | None = None,
) -> ReadingHistoryReadingRecord:
    return ReadingHistoryReadingRecord(
        planned_reading_id=id_,
        measurement_definition_id="d",
        meter_id=meter,
        location_id="l",
        business_date=business_date or at.date(),
        reading_session=ReadingSession.SESSION_0800,
        scheduled_at=at,
        task_state=state,
        close_reason=None,
        superseded_by_planned_reading_id=None,
        result_status=status,
        raw_reading=Decimal(raw) if raw is not None else None,
        last_reading=Decimal(last) if last is not None else None,
        usage_delta=Decimal(usage) if usage is not None else None,
        structured_payload_json=None,
        recorded_at=at,
        updated_by="担当",
        result_source="manual",
        note="備考",
        meter_name=meter,
        meter_serial=None,
        meter_is_active=True,
    )


def _boundary() -> ReadingHistoryBoundaryRecord:
    return ReadingHistoryBoundaryRecord(
        replacement_event_id="b",
        measurement_definition_id="d",
        location_id="l",
        business_date=date(2026, 2, 4),
        old_meter_id="old",
        new_meter_id="new",
        replaced_at=REPLACED,
        old_assignment_effective_from=START,
        new_assignment_effective_from=REPLACED,
        previous_measured_reading=Decimal("120"),
        previous_measured_planned_reading_id="a",
        old_final_reading=Decimal("130"),
        old_meter_usage_delta=Decimal("20"),
        new_initial_reading=Decimal("5.00"),
        updated_by="担当",
        note="交換",
        old_meter_name="旧",
        old_meter_serial=None,
        old_meter_is_active=True,
        new_meter_name="新",
        new_meter_serial=None,
        new_meter_is_active=True,
    )


def _assignments() -> list[MeterAssignmentRecord]:
    return [
        MeterAssignmentRecord(
            "d",
            "old",
            START,
            Decimal("2"),
            Decimal("1000"),
            REPLACED,
            initial_reading=Decimal("100"),
        ),
        MeterAssignmentRecord("d", "new", REPLACED, Decimal("3"), Decimal("100"), None),
    ]


def _build(
    readings: list[ReadingHistoryReadingRecord],
    boundaries: list[ReadingHistoryBoundaryRecord],
    assignments: list[MeterAssignmentRecord],
    *,
    query: ReadingHistoryQuery = QUERY,
    definition: ReadingHistoryDefinitionOption = DEFINITION,
    flags: list[AssignmentReviewFlagRecord] | None = None,
):
    return _HistoryBuilder(
        definition, query, readings, boundaries, assignments, flags or [], ZONE
    ).build()


def _codes(row) -> set[ReadingHistoryCode]:
    return {issue.code for issue in row.issues}


@pytest.mark.parametrize(
    ("today", "expected"),
    [
        (date(2026, 5, 15), date(2026, 4, 16)),
        (date(2026, 3, 31), date(2026, 3, 1)),
        (date(2024, 3, 31), date(2024, 3, 1)),
        (date(2026, 1, 31), date(2026, 1, 1)),
    ],
)
def test_default_period(today: date, expected: date) -> None:
    assert default_history_start_date(today) == expected


def test_exchange_order_baselines_and_saved_sum() -> None:
    prior = _reading("a", START + timedelta(days=1), "old", raw="120", last="100", usage="40")
    current = _reading("c", REPLACED, "new", raw="8.000", last="5.00", usage="9.000")
    result = _build([current, prior], [_boundary()], _assignments())
    assert [row.row_kind for row in result.rows] == [
        ReadingHistoryRowKind.READING,
        ReadingHistoryRowKind.REPLACEMENT_OLD_FINAL,
        ReadingHistoryRowKind.REPLACEMENT_NEW_INITIAL,
        ReadingHistoryRowKind.READING,
    ]
    old, new, reading = result.rows[1:]
    assert (old.last_reading, old.previous_source, old.previous_reference_id) == (
        Decimal("120"),
        PreviousReadingSource.MEASURED,
        "a",
    )
    assert (old.multiplier, old.rollover_value, new.multiplier, new.rollover_value) == (
        Decimal("2"),
        Decimal("1000"),
        Decimal("3"),
        Decimal("100"),
    )
    assert reading.previous_source is PreviousReadingSource.REPLACEMENT_INITIAL
    assert reading.previous_reference_id == "b"
    assert (reading.multiplier, reading.rollover_value) == (Decimal("3"), Decimal("100"))
    assert reading.raw_reading is not None
    assert format(reading.raw_reading, "f") == "8.000"
    assert result.total_usage == Decimal("69.000")
    assert result.warning_count == 0


def test_baseline_mismatch_preserves_values_and_reference() -> None:
    prior = _reading("a", START + timedelta(days=1), "old", raw="120", last="100", usage="40")
    current = _reading("c", START + timedelta(days=2), "old", raw="130", last="100", usage="20")
    boundary = replace(
        _boundary(),
        previous_measured_reading=Decimal("100"),
        previous_measured_planned_reading_id="saved",
    )
    result = _build([prior, current], [boundary], _assignments())
    reading = result.rows[1]
    old = result.rows[2]
    assert (reading.last_reading, reading.previous_source, reading.previous_reference_id) == (
        Decimal("100"),
        None,
        None,
    )
    assert ReadingHistoryCode.BASELINE_MISMATCH in _codes(reading)
    assert (old.last_reading, old.previous_source, old.previous_reference_id) == (
        Decimal("100"),
        None,
        "saved",
    )
    assert ReadingHistoryCode.BASELINE_MISMATCH in _codes(old)


@pytest.mark.parametrize(
    ("saved_value", "saved_id"),
    [(None, None), (Decimal("120"), None), (None, "a"), (Decimal("120"), "other")],
)
def test_boundary_missing_or_inconsistent_saved_reference_is_not_inferred(
    saved_value: Decimal | None,
    saved_id: str | None,
) -> None:
    prior = _reading("a", START + timedelta(days=1), "old", raw="120", last="100", usage="40")
    boundary = replace(
        _boundary(),
        previous_measured_reading=saved_value,
        previous_measured_planned_reading_id=saved_id,
    )
    old = _build([prior], [boundary], _assignments()).rows[1]
    assert ReadingHistoryCode.BASELINE_MISMATCH in _codes(old)
    assert (old.last_reading, old.previous_source, old.previous_reference_id) == (
        saved_value,
        None,
        saved_id,
    )


def test_outside_period_including_later_business_date() -> None:
    earlier = _reading(
        "a",
        START + timedelta(days=1),
        "old",
        raw="120",
        last="100",
        usage="40",
        business_date=date(2026, 2, 5),
    )
    later = _reading(
        "c",
        START + timedelta(days=2),
        "old",
        raw="130",
        last="120",
        usage="20",
        business_date=date(2026, 2, 4),
    )
    result = _build(
        [earlier, later],
        [],
        _assignments(),
        query=ReadingHistoryQuery("d", date(2026, 2, 4), date(2026, 2, 4)),
    )
    assert len(result.rows) == 1
    assert result.rows[0].previous_reference_id == "a"
    assert ReadingHistoryCode.BASELINE_OUTSIDE_PERIOD in _codes(result.rows[0])


@pytest.mark.parametrize(
    ("start", "end"),
    [
        (date(2026, 2, 4), date(2026, 2, 4)),
        (date(2026, 2, 4), date(2026, 2, 5)),
        (date(2026, 2, 3), date(2026, 2, 4)),
    ],
)
def test_boundary_on_period_edge_keeps_both_rows(start: date, end: date) -> None:
    result = _build([], [_boundary()], _assignments(), query=ReadingHistoryQuery("d", start, end))
    assert [row.row_kind for row in result.rows] == [
        ReadingHistoryRowKind.REPLACEMENT_OLD_FINAL,
        ReadingHistoryRowKind.REPLACEMENT_NEW_INITIAL,
    ]


def test_replacement_without_initial_and_pending_rollover() -> None:
    boundary = replace(_boundary(), new_initial_reading=None)
    reading = _reading("c", REPLACED, "new", raw="8", usage="0")
    old, new = _assignments()
    new = replace(new, needs_review=True)
    flag = AssignmentReviewFlagRecord("d", "new", REPLACED, "確認", REPLACED)
    result = _build([reading], [boundary], [old, new], flags=[flag])
    assert ReadingHistoryCode.REPLACEMENT_INITIAL_MISSING in _codes(result.rows[1])
    assert result.rows[2].previous_source is PreviousReadingSource.REPLACEMENT_NO_INITIAL
    assert {ReadingHistoryCode.NO_BASELINE, ReadingHistoryCode.ASSIGNMENT_PENDING} <= _codes(
        result.rows[2]
    )


def test_superseded_nonmeasured_is_not_a_baseline() -> None:
    old = replace(
        _reading(
            "x",
            REPLACED,
            "old",
            state=PlannedReadingTaskState.SUPERSEDED,
            status=ResultStatus.METER_FAULT,
        ),
        close_reason=PlannedReadingCloseReason.METER_REPLACED,
        superseded_by_planned_reading_id="c",
    )
    current = _reading("c", REPLACED, "new", raw="8", last="5", usage="9")
    hidden = _build([old, current], [_boundary()], _assignments())
    visible = _build(
        [old, current], [_boundary()], _assignments(), query=replace(QUERY, include_superseded=True)
    )
    superseded = next(row for row in visible.rows if row.planned_reading_id == "x")
    assert _codes(superseded) == {ReadingHistoryCode.SUPERSEDED}
    assert superseded.multiplier is None
    assert hidden.total_usage == visible.total_usage
    visible_current = next(row for row in visible.rows if row.planned_reading_id == "c")
    assert hidden.rows[-1].previous_source == visible_current.previous_source
    broken = _build(
        [replace(old, superseded_by_planned_reading_id="bad"), current],
        [_boundary()],
        _assignments(),
        query=replace(QUERY, include_superseded=True),
    )
    assert ReadingHistoryCode.STATE_MISMATCH in _codes(broken.rows[-1])
    measured = replace(
        old,
        result_status=ResultStatus.MEASURED,
        raw_reading=Decimal("10"),
        usage_delta=Decimal("1"),
    )
    corrupt = _build(
        [measured, current],
        [_boundary()],
        _assignments(),
        query=replace(QUERY, include_superseded=True),
    )
    assert ReadingHistoryCode.STATE_MISMATCH in _codes(
        next(row for row in corrupt.rows if row.planned_reading_id == "x")
    )


def test_consecutive_replacements_preserve_valid_superseded_chain() -> None:
    second_at = REPLACED + timedelta(hours=1)
    scheduled = REPLACED + timedelta(hours=2)
    second_boundary = replace(
        _boundary(),
        replacement_event_id="b2",
        old_meter_id="new",
        new_meter_id="third",
        replaced_at=second_at,
        old_assignment_effective_from=REPLACED,
        new_assignment_effective_from=second_at,
        previous_measured_reading=None,
        previous_measured_planned_reading_id=None,
    )
    old, new = _assignments()
    third = MeterAssignmentRecord("d", "third", second_at, Decimal("1"), None)
    first_row = replace(
        _reading(
            "a",
            scheduled,
            "old",
            state=PlannedReadingTaskState.SUPERSEDED,
            status=ResultStatus.METER_FAULT,
        ),
        close_reason=PlannedReadingCloseReason.METER_REPLACED,
        superseded_by_planned_reading_id="b",
    )
    second_row = replace(
        _reading("b", scheduled, "new", state=PlannedReadingTaskState.SUPERSEDED, status=None),
        close_reason=PlannedReadingCloseReason.METER_REPLACED,
        superseded_by_planned_reading_id="c",
    )
    third_row = _reading("c", scheduled, "third", state=PlannedReadingTaskState.OPEN, status=None)
    result = _build(
        [first_row, second_row, third_row],
        [_boundary(), second_boundary],
        [old, replace(new, effective_to=second_at), third],
        query=replace(QUERY, include_superseded=True),
    )
    for row in result.rows:
        if row.planned_reading_id in {"a", "b"}:
            assert _codes(row) == {ReadingHistoryCode.SUPERSEDED}
            assert row.multiplier is None


def test_recomposed_successor_does_not_invalidate_replacement_history() -> None:
    old = replace(
        _reading(
            "old",
            REPLACED,
            "old",
            state=PlannedReadingTaskState.SUPERSEDED,
            status=ResultStatus.METER_FAULT,
        ),
        close_reason=PlannedReadingCloseReason.METER_REPLACED,
        superseded_by_planned_reading_id="new",
    )
    successor = _reading(
        "new",
        REPLACED - timedelta(hours=1),
        "old",
        state=PlannedReadingTaskState.OPEN,
        status=None,
        business_date=old.business_date,
    )
    result = _build(
        [old, successor],
        [_boundary()],
        _assignments(),
        query=replace(QUERY, include_superseded=True),
    )
    row = next(item for item in result.rows if item.planned_reading_id == "old")
    assert _codes(row) == {ReadingHistoryCode.SUPERSEDED}
    assert row.multiplier is None


def test_state_and_assignment_problems_do_not_abort() -> None:
    reading = _reading("a", START + timedelta(days=1), "old", raw="10", usage=None)
    result = _build([reading], [], [])
    assert {ReadingHistoryCode.STATE_MISMATCH, ReadingHistoryCode.ASSIGNMENT_UNRESOLVED} <= _codes(
        result.rows[0]
    )
    assert result.warning_count == 1


def test_missing_assignment_and_boundary_references_remain_visible() -> None:
    boundary = replace(
        _boundary(), previous_measured_reading=None, previous_measured_planned_reading_id="a"
    )
    old, new = _assignments()
    result = _build(
        [], [boundary], [replace(old, effective_to=REPLACED - timedelta(seconds=1)), new]
    )
    assert {
        ReadingHistoryCode.ASSIGNMENT_UNRESOLVED,
        ReadingHistoryCode.BASELINE_MISMATCH,
    } <= _codes(result.rows[0])
    assert result.rows[0].previous_reference_id == "a"
    assert result.rows[0].multiplier is None
    assert result.rows[1].multiplier == Decimal("3")


def test_first_replacement_and_consecutive_replacement_without_initial() -> None:
    first = replace(
        _boundary(),
        previous_measured_reading=None,
        previous_measured_planned_reading_id=None,
        new_initial_reading=None,
    )
    second_at = REPLACED + timedelta(hours=1)
    second = replace(
        first,
        replacement_event_id="b2",
        old_meter_id="new",
        new_meter_id="third",
        replaced_at=second_at,
        old_assignment_effective_from=REPLACED,
        new_assignment_effective_from=second_at,
        old_final_reading=Decimal("3"),
        old_meter_usage_delta=Decimal("0"),
        new_initial_reading=Decimal("1"),
    )
    old, new = _assignments()
    new = replace(new, effective_to=second_at)
    third = MeterAssignmentRecord("d", "third", second_at, Decimal("1"), None)
    result = _build([], [first, second], [old, new, third])
    assert result.rows[0].last_reading == Decimal("100")
    assert result.rows[0].previous_source is PreviousReadingSource.DEFINITION_INITIAL
    assert result.rows[2].last_reading is None
    assert result.rows[2].previous_source is PreviousReadingSource.REPLACEMENT_NO_INITIAL
    assert result.rows[2].previous_reference_id == "b"


def test_rollover_and_exact_sum_even_for_warning_and_review_rows() -> None:
    first = _reading(
        "a",
        START + timedelta(days=1),
        "old",
        raw="999",
        last="90",
        usage="123456789012345678901234567890.0000001",
    )
    second = _reading("b", START + timedelta(days=2), "old", raw="1", last="999", usage="0.0000009")
    old, _ = _assignments()
    old = replace(old, needs_review=True)
    result = _build([first, second], [], [old])
    assert result.total_usage == Decimal("123456789012345678901234567890.0000010")
    assert ReadingHistoryCode.BASELINE_MISMATCH in _codes(result.rows[0])
    assert ReadingHistoryCode.ROLLOVER in _codes(result.rows[1])
    assert ReadingHistoryCode.ASSIGNMENT_PENDING in _codes(result.rows[1])
    assert result.warning_count == 2


def test_inactive_and_unentered_rows() -> None:
    open_row = _reading("a", START, "old", state=PlannedReadingTaskState.OPEN, status=None)
    closed_row = _reading(
        "b",
        START + timedelta(days=1),
        "old",
        state=PlannedReadingTaskState.AUTO_CLOSED,
        status=None,
    )
    fault = _reading("c", START + timedelta(days=2), "old", status=ResultStatus.METER_FAULT)
    result = _build(
        [open_row, closed_row, replace(fault, meter_is_active=False)], [], _assignments()
    )
    assert [_codes(row) for row in result.rows] == [
        set(),
        set(),
        {ReadingHistoryCode.INACTIVE_MASTER},
    ]
    assert result.total_usage is None
    inactive = _build(
        [open_row, closed_row], [], _assignments(), definition=replace(DEFINITION, is_active=False)
    )
    assert all(ReadingHistoryCode.INACTIVE_MASTER in _codes(row) for row in inactive.rows)


def test_duplicate_assignments_and_invalid_payloads_warn() -> None:
    reading = _reading("a", START + timedelta(days=1), "old", raw="-1", usage="1")
    old, _ = _assignments()
    overlapping = replace(old, meter_id="other")
    result = _build([reading], [], [old, overlapping])
    assert {ReadingHistoryCode.STATE_MISMATCH, ReadingHistoryCode.ASSIGNMENT_UNRESOLVED} <= _codes(
        result.rows[0]
    )
    assert result.rows[0].previous_source is None
    misplaced = _build([replace(reading, location_id="wrong")], [], [old])
    assert ReadingHistoryCode.STATE_MISMATCH in _codes(misplaced.rows[0])


def test_missing_old_final_and_inconsistent_review_reason() -> None:
    boundary = replace(
        _boundary(),
        old_final_reading=None,
        old_meter_usage_delta=None,
        previous_measured_reading=None,
        previous_measured_planned_reading_id=None,
    )
    old, new = _assignments()
    old = replace(old, needs_review=True)
    flag = AssignmentReviewFlagRecord("d", "old", START, "", START)
    result = _build([], [boundary], [old, new], flags=[flag])
    assert {
        ReadingHistoryCode.REPLACEMENT_FINAL_MISSING,
        ReadingHistoryCode.ASSIGNMENT_PENDING,
    } <= _codes(result.rows[0])


def test_local_time_and_sort_during_daylight_saving_fold() -> None:
    first_at = datetime(2026, 11, 1, 5, 30, tzinfo=UTC)
    second_at = datetime(2026, 11, 1, 6, 30, tzinfo=UTC)
    first = _reading("z", first_at, "old", raw="10", last="100", usage="0")
    second = _reading("a", second_at, "old", raw="11", last="10", usage="1")
    old, _ = _assignments()
    old = replace(old, effective_to=None)
    query = ReadingHistoryQuery("d", date(2026, 11, 1), date(2026, 11, 1))
    result = _HistoryBuilder(
        DEFINITION, query, [second, first], [], [old], [], ZoneInfo("America/New_York")
    ).build()
    assert [row.planned_reading_id for row in result.rows] == ["z", "a"]
    assert [row.at.utcoffset() for row in result.rows] == [timedelta(hours=-4), timedelta(hours=-5)]
    assert result.rows[1].previous_reference_id == "z"


def test_power_factor_and_raw_only() -> None:
    reading = _reading("a", START + timedelta(days=1), "old", usage=None)
    definition = replace(
        DEFINITION,
        value_schema=ValueSchema.POWER_FACTOR_PERCENT_PHASE,
        calculation_method=CalculationMethod.RAW_ONLY,
    )
    reading = replace(reading, structured_payload_json='{"percent":"90","phase":"lag"}')
    result = _build([reading], [], _assignments(), definition=definition)
    assert result.rows[0].power_factor == "90%（遅れ）"
    assert result.total_usage is None
    bad = _build(
        [replace(reading, structured_payload_json="{")], [], _assignments(), definition=definition
    )
    assert ReadingHistoryCode.STATE_MISMATCH in _codes(bad.rows[0])
    raw_definition = replace(DEFINITION, calculation_method=CalculationMethod.RAW_ONLY)
    raw = _build(
        [replace(reading, raw_reading=Decimal("12.340"), structured_payload_json=None)],
        [],
        _assignments(),
        definition=raw_definition,
    )
    assert raw.rows[0].raw_reading == Decimal("12.340")
    assert raw.rows[0].previous_source is None
    assert not _codes(raw.rows[0])


def test_raw_only_history_flags_negative_zero_in_readings_and_boundaries() -> None:
    definition = replace(DEFINITION, calculation_method=CalculationMethod.RAW_ONLY)
    reading = _reading("a", START + timedelta(days=1), "old", raw="-0.00", usage=None)
    boundary = replace(
        _boundary(),
        old_final_reading=Decimal("-0"),
        new_initial_reading=Decimal("-0.0"),
        previous_measured_reading=Decimal("-1"),
        old_meter_usage_delta=None,
    )
    result = _build([reading], [boundary], _assignments(), definition=definition)
    assert all(ReadingHistoryCode.STATE_MISMATCH in _codes(row) for row in result.rows)


def test_service_queries_real_repository(
    memory_conn: sqlite3.Connection,
    sqlite_repository: SQLiteRepository,
    sample_master: tuple[str, str],
    fixed_clock: datetime,
) -> None:
    service = ReadingHistoryService(
        sqlite_repository,
        read_transaction_factory=partial(read_transaction, memory_conn),
        current_business_date_provider=lambda: date(2026, 2, 4),
        timezone_provider=lambda: ZONE,
    )
    definition_id = sample_master[1]
    assert (
        service.list_definitions(include_inactive=False)[0].measurement_definition_id
        == definition_id
    )
    history = service.get_history(
        ReadingHistoryQuery(definition_id, date(2026, 2, 1), date(2026, 2, 4))
    )
    assert history.rows == ()
    for query in (
        ReadingHistoryQuery(definition_id, date(2026, 2, 5), date(2026, 2, 5)),
        ReadingHistoryQuery(definition_id, date(2026, 2, 4), date(2026, 2, 3)),
        ReadingHistoryQuery("missing", date(2026, 2, 1), date(2026, 2, 4)),
    ):
        with pytest.raises(ValidationError):
            service.get_history(query)
    assert create_app_context(memory_conn, clock=lambda: fixed_clock).reading_history
    memory_conn.execute(
        "UPDATE measurement_definitions SET is_active = 0 WHERE measurement_definition_id = ?",
        (definition_id,),
    )
    assert service.list_definitions(include_inactive=False) == ()
    assert not service.list_definitions(include_inactive=True)[0].is_active


def test_history_reads_one_snapshot(
    memory_conn: sqlite3.Connection,
    sample_master: tuple[str, str],
    fixed_clock: datetime,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    db_path = tmp_path / "history.db"
    conn = connect(db_path)
    memory_conn.backup(conn)
    writer = connect(db_path)
    try:
        repository = SQLiteRepository(conn, clock=lambda: fixed_clock)
        original = repository.list_reading_history_readings

        def change_during_read(definition_id: str) -> list[ReadingHistoryReadingRecord]:
            assert conn.in_transaction
            writer.execute(
                "UPDATE measurement_definitions SET display_name = ? "
                "WHERE measurement_definition_id = ?",
                ("変更後", definition_id),
            )
            return original(definition_id)

        monkeypatch.setattr(repository, "list_reading_history_readings", change_during_read)
        service = ReadingHistoryService(
            repository,
            read_transaction_factory=partial(read_transaction, conn),
            current_business_date_provider=lambda: date(2026, 2, 4),
            timezone_provider=lambda: ZONE,
        )
        history = service.get_history(
            ReadingHistoryQuery(sample_master[1], date(2026, 2, 1), date(2026, 2, 4))
        )
        assert history.definition.display_name == "試験用積算電力量"
        assert service.list_definitions(include_inactive=True)[0].display_name == "変更後"
    finally:
        writer.close()
        conn.close()
