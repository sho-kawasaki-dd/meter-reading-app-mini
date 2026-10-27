"""ブラウザテスト用のシートデータ組み立て補助。"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from datetime import datetime, timedelta
from typing import Any

from meter_reading_mini.core.mobile_reading import render_sheet_html

SHEET_ID = "b" * 32
LOCATION_ID = "loc-main"


def meter(
    meter_id: str,
    *,
    location_id: str | None = LOCATION_ID,
    location_name: str = "受変電室",
    meter_name: str = "試験メーター",
    category: str = "water",
    meter_serial: str | None = None,
) -> dict[str, Any]:
    return {
        "meter_id": meter_id,
        "location_id": location_id,
        "location_name": location_name,
        "meter_name": meter_name,
        "category": category,
        "meter_serial": meter_serial,
    }


def definition(
    definition_id: str,
    *,
    location_id: str | None = LOCATION_ID,
    display_name: str | None = None,
    category: str = "water",
    unit: str = "m3",
    value_schema: str = "decimal_string",
    multiplier: str = "1",
    decimal_places: int = 2,
    warning_threshold: str | None = None,
    rollover_value: str | None = None,
    calculation_method: str = "delta",
) -> dict[str, Any]:
    return {
        "measurement_definition_id": definition_id,
        "location_id": location_id,
        "display_name": display_name or f"測定 {definition_id}",
        "category": category,
        "unit": unit,
        "value_schema": value_schema,
        "multiplier": multiplier,
        "decimal_places": decimal_places,
        "warning_threshold": warning_threshold,
        "rollover_value": rollover_value,
        "calculation_method": calculation_method,
    }


def slot(
    planned_reading_id: str,
    definition_id: str,
    meter_id: str,
    *,
    session: str = "08:00",
    cycle: str = "daily_1x",
    scheduled_at: str | None = None,
    task_state: str = "open",
    previous_source: str = "none",
    last_reading: str | None = None,
    previous_scheduled_at: str | None = None,
    previous_planned_reading_id: str | None = None,
) -> dict[str, Any]:
    hour, minute = int(session[:2]), int(session[3:])
    default_utc = datetime(2026, 2, 4, hour, minute) - timedelta(hours=9)
    return {
        "planned_reading_id": planned_reading_id,
        "measurement_definition_id": definition_id,
        "meter_id": meter_id,
        "reading_session": session,
        "scheduled_at": scheduled_at or default_utc.strftime("%Y-%m-%d %H:%M:%S"),
        "reading_cycle": cycle,
        "task_state": task_state,
        "initial_last_reading": last_reading,
        "initial_previous_scheduled_at": previous_scheduled_at,
        "initial_previous_planned_reading_id": previous_planned_reading_id,
        "previous_source": previous_source,
    }


def sheet_data(
    slots: Sequence[dict[str, Any]],
    definitions: Sequence[dict[str, Any]],
    meters: Sequence[dict[str, Any]],
) -> dict[str, Any]:
    locations: dict[str, str] = {}
    for item in meters:
        if item["location_id"] is not None:
            locations.setdefault(item["location_id"], item["location_name"])
    return {
        "kind": "mrm-meter-reading-sheet",
        "v": 1,
        "sheet_id": SHEET_ID,
        "business_date": "2026-02-04",
        "generated_at": "2026-02-04 03:00:00",
        "generated_at_local": "2026-02-04 12:00:00",
        "filter": {"reading_sessions": None, "cycle_groups": None},
        "locations": [
            {"location_id": key, "location_name": name} for key, name in locations.items()
        ],
        "meters": list(meters),
        "definitions": list(definitions),
        "slots": list(slots),
    }


def sheet_html(
    slots: Sequence[dict[str, Any]],
    definitions: Sequence[dict[str, Any]],
    meters: Sequence[dict[str, Any]],
) -> str:
    return render_sheet_html(sheet_data(slots, definitions, meters))


def simple_sheet_html(
    *,
    definition_overrides: Mapping[str, Any] | None = None,
    slot_overrides: Mapping[str, Any] | None = None,
) -> str:
    """測定 1 件・枠 1 件の最小シート。"""

    return sheet_html(
        [slot("slot-1", "def-1", "meter-1", **(slot_overrides or {}))],
        [definition("def-1", **(definition_overrides or {}))],
        [meter("meter-1")],
    )
