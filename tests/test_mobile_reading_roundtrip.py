from __future__ import annotations

import sqlite3
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path

import pytest
from conftest import SheetBrowser
from mobile_sheet_helpers import definition, meter, sheet_data, slot
from playwright.sync_api import Page, expect
from reading_service_helpers import (
    DEFINITION_ID,
    LOCATION_ID,
    METER_ID,
    STAFF_ID,
    build_services,
    confirmation_context,
    create_plan,
    mobile_submission,
    sync_request,
)

from meter_reading_mini.core.contract_models import (
    CalculationMethod,
    Category,
    Purpose,
    ReadingCycle,
    ValueSchema,
)
from meter_reading_mini.core.decimal_arithmetic import exact_add, exact_multiply, exact_subtract
from meter_reading_mini.core.meter_reading_sync_repositories import (
    MeasurementDefinitionCreateRequest,
)
from meter_reading_mini.core.mobile_reading import parse_mobile_reading_result, render_sheet_html
from meter_reading_mini.db.connection import transaction
from meter_reading_mini.db.repositories import SQLiteRepository


def _saved_json_download(page: Page, tmp_path: Path) -> str:
    with page.expect_download() as download_info:
        page.locator("#result-download").click()
    download = download_info.value
    result_path = tmp_path / download.suggested_filename
    download.save_as(result_path)
    return result_path.read_text(encoding="utf-8")


def test_browser_mr1_and_json_round_trip_through_sync(
    sheet_browser: SheetBrowser,
    file_database_repository: tuple[sqlite3.Connection, SQLiteRepository],
    tmp_path: Path,
) -> None:
    conn, repository = file_database_repository
    _, sync_service = build_services(conn, repository)
    with transaction(conn):
        repository.create_measurement_definition(
            MeasurementDefinitionCreateRequest(
                measurement_definition_id="power-factor-definition",
                location_id=LOCATION_ID,
                display_name="力率",
                category=Category.WATER,
                unit="%",
                value_schema=ValueSchema.POWER_FACTOR_PERCENT_PHASE,
                multiplier=Decimal("1"),
                decimal_places=None,
                warning_threshold=None,
                rollover_value=None,
                calculation_method=CalculationMethod.RAW_ONLY,
                reading_cycle=ReadingCycle.DAILY_1X,
                purpose=Purpose.OPERATIONAL,
                generation_start_date=datetime(2026, 2, 1).date(),
                initial_schedule_json=None,
                initial_meter_id=METER_ID,
            )
        )

    scheduled = {
        "plan-0800": datetime(2026, 2, 4, 4, tzinfo=UTC),
        "plan-pf": datetime(2026, 2, 4, 5, tzinfo=UTC),
        "plan-1400": datetime(2026, 2, 4, 10, tzinfo=UTC),
        "plan-2200": datetime(2026, 2, 4, 16, tzinfo=UTC),
        "plan-unconfirmed": datetime(2026, 2, 4, 18, tzinfo=UTC),
        "plan-2300": datetime(2026, 2, 4, 20, tzinfo=UTC),
    }
    for planned_reading_id, scheduled_at in scheduled.items():
        create_plan(
            repository,
            planned_reading_id,
            scheduled_at,
            measurement_definition_id=(
                "power-factor-definition" if planned_reading_id == "plan-pf" else DEFINITION_ID
            ),
        )

    seeded = sync_service.sync(
        sync_request(
            mobile_submission("plan-1400", "200.00"),
            mobile_submission("plan-2300", "260.00"),
        ),
        confirmation_context(),
    )
    assert seeded.accepted_reading_items == 2
    with transaction(conn):
        conn.execute(
            "UPDATE planned_readings SET task_state = 'auto_closed', "
            "closed_at = '2026-02-04 03:00:00', close_reason = 'cutoff_elapsed' "
            "WHERE planned_reading_id = 'plan-2200'"
        )

    html = render_sheet_html(
        sheet_data(
            [
                slot(
                    "plan-0800",
                    DEFINITION_ID,
                    METER_ID,
                    scheduled_at="2026-02-04 04:00:00",
                ),
                slot(
                    "plan-pf",
                    "power-factor-definition",
                    METER_ID,
                    scheduled_at="2026-02-04 05:00:00",
                ),
                slot(
                    "plan-2200",
                    DEFINITION_ID,
                    METER_ID,
                    session="22:00",
                    scheduled_at="2026-02-04 16:00:00",
                    task_state="auto_closed",
                    previous_source="measured",
                    last_reading="200.00",
                    previous_scheduled_at="2026-02-04 10:00:00",
                    previous_planned_reading_id="plan-1400",
                ),
                slot(
                    "plan-unconfirmed",
                    DEFINITION_ID,
                    METER_ID,
                    session="17:00",
                    scheduled_at="2026-02-04 18:00:00",
                    previous_source="measured",
                    last_reading="200.00",
                    previous_scheduled_at="2026-02-04 10:00:00",
                    previous_planned_reading_id="plan-1400",
                ),
            ],
            [
                definition(DEFINITION_ID, display_name="積算水量"),
                definition(
                    "power-factor-definition",
                    display_name="力率",
                    value_schema=ValueSchema.POWER_FACTOR_PERCENT_PHASE.value,
                    unit="%",
                    calculation_method=CalculationMethod.RAW_ONLY.value,
                ),
            ],
            [meter(METER_ID)],
        )
    )
    page = sheet_browser.open_html(html)
    page.locator('.tab[data-tab="all"]').click()
    expect(page.locator("#result-copy")).to_be_disabled()

    plan_0800 = page.locator('.card[data-slot-id="plan-0800"]')
    plan_0800.locator(".raw").fill("150.00")
    plan_0800.locator(".note").fill("点検完了 ✅")
    plan_0800.locator(".confirm").click()

    plan_pf = page.locator('.card[data-slot-id="plan-pf"]')
    plan_pf.locator(".pf-percent").fill("97.5")
    plan_pf.locator(".pf-phase").select_option("lag")
    plan_pf.locator(".note").fill("力率確認 🧪")
    plan_pf.locator(".confirm").click()

    plan_2200 = page.locator('.card[data-slot-id="plan-2200"]')
    plan_2200.locator(".raw").fill("250.50")
    plan_2200.locator(".note").fill("再検針 ✅")
    plan_2200.locator(".confirm").click()
    expect(plan_2200).to_have_attribute("data-previous", "200.00")
    expect(plan_2200).to_have_attribute("data-usage", "50.50")
    expect(page.locator("#result-count")).to_contain_text("出力対象: 3 件")

    page.evaluate(
        """() => Object.defineProperty(navigator, "clipboard", {
          configurable: true,
          value: { writeText: () => Promise.reject(new Error("clipboard unavailable")) }
        })"""
    )
    page.locator("#result-copy").click()
    expect(page.locator("#result-feedback")).to_contain_text("コピーできませんでした")
    expect(page.locator("#result-feedback")).to_contain_text("手動でコピー")
    result_code = page.locator("#result-export-text")
    expect(result_code).to_be_visible()
    assert result_code.evaluate(
        "(node) => node.selectionStart === 0 && node.selectionEnd === node.value.length"
    )
    mr1_result = parse_mobile_reading_result(result_code.input_value())

    json_text = _saved_json_download(page, tmp_path)
    expect(page.locator("#result-feedback")).to_contain_text("ダウンロードを要求しました")
    assert "保存しました" not in page.locator("#result-feedback").inner_text()
    json_result = parse_mobile_reading_result(json_text)
    assert mr1_result.entries == json_result.entries
    assert [entry.planned_reading_id for entry in mr1_result.entries] == [
        "plan-0800",
        "plan-pf",
        "plan-2200",
    ]
    assert mr1_result.entries[0].note == "点検完了 ✅"
    assert mr1_result.entries[1].structured_payload == {"percent": "97.5", "phase": "lag"}
    assert mr1_result.entries[1].note == "力率確認 🧪"
    assert mr1_result.entries[2].note == "再検針 ✅"

    response = sync_service.sync(
        mr1_result.to_sync_request(),
        mr1_result.to_confirmation_context(staff_id=STAFF_ID, staff_name="試験担当"),
    )
    assert response.accepted_reading_items == 3
    assert response.rejected_reading_items == []

    reading_rows = conn.execute(
        "SELECT planned_reading_id, result_status, raw_reading, last_reading, usage_delta, "
        "structured_payload_json, note FROM reading_transactions "
        "WHERE planned_reading_id IN ('plan-0800','plan-pf','plan-2200','plan-2300') "
        "ORDER BY planned_reading_id"
    ).fetchall()
    by_id = {row[0]: row[1:] for row in reading_rows}
    assert by_id["plan-0800"][:5] == ("measured", "150.00", None, "0", None)
    assert by_id["plan-pf"][0] == "measured"
    assert by_id["plan-pf"][4] == '{"percent":"97.5","phase":"lag"}'
    assert by_id["plan-pf"][5] == "力率確認 🧪"
    assert by_id["plan-2200"][:5] == ("measured", "250.50", "200.00", "50.50", None)
    assert by_id["plan-2200"][5] == "再検針 ✅"
    assert conn.execute(
        "SELECT task_state FROM planned_readings WHERE planned_reading_id = 'plan-2200'"
    ).fetchone() == ("completed",)
    assert conn.execute(
        "SELECT last_reading, usage_delta FROM reading_transactions "
        "WHERE planned_reading_id = 'plan-2300'"
    ).fetchone() == ("250.50", "9.50")
    assert conn.execute(
        "SELECT task_state FROM planned_readings WHERE planned_reading_id = 'plan-unconfirmed'"
    ).fetchone() == ("open",)


@pytest.mark.parametrize(
    ("previous", "raw", "multiplier", "rollover", "decimal_places"),
    [
        ("1", "9999999999999999999999999999", "1.5", None, 2),
        (
            "0.000000001",
            "999999999999999999999999999999.123456789",
            "1234567890.123456789",
            None,
            9,
        ),
        (
            "1.25",
            "2.25",
            "123456789012345678901234567890.123456789",
            None,
            2,
        ),
        ("10.125", "12.3", "2.50", None, 3),
        ("990.500", "4.50", "2.00", "1000", 3),
    ],
    ids=(
        "28-digit",
        "beyond-28-digits",
        "large-multiplier",
        "mixed-scale-trailing-zeros",
        "rollover",
    ),
)
def test_javascript_python_and_database_decimal_results_match(
    sheet_browser: SheetBrowser,
    file_database_repository: tuple[sqlite3.Connection, SQLiteRepository],
    tmp_path: Path,
    previous: str,
    raw: str,
    multiplier: str,
    rollover: str | None,
    decimal_places: int,
) -> None:
    conn, repository = file_database_repository
    _, sync_service = build_services(
        conn,
        repository,
        multiplier=Decimal(multiplier),
        decimal_places=decimal_places,
        rollover_value=Decimal(rollover) if rollover is not None else None,
    )
    previous_scheduled = datetime(2026, 2, 4, 4, tzinfo=UTC)
    target_scheduled = datetime(2026, 2, 4, 5, tzinfo=UTC)
    create_plan(repository, "plan-history", previous_scheduled)
    create_plan(repository, "plan-target", target_scheduled)
    history_response = sync_service.sync(
        sync_request(mobile_submission("plan-history", previous)),
        confirmation_context(),
    )
    assert history_response.accepted_reading_items == 1

    sheet = sheet_data(
        [
            slot(
                "plan-target",
                DEFINITION_ID,
                METER_ID,
                scheduled_at="2026-02-04 05:00:00",
                previous_source="measured",
                last_reading=previous,
                previous_scheduled_at="2026-02-04 04:00:00",
                previous_planned_reading_id="plan-history",
            )
        ],
        [
            definition(
                DEFINITION_ID,
                multiplier=multiplier,
                decimal_places=decimal_places,
                rollover_value=rollover,
            )
        ],
        [meter(METER_ID)],
    )
    page = sheet_browser.open_html(render_sheet_html(sheet))
    target = page.locator('.card[data-slot-id="plan-target"]')
    target.locator(".raw").fill(raw)

    delta = exact_subtract(Decimal(raw), Decimal(previous))
    if delta < 0:
        assert rollover is not None
        delta = exact_add(exact_subtract(Decimal(rollover), Decimal(previous)), Decimal(raw))
    expected_usage = format(exact_multiply(delta, Decimal(multiplier)), "f")
    expect(target).to_have_attribute("data-usage", expected_usage)
    target.locator(".confirm").click()

    result_json = _saved_json_download(page, tmp_path)
    parsed = parse_mobile_reading_result(result_json)
    assert parsed.entries[0].raw_reading == Decimal(raw)
    response = sync_service.sync(
        parsed.to_sync_request(),
        parsed.to_confirmation_context(staff_id=STAFF_ID, staff_name="試験担当"),
    )
    assert response.accepted_reading_items == 1
    stored_raw, stored_previous, stored_usage = conn.execute(
        "SELECT raw_reading, last_reading, usage_delta FROM reading_transactions "
        "WHERE planned_reading_id = 'plan-target'"
    ).fetchone()
    assert stored_raw == raw
    assert stored_previous == previous
    assert stored_usage == expected_usage
