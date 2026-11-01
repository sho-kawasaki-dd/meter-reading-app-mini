from __future__ import annotations

import json
import sqlite3
from datetime import UTC, date, datetime
from decimal import Decimal

import pytest
from reading_service_helpers import (
    DEFINITION_ID,
    LOCATION_ID,
    METER_ID,
    STAFF_ID,
    build_services,
    create_plan,
)

from meter_reading_mini.core.contract_models import (
    CalculationMethod,
    Category,
    Purpose,
    ReadingCycle,
    ResultStatus,
    ValueSchema,
)
from meter_reading_mini.core.errors import ValidationError
from meter_reading_mini.core.meter_reading_sync_repositories import (
    ManualReadingInput,
    MeasurementDefinitionCreateRequest,
    PlannedReadingTaskState,
)
from meter_reading_mini.core.reading_overview import (
    ReadingOverviewFilter,
    ReadingOverviewQueryService,
)
from meter_reading_mini.db.repositories import SQLiteRepository

DAY = date(2026, 2, 4)


def _at(hour: int) -> datetime:
    return datetime(2026, 2, 4, hour, tzinfo=UTC)


def test_overview_reads_all_states_results_and_inactive_masters(
    file_database_repository: tuple[sqlite3.Connection, SQLiteRepository],
) -> None:
    conn, repository = file_database_repository
    writer, _ = build_services(conn, repository)
    for index in range(1, 6):
        create_plan(repository, f"plan-{index}", _at(index))
    for index, value in ((1, "100"), (2, "130")):
        writer.save_manual_reading(
            ManualReadingInput(
                f"plan-{index}", ResultStatus.MEASURED, _at(index), Decimal(value), None
            ),
            staff_id=STAFF_ID,
        )
    writer.save_manual_reading(
        ManualReadingInput("plan-3", ResultStatus.METER_FAULT, _at(3), None, None, "故障"),
        staff_id=STAFF_ID,
    )
    conn.execute(
        "UPDATE planned_readings SET task_state = 'superseded', "
        "close_reason = 'meter_replaced', superseded_by_planned_reading_id = 'plan-4' "
        "WHERE planned_reading_id = 'plan-3'"
    )
    conn.execute(
        "UPDATE planned_readings SET task_state = 'auto_closed' WHERE planned_reading_id = 'plan-5'"
    )
    conn.execute(
        "UPDATE measurement_definitions SET warning_threshold = '29.99', is_active = 0 "
        "WHERE measurement_definition_id = ?",
        (DEFINITION_ID,),
    )
    conn.execute("UPDATE physical_meters SET is_active = 0 WHERE meter_id = ?", (METER_ID,))
    service = ReadingOverviewQueryService(repository, business_date_provider=lambda: DAY)
    changes = conn.total_changes

    overview = service.get_overview(DAY)

    assert conn.total_changes == changes
    assert [reading.planned_reading_id for reading in overview.readings] == [
        "plan-1",
        "plan-2",
        "plan-3",
        "plan-4",
        "plan-5",
    ]
    assert overview.counts.total == 5
    assert (overview.counts.completed, overview.counts.superseded) == (2, 1)
    assert (overview.counts.open, overview.counts.auto_closed, overview.counts.warnings) == (
        1,
        1,
        1,
    )
    old = overview.readings[2]
    assert old.result_status is ResultStatus.METER_FAULT
    assert old.note == "故障"
    assert old.superseded_by_planned_reading_id == "plan-4"
    assert old.location_name == "試験場所"

    conn.execute(
        "UPDATE measurement_definitions SET warning_threshold = '30' "
        "WHERE measurement_definition_id = ?",
        (DEFINITION_ID,),
    )
    assert service.get_overview(DAY).counts.warnings == 0


def test_overview_includes_power_factor_payload_and_combined_filters(
    file_database_repository: tuple[sqlite3.Connection, SQLiteRepository],
) -> None:
    conn, repository = file_database_repository
    writer, _ = build_services(conn, repository)
    repository.create_measurement_definition(
        MeasurementDefinitionCreateRequest(
            measurement_definition_id="power-factor",
            location_id=LOCATION_ID,
            display_name="力率",
            category=Category.ELECTRICITY,
            unit="%",
            value_schema=ValueSchema.POWER_FACTOR_PERCENT_PHASE,
            multiplier=Decimal("1"),
            decimal_places=None,
            warning_threshold=Decimal("0"),
            rollover_value=None,
            calculation_method=CalculationMethod.RAW_ONLY,
            reading_cycle=ReadingCycle.MONTHLY,
            purpose=Purpose.OPERATIONAL,
            generation_start_date=DAY,
            initial_schedule_json=None,
            initial_meter_id=METER_ID,
        )
    )
    create_plan(repository, "power-factor-slot", _at(2), measurement_definition_id="power-factor")
    create_plan(repository, "water-slot", _at(1))
    writer.save_manual_reading(
        ManualReadingInput(
            "power-factor-slot",
            ResultStatus.MEASURED,
            _at(2),
            None,
            {"percent": "95.5", "phase": "lag"},
        ),
        staff_id=STAFF_ID,
    )
    service = ReadingOverviewQueryService(repository, business_date_provider=lambda: DAY)
    criteria = ReadingOverviewFilter(
        reading_cycles=frozenset({ReadingCycle.MONTHLY}),
        task_states=frozenset({PlannedReadingTaskState.COMPLETED}),
        result_statuses=frozenset({ResultStatus.MEASURED}),
        categories=frozenset({Category.ELECTRICITY}),
        location_ids=frozenset({LOCATION_ID}),
        keyword="力率",
    )

    overview = service.get_overview(DAY, criteria)

    assert overview.counts.total == overview.counts.completed == 1
    assert overview.counts.warnings == 0
    assert overview.readings[0].raw_reading is None
    assert json.loads(overview.readings[0].structured_payload_json or "") == {
        "percent": "95.5",
        "phase": "lag",
    }
    no_results = service.get_overview(DAY, ReadingOverviewFilter(keyword="見つからない"))
    assert no_results.counts.total == 0
    assert service.get_overview(DAY, ReadingOverviewFilter(keyword="試験場所")).counts.total == 2
    with pytest.raises(ValidationError, match="未来"):
        service.get_overview(date(2026, 2, 5))
