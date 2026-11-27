from __future__ import annotations

import sqlite3
from collections import Counter
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal

import pytest

from meter_reading_mini.app_context import create_app_context
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
    StaffCreateRequest,
)
from meter_reading_mini.core.meter_replacement_service import MeterReplacementInput
from meter_reading_mini.db.connection import transaction
from meter_reading_mini.db.repositories import SQLiteRepository


@pytest.mark.parametrize("registration_delay_days", [0, 1], ids=["same-day", "previous-day"])
def test_replacement_does_not_keep_generating_old_meter_slots(
    file_database_repository: tuple[sqlite3.Connection, SQLiteRepository],
    registration_delay_days: int,
) -> None:
    """当日・前日の交換後に日付を進めても旧枠が増えないことを検証する。"""
    conn, _ = file_database_repository
    current = datetime(2026, 2, 1, 0, 0, tzinfo=UTC)
    repository = SQLiteRepository(conn, clock=lambda: current)
    with transaction(conn):
        for meter_id in ("old", "new"):
            repository.create_meter_master(
                MeterMasterCreateRequest(
                    meter_id=meter_id,
                    location_id="location",
                    location_name="試験場所",
                    meter_name=meter_id,
                    category=Category.WATER,
                )
            )
        repository.create_measurement_definition(
            MeasurementDefinitionCreateRequest(
                measurement_definition_id="definition",
                location_id="location",
                display_name="試験用水道",
                category=Category.WATER,
                unit="m3",
                value_schema=ValueSchema.DECIMAL_STRING,
                multiplier=Decimal("1"),
                decimal_places=0,
                warning_threshold=None,
                rollover_value=None,
                calculation_method=CalculationMethod.DELTA,
                reading_cycle=ReadingCycle.DAILY_3X,
                purpose=Purpose.OPERATIONAL,
                generation_start_date=date(2026, 2, 1),
                initial_schedule_json=None,
                initial_meter_id="old",
            )
        )
    services = create_app_context(conn, clock=lambda: current)
    staff = services.staff.create_staff(StaffCreateRequest("試験担当"))
    services.planned_reading_maintenance.refresh()
    current = datetime(2026, 2, 3, 1, 0, tzinfo=UTC) + timedelta(days=registration_delay_days)
    services.planned_reading_maintenance.refresh()
    services.meter_replacement.register_replacement(
        MeterReplacementInput(
            old_meter_id="old",
            new_meter_id="new",
            replaced_at=datetime(2026, 2, 3, 0, 30, tzinfo=UTC),
            old_final_reading=Decimal("10"),
            new_initial_reading=Decimal("0"),
            staff_id=staff.staff_id,
        )
    )
    old_slots = conn.execute(
        "SELECT planned_reading_id, business_date, task_state FROM planned_readings "
        "WHERE meter_id = 'old' ORDER BY planned_reading_id"
    ).fetchall()
    assert Counter((row[1], row[2]) for row in old_slots) == {
        ("2026-02-01", "auto_closed"): 3,
        ("2026-02-02", "auto_closed"): 3,
        ("2026-02-03", "auto_closed" if registration_delay_days else "open"): 1,
        ("2026-02-03", "superseded"): 2,
        **({("2026-02-04", "superseded"): 3} if registration_delay_days else {}),
    }

    while current.date() < date(2026, 2, 7):
        current += timedelta(days=1)
        services.planned_reading_maintenance.refresh()
        assert conn.execute(
            "SELECT planned_reading_id, business_date FROM planned_readings "
            "WHERE meter_id = 'old' ORDER BY planned_reading_id"
        ).fetchall() == [(row[0], row[1]) for row in old_slots]
        assert conn.execute(
            "SELECT planned_reading_id FROM planned_readings "
            "WHERE meter_id = 'old' AND task_state = 'superseded' ORDER BY planned_reading_id"
        ).fetchall() == [(row[0],) for row in old_slots if row[2] == "superseded"]

    assert Counter(
        conn.execute(
            "SELECT business_date, task_state FROM planned_readings WHERE meter_id = 'new'"
        ).fetchall()
    ) == {
        ("2026-02-03", "auto_closed"): 2,
        ("2026-02-04", "auto_closed"): 3,
        ("2026-02-05", "auto_closed"): 3,
        ("2026-02-06", "auto_closed"): 3,
        ("2026-02-07", "open"): 3,
    }
    slots = conn.execute("SELECT * FROM planned_readings ORDER BY planned_reading_id").fetchall()
    services.planned_reading_maintenance.refresh()
    assert (
        conn.execute("SELECT * FROM planned_readings ORDER BY planned_reading_id").fetchall()
        == slots
    )
